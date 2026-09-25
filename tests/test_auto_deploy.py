"""Gateway integration tests; only the external Helm command boundary is substituted."""

import asyncio
import json
import os
import sys
from html.parser import HTMLParser
from pathlib import Path

import pytest
from aiohttp import ClientSession

from fractal_demo import deployment, gateway


class Elements(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.tags = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags[tag] = dict(attrs)


class Helm:
    def __init__(self):
        self.calls = []
        self.releases = {}
        self.list_result = None
        self.active = 0
        self.maximum_active = 0
        self.install_entered = asyncio.Event()
        self.finish_install = asyncio.Event()
        self.finish_install.set()

    @property
    def checks(self):
        return [args for args in self.calls if args[1] == "list"]

    @property
    def installs(self):
        return [args for args in self.calls if args[1] == "upgrade"]

    async def __call__(self, args, *, timeout):  # noqa: ASYNC109 - external runner's interface
        self.calls.append(args)
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        try:
            # Model the external command reading the supplied file on every invocation.
            path = Path(args[args.index("--kubeconfig") + 1])
            generation = path.read_text()  # noqa: ASYNC240 - tiny external-command fixture
            if generation == "invalid":
                raise deployment.HelmError("Invalid kubeconfig")
            if args[1] == "list":
                if isinstance(self.list_result, Exception):
                    raise self.list_result
                if self.list_result is not None:
                    return self.list_result
                status = self.releases.get(generation)
                return json.dumps(
                    [{"name": "fractal-demo", "namespace": "fractal-demo", "status": status}]
                    if status
                    else []
                )
            assert args[1:4] == ["upgrade", "--install", "fractal-demo"]
            self.install_entered.set()
            await self.finish_install.wait()
            self.releases[generation] = "deployed"
            return "Release installed"
        finally:
            self.active -= 1


@pytest.fixture
def booth(tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    chart = root / "deploy/helm/fractal-demo"
    chart.mkdir(parents=True)
    (chart / "Chart.yaml").write_text("apiVersion: v2\nname: fractal-demo\nversion: 0.1.0\n")
    values = root / "deploy/values.local.json"
    values.write_text('{"gatewayUrl": "http://192.168.122.1:8080"}')
    config = root / "settings/gateway.toml"
    config.parent.mkdir()
    config.write_text("poll_interval_seconds = 0.02\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    # The executable lookup belongs to the external Helm boundary too. No Helm installation needed.
    monkeypatch.setattr(deployment.shutil, "which", lambda name: "/mock/helm")
    return config, tmp_path / "kubeconfig", chart, values


def application(booth, **paths):
    config, kubeconfig, _, _ = booth
    return gateway.create_gateway_app(
        gateway.load_settings(config), kubeconfig=kubeconfig, config_path=config, **paths
    )


async def panel(client, front, *, post=False):
    method = client.post if post else client.get
    async with method(front.url + ("/redeploy" if post else "/deployment")) as response:
        assert response.status == 200, await response.text()
        assert response.content_type == "text/html"
        assert response.headers["Cache-Control"] == "no-store"
        return await response.text()


@pytest.mark.parametrize("explicit_paths", [False, True])
def test_automatic_install_and_replacement(
    booth, tmp_path, monkeypatch, tcp_app, wait_for, explicit_paths
):
    async def scenario():
        helm = Helm()
        monkeypatch.setattr(deployment, "run_command", helm)
        _, kubeconfig, chart, values = booth
        paths = {}
        if explicit_paths:
            chart = chart.rename(tmp_path / "packaged-chart")
            values = values.rename(tmp_path / "mounted-values.json")
            paths = {"chart_path": chart, "values_path": values}
        app = application(booth, **paths)
        async with tcp_app(app) as front, ClientSession() as client:
            assert "Waiting for cluster configuration" in await panel(client, front)
            assert helm.calls == []
            kubeconfig.write_text("invalid")
            await wait_for(lambda: len(helm.checks) >= 2)
            assert helm.installs == []
            # Switch a symlink twice. Startup must not pin the old target.
            first, second = kubeconfig.with_name("first"), kubeconfig.with_name("second")
            first.write_text("cluster-one")
            second.write_text("cluster-two")
            kubeconfig.unlink()
            kubeconfig.symlink_to(first)
            await wait_for(lambda: helm.releases.get("cluster-one") == "deployed")
            # No browser request caused the installation.
            baseline = len(helm.checks)
            await wait_for(lambda: len(helm.checks) >= baseline + 2)
            assert len(helm.installs) == 1
            command = helm.installs[0]
            assert command[4] == str(chart)
            assert command[command.index("--values") + 1] == str(values)
            assert command[command.index("--kubeconfig") + 1] == str(kubeconfig)
            assert command[command.index("--namespace") + 1] == "fractal-demo"
            assert "--create-namespace" in command
            assert "--wait" not in command
            assert {"--deployed", "--failed", "--pending", "--uninstalling"} <= set(helm.checks[0])
            assert helm.checks[0][helm.checks[0].index("--filter") + 1] == "^fractal-demo$"
            html = await panel(client, front)
            elements = Elements(html).tags
            assert "Renderer deployment installed." in html
            assert elements["div"]["hx-get"] == "/deployment"
            assert elements["div"]["hx-trigger"] == "every 0.02s"
            assert elements["div"]["hx-target"] == "#deployment"
            assert elements["button"]["hx-post"] == "/redeploy"
            assert "disabled" not in elements["button"]
            assert "fractal-canvas" not in html
            kubeconfig.unlink()
            await wait_for(
                lambda: (
                    app[gateway.STATE].deployment.message == "Waiting for cluster configuration."
                )
            )
            assert "Waiting for cluster configuration." in await panel(client, front)
            replacement = kubeconfig.with_name("replacement")
            replacement.symlink_to(second)
            replacement.replace(kubeconfig)
            await wait_for(lambda: helm.releases.get("cluster-two") == "deployed")
            baseline = len(helm.checks)
            await wait_for(lambda: len(helm.checks) >= baseline + 2)
            assert len(helm.installs) == 2
            assert helm.maximum_active == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "args, message",
    [
        (["--helm-chart", "/chart", "--helm-values", "/values"], "require --kubeconfig"),
        (["--kubeconfig", "/kubeconfig", "--helm-chart", "/chart"], "supplied together"),
        (["--kubeconfig", "/kubeconfig", "--helm-values", "/values"], "supplied together"),
    ],
)
def test_cli_rejects_incomplete_deployment_options(monkeypatch, capsys, args, message):
    monkeypatch.setattr(sys, "argv", ["fractal-gateway", *args])
    with pytest.raises(SystemExit) as error:
        gateway.main()
    assert error.value.code == 2
    assert message in capsys.readouterr().err


@pytest.mark.parametrize(
    "result",
    [
        deployment.HelmError("API unavailable"),
        "not json",
        "{}",
        '[{"name":"other","namespace":"fractal-demo","status":"failed"}]',
        '[{"name":"fractal-demo","namespace":"fractal-demo","status":"pending-install"}]',
        '[{"name":"fractal-demo","namespace":"fractal-demo","status":"uninstalling"}]',
    ],
)
def test_observation_failure_never_installs(booth, monkeypatch, tcp_app, wait_for, result):
    async def scenario():
        helm = Helm()
        helm.list_result = result
        monkeypatch.setattr(deployment, "run_command", helm)
        booth[1].write_text("cluster")
        async with tcp_app(application(booth)) as front, ClientSession() as client:
            await wait_for(lambda: len(helm.checks) >= 2)
            assert helm.installs == []
            html = await panel(client, front)
            assert "Renderer deployment installed." not in html
            assert "retry" in html or "operator attention" in html
            helm.list_result = None
            await wait_for(lambda: helm.releases.get("cluster") == "deployed")
            assert len(helm.installs) == 1

    asyncio.run(scenario())


def test_failed_release_retries(booth, monkeypatch, tcp_app, wait_for):
    async def scenario():
        helm = Helm()
        helm.releases["cluster"] = "failed"
        monkeypatch.setattr(deployment, "run_command", helm)
        booth[1].write_text("cluster")
        async with tcp_app(application(booth)) as front, ClientSession() as client:
            await wait_for(lambda: helm.releases["cluster"] == "deployed")
            baseline = len(helm.checks)
            await wait_for(lambda: len(helm.checks) >= baseline + 2)
            assert len(helm.installs) == 1
            assert "Renderer deployment installed." in await panel(client, front)

    asyncio.run(scenario())


def test_button_wakes_and_coalesces_without_overlapping(booth, monkeypatch, tcp_app, wait_for):
    async def scenario():
        booth[0].write_text("poll_interval_seconds = 60\n")
        booth[1].write_text("cluster")
        helm = Helm()
        helm.releases["cluster"] = "deployed"
        monkeypatch.setattr(deployment, "run_command", helm)
        async with tcp_app(application(booth)) as front, ClientSession() as client:
            await wait_for(lambda: len(helm.checks) == 1)
            for _ in range(3):
                assert "Renderer deployment installed." in await panel(client, front)
            assert len(helm.checks) == 1
            await panel(client, front, post=True)
            await wait_for(lambda: len(helm.checks) == 2)
            assert helm.installs == []
            del helm.releases["cluster"]
            helm.finish_install.clear()
            await panel(client, front, post=True)
            await asyncio.wait_for(helm.install_entered.wait(), 3)
            assert helm.active == 1
            replies = await asyncio.gather(*(panel(client, front, post=True) for _ in range(5)))
            for html in replies:
                assert "disabled" in Elements(html).tags["button"]
                assert "Installing renderer" in html
            for route in ("/healthz", "/api/workers"):
                async with client.get(front.url + route) as response:
                    assert response.status == 200
            assert len(helm.checks) == 3
            assert len(helm.installs) == 1
            helm.finish_install.set()
            await wait_for(lambda: len(helm.checks) == 4)
            assert "Renderer deployment installed." in await panel(client, front)
            assert helm.maximum_active == 1
            assert len(helm.installs) == 1
            assert len(helm.checks) == 4

    asyncio.run(scenario())


def test_disabled_gateway_needs_no_helm_or_assets(tmp_path, monkeypatch, tcp_app):
    async def scenario():
        def forbidden(*args, **kwargs):
            pytest.fail("Disabled deployment touched Helm")

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(deployment.shutil, "which", forbidden)
        monkeypatch.setattr(deployment, "run_command", forbidden)
        config = tmp_path / "gateway.toml"
        config.write_text("poll_interval_seconds = 0.02\n")
        app = gateway.create_gateway_app(gateway.load_settings(config), config_path=config)
        async with tcp_app(app) as front, ClientSession() as client:
            assert await panel(client, front) == ""
            async with client.post(front.url + "/redeploy") as response:
                assert response.status == 409
            async with client.get(front.url + "/api/workers") as response:
                assert await response.json() == {"workers": []}
            async with client.get(front.url + "/") as response:
                html = await response.text()
                assert 'id="deployment" hx-get="/deployment" hx-trigger="load"' in html
                assert "/static/vendor/htmx.min.js" in html
            async with client.get(front.url + "/static/vendor/htmx.min.js") as response:
                assert response.status == 200
                assert 'version:"2.0.8"' in await response.text()

    asyncio.run(scenario())


@pytest.mark.parametrize("missing", ["assets", "helm", "invalid_values"])
def test_enabled_gateway_reports_setup_errors(booth, monkeypatch, tcp_app, missing):
    async def scenario():
        if missing == "assets":
            booth[3].unlink()
            message = "generate worker values first"
        elif missing == "helm":
            monkeypatch.setattr(deployment.shutil, "which", lambda name: None)
            message = "requires helm on PATH"
        else:
            booth[3].write_text("[]")
            message = "must contain a JSON object"
        with pytest.raises(ValueError, match=message):
            async with tcp_app(application(booth)):
                pytest.fail("Invalid setup started serving")

    asyncio.run(scenario())


@pytest.mark.parametrize("stop", ["timeout", "shutdown"])
def test_active_child_is_killed_and_reaped(booth, tmp_path, monkeypatch, tcp_app, wait_for, stop):
    async def scenario():
        booth[0].write_text("poll_interval_seconds = 60\n")
        booth[1].write_text("cluster")
        pid_file = tmp_path / "child.pid"
        real_run = deployment.run_command
        entered = asyncio.Event()
        finished = asyncio.Event()
        observed = []
        # Ignore TERM to exercise kill escalation, not just cancellation of a mocked coroutine.
        child = (
            "import os, pathlib, signal, time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            f"ready = pathlib.Path({str(pid_file.with_suffix('.ready'))!r}); "
            "ready.write_text(str(os.getpid())); "
            f"ready.replace({str(pid_file)!r}); "
            "time.sleep(60)"
        )

        async def substitute(args, *, timeout):  # noqa: ASYNC109 - external runner's interface
            entered.set()
            try:
                return await real_run(
                    [sys.executable, "-c", child], timeout=1 if stop == "timeout" else 60
                )
            except BaseException as exc:
                observed.append(type(exc))
                raise
            finally:
                finished.set()

        monkeypatch.setattr(deployment, "run_command", substitute)
        monkeypatch.setattr(deployment, "STOP_TIMEOUT", 0.1)
        async with tcp_app(application(booth)) as front, ClientSession() as client:
            await asyncio.wait_for(entered.wait(), 3)
            await wait_for(pid_file.exists)
            pid = int(pid_file.read_text())
            os.kill(pid, 0)
            if stop == "timeout":
                await asyncio.wait_for(finished.wait(), 3)
                assert "retry" in await panel(client, front)
        assert finished.is_set()
        assert observed == [TimeoutError if stop == "timeout" else asyncio.CancelledError]
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        with pytest.raises(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)  # noqa: ASYNC222 - WNOHANG cannot block
        assert not [task for task in asyncio.all_tasks() if task.get_name() == "fractal-deployment"]

    asyncio.run(scenario())
