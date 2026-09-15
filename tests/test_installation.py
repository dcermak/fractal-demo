"""Installed-wheel entry points, process identity, and orderly local shutdown."""

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

import pytest

ROOT = Path(__file__).resolve().parents[1]
HTTP = build_opener(ProxyHandler({}))


@pytest.fixture(scope="module")
def installed(tmp_path_factory):
    directory = tmp_path_factory.mktemp("installed-wheel")
    uv = shutil.which("uv")
    assert uv, "uv is required for installed-package verification"
    subprocess.run(
        [uv, "build", "--wheel", "--offline", "--no-build-isolation", "--out-dir",
         str(directory / "dist")],
        cwd=ROOT, check=True, capture_output=True, text=True, timeout=60,
    )
    wheel, = (directory / "dist").glob("*.whl")
    target = directory / "package"
    subprocess.run(
        [uv, "pip", "install", "--offline", "--no-deps", "--python", sys.executable,
         "--target", str(target), str(wheel)],
        check=True, capture_output=True, text=True, timeout=60,
    )
    return directory, target


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def request_json(url):
    with HTTP.open(url, timeout=1) as response:
        return json.load(response)


def wait_until(predicate, process, log, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        assert process.poll() is None, log.read_text()
        try:
            result = predicate()
            if result:
                return result
        except (URLError, TimeoutError):
            pass
        time.sleep(0.04)
    pytest.fail(f"Process did not reach expected state: {log.read_text()}")


@contextmanager
def launch(installed, role, args, log_name):
    directory, target = installed
    log = directory / log_name
    # Dependencies use the test environment; application imports must come from the installed wheel.
    env = {**os.environ, "PYTHONPATH": str(target), "PYTHONUNBUFFERED": "1"}
    with log.open("w") as output:
        process = subprocess.Popen(
            [sys.executable, str(target / "bin" / role), *args],
            cwd=directory, env=env, stdout=output, stderr=subprocess.STDOUT,
        )
        try:
            yield process, log
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                    pytest.fail(f"Process did not shut down: {log.read_text()}")


def test_installed_entrypoints_assets_and_worker_restart(installed):
    directory, target = installed
    # Confirm isolation rather than accidentally testing the editable checkout.
    check = subprocess.run(
        [sys.executable, "-c", "import fractal_demo; print(fractal_demo.__file__)"],
        cwd=directory, env={**os.environ, "PYTHONPATH": str(target)},
        check=True, capture_output=True, text=True, timeout=5,
    )
    assert str(target) in check.stdout
    gateway_port, worker_port = free_port(), free_port()
    while worker_port == gateway_port:
        worker_port = free_port()
    config = directory / "gateway.toml"
    config.write_text(
        f"port = {gateway_port}\nworker_port = {worker_port}\nexpiry_seconds = 0.4\n"
    )
    origin = f"http://127.0.0.1:{gateway_port}"
    with launch(installed, "fractal-gateway", ["--config", str(config)], "gateway.log") as gateway:
        gw, glog = gateway
        wait_until(lambda: request_json(origin + "/healthz"), gw, glog)
        assert request_json(origin + "/api/workers") == {"workers": []}
        for asset, marker in (
            ("/", b"<!DOCTYPE html>"), ("/static/app.js", b"import"),
            ("/static/scheduler.js", b"export"), ("/static/app.css", b"canvas"),
        ):
            with HTTP.open(origin + asset, timeout=1) as response:
                assert marker.lower() in response.read().lower()
        worker_args = [
            "--node", "installed-worker", "--host-ip", "127.0.0.1", "--bind", "127.0.0.1",
            "--port", str(worker_port), "--gateway-url", origin,
            "--registration-interval", "0.05",
        ]
        with launch(installed, "fractal-worker", worker_args, "worker-1.log") as (wk, wlog):
            roster = wait_until(lambda: request_json(origin + "/api/workers")["workers"], wk, wlog)
            first_id = roster[0]["process_id"]
            wk.terminate()
            assert wk.wait(timeout=10) == 0, wlog.read_text()
        with launch(installed, "fractal-worker", worker_args, "worker-2.log") as (wk, wlog):
            def replaced():
                roster = request_json(origin + "/api/workers")["workers"]
                return roster if roster and roster[0]["process_id"] != first_id else None

            roster = wait_until(replaced, wk, wlog)
            worker = roster[0]
            query = (
                "?xmin=0&ymin=0&pixel_size=0.01&px=0&py=0&width=2&height=2"
                f"&iterations=20&palette=cyber&expected_process_id={worker['process_id']}"
            )
            with HTTP.open(origin + worker["render_url"] + query, timeout=3) as response:
                assert response.headers["X-Worker-ID"] == worker["process_id"]
                assert response.read().startswith(b"\x89PNG\r\n\x1a\n")
            direct = f"http://127.0.0.1:{worker_port}/render"
            with pytest.raises(HTTPError) as error:
                HTTP.open(direct + query.replace(worker["process_id"], first_id), timeout=1)
            with error.value as response:
                assert response.code == 409
                assert json.load(response)["code"] == "incarnation_mismatch"
            # HEAD cannot reach the numerical renderer, through either entry point.
            with pytest.raises(HTTPError) as error:
                HTTP.open(Request(origin + worker["render_url"] + query, method="HEAD"), timeout=1)
            error.value.close()
            assert error.value.code == 405
        gw.terminate()
        assert gw.wait(timeout=10) == 0, glog.read_text()
