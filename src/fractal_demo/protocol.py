"""Small explicit wire contracts shared by the worker and gateway."""

from __future__ import annotations

import ipaddress
import json
import math
import re
import uuid
from dataclasses import dataclass

from aiohttp import web
from multidict import MultiMapping

PALETTE_IDS = ("cyber", "fire")
REGISTRATION_LIMIT = 8 * 1024
RESPONSE_LIMIT = 1024 * 1024
ROSTER_LIMIT = 256
NO_STORE = {"Cache-Control": "no-store"}
NODE_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
INTEGER_PATTERN = re.compile(r"[0-9]+\Z")
RENDER_FIELDS = {
    "xmin", "ymin", "pixel_size", "px", "py", "width", "height", "iterations",
    "palette", "expected_process_id",
}


class ProtocolError(ValueError):
    """Invalid public request or configuration value."""


def process_id(value: object) -> str:
    if not isinstance(value, str) or len(value) != 36:
        raise ProtocolError("process_id must be a hyphenated UUID")
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise ProtocolError("process_id must be a UUID") from exc


def node_name(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 253:
        raise ProtocolError("node must be a Kubernetes-style name of 1 through 253 characters")
    if not all(NODE_PATTERN.fullmatch(label) for label in value.split(".")):
        raise ProtocolError("node must contain lowercase DNS labels")
    return value


def literal_ip(value: object) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    if not isinstance(value, str) or "%" in value:
        raise ProtocolError("host_ip must be a literal IP address without a zone identifier")
    try:
        return ipaddress.ip_address(value)
    except ValueError as exc:
        raise ProtocolError("host_ip must be a literal IP address") from exc


def unique_json(body: bytes) -> object:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ProtocolError(f"duplicate JSON field: {key}")
            result[key] = value
        return result

    try:
        return json.loads(body, object_pairs_hook=unique)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ProtocolError("body must be valid JSON with unique fields") from exc


@dataclass(frozen=True, slots=True)
class RenderRequest:
    xmin: float
    ymin: float
    pixel_size: float
    px: int
    py: int
    width: int
    height: int
    iterations: int
    palette: str
    expected_process_id: str

    def query(self) -> dict[str, str]:
        return {field: str(getattr(self, field)) for field in RENDER_FIELDS}


def parse_render_query(query: MultiMapping[str]) -> RenderRequest:
    if set(query) != RENDER_FIELDS:
        raise ProtocolError("render fields must be exactly: " + ", ".join(sorted(RENDER_FIELDS)))
    if any(len(query.getall(key)) != 1 for key in RENDER_FIELDS):
        raise ProtocolError("duplicate render query fields are not allowed")

    def integer(key: str, low: int, high: int) -> int:
        text = query[key]
        if len(text) > 12 or not INTEGER_PATTERN.fullmatch(text):
            raise ProtocolError(f"{key} must be an integer from {low} through {high}")
        value = int(text)
        if not low <= value <= high:
            raise ProtocolError(f"{key} must be from {low} through {high}")
        return value

    def finite(key: str) -> float:
        try:
            value = float(query[key])
        except ValueError as exc:
            raise ProtocolError(f"{key} must be a finite number") from exc
        if not math.isfinite(value):
            raise ProtocolError(f"{key} must be a finite number")
        return value

    xmin, ymin, pixel_size = finite("xmin"), finite("ymin"), finite("pixel_size")
    width, height = integer("width", 1, 256), integer("height", 1, 256)
    px, py = integer("px", 0, 8191), integer("py", 0, 8191)
    iterations = integer("iterations", 1, 10000)
    if px + width > 8192 or py + height > 8192:
        raise ProtocolError("offset plus dimension must not exceed 8192")
    if not 1e-13 <= pixel_size <= 4:
        raise ProtocolError("pixel_size must be from 1e-13 through 4")
    coordinates = (
        xmin + px * pixel_size, xmin + (px + width - 1) * pixel_size,
        ymin + py * pixel_size, ymin + (py + height - 1) * pixel_size,
    )
    if not all(-4 <= point <= 4 for point in coordinates):
        raise ProtocolError("first and last sampled coordinates must be within [-4, 4]")
    palette = query["palette"]
    if palette not in PALETTE_IDS:
        raise ProtocolError("palette must be cyber or fire")
    return RenderRequest(
        xmin, ymin, pixel_size, px, py, width, height, iterations, palette,
        process_id(query["expected_process_id"]),
    )


@dataclass(frozen=True, slots=True)
class Registration:
    node: str
    process_id: str
    host_ip: str


def parse_registration(body: bytes, allowed_networks: tuple) -> Registration:
    if len(body) > REGISTRATION_LIMIT:
        raise ProtocolError("registration body exceeds 8 KiB")
    data = unique_json(body)
    if not isinstance(data, dict) or set(data) != {"node", "process_id", "host_ip"}:
        raise ProtocolError("registration fields must be exactly: node, process_id, host_ip")
    address = literal_ip(data["host_ip"])
    if not any(address in network for network in allowed_networks):
        raise ProtocolError("host_ip is outside the configured node networks")
    return Registration(node_name(data["node"]), process_id(data["process_id"]), str(address))


def error_response(status: int, code: str, message: str, scope: str) -> web.Response:
    return web.json_response(
        {"code": code, "message": message[:400], "scope": scope},
        status=status, headers=NO_STORE,
    )


async def read_limited(stream, limit: int) -> bytes:
    """Read a complete body while enforcing the bound, including chunked responses."""
    result = bytearray()
    async for chunk in stream.iter_chunked(min(limit + 1, 65536)):
        if len(result) + len(chunk) > limit:
            raise ProtocolError("Response exceeds the response size limit")
        result.extend(chunk)
    return bytes(result)


def error_middleware(scope: str):
    @web.middleware
    async def middleware(request, handler):
        try:
            return await handler(request)
        except ProtocolError as exc:
            return error_response(422, "invalid_request", str(exc), scope)
        except web.HTTPException as exc:
            return error_response(exc.status, "http_error", exc.reason, scope)

    return middleware
