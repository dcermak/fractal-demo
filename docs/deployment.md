# Deployment

The demo host runs the gateway from a Podman image with a system Quadlet. The image includes Helm
and the worker chart; the host needs Python 3, Podman with Quadlet support, and systemd. The
setup does not need host Helm, uv, virsh, or kubectl. Worker nodes must be Linux amd64 and able
to pull the public worker image.

## Installing on the demo host

Use this checkout on the host. Get the gateway and worker image references from the publication
artifacts in [the image workflow](../.github/workflows/publish-image.yml); use digests when possible.
The provisioner must keep a stable credentials **directory** containing a file named `kubeconfig`.
The directory must exist when you run setup; the kubeconfig may arrive after provisioning.

```sh
sudo python3 scripts/setup_quadlet.py \
  --gateway-ip 192.168.122.1 \
  --node-network 192.168.122.0/24 \
  --cluster-dir /var/lib/fractal-demo/cluster \
  --gateway-image ghcr.io/dcermak/fractal-demo-gateway@sha256:GATEWAY_DIGEST \
  --worker-image ghcr.io/dcermak/fractal-demo@sha256:WORKER_DIGEST
```

Replace the addresses, directory, and image references. `--gateway-ip` is the demo host address
reachable from worker pods; `--node-network` contains the worker node IPs reachable from the host.
The gateway listens on that address and `127.0.0.1`. Default ports are 8081 for the gateway and
8080 for workers; use `--gateway-port` and `--worker-port` to change them. Keep both ports on the
trusted demo network: registration and rendering have no authentication. Host networking requires
the supplied gateway address to exist when the service starts. Worker nodes need a free host port,
and the host must reach them on the worker port.

The script writes `/etc/fractal-demo/config.toml`, `/etc/fractal-demo/values.local.json`, and
`/etc/containers/systemd/fractal-gateway.container`. It reloads systemd and starts or restarts
`fractal-gateway.service`. A rerun saves the previous files in `/etc/fractal-demo/previous/`.
The Quadlet starts at boot; do not enable the generated service with `systemctl enable`.

```sh
systemctl status fractal-gateway.service
journalctl -u fractal-gateway.service -f
curl --fail http://127.0.0.1:8081/api/workers
```

Open `http://127.0.0.1:8081/` on the host. The gateway can start before the cluster exists.
Its dashboard reports worker deployment status and offers **Redeploy workers** for an immediate
check. A healthy gateway alone does not mean workers have deployed; confirm registrations and
rendered tiles. The gateway reads the current kubeconfig from the mounted directory and installs
workers when a new cluster appears. Keep that directory stable across kubeconfig replacement;
certificate paths and authentication helpers referenced by it must also work inside the image.
On SELinux hosts, use container-compatible labels for the mounted directories without relabeling
a broad provisioner directory.

## Changes and recovery

Rerun the script to change host settings or images. It restarts the gateway, but a worker release
already installed in the cluster stays unchanged. To change existing workers, stop the gateway
service and upgrade the release manually with Helm, or rebuild the disposable cluster; then start
the service again. New clusters use the saved worker values. Public worker images avoid a separate
image-pull secret restoration step. The worker chart's resource limits and node selection are in
[`deploy/helm/fractal-demo`](../deploy/helm/fractal-demo/).

For a failed rerun, the script restores the replaced files and tries to restart the previous
service. To restore the saved configuration later, run:

```sh
sudo install -m 0600 /etc/fractal-demo/previous/config.toml /etc/fractal-demo/config.toml
sudo install -m 0600 /etc/fractal-demo/previous/values.local.json /etc/fractal-demo/values.local.json
sudo install -m 0644 /etc/fractal-demo/previous/fractal-gateway.container /etc/containers/systemd/fractal-gateway.container
sudo systemctl daemon-reload
sudo systemctl restart fractal-gateway.service
```

Keep a known working pair of image digests for rollback.

If workers do not appear, inspect `journalctl -u fractal-gateway.service`, the dashboard status,
and `/api/workers`. Check the mounted kubeconfig, current cluster context, image access, node
addresses, and both network directions. Pods need access to the gateway IP and port; the host
needs access to each worker node on the worker port. A release stuck pending or uninstalling needs
manual Helm repair with the gateway service stopped. Cluster replacement clears release metadata,
so the gateway can install workers again without host Helm.

## Building and publishing images

From a development machine with Podman or Docker, build `worker` and `gateway` targets from
[`Containerfile`](../Containerfile). The gateway target bundles Helm and the chart. The
[publication workflow](../.github/workflows/publish-image.yml) runs tests, checks Helm rendering,
and publishes both images to GHCR. Its image-reference artifacts contain the digest references.
Keep packages public for anonymous pulls on replacement nodes. See
[development checks](development.md#container-checks) for local verification.
