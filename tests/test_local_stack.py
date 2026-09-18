import asyncio
import signal
import sys

import pytest

from fractal_demo.gateway import GatewaySettings
from tests.support.local_stack import Child, LocalStack


def assert_stopped(stack):
    assert stack.history
    assert all(child.process.returncode is not None for child in stack.history)
    assert stack.client.closed


def test_reset_workers_and_normal_cleanup(tmp_path):
    async def scenario():
        stack = LocalStack(GatewaySettings(), 2, tmp_path, 5)
        async with stack:
            await stack.reset_workers(2)
            original = list(stack.workers.values())
            assert len(await stack.wait_roster()) == 2
            await stack.reset_workers(0)
            assert await stack.wait_roster() == []
            assert all(child.process.returncode == 0 for child in original)
            new = await stack.start_worker(next(iter(stack.ports)))
            assert new.process_id not in {child.process_id for child in original}
            assert new.process.returncode is None
        assert_stopped(stack)
        assert all(child.log.is_file() for child in stack.history)

    asyncio.run(scenario())


@pytest.mark.parametrize("role", ["gateway", "worker"])
def test_readiness_failure_cleans_up_spawned_processes(tmp_path, monkeypatch, role):
    async def scenario():
        stack = LocalStack(GatewaySettings(), 1, tmp_path, 5)
        wait_health = stack.wait_health
        failure = RuntimeError("Injected readiness failure")

        async def fail_readiness(child):
            health = await wait_health(child)
            if (child.name == "gateway") == (role == "gateway"):
                assert child.process.returncode is None
                raise failure
            return health

        monkeypatch.setattr(stack, "wait_health", fail_readiness)
        with pytest.raises(RuntimeError) as caught:
            async with stack:
                await stack.reset_workers(1)
        assert caught.value is failure
        assert len(stack.history) == (1 if role == "gateway" else 2)
        assert_stopped(stack)

    asyncio.run(scenario())


def test_body_failure_with_already_exited_worker(tmp_path):
    async def scenario():
        stack = LocalStack(GatewaySettings(), 1, tmp_path, 5)
        failure = ValueError("Test body failed")
        with pytest.raises(ValueError) as caught:
            async with stack:
                worker = await stack.start_worker(next(iter(stack.ports)))
                worker.process.terminate()
                assert await asyncio.wait_for(worker.process.wait(), 5) == 0
                raise failure
        assert caught.value is failure
        assert_stopped(stack)

    asyncio.run(scenario())


@pytest.mark.parametrize("body_failure", [False, True])
def test_cleanup_continues_after_one_child_reports_failure(tmp_path, monkeypatch, body_failure):
    async def scenario():
        stack = LocalStack(GatewaySettings(), 1, tmp_path, 5)
        stop_child = stack.stop_child
        failure = ValueError("Test body failed")

        async def fail_worker_cleanup(child):
            await stop_child(child)
            if child.name != "gateway":
                raise RuntimeError("Injected cleanup failure")

        monkeypatch.setattr(stack, "stop_child", fail_worker_cleanup)
        with pytest.raises(ValueError if body_failure else ExceptionGroup) as caught:
            async with stack:
                await stack.reset_workers(1)
                if body_failure:
                    raise failure
        if body_failure:
            assert caught.value is failure
            assert "Injected cleanup failure" in " ".join(failure.__notes__)
        else:
            assert str(caught.value.exceptions[0]) == "Injected cleanup failure"
        assert_stopped(stack)

    asyncio.run(scenario())


def test_shutdown_timeout_kills_unresponsive_child(tmp_path):
    async def scenario():
        stack = LocalStack(GatewaySettings(), 1, tmp_path, 0.1)
        process = None
        try:
            async with stack:
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-c",
                    "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                    "print('ready', flush=True); time.sleep(60)",
                    stdout=asyncio.subprocess.PIPE,
                )
                stack.history.append(Child("ignores-term", process, 0, tmp_path / "child.log"))
                assert await asyncio.wait_for(process.stdout.readline(), 5) == b"ready\n"
            assert process.returncode == -signal.SIGKILL
            assert_stopped(stack)
        finally:
            if process is not None and process.returncode is None:
                process.kill()
                await asyncio.wait_for(process.wait(), 5)

    asyncio.run(scenario())
