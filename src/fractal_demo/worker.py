"""One process, one future-owned compute slot, and periodic registration."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import math
import os
import signal
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass

from aiohttp import ClientError, ClientSession, ClientTimeout, web
from yarl import URL

from .protocol import (
    NO_STORE,
    ProtocolError,
    error_middleware,
    error_response,
    literal_ip,
    node_name,
    parse_render_query,
    read_limited,
    unique_json,
)
from .render import render_tile

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkerSettings:
    node: str
    host_ip: str
    gateway_url: str
    bind: str = "0.0.0.0"
    port: int = 8080
    registration_interval: float = 0.5
    registration_timeout: float = 2.0

    def validate(self):
        node_name(self.node)
        literal_ip(self.host_ip)
        literal_ip(self.bind)
        if not 1 <= self.port <= 65535:
            raise ProtocolError("worker port must be from 1 through 65535")
        for key in ("registration_interval", "registration_timeout"):
            value = getattr(self, key)
            if not math.isfinite(value) or value <= 0:
                raise ProtocolError(f"{key} must be positive and finite")
        url = URL(self.gateway_url)
        if (
            url.scheme != "http"
            or not url.host
            or url.user is not None
            or url.query_string
            or url.fragment
            or url.path not in ("", "/")
        ):
            raise ProtocolError("gateway_url must be an HTTP origin, such as http://192.0.2.1:8080")


class WorkerState:
    def __init__(self, settings: WorkerSettings, renderer):
        self.settings = settings
        self.process_id = str(uuid.uuid4())
        self.renderer = renderer
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fractal")
        self.admitting = True
        self.future: Future | None = None
        self.completion: asyncio.Future | None = None
        self.listener_ready = asyncio.Event()
        self.registration_task: asyncio.Task | None = None

    def submit(self, tile):
        """Only the executor future may return capacity, even after HTTP cancellation."""
        loop = asyncio.get_running_loop()
        completion = loop.create_future()
        # Retrieve exceptions even when the requesting HTTP handler has disconnected.
        completion.add_done_callback(lambda item: None if item.cancelled() else item.exception())
        future = self.executor.submit(self.renderer, tile)
        self.future, self.completion = future, completion

        def finished(done):
            # The callback runs in the executor thread; all state changes belong to the loop.
            loop.call_soon_threadsafe(self.finish, done, completion)

        future.add_done_callback(finished)
        return completion

    def finish(self, future, completion):
        if self.future is future:
            self.future = None
        if not completion.done():
            try:
                completion.set_result(future.result())
            except Exception as exc:
                completion.set_exception(exc)

    async def register(self):
        await self.listener_ready.wait()
        settings = self.settings
        url = str(URL(settings.gateway_url).with_path("/register"))
        payload = {
            "node": settings.node,
            "process_id": self.process_id,
            "host_ip": settings.host_ip,
        }
        retry = settings.registration_interval
        async with ClientSession(
            timeout=ClientTimeout(total=settings.registration_timeout),
            trust_env=False,
        ) as session:
            while self.admitting:
                try:
                    async with session.post(url, json=payload, allow_redirects=False) as response:
                        body = await read_limited(response.content, 8192)
                        if response.status != 200:
                            detail = unique_json(body)
                            message = detail.get("message", "") if isinstance(detail, dict) else ""
                            raise ValueError(
                                f"registration HTTP {response.status}: {str(message)[:400]}"
                            )
                        accepted = unique_json(body)
                        if (
                            not isinstance(accepted, dict)
                            or accepted.get("process_id") != self.process_id
                        ):
                            raise ValueError("gateway did not acknowledge this process ID")
                    retry = settings.registration_interval
                except (ClientError, TimeoutError, ValueError) as exc:
                    LOG.warning("Registration failed: %s", exc)
                    retry = min(retry * 2, max(5.0, settings.registration_interval))
                await asyncio.sleep(retry)


STATE = web.AppKey("worker_state", WorkerState)


async def render(request: web.Request):
    state = request.app[STATE]
    tile = parse_render_query(request.query)
    if tile.expected_process_id != state.process_id:
        return error_response(409, "incarnation_mismatch", "Worker process has changed", "worker")
    if not state.admitting or state.future is not None:
        return error_response(503, "busy", "Worker compute slot is occupied or stopping", "worker")
    try:
        completion = state.submit(tile)
        png = await asyncio.shield(completion)
    except Exception:
        LOG.exception("Tile computation failed")
        return error_response(
            500,
            "render_failed",
            "Tile computation failed; check worker log",
            "worker",
        )
    return web.Response(
        body=png,
        content_type="image/png",
        headers={**NO_STORE, "X-Worker-ID": state.process_id, "X-Render-Node": state.settings.node},
    )


async def health(request):
    state = request.app[STATE]
    return web.json_response({"status": "ok", "process_id": state.process_id}, headers=NO_STORE)


async def startup(app):
    state = app[STATE]
    state.registration_task = asyncio.create_task(state.register(), name="registration")


async def shutdown(app):
    state = app[STATE]
    state.admitting = False
    if state.registration_task:
        state.registration_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await state.registration_task
    if state.completion is not None:
        with contextlib.suppress(Exception):
            await asyncio.shield(state.completion)
    # Work is drained. This call cannot block the loop on running computation.
    state.executor.shutdown(wait=True)


def create_worker_app(settings: WorkerSettings, *, renderer=render_tile) -> web.Application:
    settings.validate()
    app = web.Application(middlewares=[error_middleware("worker")])
    app[STATE] = WorkerState(settings, renderer)
    app.router.add_get("/render", render, allow_head=False)
    app.router.add_get("/healthz", health)
    app.on_startup.append(startup)
    app.on_shutdown.append(shutdown)
    return app


async def serve(settings: WorkerSettings):
    app = create_worker_app(settings)
    runner = web.AppRunner(app, handler_cancellation=True)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        await runner.setup()
        await web.TCPSite(runner, settings.bind, settings.port).start()
        app[STATE].listener_ready.set()
        LOG.info(
            "Worker %s (%s) listening on %s:%s",
            settings.node,
            app[STATE].process_id,
            settings.bind,
            settings.port,
        )
        await stop.wait()
    finally:
        await runner.cleanup()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", default=os.getenv("NODE_NAME"))
    parser.add_argument("--host-ip", default=os.getenv("HOST_IP"))
    parser.add_argument("--gateway-url", default=os.getenv("GATEWAY_URL"))
    parser.add_argument("--bind", default=os.getenv("WORKER_BIND", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=os.getenv("WORKER_PORT", "8080"))
    parser.add_argument(
        "--registration-interval", type=float, default=os.getenv("REGISTRATION_INTERVAL", "0.5")
    )
    parser.add_argument(
        "--registration-timeout", type=float, default=os.getenv("REGISTRATION_TIMEOUT", "2")
    )
    args = parser.parse_args()
    if not all((args.node, args.host_ip, args.gateway_url)):
        parser.error(
            "--node, --host-ip and --gateway-url (or their environment variables) are required"
        )
    settings = WorkerSettings(**vars(args))
    try:
        settings.validate()
    except (ValueError, TypeError) as exc:
        parser.error(str(exc))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(serve(settings))
