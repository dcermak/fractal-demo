import asyncio
import threading

import pytest
from aiohttp import web

from fractal_demo import worker
from fractal_demo.render import render_tile


class TCPApp:
    """Run the actual application on an ephemeral loopback TCP listener."""

    def __init__(self, app, *, port=0, ready=True):
        self.app = app
        self.port = port
        self.ready = ready
        self.runner = web.AppRunner(app, handler_cancellation=True, shutdown_timeout=2)

    async def __aenter__(self):
        try:
            await self.runner.setup()
            site = web.TCPSite(self.runner, "127.0.0.1", self.port)
            await site.start()
            self.port = site._server.sockets[0].getsockname()[1]
            self.url = f"http://127.0.0.1:{self.port}"
            if self.ready and worker.STATE in self.app:
                self.app[worker.STATE].listener_ready.set()
            return self
        except BaseException:
            await self.runner.cleanup()
            raise

    async def __aexit__(self, *exc):
        await self.runner.cleanup()


class HeldRenderer:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self.finished = 0

    def __call__(self, tile):
        self.calls += 1
        self.entered.set()
        if not self.release.wait(10):
            raise TimeoutError("test did not release renderer")
        result = render_tile(tile)
        self.finished += 1
        return result


@pytest.fixture
def tcp_app():
    return TCPApp


@pytest.fixture
def held_renderer():
    renderer = HeldRenderer()
    try:
        yield renderer
    finally:
        renderer.release.set()


@pytest.fixture
def wait_for():
    async def wait(predicate):
        async with asyncio.timeout(3):
            # Observe thread events and production state without adding production test hooks.
            while not predicate():  # noqa: ASYNC110
                await asyncio.sleep(0.01)

    return wait
