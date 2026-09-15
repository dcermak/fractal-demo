"""Startup configuration errors should be actionable before opening listeners."""

from pathlib import Path

import pytest

from fractal_demo.gateway import load_settings
from fractal_demo.protocol import ProtocolError
from fractal_demo.worker import WorkerSettings


def test_example_configuration_and_partial_override(tmp_path):
    example = Path(__file__).resolve().parents[1] / "config.example.toml"
    settings = load_settings(str(example))
    assert settings.local_worker_ports == {"worker-a": 8081, "worker-b": 8082}
    assert settings.browser_config()["geometry"]["width"] == 960
    config = tmp_path / "partial.toml"
    config.write_text("[view]\niterations = 123\n[timing]\ndwell_ms = 2000\n")
    settings = load_settings(str(config))
    assert settings.view["iterations"] == 123
    assert settings.view["pixel_size"] == 0.00025
    assert settings.timing["dwell_ms"] == 2000
    assert "node_networks" not in settings.browser_config()


@pytest.mark.parametrize(("text", "message"), [
    ("potr = 8080", "potr"),
    ("bind = '127.0.0.1'", "bind"),
    ("bind = ['0.0.0.0']", "bind"),
    ("node_networks = ['not-a-network']", "node_networks"),
    ("port = true", "port"),
    ("upstream_timeout = nan", "upstream_timeout"),
    ("connect_timeout = 20", "connect_timeout"),
    ("[timing]\nrender_timeout_ms = 100", "render_timeout_ms"),
    ("[geometry]\ncolumns = 17", "geometry"),
    ("[geometry]\ncolumns = 1", "geometry"),
    ("[view]\nxmin = 100", "view"),
    ("[view]\niterations = 1.5", "iterations"),
    ("[view]\npixel_size = true", "pixel_size"),
    ("[timing]\ndewll_ms = 1000", "timing"),
    ("[local_worker_ports]\nworker-a = 70000", "worker-a"),
])
def test_invalid_config_names_setting(tmp_path, text, message):
    config = tmp_path / "invalid.toml"
    config.write_text(text)
    with pytest.raises(ProtocolError, match=message):
        load_settings(str(config))


@pytest.mark.parametrize("origin", [
    "https://example.test", "http://user:pass@example.test", "http://example.test/path",
    "http://example.test?path=/register", "http://example.test#fragment", "example.test",
])
def test_worker_registration_requires_explicit_http_origin(origin):
    with pytest.raises(ProtocolError, match="gateway_url"):
        WorkerSettings(node="worker-a", host_ip="127.0.0.1", gateway_url=origin).validate()
