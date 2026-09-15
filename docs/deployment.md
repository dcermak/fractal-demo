# Container and Kubernetes deployment

## Preparation status

The Containerfile, GitHub Actions workflow, and Kubernetes manifests are prepared. Container builds,
publication, and deployment have not run in this session. The operator runs these steps.

The image uses `registry.opensuse.org/opensuse/bci/python:3.14`. The workflow builds `linux/amd64`, and
the DaemonSet selects matching Linux nodes. Confirm the booth worker architecture before deploying.

## Building locally

Choose Podman or Docker on the build machine. Run these commands from the repository root:

```sh
# Set ENGINE=docker if using Docker.
ENGINE=podman
"$ENGINE" build -f Containerfile -t fractal-worker:local .
"$ENGINE" run --rm fractal-worker:local --help
"$ENGINE" run --rm -i --entrypoint /opt/fractal/.venv/bin/python \
  fractal-worker:local - < scripts/smoke_image.py
```

The smoke check starts the installed gateway and worker inside the container. It checks registration,
identity, decoded pixels, packaged assets, and orderly shutdown. It also checks that development
packages are absent. It uses container loopback and needs no published ports or cluster access.

The build stage uses uv 0.12.3 with frozen runtime dependencies. The runtime stage contains the installed
virtual environment and BCI base. It runs as UID/GID 10001 and starts `fractal-worker` directly.
The `.containerignore` and `.dockerignore` files contain matching build-context filters.

If a build or smoke check fails, retain the command output for diagnosis. Do not change host security
settings or sandbox permissions to work around the failure.

## Publishing through GitHub Actions

Push the prepared files to the intended GitHub repository. The workflow derives the image repository
from its lowercase owner and repository name:

```text
ghcr.io/<owner>/<repository>:sha-<full-commit-sha>
```

The workflow runs Python checks on pushes and pull requests. On the default branch, successful checks
allow image building, smoke testing, and publication. Manual workflow dispatch also publishes when
run on the default branch.

The publication job uses `GITHUB_TOKEN` with `packages: write`. It adds source and revision labels to
associate the image with the repository. No registry password is stored in the repository.

After smoke testing, the workflow tags and pushes the same local image. The job summary and
`worker-image-reference` artifact contain the resulting immutable reference:

```text
ghcr.io/<owner>/<repository>@sha256:<digest>
```

Use that digest in the DaemonSet. The commit tag helps locate builds, but deployment uses the digest.
Keep the previous working digest and edited configuration before replacing an image.

GitHub Container Registry package visibility is a separate setting. For anonymous cluster pulls, make
the package public. For a private package, supply image-pull credentials in the demo namespace and
enable `imagePullSecrets` in the local manifest. Successful publication does not verify cluster pull
access. New nodes need registry access to pull the image.

## Configuring the host gateway

Choose a demo-PC interface reachable from the worker VMs. Confirm both network directions:

- Worker pods reach the demo-PC registration endpoint.
- The demo PC reaches each advertised node IP on host port 8080.

Host access to pod networks is not required. The cluster must support the DaemonSet's host-port mapping.
Keep registration on the trusted demo network, separate from the shared conference network.

Create a separate gateway configuration:

```sh
cp config.example.toml config.booth.toml
$EDITOR config.booth.toml
```

Replace the following placeholders with the actual addresses. Port 8081 matches the successful local
setup and remains a candidate for the booth. Match it in the worker manifest if you change it.

```toml
bind = ["127.0.0.1", "REPLACE_WITH_DEMO_PC_IP"]
port = 8081
node_networks = ["REPLACE_WITH_NODE_CIDR"]
worker_port = 8080
```

Remove the `[local_worker_ports]` table and its entries. Those development overrides take precedence
over `worker_port`. Leave the geometry, view, and timing sections for workload tuning during rehearsal.

Start the gateway on the demo PC before deploying workers:

```sh
uv sync --frozen --no-dev
"$PWD/.venv/bin/fractal-gateway" --config "$PWD/config.booth.toml"
```

Use a second terminal for subsequent commands. Open `http://127.0.0.1:8081/` in the browser. With no
workers, the page waits. Keep one active rendering browser window.

## Preparing the DaemonSet

Copy the manifest before editing deployment-specific values:

```sh
cp deploy/worker-daemonset.yaml deploy/worker-daemonset.local.yaml
$EDITOR deploy/worker-daemonset.local.yaml
```

Replace the image placeholder with the published digest. Replace `GATEWAY_URL` with the reachable
demo-PC HTTP origin and gateway port. The local manifest and `config.booth.toml` are ignored by Git.

The manifest runs one worker on each eligible node. Required affinity excludes nodes with either
control-plane label, even when those nodes have no taints. It reads `spec.nodeName` and `status.hostIP`
through the Downward API and does not mount a service-account token.

The initial CPU request and limit are one core. Memory request and limit are 128 MiB and 256 MiB.
Termination grace is 30 s. These are provisional settings, pending workload and drain measurements.

Health probes check process responsiveness independently of computation and registration. A Ready
pod does not establish successful registration or rendering. Verify both after deployment.

## Deploying to the demo cluster

Select the operator-supplied kubeconfig and intended context. Run the following commands only against
the disposable demo cluster:

```sh
export KUBECONFIG=/absolute/path/to/demo-kubeconfig
CONTEXT=REPLACE_WITH_DEMO_CONTEXT
kubectl --context "$CONTEXT" get nodes \
  -L kubernetes.io/arch,node-role.kubernetes.io/control-plane,node-role.kubernetes.io/master
kubectl --context "$CONTEXT" apply -f deploy/namespace.yaml
```

For a private image, create the `ghcr-pull` image-pull secret in namespace `fractal-demo` using
operator-managed credentials. Uncomment the corresponding `imagePullSecrets` section before applying
the DaemonSet. Do not commit credentials.

Apply the edited manifest:

```sh
kubectl --context "$CONTEXT" apply -f deploy/worker-daemonset.local.yaml
kubectl --context "$CONTEXT" -n fractal-demo rollout status daemonset/fractal-worker --timeout=120s
kubectl --context "$CONTEXT" -n fractal-demo get pods -o wide
kubectl --context "$CONTEXT" -n fractal-demo logs \
  -l app.kubernetes.io/name=fractal-worker --prefix --tail=30
curl --fail http://127.0.0.1:8081/api/workers
```

Confirm that eligible workers appear in the roster and contribute attributed tiles. Confirm that
control-plane nodes have no render worker. Record the result in [rehearsal.md](rehearsal.md).

## Restoring and rolling back

Full cluster reconstruction removes the workload definition and image-pull secrets. Keep the browser
and host gateway running while the operator restores the cluster.

1. Obtain current kubeconfig and select the restored demo context.
2. Recheck node addressing against the gateway's allowed networks.
3. Reapply this project's namespace and required image-pull credentials.
4. Reapply the saved local DaemonSet manifest with its recorded digest.
5. Verify registration and completion of the retained partial frame.

For rollback, restore the previous image digest in the local manifest and apply it again. Restore the
matching gateway configuration if its settings changed. Verify registration and rendering afterward.
On the first deployment, no previous working image exists. Remove the worker DaemonSet if that
deployment needs to be withdrawn.

## Cleaning up

Remove this application's workers:

```sh
kubectl --context "$CONTEXT" delete -f deploy/worker-daemonset.local.yaml
```

To remove its namespace and image-pull secrets too:

```sh
kubectl --context "$CONTEXT" delete -f deploy/namespace.yaml
```

Stop the host gateway with Ctrl+C. VM and cluster management remain outside this application.
