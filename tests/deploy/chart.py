"""CLI-to-Helm checks: python -m pytest tests/deploy/chart.py. Requires Helm, no cluster."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "deploy/helm/fractal-demo"
HELPER = ROOT / "scripts/deployment_values.py"


def run(*command, **kwargs):
    return subprocess.run(command, capture_output=True, text=True, timeout=30, **kwargs)


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "gateway.toml"
    path.write_text(
        'bind = ["192.168.122.1"]\nport = 9081\nworker_port = 9082\n'
        'node_networks = ["192.168.122.0/24"]\n'
    )
    return path


@pytest.fixture
def virsh(tmp_path):
    # Only the external libvirt command is substituted. The helper is a real process.
    executable = tmp_path / "virsh"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import pathlib, sys\n"
        "assert sys.argv[1:] == ['--connect', 'test:///demo', 'net-dumpxml', 'booth']\n"
        f"print(pathlib.Path({str(tmp_path / 'network.xml')!r}).read_text())\n"
    )
    executable.chmod(0o755)
    return {**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"}


@pytest.mark.parametrize("addressing", ['netmask="255.255.255.0"', 'prefix="24"'])
def test_discovery_to_helm(config, virsh, addressing):
    (config.parent / "network.xml").write_text(
        f'<network><ip address="192.168.122.1" {addressing}/></network>'
    )
    generated = run(
        sys.executable,
        str(HELPER),
        "--config",
        str(config),
        "--libvirt-uri",
        "test:///demo",
        "--network",
        "booth",
        env=virsh,
    )
    assert generated.returncode == 0, generated.stderr
    assert json.loads(generated.stdout) == {
        "gatewayUrl": "http://192.168.122.1:9081",
        "worker": {"port": 9082},
    }
    values = config.parent / "values.json"
    values.write_text(generated.stdout)
    lint = run("helm", "lint", str(CHART), "-f", str(values), "--strict")
    assert lint.returncode == 0, lint.stdout + lint.stderr
    rendered = run(
        "helm", "template", "demo", str(CHART), "--namespace", "test-demo", "-f", str(values)
    )
    assert rendered.returncode == 0, rendered.stderr
    (daemonset,) = yaml.safe_load_all(rendered.stdout)
    assert daemonset["kind"] == "DaemonSet"
    assert daemonset["metadata"]["namespace"] == "test-demo"
    pod = daemonset["spec"]["template"]["spec"]
    (worker,) = pod["containers"]
    assert worker["image"] == "ghcr.io/dcermak/fractal-demo:latest"
    assert worker["imagePullPolicy"] == "Always"
    env = {entry["name"]: entry for entry in worker["env"]}
    assert env["GATEWAY_URL"]["value"] == "http://192.168.122.1:9081"
    assert env["WORKER_PORT"]["value"] == "9082"
    assert worker["ports"] == [
        {"name": "http", "containerPort": 9082, "hostPort": 9082, "protocol": "TCP"}
    ]
    assert env["NODE_NAME"]["valueFrom"]["fieldRef"]["fieldPath"] == "spec.nodeName"
    assert env["HOST_IP"]["valueFrom"]["fieldRef"]["fieldPath"] == "status.hostIP"
    assert pod["automountServiceAccountToken"] is False
    affinity = pod["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]
    (term,) = affinity["nodeSelectorTerms"]
    expressions = {entry["key"]: entry for entry in term["matchExpressions"]}
    for role in ("control-plane", "master"):
        assert expressions[f"node-role.kubernetes.io/{role}"]["operator"] == "DoesNotExist"
    assert expressions["kubernetes.io/os"]["values"] == ["linux"]
    assert expressions["kubernetes.io/arch"]["values"] == ["amd64"]
    for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
        assert worker[probe]["httpGet"] == {"path": "/healthz", "port": "http"}


def test_explicit_url_and_image_to_helm(config):
    image = "ghcr.io/example/worker@sha256:" + "a" * 64
    # No virsh on PATH: an explicit URL must bypass discovery.
    generated = run(
        sys.executable,
        str(HELPER),
        "--config",
        str(config),
        "--gateway-url",
        "http://192.168.122.1:9081",
        "--image",
        image,
        env={**os.environ, "PATH": ""},
    )
    assert generated.returncode == 0, generated.stderr
    values = config.parent / "values.json"
    values.write_text(generated.stdout)
    rendered = run(
        "helm",
        "template",
        "demo",
        str(CHART),
        "-f",
        str(values),
        "--set",
        "imagePullSecrets[0].name=ghcr-pull",
    )
    assert rendered.returncode == 0, rendered.stderr
    (daemonset,) = yaml.safe_load_all(rendered.stdout)
    pod = daemonset["spec"]["template"]["spec"]
    assert pod["containers"][0]["image"] == image
    assert pod["imagePullSecrets"] == [{"name": "ghcr-pull"}]


@pytest.mark.parametrize(
    ("extra", "url", "error"),
    [
        (
            "\n[local_worker_ports]\nworker = 8082\n",
            "http://192.168.122.1:9081",
            "local_worker_ports",
        ),
        ("", "http://192.168.122.1:8081", "port must match"),
        ("", "http://192.168.122.2:9081", "bind addresses"),
        ("", "http://192.168.122.1:9081/path", "HTTP origin"),
        ("invalid TOML", "http://192.168.122.1:9081", "error:"),
    ],
)
def test_invalid_configuration_has_no_values(config, extra, url, error):
    config.write_text(config.read_text() + extra)
    result = run(sys.executable, str(HELPER), "--config", str(config), "--gateway-url", url)
    assert result.returncode != 0
    assert result.stdout == ""
    assert error in result.stderr


@pytest.mark.parametrize(
    ("xml", "error"),
    [
        ("<network/>", "must have one IPv4 address"),
        (
            '<network><ip address="192.168.122.1" prefix="24"/>'
            '<ip address="192.168.123.1" prefix="24"/></network>',
            "must have one IPv4 address",
        ),
        ('<network><ip address="10.0.0.1" prefix="24"/></network>', "does not overlap"),
        ('<network><ip address="192.168.122.1"/></network>', "no IPv4 prefix or netmask"),
        ("not XML", "error:"),
    ],
)
def test_invalid_discovery_has_no_values(config, virsh, xml, error):
    (config.parent / "network.xml").write_text(xml)
    result = run(
        sys.executable,
        str(HELPER),
        "--config",
        str(config),
        "--libvirt-uri",
        "test:///demo",
        "--network",
        "booth",
        env=virsh,
    )
    assert result.returncode != 0
    assert result.stdout == ""
    assert error in result.stderr


def test_failed_virsh_has_no_values(config, virsh):
    (config.parent / "virsh").write_text(
        f"#!{sys.executable}\nimport sys\nsys.exit('network not found')\n"
    )
    result = run(sys.executable, str(HELPER), "--config", str(config), env=virsh)
    assert result.returncode != 0
    assert result.stdout == ""
    assert "virsh failed: network not found" in result.stderr


def test_missing_gateway_url_fails_helm():
    result = run("helm", "template", "demo", str(CHART))
    assert result.returncode != 0
    assert "Set gatewayUrl" in result.stderr
