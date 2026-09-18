# Deployment

Run the gateway on the demo PC and one worker per eligible Kubernetes node.
The image uses `registry.opensuse.org/opensuse/bci/python:3.14`.
The publication workflow and DaemonSet target Linux amd64 workers.

## Building locally

Use Podman or Docker. Run these commands from the repository root:

```sh
# Set ENGINE=docker to use Docker.
ENGINE=podman
"$ENGINE" build -f Containerfile -t fractal-worker:local .
"$ENGINE" run --rm fractal-worker:local --help
"$ENGINE" run --rm -i --entrypoint /opt/fractal/.venv/bin/python \
  fractal-worker:local - < scripts/smoke_image.py
```

The build installs the distribution's uv package and uses frozen runtime dependencies.
The runtime image contains the installed virtual environment and runs `fractal-worker` as user and group ID 10001.
[.dockerignore](../.dockerignore) restricts the build context to the required files.

The smoke check starts a gateway and worker inside the container. It verifies registration, pixels,
worker identity, packaged assets, shutdown, and exclusion of development packages. No published ports are needed.

## Publishing images

[Lint and format checks](../.github/workflows/lint.yml) and
[Python tests and Helm rendering checks](../.github/workflows/publish-image.yml) run on pushes and pull requests.
On the default branch, successful tests in the publish-image workflow allow image building, smoke testing,
and publication to GitHub Container Registry (GHCR).
The lint and format workflow runs separately and does not gate publication.
Manual workflow dispatch also publishes from the default branch.

The workflow derives the image name from the lowercase repository owner and name:

```text
ghcr.io/<owner>/<repository>:sha-<full-commit-sha>
ghcr.io/<owner>/<repository>:latest
```

Publication uses `GITHUB_TOKEN` with `packages: write` permission. The job summary and
`worker-image-reference` artifact contain the immutable reference:

```text
ghcr.io/<owner>/<repository>@sha256:<digest>
```

The Helm chart defaults to `ghcr.io/dcermak/fractal-demo:latest`. Override `image` when publishing from another repository or deploying a digest.
Retain a working digest and its configuration for reproducible rollback.
For anonymous pulls, make the GHCR package public. Private images require an image-pull secret.
New cluster nodes need registry access to pull images.

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

If the block fails, stop and correct the error before continuing.
An existing values file remains unchanged; it must not be mistaken for newly generated values.
If no saved file existed, failure leaves none.

### Previewing worker manifests

For a private image, append `--set 'imagePullSecrets[0].name=ghcr-pull'` to both the preview and installation commands.
Apply any other Helm overrides consistently to both commands too.

Preview the manifest locally:

```sh
helm template fractal-demo deploy/helm/fractal-demo \
  --namespace fractal-demo -f deploy/values.local.json
```

Stop if the preview fails. Check the rendered image reference, gateway URL, and worker port before installing.
Keep the saved values and Helm overrides unchanged between preview and installation.

The chart sets `WORKER_PORT`, `containerPort`, and `hostPort` from the same value.
After changing either port in TOML, restart the gateway and repeat [values generation](#generating-worker-values), preview, and installation.
Include the intended image reference and still-applicable options whenever you regenerate values.

The DaemonSet excludes nodes labeled `node-role.kubernetes.io/control-plane` or `node-role.kubernetes.io/master`.
The Downward API supplies node name and host IP. Workers do not mount a service-account token.

Defaults request and limit CPU to one core, request 128 MiB of memory, and limit memory to 256 MiB.
Termination grace is 30 s. Validate these settings with the intended workload under the deployed resource limits.
Health probes check process responsiveness; readiness alone does not confirm registration or rendering.

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

If Helm fails, stop and inspect the error before continuing. Preserving the values file does not roll back a failed upgrade.
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

For pod events:

```sh
kubectl --context "$CONTEXT" -n fractal-demo describe pod REPLACE_WITH_POD_NAME
```

## Restoring and rolling back

Cluster reconstruction removes workload definitions and image-pull secrets. Keep the host gateway and browser running during reconstruction.

1. Obtain the restored cluster's kubeconfig and select its context.
2. Check node addresses against the gateway's allowed networks and verify connectivity in both directions.
3. Reapply the namespace and required image-pull credentials.
4. [Preview the saved values](#previewing-worker-manifests), then run the [install-only command](#installing-or-upgrading-workers) with the same overrides.
5. Verify registration and completion of the retained frame.

Normal restoration reuses `deploy/values.local.json` without invoking the helper.
Changed worker addresses alone do not require new Helm values if the gateway URL and configured ports remain unchanged.
Update the allowed networks in TOML and restart the gateway if needed.

If the advertised gateway URL or configured ports changed, update TOML and restart the gateway first.
Repeat [values generation](#generating-worker-values) with the saved image reference and relevant options, then preview and install the new values.
The JSON file stores the resolved URL, worker port, and optional image reference, not the original discovery arguments.

For rollback, set `image` to the previous digest and upgrade the release with the saved worker settings.
Rolling back Helm values containing `latest` does not restore a previous image.
Restore the matching host gateway version and configuration when needed. Restart the gateway and reload the browser after gateway changes.
A DaemonSet rollback does not restore the host gateway. Verify registration and rendering after either operation.

## Cleaning up

Remove the workers:

```sh
helm uninstall fractal-demo --kube-context "$CONTEXT" --namespace fractal-demo
```

To also remove the namespace and its image-pull secrets:

```sh
kubectl --context "$CONTEXT" delete -f deploy/namespace.yaml
```

Stop the host gateway with Ctrl+C.
