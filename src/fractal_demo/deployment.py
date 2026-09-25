"""Host-side Helm recovery for the disposable demo cluster."""

import asyncio
import json
import logging
import shutil
from contextlib import suppress
from html import escape
from pathlib import Path

LOG = logging.getLogger(__name__)
CHECK_TIMEOUT = 10
INSTALL_TIMEOUT = 60
STOP_TIMEOUT = 2
OUTPUT_LIMIT = 256 * 1024
RELEASE = "fractal-demo"


class HelmError(Exception):
    """A failed Helm command or an unusable release listing."""


def validate_assets(chart, values):
    if not (chart / "Chart.yaml").is_file():
        raise ValueError(f"{chart} must contain Chart.yaml")
    with values.open() as stream:
        if not isinstance(json.load(stream), dict):
            raise ValueError(f"{values} must contain a JSON object")
    return chart, values


def discover_assets(config_path):
    starts = [Path(config_path).absolute().parent] if config_path is not None else []
    starts.append(Path.cwd())
    seen = set()
    for start in starts:
        for root in (start, *start.parents):
            if root in seen:
                continue
            seen.add(root)
            chart = root / "deploy/helm/fractal-demo"
            values = root / "deploy/values.local.json"
            if (chart / "Chart.yaml").is_file() and values.is_file():
                return validate_assets(chart, values)
    raise ValueError(
        "Could not find deploy/helm/fractal-demo/Chart.yaml and deploy/values.local.json "
        "together above the configuration file or working directory; generate worker values first"
    )


async def read_output(stream):
    output = bytearray()
    oversized = False
    while chunk := await stream.read(65536):
        if len(output) + len(chunk) > OUTPUT_LIMIT:
            oversized = True
        elif not oversized:
            output.extend(chunk)
    return output.decode("utf-8", errors="replace"), oversized


async def run_command(args, *, timeout):  # noqa: ASYNC109 - deadline includes owned-process cleanup
    """Run a bounded command and reap it on success, timeout, or cancellation."""
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    # Keep warnings on stderr out of Helm's JSON, and keep draining both pipes during cleanup.
    readers = asyncio.gather(read_output(process.stdout), read_output(process.stderr))
    try:
        async with asyncio.timeout(timeout):
            (output, oversized), (error, error_oversized) = await asyncio.shield(readers)
            await process.wait()
        if oversized or error_oversized:
            raise HelmError("Helm output exceeded the size limit")
        if process.returncode:
            raise HelmError(error.strip()[-2000:] or f"Helm exited with code {process.returncode}")
        return output
    finally:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.terminate()
        try:
            async with asyncio.timeout(STOP_TIMEOUT):
                await asyncio.shield(readers)
                await process.wait()
        except TimeoutError:
            with suppress(ProcessLookupError):
                process.kill()
            await readers
            await process.wait()


class Deployment:
    def __init__(
        self,
        kubeconfig,
        poll_interval_seconds,
        config_path=None,
        *,
        chart_path=None,
        values_path=None,
    ):
        if (chart_path is None) != (values_path is None):
            raise ValueError("--helm-chart and --helm-values must be supplied together")
        if chart_path is None:
            self.chart, self.values = discover_assets(config_path)
        else:
            self.chart, self.values = validate_assets(
                Path(chart_path).expanduser().absolute(), Path(values_path).expanduser().absolute()
            )
        self.helm = shutil.which("helm")
        if self.helm is None:
            raise ValueError("Automatic deployment requires helm on PATH")
        # Do not resolve symlinks: the provisioner may replace their targets.
        self.kubeconfig = Path(kubeconfig).expanduser().absolute()
        self.interval = poll_interval_seconds
        self.message = "Waiting for cluster access."
        self.busy = False
        self.wake = asyncio.Event()

    def request_redeploy(self):
        self.wake.set()

    async def run(self):
        while True:
            self.wake.clear()
            await self.check()
            try:
                async with asyncio.timeout(self.interval):
                    await self.wake.wait()
            except TimeoutError:
                pass

    async def check(self):
        common = ["--kubeconfig", str(self.kubeconfig), "--namespace", RELEASE]
        try:
            # Helm must never fall back to the operator's default configuration.
            if not self.kubeconfig.is_file():
                self.message = "Waiting for cluster configuration."
                return
            output = await run_command(
                [
                    self.helm,
                    "list",
                    *common,
                    # Explicit status filters work with Helm 3 and 4 (--all was removed in 4).
                    "--deployed",
                    "--failed",
                    "--pending",
                    "--uninstalling",
                    "--uninstalled",
                    "--superseded",
                    "--filter",
                    f"^{RELEASE}$",
                    "--output",
                    "json",
                ],
                timeout=CHECK_TIMEOUT,
            )
            releases = json.loads(output)
            if not isinstance(releases, list) or len(releases) > 1:
                raise HelmError("Unexpected Helm release listing")
            status = None
            if releases:
                release = releases[0]
                if (
                    not isinstance(release, dict)
                    or release.get("name") != RELEASE
                    or release.get("namespace") != RELEASE
                    or not isinstance(release.get("status"), str)
                ):
                    raise HelmError("Unexpected Helm release listing")
                status = release["status"]
            if status == "deployed":
                self.message = "Renderer deployment installed."
                return
            if status not in (None, "failed"):
                self.message = f"Helm release is {status[:80]}; operator attention may be needed."
                return
            self.busy = True
            self.message = "Installing renderer."
            await run_command(
                [
                    self.helm,
                    "upgrade",
                    "--install",
                    RELEASE,
                    str(self.chart),
                    *common,
                    "--create-namespace",
                    "--values",
                    str(self.values),
                    "--timeout",
                    "45s",
                ],
                timeout=INSTALL_TIMEOUT,
            )
            self.message = "Renderer deployment installed."
        except (OSError, TimeoutError, HelmError, ValueError) as exc:
            self.message = (
                "Deployment failed; retrying."
                if self.busy
                else "Waiting for cluster access; deployment check will retry."
            )
            LOG.warning("%s %s", self.message, exc)
        finally:
            self.busy = False

    def panel(self):
        disabled = " disabled" if self.busy else ""
        # Synchronize GET and POST on the persistent outer container. Never replace the canvas.
        return (
            '<div class="deployment-panel" hx-get="/deployment" '
            f'hx-trigger="every {self.interval:g}s" hx-target="#deployment" '
            'hx-swap="innerHTML" hx-sync="#deployment:queue last">'
            '<span role="status" aria-live="polite">'
            f"{escape(self.message)}</span>"
            '<button type="button" hx-post="/redeploy" hx-trigger="click" '
            'hx-disabled-elt="this" title="Check now and restore a missing or failed release. '
            'An installed release is unchanged."'
            f"{disabled}>Redeploy workers</button></div>"
        )
