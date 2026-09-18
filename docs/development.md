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
| Worker `GET /render` | Render a tile |
| Both `GET /healthz` | Check process responsiveness |

The gateway fixes the destination identity for each request. The browser checks assignment ownership,
worker identity, image decoding, and tile dimensions before painting. Results from an old frame or
worker process cannot paint a newer assignment.

Errors contain `code`, `message`, and `scope` fields. Busy responses trigger cooldowns.
Missing registrations and explicit upstream transport failures can mark unfinished tiles LOST for retry.
Browser transport and discovery failures pause dispatch while discovery refreshes.
Invalid images receive one retry before rendering pauses. A timed-out decoder must settle before dispatch resumes.

A disconnected request retains its worker slot until computation finishes. Shutdown drains admitted work.
Protocol validation is in `src/fractal_demo/protocol.py`; browser scheduling is in
`src/fractal_demo/static/scheduler.js`.

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

The [deployment guide](deployment.md#building-locally) covers container builds and the image smoke check.
CI runs lint and format checks plus the default tests on pushes and pull requests. On the default branch, it also builds,
smoke-tests, and publishes the worker image. Playwright and other development dependencies are excluded from the image.
