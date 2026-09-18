"""Run test-owned gateway and worker subprocesses on loopback."""

import asyncio
import contextlib
import dataclasses
import json
import socket
import sys
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout


def reserve_port_numbers(count):
    # Select distinct ports together. Other processes can claim them after release.
    with contextlib.ExitStack() as stack:
        sockets = [stack.enter_context(socket.socket()) for _ in range(count)]
        for listener in sockets:
            listener.bind(("127.0.0.1", 0))
        return [listener.getsockname()[1] for listener in sockets]


def write_gateway_config(path, settings):
    values = dataclasses.asdict(settings)
    lines = [
        f"{key} = {json.dumps(value)}"
        for key, value in values.items()
        if not isinstance(value, dict)
    ]
    for key, table in values.items():
        if isinstance(table, dict):
            lines.extend(["", f"[{key}]"])
            lines.extend(
                f"{json.dumps(name)} = {json.dumps(value)}" for name, value in table.items()
            )
    path.write_text("\n".join(lines) + "\n")


@dataclasses.dataclass
class Child:
    name: str
    process: asyncio.subprocess.Process
    port: int
    log: Path
    process_id: str | None = None


class LocalStack:
    def __init__(self, settings, maximum_workers, output, shutdown_timeout):
        ports = reserve_port_numbers(maximum_workers + 1)
        self.ports = {f"test-worker-{index + 1}": port for index, port in enumerate(ports[1:])}
        self.settings = dataclasses.replace(
            settings,
            bind=("127.0.0.1",),
            port=ports[0],
            node_networks=("127.0.0.0/8",),
            worker_port=ports[1],
            local_worker_ports=self.ports,
        )
        self.settings.validate()
        self.output = output
        self.config_path = output / "gateway.toml"
        write_gateway_config(self.config_path, self.settings)
        self.origin = f"http://127.0.0.1:{ports[0]}"
        self.workers: dict[str, Child] = {}
        self.gateway: Child | None = None
        self.history: list[Child] = []
        self.shutdown_timeout = shutdown_timeout
        self.client = None

    async def spawn(self, role, name, port, arguments):
        executable = Path(sys.executable).parent / f"fractal-{role}"
        if not executable.is_file():
            raise RuntimeError(f"Missing {executable}; run uv sync --frozen first")
        log = self.output / f"{name}-{len(self.history) + 1}.log"
        with log.open("w") as stream:
            process = await asyncio.create_subprocess_exec(
                str(executable),
                *arguments,
                stdout=stream,
                stderr=asyncio.subprocess.STDOUT,
            )
        child = Child(name, process, port, log)
        self.history.append(child)
        return child

    async def wait_health(self, child):
        try:
            async with asyncio.timeout(15):
                while True:
                    if child.process.returncode is not None:
                        raise RuntimeError(f"{child.name} exited: {child.log.read_text()[-4000:]}")
                    try:
                        url = f"http://127.0.0.1:{child.port}/healthz"
                        async with self.client.get(url) as response:
                            data = await response.json()
                            if response.status == 200:
                                return data
                    except OSError, TimeoutError:
                        pass
                    await asyncio.sleep(0.05)
        except TimeoutError as error:
            error.add_note(f"Waiting for {child.name}; see {child.log}")
            raise

    async def start_gateway(self):
        self.gateway = await self.spawn(
            "gateway",
            "gateway",
            self.settings.port,
            ["--config", str(self.config_path)],
        )
        await self.wait_health(self.gateway)

    async def start_worker(self, node):
        if node not in self.ports or node in self.workers:
            raise ValueError(f"Worker is not an available owned target: {node}")
        child = await self.spawn(
            "worker",
            node,
            self.ports[node],
            [
                "--node",
                node,
                "--host-ip",
                "127.0.0.1",
                "--bind",
                "127.0.0.1",
                "--port",
                str(self.ports[node]),
                "--gateway-url",
                self.origin,
            ],
        )
        self.workers[node] = child
        child.process_id = (await self.wait_health(child))["process_id"]
        await self.wait_roster()
        return child

    async def wait_roster(self):
        async with asyncio.timeout(15):
            while True:
                for child in self.workers.values():
                    if child.process.returncode is not None:
                        raise RuntimeError(f"Owned worker exited: {child.name}; see {child.log}")
                async with self.client.get(self.origin + "/api/workers") as response:
                    rows = (await response.json())["workers"]
                if {row["node"]: row["process_id"] for row in rows} == {
                    node: child.process_id for node, child in self.workers.items()
                }:
                    return rows
                await asyncio.sleep(0.1)

    async def stop_child(self, child):
        if child.process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            child.process.terminate()
        try:
            await asyncio.wait_for(child.process.wait(), self.shutdown_timeout)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                child.process.kill()
            await asyncio.wait_for(child.process.wait(), 5)

    async def reset_workers(self, count):
        for child in list(self.workers.values()):
            await self.stop_child(child)
        self.workers.clear()
        for node in list(self.ports)[:count]:
            await self.start_worker(node)

    async def __aenter__(self):
        self.client = ClientSession(timeout=ClientTimeout(total=3), trust_env=False)
        try:
            await self.start_gateway()
        except BaseException as error:
            await self.__aexit__(type(error), error, error.__traceback__)
            raise
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        errors = []
        # History includes children whose startup failed before joining the active map.
        for child in reversed(self.history):
            try:
                await self.stop_child(child)
            except Exception as error:
                error.add_note(
                    f"Cleaning up {child.name} (PID {child.process.pid}); see {child.log}"
                )
                errors.append(error)
        try:
            await self.client.close()
        except Exception as error:
            errors.append(error)
        if errors:
            if exc is not None:
                for error in errors:
                    exc.add_note(f"Cleanup failed: {error!r}; {getattr(error, '__notes__', [])}")
            else:
                raise ExceptionGroup("Local stack cleanup failed", errors)
