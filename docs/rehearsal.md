# Booth rehearsal record

## Current evidence

On 2026-09-15, the operator confirmed that local testing works after correcting the worker port mappings.
The reported setup uses the gateway on 8081 and two workers on 8082 and 8083. Detailed visual checks,
container execution, cluster deployment, and failure timing measurements remain unrecorded.

This file is a record template. Replace pending values with observed results and retain failed trials.
Deployment and restoration commands are in [deployment.md](deployment.md).

## Environment

| Item | Recorded value |
| --- | --- |
| Date and operator | Pending |
| Source commit | Pending |
| Demo-PC OS, CPU, and memory | Pending |
| Guest OS and k3s version | Pending |
| Worker architecture | Pending; prepared image and manifest target amd64 |
| Kubeconfig location and context | Pending; record the location, not credentials |
| Browser versions | Pending |
| Screen resolution, scaling, and split-pane dimensions | Pending |
| Gateway bind addresses and port | Pending |
| Worker registration origin | Pending |
| Allowed node networks and advertised node IPs | Pending |
| Worker image digest | Pending |
| Previous working image digest | None recorded |
| Registry visibility and cluster pull access | Pending |
| Control-plane and eligible worker counts | Pending |
| Gateway configuration and local manifest locations | Pending |

## Workload and timing settings

Record the values used for each trial. The candidates below are not measurements.

| Setting | Initial candidate | Selected value |
| --- | --- | --- |
| Raster and grid | 960 × 540, 16 × 9 | Pending |
| Tile dimensions | 60 × 60 | Pending |
| View | xmin=-0.9, ymin=0.08, pixel_size=0.00025 | Pending |
| Iterations and palette | 800, cyber | Pending |
| Compute slots per worker | One | Pending |
| CPU request / limit | 1 / 1 core | Pending |
| Memory request / limit | 128 / 256 MiB | Pending |
| Registration interval / deadline | 500 ms / 2 s | Pending |
| Registration expiry | 1.5 s | Pending |
| Roster poll interval / deadline | 500 ms / 2 s | Pending |
| Proxy connection / total deadline | 1 s / 10 s | Pending |
| Browser render deadline | 12 s | Pending |
| Worker and shared retry cooldown | 350 ms | Pending |
| Proxy / browser render limit | 5 / 5 | Pending |
| LOST display | 600 ms | Pending |
| Completed-frame dwell | 1,500 ms | Pending |
| Rolling rate window | 5 s | Pending |
| Termination grace | 30 s | Pending |

Record healthy tile times, control-request progress, memory use, and shutdown drain under CPU contention.
Include a 256 × 256 tile at the 10,000-iteration ceiling when measuring maximum admitted work.
Select deadlines that allow healthy computation and delivery of structured proxy errors before browser
timeouts. Check probe responsiveness under the selected resource limits.

## Joint checks

- [ ] Start the host gateway and browser with zero cluster workers.
- [ ] Verify both network directions and availability of node host port 8080.
- [ ] Deploy eligible nodes and verify registry pulls, registration, and attributed rendering.
- [ ] Verify exclusion of both control-plane label forms.
- [ ] Inspect seams, borders, legend labels, controls, and dwell in the intended split pane.
- [ ] Exercise RENDER, palette changes, worker replacement, and repeated render cycles.
- [ ] Hide and restore the page, then check discovery refresh and failure presentation.
- [ ] Complete a Chromium smoke check in addition to Firefox.
- [ ] Destroy an approved worker during unfinished computation and observe retry on survivors.
- [ ] Destroy approved control-plane nodes and record whether surviving worker networking continues.
- [ ] Remove every cluster node and confirm that the host page stays responsive and retains its image.
- [ ] Restore the cluster and workload, then finish pending tiles without restarting the host page.
- [ ] Exercise the recorded restoration and known-good image rollback procedures.
- [ ] Compare worker counts using the same view, workload, dwell, and resource settings.

Record the active context and explicit node targets before destructive trials. Node-management commands
remain operator-owned. Restore injected network faults even when a trial fails.

## Failure trials

Use one observer timeline, such as a video recording with a visible action marker. Record elapsed times
from that marker. Do not subtract monotonic timestamps from different processes or machines.

| Trial | Target and action | Workers before / after | Contact-loss indication | LOST onset | First retry | Recovery | Result and recording |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Pending | Pending | Pending | Unmeasured | Unmeasured | Unmeasured | Unmeasured | Pending |

The provisional target is 2.5 s from hard worker loss to the first visible contact-loss indication.
Measure LOST onset separately. Record missed targets and unsuccessful restoration attempts alongside
successful trials. Heartbeat age alone does not establish kill-to-visible latency.

## Worker-count comparison

| Trial | Eligible workers | Settings record | Frame computation time | Painted tiles / s | Host contention and observations |
| --- | --- | --- | --- | --- | --- |
| Pending | Pending | Pending | Unmeasured | Unmeasured | Pending |

Report the measured useful range. All VMs share the demo PC's physical resources, and the browser's
single-origin connection limit can restrict parallelism. Keep individual observations with the results.

## Restart and restoration notes

Record the commands and edited configuration used for successful startup and restoration. Keep the
previous working image digest and configuration together. Store registry credentials separately.

- Host gateway startup: pending.
- Cluster workload reapplication: pending.
- Known-good rollback: pending.
- Required network restoration: pending.
- Unresolved findings and next actions: pending.
