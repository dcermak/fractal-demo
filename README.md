# Kubernetes fractal booth demo

A distributed Mandelbrot renderer for a conference kiosk. Each worker computes tiles, and the browser
assembles them into an image. Tile borders identify the producing node. When worker contact is lost,
unfinished tiles are retried while completed pixels remain visible.

The gateway runs on the demo PC, outside the disposable cluster. With zero workers, the browser keeps
its image and waits. Workers register automatically when they return.

## Implementation status

The operator confirmed that local testing works after correcting the local port mappings.
The application has Python numerical, HTTP integration, and installed-package tests.
Detailed browser checks and booth measurements remain recorded tasks in [PLAN.md](PLAN.md).

Container and deployment preparation is available in [docs/deployment.md](docs/deployment.md).
The selected base is `registry.opensuse.org/opensuse/bci/python:3.14`. GitHub Actions is prepared to
publish tested worker images to GitHub Container Registry. Builds, publication, and deployment have
not run in this session. Playwright tests and dependencies remain deferred.

The earlier visual mock is archived at `docs/reference/index.html`. Open the gateway URL to use the application.
`k3s-kvm-demo/` is an independent reference project; this application does not access it.

## Local setup

Use Python 3.14 and uv. Run these commands from this repository:

```sh
uv sync --frozen
cp config.example.toml config.toml
```

The example uses loopback only, gateway port 8080, and two explicit local worker ports. Check that
these ports are available. Use three terminals for the following processes.

If you change ports, update each worker's `--port` and its matching `[local_worker_ports]` entry.
Those entries override `worker_port`. For example, a gateway on 8081 and workers on 8082 and 8083
require `worker-a = 8082` and `worker-b = 8083`, with both workers registering at
`http://127.0.0.1:8081`. Restart the gateway after changing its configuration.

Gateway:

```sh
uv run --frozen fractal-gateway --config config.toml
```

First worker:

```sh
uv run --frozen fractal-worker \
  --node worker-a --host-ip 127.0.0.1 --bind 127.0.0.1 --port 8081 \
  --gateway-url http://127.0.0.1:8080
```

Second worker:

```sh
uv run --frozen fractal-worker \
  --node worker-b --host-ip 127.0.0.1 --bind 127.0.0.1 --port 8082 \
  --gateway-url http://127.0.0.1:8080
```

Open **http://127.0.0.1:8080/** in one active browser window. The page starts rendering as workers
register. Use **RENDER** to start a fresh frame or select the other palette. Completed frames dwell
briefly before the same view is recomputed.

Stop a worker with Ctrl+C and start it again using the same command. Its process ID changes, and it
registers again. Ctrl+C performs an orderly drain; it does not measure hard node-loss behavior.

Stop the workers and gateway with Ctrl+C when finished. These commands create no cluster resources.

## Configuration

The gateway accepts `--config PATH`; without it, it uses loopback-only defaults. Configuration is loaded
at startup. After changing gateway configuration, restart the gateway and reload the browser.

| Setting | Purpose |
| --- | --- |
| `bind`, `port` | Explicit host interfaces and gateway port; wildcard bindings are rejected |
| `node_networks` | Allowed node IP networks; registrations accept literal IPs, not URLs |
| `worker_port` | Fixed worker port used for registered nodes |
| `local_worker_ports` | Explicit node-to-port overrides for local testing; loopback addresses only |
| `expiry_seconds` | Registration expiry based on gateway monotonic receipt time |
| `proxy_limit` | Maximum simultaneous upstream renders; excess requests receive gateway-scoped busy |
| `connect_timeout`, `upstream_timeout` | Bounded upstream connection and total request times, in seconds |
| `render_limit` | Browser render-request limit; default five, with polling capacity checked in rehearsal |
| `default_palette` | `cyber` or `fire` |
| `[geometry]` | Raster dimensions and grid columns/rows; dimensions must divide evenly |
| `[view]` | Mandelbrot origin, pixel size, and iteration count |
| `[timing]` | Browser timing settings, in milliseconds |

Each grid tile must be at most 256 × 256 pixels. Coordinates and iterations also pass the same
validation as individual worker requests. The iteration ceiling is 10,000; it is not the default
workload. Expensive-work timing and shutdown drain at the ceiling need joint measurement.

Worker command-line options take precedence over their environment defaults:

| Option | Environment variable | Default |
| --- | --- | --- |
| `--node` | `NODE_NAME` | Required |
| `--host-ip` | `HOST_IP` | Required |
| `--gateway-url` | `GATEWAY_URL` | Required HTTP origin reachable from the worker |
| `--bind` | `WORKER_BIND` | `0.0.0.0` |
| `--port` | `WORKER_PORT` | `8080` |
| `--registration-interval` | `REGISTRATION_INTERVAL` | `0.5` seconds |
| `--registration-timeout` | `REGISTRATION_TIMEOUT` | `2` seconds |

For Kubernetes, node name and host IP will come from the Downward API. The gateway must reach each
node's host port; host access to pod addresses is not assumed. Remove the local port-override table
for that deployment and configure the actual node networks and demo-PC interfaces together.

Registration has no authentication on the trusted demo network. Keep its writable endpoint off the
shared conference network. The application does not configure firewalls or host networking.

## Rendering and recovery

- Each worker admits one computation or responds busy. A disconnected HTTP request does not free
  the slot while its executor future is still running.
- The gateway snapshots the selected worker identity before proxying. It never substitutes a newer
  process ID into an existing assignment.
- The browser checks assignment ownership, response identity, PNG decoding, and dimensions before
  painting. Old results cannot paint a newer frame or increment its progress.
- Busy responses cause short cooldowns. Successful registration does not clear a render cooldown.
- Successful discovery that omits a worker, or an explicit upstream transport failure, can mark its
  unfinished tiles LOST. This indicates contact loss, not proof of node death.
- Browser transport failures and roster failures pause new dispatch and refresh discovery. They do
  not establish that all workers were lost.
- Invalid images or identities get one tile retry before rendering pauses with an error. A decoder
  timeout pauses rendering and retains a cleanup barrier until the outstanding decoder settles.
- Automatic cycles wait for current discovery and available rendering capacity. Completed images
  remain visible during outages. Manual RENDER intentionally clears the current frame.
- Hidden pages suspend new dispatch and cycles. Pending transport is aborted without releasing its
  credits early, and discovery refreshes before work resumes.

The displayed worker count is the registration count, not Kubernetes readiness or free compute slots.
The tile rate counts successfully painted tiles over a rolling window. A `?` beside the count means
discovery is not current. Node labels are abbreviated for readability; hover for the full name.

## Python verification

```sh
uv run --frozen ruff check src tests
uv run --frozen pytest
```

Tests use actual aiohttp applications on loopback TCP sockets, real executor threads, small numerical
renders, and worker subprocesses. Test-owned upstream servers supply malformed responses and stalls.
The installation test builds a wheel offline using installed build dependencies, installs it without
dependencies into a temporary directory, and runs its entry points outside the checkout.

The suite does not use libvirt, Kubernetes, a container daemon, or browser automation. It verifies
backend behavior and asset packaging; it does not verify canvas painting or the physical display.

If a required command is blocked by sandbox permissions, stop that operation and ask the operator for
assistance with the command and error. Do not change permissions or security settings to bypass it.

## Joint local testing checklist

- [ ] Open the production page beside the node dashboard in Firefox, using the intended split pane.
- [ ] Confirm attractive colors, a useful saved view, aligned borders, and readable matching node labels.
- [ ] Observe both workers contributing and inspect the displayed metrics.
- [ ] Add enough local workers to check that workers beyond the render limit also contribute.
- [ ] Stop a worker during unfinished work; inspect contact loss, LOST, retry, and preserved pixels.
- [ ] Stop all workers, then restore one; verify that pending tiles resume automatically.
- [ ] Press RENDER repeatedly and switch palettes while requests are outstanding; inspect stale painting.
- [ ] Watch completed-frame dwell and several fresh render cycles.
- [ ] Interrupt gateway availability; verify the shared-path message and recovery after restart.
- [ ] Hide the page and return; confirm refresh before dispatch and no replayed LOST animations.
- [ ] Perform a Chromium smoke check and record the browser versions, pane dimensions, and scaling.

The initial workload is 960 × 540 pixels, 16 × 9 tiles, and 800 iterations. Default heartbeat, expiry,
poll, LOST, and dwell candidates are 500 ms, 1.5 s, 500 ms, 600 ms, and 1,500 ms. Upstream and browser
render deadlines start at 10 s and 12 s. These values are unmeasured candidates; tune against healthy
work and visible behavior on the booth PC. Rendering contains no artificial delays.

## Deployment and rehearsal

Follow [docs/deployment.md](docs/deployment.md) for human-run builds, the GitHub Actions publication
workflow, host gateway configuration, and worker DaemonSet deployment. Record observed behavior in
[docs/rehearsal.md](docs/rehearsal.md).

The workflow publishes to `ghcr.io/<owner>/<repository>` on the default branch after Python checks and
an image smoke check pass. Use the immutable reference from its job summary or artifact in the
DaemonSet. The prepared image and node selection target Linux amd64 workers.

For complete cluster reconstruction, the operator obtains current kubeconfig and reapplies this
project's namespace, any image-pull credentials, and its DaemonSet. Leave the browser and gateway
running; returning workers should finish the retained partial frame. Registration does not recreate
missing Kubernetes resources.

Measure worker loss, control-plane loss, complete loss, restoration, resource settings, and shutdown
drain together. Record the kill action and visible effects on one observer timeline. The provisional
2.5 s contact-loss target is separate from LOST onset and is not a measured guarantee.
