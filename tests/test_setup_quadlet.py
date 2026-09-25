"""Generated host assets and installation without host Helm or systemd."""

import argparse
import json
import stat
import subprocess
import tomllib
from types import SimpleNamespace

import pytest

from fractal_demo.gateway import load_settings
from scripts import setup_quadlet


@pytest.fixture
def args(tmp_path):
    cluster = tmp_path / "cluster"
    cluster.mkdir()
    return SimpleNamespace(
        gateway_ip="192.168.122.1",
        node_network="192.168.122.0/24",
        cluster_dir=str(cluster),
        gateway_image="ghcr.io/demo/gateway@sha256:" + "a" * 64,
        worker_image="ghcr.io/demo/worker@sha256:" + "b" * 64,
        gateway_port=9081,
        worker_port=9082,
    )


def test_render_matches_gateway_and_quadlet(tmp_path, args):
    config, values, unit = setup_quadlet.render(args)
    config_path = tmp_path / "config.toml"
    config_path.write_bytes(config)
    parsed = tomllib.loads(config.decode())
    assert "local_worker_ports" not in parsed
    settings = load_settings(str(config_path))
    assert settings.bind == ("127.0.0.1", args.gateway_ip)
    assert settings.node_networks == (args.node_network,)
    assert (settings.port, settings.worker_port) == (9081, 9082)
    assert json.loads(values) == {
        "gatewayUrl": "http://192.168.122.1:9081",
        "worker": {"port": 9082},
        "image": args.worker_image,
    }
    assert f"Image={args.gateway_image}" in unit.decode()
    assert f"Volume={args.cluster_dir}:/run/fractal-cluster:ro" in unit.decode()
    assert "Volume=/etc/fractal-demo:/etc/fractal-demo:ro" in unit.decode()
    assert "--kubeconfig /run/fractal-cluster/kubeconfig" in unit.decode()
    assert "--helm-values /etc/fractal-demo/values.local.json" in unit.decode()


def test_install_and_rerun_preserve_previous_set(tmp_path, args):
    config_dir, unit_dir = tmp_path / "etc/fractal-demo", tmp_path / "etc/containers/systemd"
    calls = []

    def run(command, *, check):
        assert check
        calls.append(command)

    first = setup_quadlet.render(args)
    setup_quadlet.install(first, config_dir, unit_dir, run=run)
    targets = (
        config_dir / "config.toml",
        config_dir / "values.local.json",
        unit_dir / "fractal-gateway.container",
    )
    assert [path.read_bytes() for path in targets] == list(first)
    assert [stat.S_IMODE(path.stat().st_mode) for path in targets] == [0o600, 0o600, 0o644]
    assert stat.S_IMODE(config_dir.stat().st_mode) == 0o700
    assert not (config_dir / "previous").exists()
    args.worker_image = "ghcr.io/demo/worker:second"
    second = setup_quadlet.render(args)
    setup_quadlet.install(second, config_dir, unit_dir, run=run)
    assert [path.read_bytes() for path in targets] == list(second)
    assert [(config_dir / "previous" / path.name).read_bytes() for path in targets] == list(first)
    assert (
        calls
        == [
            ["systemctl", "daemon-reload"],
            ["systemctl", "restart", "fractal-gateway.service"],
        ]
        * 2
    )


def test_failed_restart_restores_existing_files(tmp_path, args):
    config_dir, unit_dir = tmp_path / "etc/fractal-demo", tmp_path / "etc/containers/systemd"
    first = setup_quadlet.render(args)
    setup_quadlet.install(first, config_dir, unit_dir, run=lambda *a, **kw: None)
    args.gateway_image = "ghcr.io/demo/gateway:bad"
    calls = []

    def run(command, *, check):
        calls.append(command)
        if len(calls) == 2:
            raise subprocess.CalledProcessError(1, command)

    with pytest.raises(subprocess.CalledProcessError):
        setup_quadlet.install(setup_quadlet.render(args), config_dir, unit_dir, run=run)
    assert [
        (config_dir / "config.toml").read_bytes(),
        (config_dir / "values.local.json").read_bytes(),
        (unit_dir / "fractal-gateway.container").read_bytes(),
    ] == list(first)
    assert calls == [
        ["systemctl", "daemon-reload"],
        ["systemctl", "restart", "fractal-gateway.service"],
        ["systemctl", "daemon-reload"],
        ["systemctl", "restart", "fractal-gateway.service"],
    ]


@pytest.mark.parametrize("value", ["../cluster", "/tmp/has space", "/tmp/evil\n[Service]"])
def test_unsafe_cluster_dir_is_rejected(value):
    with pytest.raises(argparse.ArgumentTypeError):
        setup_quadlet.credential_directory(value)


@pytest.mark.parametrize("value", ["", "image\nExec=evil", "image with space"])
def test_unsafe_image_is_rejected(value):
    with pytest.raises(argparse.ArgumentTypeError):
        setup_quadlet.image_reference(value)


@pytest.mark.parametrize("value", ["0", "65536", "not-a-port"])
def test_invalid_port_is_rejected(value):
    with pytest.raises(argparse.ArgumentTypeError):
        setup_quadlet.port(value)


@pytest.mark.parametrize("field,value", [("gateway_ip", "not-an-ip"), ("node_network", "bad")])
def test_invalid_network_input_has_no_files(args, field, value, tmp_path):
    setattr(args, field, value)
    with pytest.raises(ValueError):
        setup_quadlet.render(args)
    assert list(tmp_path.glob("etc/**")) == []
