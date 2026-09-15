"""Run inside the built image; no test framework or host networking is required."""

import importlib.util
import json
import math
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, build_opener

from PIL import Image

HTTP = build_opener(ProxyHandler({}))
BIN = Path(sys.prefix) / "bin"


def available_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


@contextmanager
def running(command, log):
    with log.open("w") as stream:
        process = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT)
        try:
            yield process
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
                raise RuntimeError(f"Process did not drain: {command[0]}") from None
            if process.returncode != 0:
                raise RuntimeError(f"Process exited with {process.returncode}: {log.read_text()}")


def wait_for_workers(origin, processes):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if any(process.poll() is not None for process in processes):
            raise RuntimeError("An application process exited before registration")
        try:
            with HTTP.open(origin + "/api/workers", timeout=2) as response:
                workers = json.load(response)["workers"]
                if workers:
                    return workers
        except (URLError, TimeoutError):
            pass
        time.sleep(0.1)
    raise RuntimeError("Worker did not register within the smoke-test deadline")


def main():
    for package in ("pytest", "ruff", "playwright", "hatchling"):
        assert importlib.util.find_spec(package) is None, f"Development package shipped: {package}"
    gateway_port, worker_port = available_port(), available_port()
    while worker_port == gateway_port:
        worker_port = available_port()
    origin = f"http://127.0.0.1:{gateway_port}"
    with tempfile.TemporaryDirectory(prefix="fractal-smoke-") as temporary:
        directory = Path(temporary)
        config = directory / "gateway.toml"
        config.write_text(f"port = {gateway_port}\nworker_port = {worker_port}\n")
        try:
            with (
                running([str(BIN / "fractal-gateway"), "--config", str(config)],
                        directory / "gateway.log") as gateway,
                running([
                    str(BIN / "fractal-worker"), "--node", "image-smoke",
                    "--host-ip", "127.0.0.1", "--bind", "127.0.0.1",
                    "--port", str(worker_port), "--gateway-url", origin,
                ], directory / "worker.log") as worker,
            ):
                roster = wait_for_workers(origin, (gateway, worker))
                assert len(roster) == 1 and roster[0]["node"] == "image-smoke"
                selected = roster[0]
                for port in (gateway_port, worker_port):
                    with HTTP.open(f"http://127.0.0.1:{port}/healthz", timeout=3) as response:
                        assert json.load(response)["status"] == "ok"
                for path in ("/", "/static/app.css", "/static/app.js", "/static/scheduler.js"):
                    with HTTP.open(origin + path, timeout=3) as response:
                        assert response.read(), f"Empty packaged asset: {path}"
                query = urlencode({
                    "xmin": 0, "ymin": 0, "pixel_size": 3, "px": 0, "py": 0,
                    "width": 2, "height": 1, "iterations": 20, "palette": "cyber",
                    "expected_process_id": selected["process_id"],
                })
                render_url = origin + selected["render_url"] + "?" + query
                with HTTP.open(render_url, timeout=10) as response:
                    assert response.headers["X-Worker-ID"] == selected["process_id"]
                    assert response.headers["X-Render-Node"] == "image-smoke"
                    assert response.headers["Content-Type"].startswith("image/png")
                    with Image.open(BytesIO(response.read())) as image:
                        image.load()
                        assert image.mode == "RGB" and image.size == (2, 1)
                        assert image.getpixel((0, 0)) == (10, 13, 20)
                        # Independent c=3 escape after one iteration, on the first color segment.
                        weight = (2 - math.log2(math.log(3))) / 8
                        expected = tuple(math.floor(a + weight * (b - a))
                                         for a, b in zip((18, 25, 65), (33, 205, 225), strict=True))
                        assert image.getpixel((1, 0)) == expected
        except BaseException:
            for log in directory.glob("*.log"):
                print(f"{log.name}:\n{log.read_text()}", file=sys.stderr)
            raise
    print("Image smoke check passed: registration, identity, pixels, packaged assets, and shutdown")


if __name__ == "__main__":
    main()
