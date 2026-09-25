# Development

## Running checks

Use Python 3.14 and uv. Run commands from the repository root:

```sh
uv sync --frozen
uv run --frozen ruff check src tests scripts
uv run --frozen ruff format --check src tests scripts
uv run --frozen pytest
```

Run `uv run --frozen ruff format src tests scripts` to apply formatting.

The default suite covers numerical rendering, configuration, HTTP behavior, subprocess cleanup,
and wheel installation. HTTP tests use loopback servers and renderer threads.
The installation test builds a wheel with installed build dependencies and runs it outside the checkout.

## Checking automatic deployment

The default suite includes `tests/test_auto_deploy.py`. Run it separately with:

```sh
uv run --frozen pytest tests/test_auto_deploy.py
```

These integration tests start the gateway on a loopback TCP listener with temporary configuration and deployment files.
They substitute the external Helm command boundary and executable lookup. The controller, HTTP handlers, file discovery, and application lifecycle run normally.
The scenarios cover cluster replacement, explicit deployment paths, failed observations, failed releases, button requests, and startup without recovery.
Timeout and shutdown checks use a Python child process with the production subprocess runner to verify termination and reaping.
No cluster or Helm installation is required for these tests.

Mocked commands do not verify Helm or Kubernetes deployment semantics. The [kiosk recovery rehearsal](rehearsal.md#checking-automatic-redeployment) covers cluster replacement and browser execution of HTMX.

## Running browser tests

Browser tests start their own gateway and workers on temporary loopback ports.
They cover navigation, settings, persistence, timing changes, and stale responses or decoded images.
Install the managed browsers and their required system libraries before running:

```sh
uv run --frozen playwright install firefox chromium
uv run --frozen python -m pytest tests/browser/ui_settings.py
FRACTAL_TEST_BROWSER=chromium uv run --frozen python -m pytest tests/browser/ui_settings.py
```

The filename `ui_settings.py` keeps these tests outside default pytest discovery and continuous
integration (CI). Invoke the file explicitly. Missing browser prerequisites fail the explicit run.
Check the physical kiosk display with the [pre-demo checklist](rehearsal.md).

`tests/support/local_stack.py` manages test subprocesses, readiness checks, configuration, and logs.
It stops owned children on exit and escalates to a kill after the shutdown timeout.
Port selection releases sockets before processes bind; a competing process can claim a selected port.
Readiness failures report startup errors or time out. Logs are retained in pytest's temporary directory.

## Architecture

The browser fetches configuration and worker registrations from the host gateway, then schedules tiles
through that gateway. Workers register their node address and process identity periodically.
Each worker computes one tile at a time using NumPy and encodes the result as PNG with Pillow.

| Interface | Purpose |
| --- | --- |
| Gateway `POST /register` | Register or refresh a worker |
| Gateway `GET /api/workers` | List current worker identities |
| Gateway `GET /api/config` | Read browser defaults |
| Gateway `GET /api/render/{process_id}` | Proxy a tile to the selected worker identity |
| Gateway `GET /deployment` | Return the cached deployment status and button as HTML |
| Gateway `POST /redeploy` | Request an immediate deployment check and return the HTML fragment |
| Worker `GET /render` | Render a tile |
| Both `GET /healthz` | Check process responsiveness |

The gateway and browser keep these rules:

- The gateway fixes the destination identity for each request. The browser checks assignment ownership,
  worker identity, image decoding, and tile dimensions before painting.
- Results from an old frame or worker process cannot paint a newer assignment.
- Missing registrations and explicit upstream transport failures can mark unfinished tiles LOST for retry.
  Browser transport and discovery failures pause dispatch while discovery refreshes.
- Errors contain `code`, `message`, and `scope` fields. Busy responses trigger cooldowns.
- Invalid images receive one retry before rendering pauses. A timed-out decoder must settle before dispatch resumes.
- A disconnected request retains its worker slot until computation finishes. Shutdown drains admitted work.

Protocol validation is in `src/fractal_demo/protocol.py`; browser scheduling is in
`src/fractal_demo/static/scheduler.js`.

Optional Helm recovery runs in `src/fractal_demo/deployment.py` as a gateway background task.
HTMX refreshes the deployment panel independently of the renderer. The pinned distribution and license are in `src/fractal_demo/static/vendor/`.
Without `--kubeconfig`, the deployment fragment is empty and starts no browser polling.

## Checking deployment values

Install Helm 3 or newer, then run the CLI-to-manifest checks:

```sh
uv run --frozen python -m pytest tests/deploy/chart.py
```

These tests run the values helper, `helm lint`, and `helm template`, then inspect the rendered YAML.
Only the external `virsh` command is replaced with fixture output. No cluster or live libvirt connection is needed.
The tests cover discovery, explicit overrides, non-default ports, and invalid configuration.
CI invokes them explicitly; missing Helm fails the run.

## Container checks

See [Building locally](deployment.md#building-locally) for the container build and image smoke check,
and [Publishing images](deployment.md#publishing-images) for what CI runs.
Build targets `worker` and `gateway` share the installed Python environment.
The gateway checks also run its Helm binary and render the packaged chart.
Playwright and other development dependencies are excluded from both images.
