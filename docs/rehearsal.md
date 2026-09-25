# Pre-demo checklist

Use the intended kiosk hardware, browser, pane dimensions, and deployment configuration.
Keep the working image digest and local configuration available for [restoration](deployment.md#restoring-and-rolling-back).
Record failures with the settings and steps needed to reproduce them.

## Startup and controls

- [ ] Start the gateway with zero workers. The page stays responsive and waits for workers.
- [ ] Add workers. Each registered worker contributes tiles with matching node labels and borders.
- [ ] Check readability, tile alignment, controls, and colors in the intended split pane.
- [ ] Pan, zoom, change the palette, and apply grid and iteration changes during rendering.
      Each new frame uses the accepted settings, with no tiles from an earlier frame.
- [ ] Reload and reset settings. Saved values survive reload; reset restores the configured defaults.
- [ ] Adjust dwell and LOST duration. Check repeated frames and visible failure markers.
- [ ] Hide and restore the page. Discovery refreshes and rendering resumes.
- [ ] Run the checks in Firefox and Chromium.

## Failure and recovery

Select explicit targets in the disposable demo cluster before removing nodes.
Keep the host gateway running during node-loss checks.

### Demonstrating worker loss

1. Start at least two eligible workers and confirm that both contribute tiles.
2. Match one worker's node label in the compute pool to its disposable VM in the VM manager.
   Select a worker VM, not the gateway, VM-management host, or a control-plane node.
3. Keep the renderer visible beside the VM manager in separate windows or a split pane.
   Switching to another tab pauses rendering and invalidates unfinished assignments, which can hide the intended LOST moment.
4. Use the rehearsed view, iteration count, and LOST display duration. Wait for an active tile assigned to the selected worker.
   Tune these settings beforehand so unfinished work and failure markers are visible without healthy renders timing out.
5. Abruptly stop the selected worker VM using the VM manager's power-off or kill operation.
   Graceful Ctrl+C shutdown or draining can let admitted work finish instead.
6. Observe LOST tiles retrying on surviving workers while completed pixels remain visible.
   If the assignment finished before the stop, a LOST marker is not guaranteed. Repeat with another unfinished assignment after recovery.
7. Restore or replace the worker using the VM manager's procedure, then verify registration and new tile contribution.
   A kill operation may delete the VM or its disks. The returning worker's name, process identity, and color may differ.
   Confirm contribution before starting another failure demonstration.

### Checking recovery scenarios

- [ ] Remove a worker during unfinished work. LOST tiles retry on survivors; completed pixels remain visible.
- [ ] Remove all workers, then restore one. Pending work resumes without restarting the browser.
- [ ] Remove a control-plane node. Check whether the deployment's surviving worker networking continues.
- [ ] Stop and restart the host gateway. The page reports unavailable discovery and recovers after restart.
- [ ] Reconstruct the cluster and follow the [restoration procedure](deployment.md#restoring-and-rolling-back).
      Reapplied workers register and complete the retained frame.
- [ ] Restore the previous image and matching configuration. Verify registration and rendering.
- [ ] Confirm healthy renders, responsive probes, and orderly shutdown under the deployed CPU and memory limits.

### Checking automatic redeployment

- [ ] Start the gateway with `--kubeconfig` pointing to the provisioner's continuously updated file.
      Begin with the cluster absent. The gateway waits and the browser remains responsive.
- [ ] Provision the cluster and update the kubeconfig. Confirm automatic installation, worker registration, and rendering.
- [ ] Click **Redeploy workers**. Confirm that an installed release and the retained image stay unchanged.
- [ ] Remove all cluster VMs through the provisioning dashboard, then recreate the cluster.
      Confirm automatic deployment and resumed rendering without restarting the gateway or reloading the browser.
- [ ] Repeat cluster replacement while the renderer page is hidden. Deployment continues, and rendering resumes when the page becomes visible.
- [ ] Confirm that the locally served HTMX asset refreshes status and submits the button without external browser downloads.
- [ ] Stop the recovery-enabled gateway before manual release maintenance or cleanup.

### Checking container deployment

- [ ] Start the gateway with the documented Podman command. Confirm worker registration and rendering.
- [ ] Stop that container and start the Quadlet service. Check its journal and confirm rendering.
- [ ] Recreate the cluster. Confirm the mounted kubeconfig replacement restores workers without restarting the gateway.
- [ ] Reboot the demo host. Confirm the bridge, gateway service, and rendering return.

If a check fails, inspect the gateway and worker logs using the [troubleshooting guide](deployment.md#troubleshooting).
Restore any injected network faults. Repeat the failed check after correcting the cause.
