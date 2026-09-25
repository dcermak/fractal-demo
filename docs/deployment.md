# Deployment

Run the gateway on the demo PC and one worker per eligible Kubernetes node.
Both images use `registry.opensuse.org/opensuse/bci/python:3.14` and target Linux amd64.
Run the gateway directly or use the [gateway container and Quadlet](#running-the-gateway-container).

## Building locally

Use Podman or Docker. Run these commands from the repository root:

```sh
# Set ENGINE=docker to use Docker.
ENGINE=podman
for role in worker gateway; do
  "$ENGINE" build --target "$role" -f Containerfile -t "fractal-$role:local" .
  "$ENGINE" run --rm "fractal-$role:local" --help
  "$ENGINE" run --rm -i --entrypoint /opt/fractal/.venv/bin/python \
    "fractal-$role:local" - < scripts/smoke_image.py
done
"$ENGINE" run --rm --entrypoint helm fractal-gateway:local version --short
"$ENGINE" run --rm --entrypoint helm fractal-gateway:local template fractal-demo \
  /opt/fractal/deploy/helm/fractal-demo --namespace fractal-demo \
  --set gatewayUrl=http://192.168.122.1:8081
```

The build installs the distribution's uv package and uses frozen runtime dependencies.
Both images share the base and installed Python environment, and default to user and group ID 10001.
The `worker` target runs `fractal-worker` and remains the default when `--target` is omitted.
The `gateway` target runs `fractal-gateway` and includes the distribution's Helm package, installed with zypper, and the chart.
[.dockerignore](../.dockerignore) restricts the build context to the required files.

The smoke check starts a gateway and worker inside the container. It verifies registration, pixels,
worker identity, packaged assets, shutdown, and exclusion of development packages. No published ports are needed.

## Publishing images

[Lint and format checks](../.github/workflows/lint.yml) and
[Python tests and Helm rendering checks](../.github/workflows/publish-image.yml) run on pushes and pull requests.
On the default branch, successful tests in the publish-image workflow allow image building, Helm checks,
and publication to GitHub Container Registry (GHCR). The lint and format workflow does not gate publication.
Manual workflow dispatch also publishes from the default branch.

The workflow builds both targets in one job, reusing their shared layers.
It checks the bundled Helm binary and renders the packaged chart before publishing.
The application smoke check above is a manual local check.
It derives image names from the lowercase repository owner and name:

```text
ghcr.io/<owner>/<repository>:sha-<full-commit-sha>
ghcr.io/<owner>/<repository>:latest
ghcr.io/<owner>/<repository>-gateway:sha-<full-commit-sha>
ghcr.io/<owner>/<repository>-gateway:latest
```

Publication uses `GITHUB_TOKEN` with `packages: write` permission.
The `worker-image-reference` and `gateway-image-reference` artifacts contain the immutable references:

```text
ghcr.io/<owner>/<repository>@sha256:<digest>
ghcr.io/<owner>/<repository>-gateway@sha256:<digest>
```

The Helm chart defaults to `ghcr.io/dcermak/fractal-demo:latest`. Override `image` when publishing from
another repository or deploying a digest. Retain a working digest and its configuration for rollback.
Make the GHCR package public for anonymous pulls; private images require an image-pull secret, and new
cluster nodes need registry access.

## Configuring the gateway

Confirm both network directions:

- Worker pods must reach the demo PC's gateway address.
- The demo PC must reach each advertised node IP on the configured `worker_port`.

The cluster must support host-port mapping. Host access to pod addresses is unnecessary.
Registration and rendering are unauthenticated. Restrict gateway and worker ports to the trusted demo network.

Create a local configuration, preserving an existing file:

```sh
cp -n config.example.toml config.booth.toml
$EDITOR config.booth.toml
```

Set explicit demo-PC interfaces and the node network. This deployment example uses gateway port 8081;
the local-development example uses 8080. Replace the address placeholders:

```toml
bind = ["127.0.0.1", "REPLACE_WITH_DEMO_PC_IP"]
port = 8081
node_networks = ["REPLACE_WITH_NODE_CIDR"]
worker_port = 8080
```

Remove the entire `[local_worker_ports]` table. Keep the geometry, view, and timing sections.
Start the gateway before deploying workers:

```sh
uv sync --frozen --no-dev
"$PWD/.venv/bin/fractal-gateway" --config "$PWD/config.booth.toml"
```

Open **http://127.0.0.1:8081/** in one active browser window. Use another terminal for deployment commands.

## Deploying workers

Install Helm 3 or newer.

### Generating worker values

Generate values from the same TOML file used by the running gateway.

The helper runs `virsh --connect qemu:///system net-dumpxml default` to discover the network's IPv4 host address.
It reads both ports from TOML and checks the discovered address against `bind` and the subnet against `node_networks`.
The subnet must overlap the allowed node networks; verify that those networks cover every worker address.
Nonempty `local_worker_ports` are rejected. Local values and `config.booth.toml` are ignored by Git.

Use `--libvirt-uri URI` and `--network NAME` to select another libvirt network.
Use `--gateway-url http://ADDRESS:PORT` to bypass discovery. The origin must use plain HTTP.
Its address and port must match the host TOML.
Use `--image ghcr.io/OWNER/REPOSITORY@sha256:DIGEST` to pin an image.
These options also accept shell command substitutions.

Add your selected options to the helper invocation below, before `> "$candidate"`.
The block writes a temporary candidate and replaces the saved values only after successful generation.
It runs in a subshell so failure does not close your terminal:

```sh
(
  candidate=$(mktemp deploy/values.XXXXXX.local.json) || exit 1
  if uv run --frozen --no-dev python scripts/deployment_values.py \
      --config config.booth.toml > "$candidate" &&
      mv -- "$candidate" deploy/values.local.json; then
    echo "Saved deploy/values.local.json"
  else
    rm -f -- "$candidate"
    echo "Could not save new values. Stop here; existing values were not replaced." >&2
    exit 1
  fi
)
```

### Previewing worker manifests

For a private image, append `--set 'imagePullSecrets[0].name=ghcr-pull'` to both the preview and installation commands.

Preview the manifest locally:

```sh
helm template fractal-demo deploy/helm/fractal-demo \
  --namespace fractal-demo -f deploy/values.local.json
```

Check the rendered image reference, gateway URL, and worker port before installing.

The chart sets `WORKER_PORT`, `containerPort`, and `hostPort` from the same value.
After changing either port in TOML, restart the gateway and repeat [values generation](#generating-worker-values), preview, and installation.

The DaemonSet excludes nodes labeled `node-role.kubernetes.io/control-plane` or `node-role.kubernetes.io/master`.
The Downward API supplies node name and host IP. Workers do not mount a service-account token.
See [values.yaml](../deploy/helm/fractal-demo/values.yaml) and the DaemonSet template for resource
limits, termination grace, and probes. Validate those settings with the intended workload before the demo.

Select the disposable demo cluster:

```sh
export KUBECONFIG=/absolute/path/to/demo-kubeconfig
CONTEXT=REPLACE_WITH_DEMO_CONTEXT
kubectl --context "$CONTEXT" get nodes \
  -L kubernetes.io/arch,node-role.kubernetes.io/control-plane,node-role.kubernetes.io/master
kubectl --context "$CONTEXT" apply -f deploy/namespace.yaml
```

For a private image, create the `ghcr-pull` secret in namespace `fractal-demo` using your registry credentials.
Use the same secret reference selected for the preview. Keep credentials outside the repository.

### Installing or upgrading workers

Install or upgrade using the saved values that passed the preview, with the same Helm overrides.
This command reuses the file without running discovery or regenerating values:

```sh
helm upgrade --install fractal-demo deploy/helm/fractal-demo \
  --kube-context "$CONTEXT" --namespace fractal-demo --create-namespace \
  -f deploy/values.local.json
```

After successful installation, verify the rollout and registration. The checks stop at the first failure:

```sh
kubectl --context "$CONTEXT" -n fractal-demo rollout status daemonset/fractal-worker --timeout=120s &&
kubectl --context "$CONTEXT" -n fractal-demo get pods -o wide &&
kubectl --context "$CONTEXT" -n fractal-demo logs \
  -l app.kubernetes.io/name=fractal-worker --prefix --tail=30 &&
curl --fail http://127.0.0.1:8081/api/workers
```

Confirm that eligible nodes register and contribute tiles. Control-plane nodes should have no render worker.
Use the [pre-demo checklist](rehearsal.md) before presenting.

The image pull policy is `Always`, but publishing a new `latest` image does not restart existing pods.
To refresh them:

```sh
kubectl --context "$CONTEXT" -n fractal-demo rollout restart daemonset/fractal-worker
kubectl --context "$CONTEXT" -n fractal-demo rollout status daemonset/fractal-worker --timeout=120s
```

Only one deployment can use a given worker host port on the same nodes, even across namespaces.

## Enabling automatic kiosk deployment

The host gateway can deploy workers automatically when the provisioning dashboard recreates the cluster.
The provisioning dashboard continues to manage the VMs and kubeconfig without deploying the renderer.

1. Generate and preview `deploy/values.local.json` using the instructions above.
2. Stop the running gateway.
3. Restart it with the continuously updated kubeconfig path:

   ```sh
   uv run --frozen fractal-gateway --config /absolute/path/to/config.booth.toml \
     --kubeconfig /fixed/path/to/demo-kubeconfig
   ```

The flag enables automatic recovery. No Helm installation or deployment files are required when the flag is omitted.
With the flag, Helm and both deployment files must exist at startup. A missing kubeconfig or unreachable cluster is a waiting condition.
The gateway reads the kubeconfig's current context on each Helm invocation, including after file or symlink replacement.

Use `--helm-chart DIRECTORY --helm-values FILE` with `--kubeconfig` to select deployment assets explicitly.
The values file must contain a JSON object. Supply both path options together.
Invalid explicit paths fail startup without falling back to discovery.

Without these options, the gateway searches ancestors of the configuration directory, then ancestors of its working directory, for these files:

```text
deploy/helm/fractal-demo/Chart.yaml
deploy/values.local.json
```

For discovery, both files must belong to the same repository layout. Without `--config`, discovery starts at the working directory.
The gateway retains the selected paths until restart. Explicit paths allow an installed wheel to use assets outside a checkout.

Set the check interval before any TOML table:

```toml
poll_interval_seconds = 3
```

The interval must be positive and finite. It controls deployment checks, independently of browser worker discovery.
The gateway checks immediately at startup. Checks continue without an open browser.
A running Helm command delays the next check until completion or its deadline.
Release checks have a 10-second deadline. Installation commands have a 60-second deadline, followed by bounded process cleanup.

The gateway checks release `fractal-demo` in namespace `fractal-demo`.
A missing or failed release triggers `helm upgrade --install` with the discovered chart and saved values.
An installed release stays unchanged. Failed checks retry without assuming the release is absent.
The gateway does not regenerate values or require kubectl for recovery.

**Redeploy workers** requests an immediate check through the same background loop.
The button is disabled during installation. Repeated requests do not run concurrent Helm commands.
The status describes Helm deployment. Use the existing worker count and rendering display to confirm that workers contribute tiles.
The controls use a locally served HTMX file and do not require a content delivery network.

Keep the gateway address, ports, and allowed node networks valid after reconstruction.
Replacement clusters need eligible Linux amd64 worker nodes and registry access.
For unattended recovery, use a publicly accessible image or an existing mechanism that restores image-pull credentials.
Put required overrides, including image-pull secret references, in `deploy/values.local.json`.
Overrides supplied only to a previous manual Helm command are not retained for recovery.

A release stuck in a pending or uninstalling state requires operator intervention.
The gateway does not automatically uninstall releases, roll them back, or repair resources within a deployed release.
Full cluster replacement removes the old release metadata, allowing a fresh installation.
Stop the recovery-enabled gateway before manual Helm maintenance or intentional removal of the release.

## Running the gateway container

These commands run as root on the Linux demo host, outside the disposable cluster.
Use the same gateway addresses, worker port, and node networks as for a native gateway.
The configured host addresses must exist before startup. Restrict access to the trusted demo network.

After configuring the gateway and generating worker values, install the runtime configuration:

```sh
install -d -m 0700 /etc/fractal-demo
install -m 0600 config.booth.toml /etc/fractal-demo/config.toml
install -m 0600 deploy/values.local.json /etc/fractal-demo/values.local.json
```

Mount the provisioner's stable credential directory, containing a file named `kubeconfig`.
Adjust the filename in the command if needed. Do not copy credentials once and expect cluster recovery to update that copy.
Directory mounts expose file replacements. Replacing the mounted directory itself or mounting only the kubeconfig file can leave stale credentials.
Symlink targets and referenced certificate paths must be accessible inside the container.
Use credentials that do not require external authentication helpers absent from the image.

Set the image reference and credential directory, then start the gateway:

```sh
# Prefer the gateway digest from the publication artifact for repeatable deployments.
IMAGE=ghcr.io/dcermak/fractal-demo-gateway:latest
CLUSTER_DIR=/absolute/path/to/provisioner/credentials
podman run --detach --rm --name fractal-gateway \
  --network=host --user=0:0 \
  --cap-drop=all --security-opt=no-new-privileges \
  --read-only --tmpfs /tmp:rw,nosuid,nodev,mode=1777 \
  --env HOME=/tmp --stop-timeout=90 \
  --volume /etc/fractal-demo:/etc/fractal-demo:ro \
  --volume "$CLUSTER_DIR:/run/fractal-cluster:ro" \
  "$IMAGE" \
  --config /etc/fractal-demo/config.toml \
  --kubeconfig /run/fractal-cluster/kubeconfig \
  --helm-chart /opt/fractal/deploy/helm/fractal-demo \
  --helm-values /etc/fractal-demo/values.local.json
podman logs fractal-gateway
curl --fail http://127.0.0.1:8081/healthz
curl --fail http://127.0.0.1:8081/api/workers
```

Host networking uses the configured addresses directly. No published ports or libvirt socket are needed.
The root override reads root-owned, mode-0600 kubeconfigs without changing the provisioner.
The image defaults to UID/GID 10001. To use that default, omit `--user` and grant that identity read and directory traversal access.
Those permissions must survive every credential replacement. Non-root execution reduces host-file access if the application is compromised.

On SELinux hosts, the mounted directories need labels that allow container access and remain compatible with the provisioner.
Do not relabel a broad system directory to make this mount work.

Restart the container after editing TOML. Replaced values are visible to later Helm commands, but an installed release remains unchanged.
Stop the gateway before manual Helm maintenance:

```sh
podman stop fractal-gateway
```

To upgrade or roll back the gateway, stop it and repeat the run command with the desired image digest.
Changing the gateway image does not roll back an installed worker release.

### Running with Quadlet

Use Podman with Quadlet support and cgroup v2. The unit uses the same runtime settings as the command above.
Install the unit from the repository:

```sh
install -d /etc/containers/systemd
install -m 0644 deploy/quadlet/fractal-gateway.container /etc/containers/systemd/
$EDITOR /etc/containers/systemd/fractal-gateway.container
```

Set `Image=` to the gateway reference and change the credential volume source to the provisioner's directory.
Adjust the kubeconfig filename in `Exec=` if needed. Stop a directly started gateway before starting the service.

```sh
systemctl daemon-reload
systemctl start fractal-gateway.service
systemctl status fractal-gateway.service
journalctl -u fractal-gateway.service
```

The `[Install]` section starts the service at boot. Do not run `systemctl enable` on the generated service.
The unit retries startup if the configured bridge is not present yet.
The host must create that bridge independently of the gateway.

Use `systemctl stop fractal-gateway.service` before manual Helm maintenance.
Use `systemctl restart fractal-gateway.service` after changing TOML.
After editing the Quadlet, run `systemctl daemon-reload` and restart the service.
For an image upgrade or rollback, pull the selected reference and update `Image=` before reloading and restarting.

If generation fails, inspect the generator output:

```sh
QUADLET_UNIT_DIRS=/etc/containers/systemd \
  /usr/lib/systemd/system-generators/podman-system-generator --dryrun
```

See [podman-systemd.unit(5)](https://docs.podman.io/en/latest/markdown/podman-systemd.unit.5.html) for Quadlet details.

## Troubleshooting

| Symptom | Checks |
| --- | --- |
| No workers in `/api/workers` | Check worker logs, `GATEWAY_URL`, gateway bind addresses, and allowed `node_networks`. Verify pod-to-host connectivity. |
| Workers register but tiles fail | Check gateway logs and host-to-node connectivity on the configured `worker_port`. Inspect the browser status message. |
| Local workers fail or requests reach the gateway itself | Match each `[local_worker_ports]` entry to the worker's `--port`. Restart the gateway after editing configuration. |
| Pods stay Pending | Check node architecture, control-plane labels, available resources, and host-port conflicts with `kubectl describe pod`. |
| `ImagePullBackOff` | Check the image digest, registry access, package visibility, and the namespace's image-pull secret. |
| TOML changes appear ineffective | Restart the gateway, reload the page, and click **Reset settings** to clear browser overrides. |
| Healthy work times out | Review the view, grid, iterations, and CPU contention. Allow the gateway deadline to expire before the browser render deadline. |
| Automatic deployment waits or reports failure | Check the gateway log, current kubeconfig, selected context, registry access, and saved values. |
| Helm release remains pending or uninstalling | Stop the recovery-enabled gateway and inspect the release with Helm. Repair or remove it manually before restarting recovery. |

For pod events:

```sh
kubectl --context "$CONTEXT" -n fractal-demo describe pod REPLACE_WITH_POD_NAME
```

## Restoring and rolling back

Cluster reconstruction removes workload definitions and image-pull secrets. Keep the host gateway and browser running during reconstruction.

With [automatic kiosk deployment](#enabling-automatic-kiosk-deployment), the gateway reinstalls workers after the provisioner updates the kubeconfig.
Restore private-image credentials separately when needed. Confirm that workers register and rendering resumes.
Without automatic deployment, follow these steps:

1. Obtain the restored cluster's kubeconfig and select its context.
2. Check node addresses against the gateway's allowed networks and verify connectivity in both directions.
3. Reapply the namespace and required image-pull credentials.
4. [Preview the saved values](#previewing-worker-manifests), then run the [install-only command](#installing-or-upgrading-workers) with the same overrides.
5. Verify registration and completion of the retained frame.

Normal restoration reuses `deploy/values.local.json` without invoking the helper. Changed worker addresses
alone do not require new Helm values if the gateway URL and configured ports stay the same. Update the
allowed networks in TOML and restart the gateway if needed.

If the advertised gateway URL or a configured port changed, update TOML, restart the gateway, and repeat
[values generation](#generating-worker-values) with the saved image reference. Then preview and install the
new values. The JSON file stores the resolved URL, worker port, and optional image reference, not the
original discovery arguments.

Before manual rollback, stop the recovery-enabled gateway. Set `image` to the previous digest and upgrade the release with the saved worker settings.
Rolling back Helm values that contain `latest` does not restore a previous image. Restore the matching host
gateway version and configuration when needed, then restart the gateway and reload the browser. A DaemonSet
rollback does not restore the host gateway. Verify registration and rendering after either operation.

## Cleaning up

Stop a recovery-enabled gateway before removing workers. Otherwise, it reinstalls the missing release.
Remove the workers:

```sh
helm uninstall fractal-demo --kube-context "$CONTEXT" --namespace fractal-demo
```

To also remove the namespace and its image-pull secrets:

```sh
kubectl --context "$CONTEXT" delete -f deploy/namespace.yaml
```

Stop the host gateway with Ctrl+C.
