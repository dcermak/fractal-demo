import asyncio
import contextlib
import json
import math
import shutil
import socket
import uuid
from io import BytesIO

import pytest
from aiohttp import ClientSession, ClientTimeout, web
from PIL import Image

from fractal_demo import gateway, worker
from fractal_demo.protocol import RESPONSE_LIMIT
from fractal_demo.render import render_tile


def query(process_id, **changes):
    values = {
        "xmin": "0.0",
        "ymin": "0.0",
        "pixel_size": "3.0",
        "px": "0",
        "py": "0",
        "width": "2",
        "height": "1",
        "iterations": "20",
        "palette": "cyber",
        "expected_process_id": process_id,
    }
    values.update({key: str(value) for key, value in changes.items()})
    return values


def worker_app(*, renderer=render_tile, gateway_url="http://127.0.0.1:1", node="node-a"):
    return worker.create_worker_app(
        worker.WorkerSettings(
            node=node,
            host_ip="127.0.0.1",
            gateway_url=gateway_url,
            registration_interval=0.02,
            registration_timeout=0.5,
        ),
        renderer=renderer,
    )


async def register(client, server, process_id, *, node="node-a", host_ip="127.0.0.1"):
    async with client.post(
        server.url + "/register",
        json={
            "node": node,
            "process_id": process_id,
            "host_ip": host_ip,
        },
    ) as response:
        assert response.status == 200, await response.text()
        assert await response.json() == {"process_id": process_id}


async def roster(client, server):
    async with client.get(server.url + "/api/workers") as response:
        assert response.status == 200
        assert response.headers["Cache-Control"] == "no-store"
        return (await response.json())["workers"]


async def error(response, status, code, scope):
    assert response.status == status, await response.text()
    body = await response.json()
    assert set(body) == {"code", "message", "scope"}
    assert body["code"] == code
    assert body["scope"] == scope
    assert 0 < len(body["message"]) <= 400
    assert response.headers["Cache-Control"] == "no-store"


async def assert_png(response, process_id, node="node-a"):
    assert response.status == 200, await response.text()
    assert response.content_type == "image/png"
    assert response.headers["X-Worker-ID"] == process_id
    assert response.headers["X-Render-Node"] == node
    assert response.headers["Cache-Control"] == "no-store"
    with Image.open(BytesIO(await response.read())) as image:
        image.load()
        assert image.mode == "RGB"
        assert image.size == (2, 1)
        assert image.getpixel((0, 0)) == (10, 13, 20)  # c=0 never escapes.
        # c=3 escapes at n=1, z=3. Independently interpolate the first palette segment.
        weight = (2 - math.log2(math.log(3))) / 8
        expected = tuple(
            math.floor(a + weight * (b - a))
            for a, b in zip((18, 25, 65), (33, 205, 225), strict=True)
        )
        assert image.getpixel((1, 0)) == expected


def test_periodic_registration_roster_and_real_proxy_pixels(tcp_app, wait_for):
    async def scenario():
        ports = {}
        app = gateway.create_gateway_app(gateway.GatewaySettings(local_worker_ports=ports))
        async with tcp_app(app) as front, ClientSession() as client:
            backend = worker_app(gateway_url=front.url)
            async with tcp_app(backend, ready=False) as back:
                process_id = backend[worker.STATE].process_id
                assert await roster(client, front) == []
                ports["node-a"] = back.port
                backend[worker.STATE].listener_ready.set()
                await wait_for(lambda: app[gateway.STATE].registry.lookup(process_id) is not None)
                assert await roster(client, front) == [
                    {
                        "node": "node-a",
                        "process_id": process_id,
                        "render_url": f"/api/render/{process_id}",
                    }
                ]
                async with client.get(
                    front.url + f"/api/render/{process_id}", params=query(process_id)
                ) as response:
                    await assert_png(response, process_id)
                async with client.get(front.url + "/api/config") as response:
                    assert response.status == 200
                    assert response.headers["Cache-Control"] == "no-store"
                    assert (await response.json())["geometry"]["width"] > 0

    asyncio.run(scenario())


def test_two_local_workers_use_their_configured_ports(tcp_app, tmp_path):
    async def scenario():
        first = worker_app(node="worker-a")
        second = worker_app(node="worker-b")
        async with tcp_app(first, ready=False) as a, tcp_app(second, ready=False) as b:
            config = tmp_path / "gateway.toml"
            # A fallback to worker_port for worker-b would silently select worker-a's endpoint.
            config.write_text(
                f"worker_port = {a.port}\n[local_worker_ports]\n"
                f"worker-a = {a.port}\nworker-b = {b.port}\n"
            )
            app = gateway.create_gateway_app(gateway.load_settings(str(config)))
            async with tcp_app(app) as front, ClientSession() as client:
                identities = {
                    "worker-a": first[worker.STATE].process_id,
                    "worker-b": second[worker.STATE].process_id,
                }
                for node, process_id in identities.items():
                    await register(client, front, process_id, node=node)
                rows = await roster(client, front)
                assert {row["node"]: row["process_id"] for row in rows} == identities

                async def render_worker(row):
                    async with client.get(
                        front.url + row["render_url"], params=query(row["process_id"])
                    ) as response:
                        await assert_png(response, row["process_id"], row["node"])

                await asyncio.gather(*(render_worker(row) for row in rows))
                assert app[gateway.STATE].active == 0

    asyncio.run(scenario())


def test_local_mapping_to_gateway_is_rejected_and_logged(tcp_app, wait_for, caplog):
    async def scenario():
        ports = {}
        unexpected_renders = []
        app = gateway.create_gateway_app(gateway.GatewaySettings(local_worker_ports=ports))

        @web.middleware
        async def observe_render(request, handler):
            if request.path == "/render":
                unexpected_renders.append(request.path)
            return await handler(request)

        app.middlewares.append(observe_render)
        async with tcp_app(app) as front, ClientSession() as client:
            ports["node-a"] = front.port
            backend = worker_app(gateway_url=front.url)
            async with tcp_app(backend):
                process_id = backend[worker.STATE].process_id
                await wait_for(lambda: "routes to the gateway" in caplog.text)
                assert "local_worker_ports" in caplog.text
                assert await roster(client, front) == []
                async with client.post(
                    front.url + "/register",
                    json={
                        "node": "node-a",
                        "process_id": process_id,
                        "host_ip": "127.0.0.1",
                    },
                ) as response:
                    await error(response, 422, "invalid_request", "gateway")
                async with client.get(
                    front.url + f"/api/render/{process_id}", params=query(process_id)
                ) as response:
                    await error(response, 404, "unknown_worker", "gateway")
                assert unexpected_renders == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "duplicate",
        "unknown",
        {"xmin": "nan"},
        {"ymin": "inf"},
        {"pixel_size": "-inf"},
        {"width": 0},
        {"height": 257},
        {"px": -1},
        {"px": 8191, "width": 2},
        {"iterations": 10001},
        {"iterations": "1.5"},
        {"pixel_size": "1e-14"},
        {"pixel_size": 5},
        {"xmin": -5},
        {"xmin": 3},
        {"palette": "unknown"},
        {"expected_process_id": "not-a-uuid"},
    ],
)
def test_invalid_render_never_computes_on_worker_or_gateway(tcp_app, change):
    async def scenario():
        calls = []

        def counting_renderer(tile):
            calls.append(tile)
            return render_tile(tile)

        backend = worker_app(renderer=counting_renderer)
        process_id = backend[worker.STATE].process_id
        async with tcp_app(backend, ready=False) as back:
            app = gateway.create_gateway_app(gateway.GatewaySettings(worker_port=back.port))
            async with tcp_app(app) as front, ClientSession() as client:
                await register(client, front, process_id)
                params = query(process_id)
                if change == "missing":
                    del params["width"]
                elif change == "duplicate":
                    params = [*params.items(), ("width", "2")]
                elif change == "unknown":
                    params["extra"] = "1"
                else:
                    params.update({key: str(value) for key, value in change.items()})
                for url, scope in (
                    (back.url + "/render", "worker"),
                    (front.url + f"/api/render/{process_id}", "gateway"),
                ):
                    async with client.get(url, params=params) as response:
                        await error(response, 422, "invalid_request", scope)
                assert calls == []
                assert backend[worker.STATE].future is None
                assert app[gateway.STATE].active == 0

    asyncio.run(scenario())


def test_head_and_wrong_identity_do_not_take_compute_slot(tcp_app):
    async def scenario():
        calls = []

        def counting_renderer(tile):
            calls.append(tile)
            return render_tile(tile)

        backend = worker_app(renderer=counting_renderer)
        process_id = backend[worker.STATE].process_id
        async with tcp_app(backend, ready=False) as back:
            app = gateway.create_gateway_app(gateway.GatewaySettings(worker_port=back.port))
            async with tcp_app(app) as front, ClientSession() as client:
                await register(client, front, process_id)
                for url, scope in (
                    (back.url + "/render", "worker"),
                    (front.url + f"/api/render/{process_id}", "gateway"),
                ):
                    async with client.head(url, params=query(process_id)) as response:
                        assert response.status == 405
                    async with client.get(url, params=query(str(uuid.uuid4()))) as response:
                        await error(response, 409, "incarnation_mismatch", scope)
                assert calls == []
                assert backend[worker.STATE].future is None
                async with client.get(back.url + "/render", params=query(process_id)) as response:
                    await assert_png(response, process_id)
                assert len(calls) == 1

    asyncio.run(scenario())


def test_disconnect_retains_permit_while_health_and_registration_continue(
    tcp_app,
    held_renderer,
    wait_for,
):
    async def scenario():
        ports = {}
        app = gateway.create_gateway_app(gateway.GatewaySettings(local_worker_ports=ports))
        async with tcp_app(app) as front, ClientSession() as client:
            backend = worker_app(renderer=held_renderer, gateway_url=front.url)
            cancelled = asyncio.Event()

            @web.middleware
            async def observe_cancellation(request, handler):
                try:
                    return await handler(request)
                except asyncio.CancelledError:
                    if request.path == "/render":
                        cancelled.set()
                    raise

            backend.middlewares.append(observe_cancellation)
            async with tcp_app(backend, ready=False) as back:
                ports["node-a"] = back.port
                state = backend[worker.STATE]
                state.listener_ready.set()
                pending = asyncio.create_task(
                    client.get(back.url + "/render", params=query(state.process_id))
                )
                try:
                    await wait_for(held_renderer.entered.is_set)
                    await wait_for(lambda: app[gateway.STATE].registry.lookup(state.process_id))
                    received = app[gateway.STATE].registry.lookup(state.process_id).last_seen
                    pending.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await pending
                    async with asyncio.timeout(2):
                        await cancelled.wait()
                    assert state.future is not None and not state.future.done()
                    for expected in (str(uuid.uuid4()), state.process_id):
                        async with client.get(
                            back.url + "/render", params=query(expected)
                        ) as response:
                            if expected == state.process_id:
                                await error(response, 503, "busy", "worker")
                            else:
                                await error(response, 409, "incarnation_mismatch", "worker")
                    async with client.get(back.url + "/healthz") as response:
                        assert response.status == 200
                        assert (await response.json())["process_id"] == state.process_id
                    await wait_for(
                        lambda: (
                            app[gateway.STATE].registry.lookup(state.process_id).last_seen
                            > received
                        )
                    )
                    assert held_renderer.calls == 1
                    assert held_renderer.finished == 0
                finally:
                    held_renderer.release.set()
                    pending.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await pending
                await wait_for(lambda: state.future is None)
                async with client.get(
                    back.url + "/render", params=query(state.process_id)
                ) as response:
                    await assert_png(response, state.process_id)
                assert held_renderer.calls == held_renderer.finished == 2

    asyncio.run(scenario())


def test_gateway_and_worker_busy_scopes_with_independent_control_requests(
    tcp_app,
    held_renderer,
    wait_for,
):
    async def scenario():
        backend = worker_app(renderer=held_renderer)
        process_id = backend[worker.STATE].process_id
        async with tcp_app(backend, ready=False) as back:
            app = gateway.create_gateway_app(
                gateway.GatewaySettings(
                    worker_port=back.port,
                    proxy_limit=1,
                )
            )
            async with tcp_app(app) as front, ClientSession() as client:
                await register(client, front, process_id)
                url = front.url + f"/api/render/{process_id}"
                pending = asyncio.create_task(client.get(url, params=query(process_id)))
                try:
                    await wait_for(held_renderer.entered.is_set)
                    assert app[gateway.STATE].active == 1
                    async with client.get(url, params=query(process_id)) as response:
                        await error(response, 503, "busy", "gateway")
                    async with asyncio.timeout(2):
                        await register(client, front, process_id)
                        assert len(await roster(client, front)) == 1
                        for path in ("/healthz", "/api/config"):
                            async with client.get(front.url + path) as response:
                                assert response.status == 200
                                await response.read()
                    # Disconnect the proxy handler; the worker still owns its future.
                    pending.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await pending
                    await wait_for(lambda: app[gateway.STATE].active == 0)
                    async with client.get(url, params=query(process_id)) as response:
                        await error(response, 503, "busy", "worker")
                    assert held_renderer.calls == 1
                    assert app[gateway.STATE].active == 0
                finally:
                    held_renderer.release.set()
                    pending.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await pending
                await wait_for(lambda: backend[worker.STATE].future is None)
                async with client.get(url, params=query(process_id)) as response:
                    await assert_png(response, process_id)
                assert held_renderer.calls == 2

    asyncio.run(scenario())


def test_registry_exact_expiry_capacity_refresh_and_same_process_return(tcp_app):
    async def scenario():
        now = [0.0]
        registry = gateway.Registry(10, clock=lambda: now[0], capacity=2)
        app = gateway.create_gateway_app(gateway.GatewaySettings(), registry=registry)
        first, second, third = (str(uuid.uuid4()) for _ in range(3))
        async with tcp_app(app) as front, ClientSession() as client:
            await register(client, front, first)
            await register(client, front, second, node="node-b")
            now[0] = 1
            await register(client, front, second, node="node-b")  # Refresh at capacity.
            async with client.post(
                front.url + "/register",
                json={
                    "node": "node-c",
                    "process_id": third,
                    "host_ip": "127.0.0.1",
                },
            ) as response:
                await error(response, 503, "busy", "gateway")
            now[0] = math.nextafter(10, 0)
            assert len(await roster(client, front)) == 2
            now[0] = 10
            # Admission itself must expire node-a before checking capacity.
            await register(client, front, third, node="node-c")
            assert {row["process_id"] for row in await roster(client, front)} == {second, third}
            async with client.get(
                front.url + f"/api/render/{first}", params=query(first)
            ) as response:
                await error(response, 404, "unknown_worker", "gateway")
            now[0] = 11
            # Lookup itself must expire node-b at the exact boundary.
            async with client.get(
                front.url + f"/api/render/{second}", params=query(second)
            ) as response:
                await error(response, 404, "unknown_worker", "gateway")
            await register(client, front, first)
            assert {row["process_id"] for row in await roster(client, front)} == {first, third}
            now[0] = 20
            assert [row["process_id"] for row in await roster(client, front)] == [first]

    asyncio.run(scenario())


def test_replacement_at_same_tcp_address_rejects_stale_selection(tcp_app):
    async def scenario():
        first_app = worker_app()
        old_id = first_app[worker.STATE].process_id
        async with tcp_app(first_app, ready=False) as first:
            port = first.port
        calls = []

        def counting_renderer(tile):
            calls.append(tile)
            return render_tile(tile)

        replacement = worker_app(renderer=counting_renderer)
        new_id = replacement[worker.STATE].process_id
        assert new_id != old_id
        app = gateway.create_gateway_app(gateway.GatewaySettings(worker_port=port))
        async with tcp_app(replacement, port=port, ready=False), tcp_app(app) as front:
            async with ClientSession() as client:
                await register(client, front, old_id)
                async with client.get(
                    front.url + f"/api/render/{old_id}", params=query(old_id)
                ) as response:
                    await error(response, 409, "incarnation_mismatch", "worker")
                assert calls == []
                await register(client, front, new_id)
                assert [row["process_id"] for row in await roster(client, front)] == [new_id]
                async with client.get(
                    front.url + f"/api/render/{old_id}", params=query(old_id)
                ) as response:
                    await error(response, 404, "unknown_worker", "gateway")
                async with client.get(
                    front.url + f"/api/render/{new_id}", params=query(new_id)
                ) as response:
                    await assert_png(response, new_id)
                assert len(calls) == 1

    asyncio.run(scenario())


def test_registration_interruption_expires_and_same_live_worker_returns(tcp_app, wait_for):
    async def scenario():
        now = [0.0]
        accepting = [True]
        rejected = []
        ports = {}
        registry = gateway.Registry(10, clock=lambda: now[0])
        app = gateway.create_gateway_app(
            gateway.GatewaySettings(local_worker_ports=ports),
            registry=registry,
        )

        @web.middleware
        async def interrupt_registration(request, handler):
            if request.path == "/register" and not accepting[0]:
                rejected.append(await request.json())
                return web.json_response({"error": "test registration interruption"}, status=503)
            return await handler(request)

        app.middlewares.append(interrupt_registration)
        async with tcp_app(app) as front, ClientSession() as client:
            backend = worker_app(gateway_url=front.url)
            async with tcp_app(backend, ready=False) as back:
                process_id = backend[worker.STATE].process_id
                ports["node-a"] = back.port
                backend[worker.STATE].listener_ready.set()
                await wait_for(lambda: registry.lookup(process_id))
                accepting[0] = False
                await wait_for(lambda: rejected)
                now[0] = 10
                assert await roster(client, front) == []
                async with client.get(back.url + "/healthz") as response:
                    assert (await response.json())["process_id"] == process_id
                async with client.get(back.url + "/render", params=query(process_id)) as response:
                    await assert_png(response, process_id)
                accepting[0] = True
                await wait_for(lambda: registry.lookup(process_id))
                assert [row["process_id"] for row in await roster(client, front)] == [process_id]
                assert {item["process_id"] for item in rejected} == {process_id}
                async with client.get(
                    front.url + f"/api/render/{process_id}", params=query(process_id)
                ) as response:
                    await assert_png(response, process_id)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "fault",
    [
        "malformed-png",
        "redirect",
        "identity",
        "node",
        "encoding",
        "content-type",
        "oversize",
        "truncated",
        "stall",
    ],
)
def test_bounded_proxy_against_actual_faulty_upstream(tcp_app, fault):
    async def scenario():
        process_id = str(uuid.uuid4())
        release = asyncio.Event()
        requests = []
        redirects = []

        async def redirected(request):
            redirects.append(request.path)
            return web.Response(body=b"unexpected")

        async def upstream(request):
            requests.append(dict(request.query))
            assert request.headers["Accept-Encoding"] == "identity"
            headers = {"X-Worker-ID": process_id, "X-Render-Node": "node-a"}
            if fault == "redirect":
                raise web.HTTPFound("/redirected")
            if fault in ("stall", "truncated"):
                response = web.StreamResponse(
                    headers={
                        **headers,
                        "Content-Type": "image/png",
                        "Content-Length": "100",
                    }
                )
                await response.prepare(request)
                await response.write(b"partial")
                if fault == "truncated":
                    request.transport.close()
                else:
                    await release.wait()
                return response
            if fault == "identity":
                headers["X-Worker-ID"] = str(uuid.uuid4())
            if fault == "node":
                headers["X-Render-Node"] = "wrong-node"
            if fault == "encoding":
                headers["Content-Encoding"] = "gzip"
            body = b"not a PNG"
            if fault == "oversize":
                # Chunked transfer checks the streaming limit without a Content-Length shortcut.
                response = web.StreamResponse(headers={**headers, "Content-Type": "image/png"})
                await response.prepare(request)
                for _ in range(RESPONSE_LIMIT // 65536 + 1):
                    await response.write(b"x" * 65536)
                await response.write_eof()
                return response
            return web.Response(
                body=body,
                headers=headers,
                content_type="text/plain" if fault == "content-type" else "image/png",
            )

        upstream_app = web.Application()
        upstream_app.router.add_get("/render", upstream)
        upstream_app.router.add_get("/redirected", redirected)
        async with tcp_app(upstream_app) as back:
            app = gateway.create_gateway_app(
                gateway.GatewaySettings(
                    worker_port=back.port,
                    connect_timeout=0.1,
                    upstream_timeout=0.25,
                )
            )
            async with (
                tcp_app(app) as front,
                ClientSession(timeout=ClientTimeout(total=3)) as client,
            ):
                await register(client, front, process_id)
                try:
                    async with client.get(
                        front.url + f"/api/render/{process_id}", params=query(process_id)
                    ) as response:
                        if fault == "malformed-png":
                            assert response.status == 200
                            assert await response.read() == b"not a PNG"
                            assert response.headers["X-Worker-ID"] == process_id
                            assert response.content_type == "image/png"
                        elif fault == "stall":
                            await error(response, 504, "upstream_timeout", "gateway")
                        elif fault == "truncated":
                            await error(response, 502, "upstream_failure", "gateway")
                        else:
                            await error(response, 502, "upstream_protocol", "gateway")
                    assert len(requests) == 1
                    assert requests[0] == query(process_id)
                    assert redirects == []
                    assert app[gateway.STATE].active == 0
                    assert len(await roster(client, front)) == 1
                finally:
                    release.set()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "host_ip,networks,override",
    [
        ("127.0.0.1", ("192.0.2.0/24",), False),
        ("192.0.2.1", ("192.0.2.0/24",), True),
        ("localhost", ("127.0.0.0/8",), False),
        ("http://127.0.0.1/render", ("127.0.0.0/8",), False),
    ],
)
def test_forbidden_registration_never_connects(tcp_app, host_ip, networks, override):
    async def scenario():
        connections = []

        def connected(reader, writer):
            connections.append(writer.get_extra_info("peername"))
            writer.close()

        listener = await asyncio.start_server(connected, "127.0.0.1", 0)
        async with listener:
            port = listener.sockets[0].getsockname()[1]
            app = gateway.create_gateway_app(
                gateway.GatewaySettings(
                    worker_port=port,
                    node_networks=networks,
                    local_worker_ports={"node-a": port} if override else {},
                )
            )
            async with tcp_app(app) as front, ClientSession() as client:
                process_id = str(uuid.uuid4())
                async with client.post(
                    front.url + "/register",
                    json={
                        "node": "node-a",
                        "process_id": process_id,
                        "host_ip": host_ip,
                    },
                ) as response:
                    await error(response, 422, "invalid_request", "gateway")
                assert await roster(client, front) == []
                async with client.get(
                    front.url + f"/api/render/{process_id}", params=query(process_id)
                ) as response:
                    await error(response, 404, "unknown_worker", "gateway")
                assert connections == []
                assert app[gateway.STATE].active == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("fault", ["missing", "duplicate", "unknown", "oversize", "node", "uuid"])
def test_registration_validation_leaves_registry_empty(tcp_app, fault):
    async def scenario():
        payload = {"node": "node-a", "process_id": str(uuid.uuid4()), "host_ip": "127.0.0.1"}
        if fault == "missing":
            del payload["node"]
        elif fault == "unknown":
            payload["port"] = 1234
        elif fault == "node":
            payload["node"] = "INVALID/name"
        elif fault == "uuid":
            payload["process_id"] = "invalid"
        body = json.dumps(payload)
        if fault == "duplicate":
            body = body[:-1] + ', "node": "node-b"}'
        if fault == "oversize":
            body += " " * 8192
        async with tcp_app(gateway.create_gateway_app(gateway.GatewaySettings())) as front:
            async with ClientSession() as client:
                async with client.post(
                    front.url + "/register", data=body, headers={"Content-Type": "application/json"}
                ) as response:
                    if fault == "oversize":
                        await error(response, 413, "http_error", "gateway")
                    else:
                        await error(response, 422, "invalid_request", "gateway")
                assert await roster(client, front) == []

    asyncio.run(scenario())


def test_worker_entrypoint_restart_changes_uuid_and_registers_again(tcp_app, tmp_path):
    async def scenario():
        executable = shutil.which("fractal-worker")
        assert executable is not None
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        app = gateway.create_gateway_app(gateway.GatewaySettings(worker_port=port))
        identities = []
        async with tcp_app(app) as front, ClientSession(timeout=ClientTimeout(total=1)) as client:
            for run in range(2):
                with (tmp_path / f"worker-{run}.log").open("wb") as log:
                    process = await asyncio.create_subprocess_exec(
                        executable,
                        "--node",
                        "node-a",
                        "--host-ip",
                        "127.0.0.1",
                        "--bind",
                        "127.0.0.1",
                        "--port",
                        str(port),
                        "--gateway-url",
                        front.url,
                        "--registration-interval",
                        "0.05",
                        stdout=log,
                        stderr=log,
                    )
                    try:
                        async with asyncio.timeout(8):
                            while True:
                                assert process.returncode is None
                                rows = await roster(client, front)
                                if rows and rows[0]["process_id"] not in identities:
                                    break
                                await asyncio.sleep(0.02)
                        process_id = rows[0]["process_id"]
                        assert str(uuid.UUID(process_id)) == process_id
                        identities.append(process_id)
                        async with client.get(
                            front.url + rows[0]["render_url"], params=query(process_id)
                        ) as response:
                            await assert_png(response, process_id)
                    finally:
                        if process.returncode is None:
                            process.terminate()
                        try:
                            await asyncio.wait_for(process.wait(), 5)
                        except TimeoutError:
                            process.kill()
                            await process.wait()
                            raise
                    assert process.returncode == 0
            assert len(set(identities)) == 2

    asyncio.run(scenario())


def test_shutdown_drains_admitted_work_before_executor_closes(tcp_app, held_renderer, wait_for):
    async def scenario():
        backend = worker_app(renderer=held_renderer)
        state = backend[worker.STATE]
        async with tcp_app(backend, ready=False) as back, ClientSession() as client:
            pending = asyncio.create_task(
                client.get(
                    back.url + "/render",
                    params=query(state.process_id),
                )
            )
            stopping = None
            try:
                await wait_for(held_renderer.entered.is_set)
                stopping = asyncio.create_task(back.runner.cleanup())
                await wait_for(lambda: not state.admitting)
                await wait_for(lambda: state.registration_task.done())
                assert not stopping.done()
                assert state.future is not None
                assert held_renderer.finished == 0
                held_renderer.release.set()
                async with await pending as response:
                    await assert_png(response, state.process_id)
                await asyncio.wait_for(stopping, 3)
                assert state.future is None
                assert held_renderer.calls == held_renderer.finished == 1
            finally:
                held_renderer.release.set()
                pending.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pending
                if stopping is not None:
                    await stopping

    asyncio.run(scenario())


def test_compute_exception_returns_capacity_and_preserves_worker_error(tcp_app):
    async def scenario():
        calls = 0

        def fails_once(tile):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ArithmeticError("test computation failure")
            return render_tile(tile)

        backend = worker_app(renderer=fails_once)
        process_id = backend[worker.STATE].process_id
        async with tcp_app(backend, ready=False) as back:
            app = gateway.create_gateway_app(gateway.GatewaySettings(worker_port=back.port))
            async with tcp_app(app) as front, ClientSession() as client:
                await register(client, front, process_id)
                url = front.url + f"/api/render/{process_id}"
                async with client.get(url, params=query(process_id)) as response:
                    await error(response, 500, "render_failed", "worker")
                async with client.get(url, params=query(process_id)) as response:
                    await assert_png(response, process_id)
                assert calls == 2
                assert backend[worker.STATE].future is None
                assert app[gateway.STATE].active == 0

    asyncio.run(scenario())
