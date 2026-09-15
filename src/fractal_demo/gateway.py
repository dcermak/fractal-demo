"""Host-side worker discovery, packaged kiosk assets, and bounded render proxy."""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import logging
import math
import signal
import time
import tomllib
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path

from aiohttp import ClientError, ClientSession, ClientTimeout, TCPConnector, web
from multidict import MultiDict
from yarl import URL

from .protocol import (
    NO_STORE,
    REGISTRATION_LIMIT,
    RESPONSE_LIMIT,
    ROSTER_LIMIT,
    ProtocolError,
    error_middleware,
    error_response,
    literal_ip,
    node_name,
    parse_registration,
    parse_render_query,
    process_id,
    read_limited,
    unique_json,
)
from .render import PALETTES

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class GatewaySettings:
    bind: tuple[str, ...] = ("127.0.0.1",)
    port: int = 8080
    node_networks: tuple[str, ...] = ("127.0.0.0/8",)
    worker_port: int = 8080
    local_worker_ports: dict[str, int] = field(default_factory=dict)
    expiry_seconds: float = 1.5
    proxy_limit: int = 5
    connect_timeout: float = 1.0
    upstream_timeout: float = 10.0
    geometry: dict = field(default_factory=lambda: {
        "width": 960, "height": 540, "columns": 16, "rows": 9,
    })
    view: dict = field(default_factory=lambda: {
        "xmin": -0.9, "ymin": 0.08, "pixel_size": 0.00025, "iterations": 800,
    })
    default_palette: str = "cyber"
    timing: dict = field(default_factory=lambda: {
        "poll_interval_ms": 500, "poll_timeout_ms": 2000, "render_timeout_ms": 12000,
        "cooldown_ms": 350, "lost_ms": 600, "dwell_ms": 1500, "rate_window_ms": 5000,
    })
    render_limit: int = 5

    def validate(self):
        for key in ("port", "worker_port"):
            bounded_int(getattr(self, key), key, 1, 65535)
        if not self.bind:
            raise ProtocolError("bind must contain at least one local interface address")
        for address in self.bind:
            if literal_ip(address).is_unspecified:
                raise ProtocolError("bind must name intended interfaces, not a wildcard address")
        if not self.node_networks:
            raise ProtocolError("node_networks must contain at least one allowed node network")
        for network in self.node_networks:
            try:
                ipaddress.ip_network(network)
            except ValueError as exc:
                raise ProtocolError(f"node_networks contains invalid network: {network}") from exc
        for node, port in self.local_worker_ports.items():
            node_name(node)
            bounded_int(port, f"local_worker_ports.{node}", 1, 65535)
        bounded_int(self.proxy_limit, "proxy_limit", 1, 256)
        bounded_int(self.render_limit, "render_limit", 1, 32)
        for key in ("expiry_seconds", "connect_timeout", "upstream_timeout"):
            positive(getattr(self, key), key)
        if self.connect_timeout > self.upstream_timeout:
            raise ProtocolError("connect_timeout must not exceed upstream_timeout")
        if set(self.geometry) != {"width", "height", "columns", "rows"}:
            raise ProtocolError("geometry requires width, height, columns, rows")
        for key, value in self.geometry.items():
            bounded_int(value, f"geometry.{key}", 1, 8192)
        width, height = self.geometry["width"], self.geometry["height"]
        columns, rows = self.geometry["columns"], self.geometry["rows"]
        if width % columns or height % rows or columns * rows > 4096:
            raise ProtocolError("geometry must divide evenly into at most 4096 tiles")
        if set(self.view) != {"xmin", "ymin", "pixel_size", "iterations"}:
            raise ProtocolError("view requires xmin, ymin, pixel_size, iterations")
        bounded_int(self.view["iterations"], "view.iterations", 1, 10000)
        for key in ("xmin", "ymin", "pixel_size"):
            value = self.view[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ProtocolError(f"view.{key} must be a number")
        # Validate both corners of the entire raster, with the same parser as actual requests.
        for px, py in ((0, 0), (width - width // columns, height - height // rows)):
            values = {
                **self.view, "px": px, "py": py, "width": width // columns,
                "height": height // rows, "palette": self.default_palette,
                "expected_process_id": "00000000-0000-0000-0000-000000000000",
            }
            try:
                parse_render_query(MultiDict({key: str(value) for key, value in values.items()}))
            except ProtocolError as exc:
                raise ProtocolError(f"view/geometry: {exc}") from exc
        expected_timing = GatewaySettings().timing
        if set(self.timing) != set(expected_timing):
            raise ProtocolError("timing contains missing or unknown keys")
        for key, value in self.timing.items():
            bounded_int(value, f"timing.{key}", 1, 300000)
        if self.timing["render_timeout_ms"] <= self.upstream_timeout * 1000:
            raise ProtocolError(
                "timing.render_timeout_ms must exceed upstream_timeout in milliseconds"
            )

    def browser_config(self):
        return {
            "geometry": self.geometry, "view": self.view, "palettes": PALETTES,
            "default_palette": self.default_palette, "timing": self.timing,
            "render_limit": self.render_limit,
        }


def bounded_int(value, key, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ProtocolError(f"{key} must be an integer from {low} through {high}")


def positive(value, key):
    if (
        isinstance(value, bool) or not isinstance(value, (int, float))
        or not math.isfinite(value) or value <= 0
    ):
        raise ProtocolError(f"{key} must be positive and finite")


def load_settings(path: str | None) -> GatewaySettings:
    if path is None:
        return GatewaySettings()
    with Path(path).open("rb") as stream:
        data = tomllib.load(stream)
    scalar_keys = {
        "bind", "port", "node_networks", "worker_port", "expiry_seconds", "proxy_limit",
        "connect_timeout", "upstream_timeout", "default_palette", "render_limit",
    }
    table_keys = {"geometry", "view", "timing", "local_worker_ports"}
    if set(data) - scalar_keys - table_keys:
        unknown = ", ".join(sorted(set(data) - scalar_keys - table_keys))
        raise ProtocolError("unknown configuration keys: " + unknown)
    defaults = GatewaySettings()
    for key in ("bind", "node_networks"):
        if key in data:
            if not isinstance(data[key], list) or not all(isinstance(x, str) for x in data[key]):
                raise ProtocolError(f"{key} must be an array of strings")
            data[key] = tuple(data[key])
    for key in table_keys:
        if key in data:
            if not isinstance(data[key], dict):
                raise ProtocolError(f"{key} must be a table")
            data[key] = {**getattr(defaults, key), **data[key]}
    settings = GatewaySettings(**data)
    settings.validate()
    return settings


@dataclass(frozen=True, slots=True)
class WorkerRecord:
    node: str
    process_id: str
    host_ip: str
    last_seen: float


class Registry:
    """Loop-owned registry: methods contain no awaits or network calls."""

    def __init__(self, expiry: float, *, clock=time.monotonic, capacity=ROSTER_LIMIT):
        self.records: dict[str, WorkerRecord] = {}
        self.expiry = expiry
        self.clock = clock
        self.capacity = capacity

    def expire(self):
        now = self.clock()
        self.records = {
            node: record for node, record in self.records.items()
            if now - record.last_seen < self.expiry
        }

    def register(self, registration):
        self.expire()
        if registration.node not in self.records and len(self.records) >= self.capacity:
            return False
        self.records[registration.node] = WorkerRecord(
            registration.node, registration.process_id, registration.host_ip, self.clock(),
        )
        return True

    def lookup(self, selected_id):
        self.expire()
        return next((x for x in self.records.values() if x.process_id == selected_id), None)

    def roster(self):
        self.expire()
        return [{"node": record.node, "process_id": record.process_id,
                 "render_url": f"/api/render/{record.process_id}"}
                for record in self.records.values()]


class GatewayState:
    def __init__(self, settings, registry):
        self.settings = settings
        self.registry = registry
        self.networks = tuple(ipaddress.ip_network(x) for x in settings.node_networks)
        self.active = 0
        self.session: ClientSession | None = None


STATE = web.AppKey("gateway_state", GatewayState)


async def register(request):
    state = request.app[STATE]
    registration = parse_registration(await request.read(), state.networks)
    if registration.node in state.settings.local_worker_ports:
        if not literal_ip(registration.host_ip).is_loopback:
            raise ProtocolError("local_worker_ports overrides require a loopback host_ip")
    target_port = state.settings.local_worker_ports.get(
        registration.node, state.settings.worker_port,
    )
    # Use the bound socket's port, including when a test listener chooses an ephemeral port.
    listener = request.transport.get_extra_info("sockname")
    if target_port == listener[1] and literal_ip(registration.host_ip) in {
        literal_ip(address) for address in state.settings.bind
    }:
        raise ProtocolError(
            f"{registration.node} routes to the gateway at {registration.host_ip}:{target_port}; "
            "correct local_worker_ports or worker_port to match the worker listener"
        )
    if not state.registry.register(registration):
        return error_response(503, "busy", "Worker registry is full; retry after expiry", "gateway")
    return web.json_response({"process_id": registration.process_id}, headers=NO_STORE)


async def workers(request):
    return web.json_response({"workers": request.app[STATE].registry.roster()}, headers=NO_STORE)


async def config(request):
    return web.json_response(request.app[STATE].settings.browser_config(), headers=NO_STORE)


async def health(request):
    return web.json_response({"status": "ok"}, headers=NO_STORE)


async def read_bounded(response, limit=RESPONSE_LIMIT):
    return await read_limited(response.content, limit)


async def proxy(request):
    state = request.app[STATE]
    tile = parse_render_query(request.query)
    selected_id = process_id(request.match_info["process_id"])
    if selected_id != tile.expected_process_id:
        return error_response(
            409, "incarnation_mismatch", "Selected process IDs disagree", "gateway",
        )
    record = state.registry.lookup(selected_id)
    if record is None:
        return error_response(
            404, "unknown_worker", "Worker registration is no longer current", "gateway",
        )
    if state.active >= state.settings.proxy_limit:
        return error_response(503, "busy", "Gateway render capacity is occupied", "gateway")
    # Snapshot identity before awaiting. Replacement registration cannot retarget this assignment.
    port = state.settings.local_worker_ports.get(record.node, state.settings.worker_port)
    url = URL.build(scheme="http", host=record.host_ip, port=port, path="/render")
    state.active += 1
    try:
        async with state.session.get(
            url, params=tile.query(), allow_redirects=False,
            headers={"Accept-Encoding": "identity"},
        ) as response:
            encoding = response.headers.get("Content-Encoding", "identity").lower()
            if encoding != "identity":
                raise ProtocolError("Unexpected upstream content encoding")
            if response.status == 200:
                if (
                    response.headers.get("X-Worker-ID") != record.process_id
                    or response.headers.get("X-Render-Node") != record.node
                ):
                    raise ProtocolError("Upstream response identity does not match selected worker")
                if response.content_type != "image/png":
                    raise ProtocolError("Expected an upstream PNG response")
                body = await read_bounded(response)
                return web.Response(body=body, content_type="image/png", headers={
                    **NO_STORE, "X-Worker-ID": record.process_id, "X-Render-Node": record.node,
                })
            if not 400 <= response.status <= 599 or response.content_type != "application/json":
                raise ProtocolError("Unexpected upstream status or error content type")
            body = await read_bounded(response, 8192)
            error = unique_json(body)
            if (
                not isinstance(error, dict) or set(error) != {"code", "message", "scope"}
                or error["scope"] != "worker"
                or not isinstance(error["code"], str) or not 1 <= len(error["code"]) <= 64
                or not isinstance(error["message"], str) or len(error["message"]) > 400
            ):
                raise ProtocolError("Invalid upstream error response")
            return web.json_response(error, status=response.status, headers=NO_STORE)
    except ProtocolError as exc:
        return error_response(502, "upstream_protocol", str(exc), "gateway")
    except TimeoutError:
        return error_response(504, "upstream_timeout", "Worker render path timed out", "gateway")
    except ClientError:
        return error_response(
            502, "upstream_failure", "Lost contact with worker render path", "gateway",
        )
    finally:
        state.active -= 1


ASSETS = {
    "/": ("index.html", "text/html"),
    "/static/app.css": ("app.css", "text/css"),
    "/static/app.js": ("app.js", "text/javascript"),
    "/static/scheduler.js": ("scheduler.js", "text/javascript"),
}


async def asset(request):
    name, content_type = ASSETS[request.path]
    body = files("fractal_demo").joinpath("static", name).read_bytes()
    return web.Response(body=body, content_type=content_type, headers=NO_STORE)


async def session_context(app):
    state = app[STATE]
    settings = state.settings
    async with ClientSession(
        connector=TCPConnector(limit=settings.proxy_limit),
        timeout=ClientTimeout(
            total=settings.upstream_timeout, sock_connect=settings.connect_timeout,
            ceil_threshold=math.inf,
        ),
        trust_env=False, auto_decompress=False,
    ) as session:
        state.session = session
        yield


def create_gateway_app(settings: GatewaySettings, *, registry=None) -> web.Application:
    settings.validate()
    app = web.Application(
        client_max_size=REGISTRATION_LIMIT + 1, middlewares=[error_middleware("gateway")],
    )
    app[STATE] = GatewayState(settings, registry or Registry(settings.expiry_seconds))
    app.cleanup_ctx.append(session_context)
    for path in ASSETS:
        app.router.add_get(path, asset)
    app.router.add_get("/healthz", health)
    app.router.add_post("/register", register)
    app.router.add_get("/api/workers", workers)
    app.router.add_get("/api/config", config)
    app.router.add_get("/api/render/{process_id}", proxy, allow_head=False)
    return app


async def serve(settings):
    runner = web.AppRunner(create_gateway_app(settings), handler_cancellation=True)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        await runner.setup()
        for address in settings.bind:
            await web.TCPSite(runner, address, settings.port).start()
        LOG.info("Gateway listening on %s port %s", ", ".join(settings.bind), settings.port)
        await stop.wait()
    finally:
        await runner.cleanup()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Gateway TOML file; defaults to loopback-only settings")
    args = parser.parse_args()
    try:
        settings = load_settings(args.config)
    except (ValueError, OSError, TypeError) as exc:
        parser.error(str(exc))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(serve(settings))
