# Kubernetes fractal demo

A distributed Mandelbrot renderer for a conference kiosk. Workers compute tiles, and the browser
assembles the image. Tile borders identify the node that produced each tile.

The gateway runs on the demo PC, outside the disposable Kubernetes cluster. When a worker disappears,
unfinished tiles move to surviving workers. With no workers, the page retains its image and waits
for workers to register again.

## Running locally

Install Python 3.14 and [uv](https://docs.astral.sh/uv/). Run these commands from the repository root:

```sh
uv sync --frozen
cp -n config.example.toml config.toml
```

The example uses gateway port 8080 and worker ports 8081 and 8082 on loopback.
If `config.toml` already exists, compare its ports and `[local_worker_ports]` entries with the example.
Use three terminals to start the processes.

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

Open **http://127.0.0.1:8080/** in one active browser window. Rendering starts as workers register.
Stop a worker with Ctrl+C and restart it to observe its return with a new process identity.
Stop all three processes when finished.

## Using the controls

- **RENDER** clears the image and starts a new frame. Changing the palette also starts a new frame.
- The arrow buttons pan by a quarter of the view. Zoom buttons change scale by a factor of two around the center.
- **Settings** adjusts columns, rows, iterations, frame dwell, and LOST display duration.
  Click **Apply** to accept the numeric inputs together. Invalid inputs leave the running frame unchanged.
- Grid and iteration changes start a new frame. Timing-only changes preserve the image.
  A shorter dwell can start the next frame immediately. LOST duration changes affect future losses.
- **Reset settings** restores configured defaults, discards unapplied edits, and clears saved overrides.
  It preserves the selected palette.

Navigation uses accepted settings and preserves unapplied numeric edits. Completed frames repeat
after the dwell interval. Hidden pages suspend new rendering until visible again.

The status area reports the worker count, active requests, progress, and a rolling rate of successfully
painted tiles. The worker count shows registrations, not Kubernetes readiness or free compute slots.
A `?` means discovery is stale. LOST indicates contact loss, not confirmed node death.
Throughput depends on the view, grid, iterations, hardware, and browser connection limits.

Image width and height come from TOML. Columns and rows must divide those dimensions evenly,
with at most 4,096 tiles and at most 256 × 256 pixels per tile. Iterations accept 1 through 10,000.
Dwell and LOST durations accept 1 through 300,000 ms.

Accepted navigation and settings persist in browser storage under `fractal-demo.ui-settings.v1`.
Each origin has separate settings, so changing the hostname or port changes which settings load.
Saved values override the corresponding TOML defaults. After changing TOML, restart the gateway,
reload the page, and click **Reset settings** to use the new defaults.

## Configuration

The gateway can also run in a container with Helm and host networking.
See [Podman and rootful Quadlet deployment](docs/deployment.md#running-the-gateway-container).

The gateway reads `--config PATH` at startup. Without a file, it uses loopback-only defaults.
See [config.example.toml](config.example.toml) for all settings and their initial values.

For automatic deployment into a replacement cluster, start the gateway with `--kubeconfig PATH`.
The gateway reads the provisioner's updated kubeconfig and restores a missing or failed Helm release.
The dashboard shows deployment status and a **Redeploy workers** button for an immediate check.
An installed release stays unchanged. Rendering resumes as workers register.
Set the top-level `poll_interval_seconds` in TOML to adjust deployment checks. The default is 3 seconds.
See [automatic kiosk deployment](docs/deployment.md#enabling-automatic-kiosk-deployment) for setup.

Keep these rules in mind:

- `bind` must list explicit interfaces for the deployment.
- `local_worker_ports` overrides `worker_port` for loopback nodes. Match each named worker's `--port`
  and each worker's `--gateway-url` to the gateway listener. Remove the table for Kubernetes.
- The browser render deadline must exceed the gateway's upstream deadline.

Worker command-line options override environment variables. Run `fractal-worker --help` for the full
list and defaults. The gateway URL must be a plain HTTP origin, for example `http://192.0.2.1:8080`.
HTTPS is rejected.

## Operating limits

Use a trusted demo network. Registration and worker rendering have no authentication.
Keep the gateway and worker host ports off the shared conference network.
The application does not configure firewalls or manage virtual machines.

Deploy workers with the Helm chart and values generated from the host TOML and libvirt network.
See the [deployment guide](docs/deployment.md) for commands and image overrides.
Cluster reconstruction removes workload definitions and image-pull credentials.
With `--kubeconfig`, the host gateway reinstalls the release. Private-image credentials require separate restoration.
Without this flag, reinstall the release manually. See
[Restoring and rolling back](docs/deployment.md#restoring-and-rolling-back).

## Documentation

- [Deployment and troubleshooting](docs/deployment.md)
- [Pre-demo checklist](docs/rehearsal.md)
- [Development and testing](docs/development.md)
