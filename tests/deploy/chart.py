"""Generated worker values rendered with Helm. Run explicitly; no cluster needed."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from scripts.setup_quadlet import render

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "deploy/helm/fractal-demo"


def run(*command):
    return subprocess.run(command, capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize("worker_port", [8080, 9082])
def test_generated_values_to_helm(tmp_path, worker_port):
    args = SimpleNamespace(
        gateway_ip="192.168.122.1",
        node_network="192.168.122.0/24",
        cluster_dir=str(tmp_path),
        gateway_image="ghcr.io/example/gateway:demo",
        worker_image="ghcr.io/example/worker@sha256:" + "a" * 64,
        gateway_port=9081,
        worker_port=worker_port,
    )
    _, generated, _ = render(args)
    values = tmp_path / "values.json"
    values.write_bytes(generated)
    assert json.loads(generated)["worker"]["port"] == worker_port
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
    assert worker["image"] == args.worker_image
    assert worker["imagePullPolicy"] == "Always"
    env = {entry["name"]: entry for entry in worker["env"]}
    assert env["GATEWAY_URL"]["value"] == "http://192.168.122.1:9081"
    assert env["WORKER_PORT"]["value"] == str(worker_port)
    assert worker["ports"] == [
        {"name": "http", "containerPort": worker_port, "hostPort": worker_port, "protocol": "TCP"}
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


def test_missing_gateway_url_fails_helm():
    result = run("helm", "template", "demo", str(CHART))
    assert result.returncode != 0
    assert "Set gatewayUrl" in result.stderr
