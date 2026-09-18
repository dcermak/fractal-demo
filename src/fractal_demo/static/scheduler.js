const clock = () => performance.now();
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function parseRoster(data) {
  if (!data || !Array.isArray(data.workers)) throw new Error("Invalid worker roster");
  const processes = new Set();
  const nodes = new Set();
  for (const worker of data.workers) {
    if (!worker || typeof worker.node !== "string" || !worker.node ||
        !UUID.test(worker.process_id) ||
        worker.render_url !== `/api/render/${worker.process_id}` ||
        processes.has(worker.process_id) || nodes.has(worker.node)) {
      throw new Error("Invalid worker identity in roster");
    }
    processes.add(worker.process_id);
    nodes.add(worker.node);
  }
  return data.workers;
}

function makeTiles({ width, height, columns, rows }) {
  const tiles = [];
  for (let row = 0; row < rows; row++) {
    for (let column = 0; column < columns; column++) {
      tiles.push({
        index: tiles.length, px: column * width / columns, py: row * height / rows,
        width: width / columns, height: height / rows,
        token: null, state: "pending", node: null, readyAt: 0, faults: 0, pulseUntil: 0,
      });
    }
  }
  return tiles;
}

export class Scheduler {
  constructor(config, hooks) {
    this.config = config;
    this.hooks = hooks;
    this.timing = config.timing;
    // The default is five. Tune only after checking discovery progress in the booth browser.
    this.limit = config.render_limit;
    this.palette = config.default_palette;
    this.tiles = makeTiles(config.geometry);
    this.active = new Map();
    this.workers = new Map();
    this.cooldowns = new Map();
    this.arrivals = new Map();
    this.decodeBlocked = new Set();
    this.decodes = new Map();
    this.nextToken = 0;
    this.frame = 0;
    this.completed = 0;
    this.completedAt = null;
    this.completedRAFFrame = null;
    this.paintTimes = [];
    this.tileCursor = 0;
    this.workerCursor = "";
    this.sharedCooldown = 0;
    this.error = "";
    this.discoveryOK = false;
    this.discoveryMessage = "Discovering workers…";
    this.discoveryEpoch = 0;
    this.lastPollAt = -Infinity;
    this.nextPollAt = 0;
    this.pollJob = null;
    this.hidden = document.hidden;
    this.stopped = false;
    this.visibilityListener = () => this.visibilityChanged();
    document.addEventListener("visibilitychange", this.visibilityListener);
  }

  start() {
    this.restart(this.palette);
    this.interval = setInterval(() => this.tick(), Math.min(100, this.timing.poll_interval_ms));
  }

  applySettings(config) {
    // The caller validates first. Install both grids before restart can dispatch or
    // invoke change hooks. Old jobs keep their old tile objects and request credits.
    const gridChanged = Object.keys(config.geometry).some(key =>
      config.geometry[key] !== this.config.geometry[key]);
    const viewChanged = Object.keys(config.view).some(key =>
      config.view[key] !== this.config.view[key]);
    const timingChanged = Object.keys(config.timing).some(key =>
      config.timing[key] !== this.timing[key]);
    if (!gridChanged && !viewChanged && !timingChanged) return;
    const tiles = gridChanged ? makeTiles(config.geometry) : this.tiles;
    if (gridChanged) this.hooks.grid(config.geometry);
    this.config = config;
    this.timing = config.timing;
    this.tiles = tiles;
    if (gridChanged || viewChanged) {
      this.tileCursor = 0;
      this.restart();
    } else {
      // Existing LOST deadlines remain unchanged; dwell uses the current duration.
      this.tick();
    }
  }

  restart(palette = this.palette) {
    this.palette = palette;
    this.frame++;
    this.error = "";
    this.completed = 0;
    this.completedAt = null;
    this.completedRAFFrame = null;
    // Superseding tokens does NOT release request credits or decoder barriers.
    for (const tile of this.tiles) {
      Object.assign(tile, { token: null, state: "pending", node: null, readyAt: 0, faults: 0, pulseUntil: 0 });
    }
    this.hooks.clear();
    this.tick();
  }

  owns(job) {
    return !this.stopped && this.active.get(job.token) === job &&
      job.frame === this.frame && job.tile.token === job.token;
  }

  invalidate(job, lost = false) {
    if (!this.owns(job)) return;
    const tile = job.tile;
    tile.token = null;
    tile.state = lost && !this.hidden ? "lost" : "pending";
    tile.readyAt = tile.state === "lost" ? clock() + this.timing.lost_ms : 0;
    tile.node = tile.state === "lost" ? job.worker.node : null;
    tile.pulseUntil = 0;
  }

  release(job) {
    if (this.active.get(job.token) !== job) return;
    clearTimeout(job.timer);
    this.active.delete(job.token);
  }

  pause(message) {
    this.error = message;
    for (const job of this.active.values()) this.invalidate(job);
  }

  refresh(message = "Refreshing worker discovery…") {
    this.discoveryOK = false;
    this.discoveryMessage = message;
    // A poll started before this failure cannot certify recovery from it.
    this.discoveryEpoch++;
    this.nextPollAt = 0;
  }

  currentDiscovery(now) {
    return this.discoveryOK && now - this.lastPollAt <=
      this.timing.poll_interval_ms + this.timing.poll_timeout_ms;
  }

  visibilityChanged() {
    this.hidden = document.hidden;
    this.refresh(this.hidden ? "Paused while this window is hidden." : "Refreshing workers before resuming…");
    if (this.hidden) {
      // A finished image gets its full visible dwell after returning to this window.
      this.completedAt = null;
      // Results queued while hidden must not replay old contact-loss indications on return.
      // Abort transport, but let its completion/deadline release the request credit.
      for (const job of this.active.values()) {
        this.invalidate(job);
        job.controller.abort();
      }
      for (const tile of this.tiles) {
        if (tile.state === "lost") {
          tile.state = "pending";
          tile.node = null;
          tile.readyAt = 0;
        }
      }
    }
    this.tick();
  }

  async poll() {
    if (this.pollJob || this.hidden || this.stopped) return;
    let response = null;
    const job = { controller: new AbortController(), epoch: this.discoveryEpoch,
      deadline: clock() + this.timing.poll_timeout_ms };
    this.pollJob = job;
    const finish = () => {
      if (this.pollJob !== job) return false;
      clearTimeout(job.timer);
      this.pollJob = null;
      this.nextPollAt = job.epoch === this.discoveryEpoch ? clock() + this.timing.poll_interval_ms : 0;
      return true;
    };
    job.expire = () => {
      if (!finish()) return;
      job.controller.abort();
      this.discoveryOK = false;
      this.discoveryMessage = "Gateway discovery timed out. Retrying…";
      this.hooks.change(this);
    };
    job.timer = setTimeout(job.expire, this.timing.poll_timeout_ms);
    try {
      response = await fetch("/api/workers", { cache: "no-store", signal: job.controller.signal });
      if (!response.ok) throw new Error("Roster request failed");
      const workers = parseRoster(await response.json());
      if (clock() >= job.deadline) { job.expire(); return; }
      if (this.pollJob !== job || this.hidden || job.epoch !== this.discoveryEpoch || this.stopped) return;
      this.reconcile(workers);
      this.discoveryOK = true;
      this.lastPollAt = clock();
      this.discoveryMessage = "";
    } catch (error) {
      if (this.pollJob === job && job.epoch === this.discoveryEpoch && !this.stopped) {
        this.discoveryOK = false;
        this.discoveryMessage = "Gateway discovery unavailable. Retrying…";
      }
    } finally {
      if (response?.body && !response.bodyUsed) {
        try { await response.body.cancel(); } catch (error) { /* Already aborted. */ }
      }
      finish();
      this.tick();
    }
  }

  reconcile(roster) {
    const next = new Map(roster.map(worker => [worker.process_id, worker]));
    const byNode = new Map(roster.map(worker => [worker.node, worker]));
    const now = clock();
    // Check all unfinished assignments, not only identities removed from the last
    // roster. Even an omission followed by same-process registration revokes ownership.
    for (const job of this.active.values()) {
      const current = next.get(job.worker.process_id);
      if (!current || current.node !== job.worker.node) {
        const replaced = byNode.has(job.worker.node);
        this.invalidate(job, !replaced);
        job.controller.abort();
      }
    }
    for (const worker of roster) {
      if (!this.workers.has(worker.process_id)) this.arrivals.set(worker.process_id, now + 1500);
    }
    this.workers = next;
  }

  availableWorkers(now) {
    const occupied = new Set([...this.active.values()].map(job => job.worker.process_id));
    return [...this.workers.values()].filter(worker =>
      !occupied.has(worker.process_id) && (this.cooldowns.get(worker.process_id) || 0) <= now
    ).sort((a, b) => this.workerKey(a).localeCompare(this.workerKey(b), "en"));
  }

  workerKey(worker) { return `${worker.node}/${worker.process_id}`; }

  chooseWorker(workers) {
    // Cursor persists through refreshes and frames; workers beyond the limit get turns.
    const worker = workers.find(item => this.workerKey(item).localeCompare(this.workerCursor, "en") > 0) || workers[0];
    this.workerCursor = this.workerKey(worker);
    return worker;
  }

  nextTile(now) {
    for (let offset = 0; offset < this.tiles.length; offset++) {
      const index = (this.tileCursor + offset) % this.tiles.length;
      const tile = this.tiles[index];
      if (tile.state === "pending" && tile.readyAt <= now) {
        this.tileCursor = (index + 1) % this.tiles.length;
        return tile;
      }
    }
    return null;
  }

  canDispatch(now) {
    return !this.stopped && !this.hidden && !this.error && this.currentDiscovery(now) &&
      this.sharedCooldown <= now && this.decodeBlocked.size === 0 && this.active.size < this.limit;
  }

  beginDwell() {
    if (this.hidden || this.completedAt !== null || this.completedRAFFrame === this.frame) return;
    const frame = this.frame;
    this.completedRAFFrame = frame;
    // Two callbacks give the completed image a rendering opportunity before dwell.
    requestAnimationFrame(() => requestAnimationFrame(() => {
      if (this.completedRAFFrame === frame) this.completedRAFFrame = null;
      if (!this.stopped && !this.hidden && this.frame === frame) {
        this.completedAt = clock();
        this.tick();
      }
    }));
  }

  tick() {
    if (this.stopped) return;
    const now = clock();
    // Timers can be throttled in hidden windows. Expire ownership before accepting
    // any newly available capacity; elapsed browser deadlines never imply LOST.
    for (const job of this.active.values()) if (now >= job.deadline) this.expire(job);
    if (this.pollJob && now >= this.pollJob.deadline) this.pollJob.expire();
    for (const tile of this.tiles) {
      if (tile.state === "lost" && now >= tile.readyAt) {
        tile.state = "pending";
        tile.node = null;
      }
    }
    for (const [id, until] of this.cooldowns) if (now >= until) this.cooldowns.delete(id);
    for (const [id, until] of this.arrivals) if (now >= until) this.arrivals.delete(id);
    while (this.paintTimes.length && this.paintTimes[0] <= now - this.timing.rate_window_ms) this.paintTimes.shift();
    if (!this.hidden && !this.pollJob && now >= this.nextPollAt) void this.poll();
    if (this.completed === this.tiles.length) this.beginDwell();
    if (this.completed === this.tiles.length && this.completedAt !== null &&
        now >= this.completedAt + this.timing.dwell_ms && this.canDispatch(now) &&
        this.availableWorkers(now).length) {
      this.restart();
      return;
    }
    while (this.canDispatch(now)) {
      const workers = this.availableWorkers(now);
      if (!workers.length) break;
      const tile = this.nextTile(now);
      if (!tile) break;
      this.dispatch(tile, this.chooseWorker(workers));
    }
    this.hooks.change(this);
  }

  dispatch(tile, worker) {
    const token = ++this.nextToken;
    const job = { token, tile, worker, frame: this.frame, controller: new AbortController(),
      stage: "transport", deadline: clock() + this.timing.render_timeout_ms };
    tile.token = token;
    tile.state = "active";
    tile.node = worker.node;
    tile.pulseUntil = this.arrivals.get(worker.process_id) || 0;
    this.active.set(token, job);
    job.timer = setTimeout(() => { this.expire(job); this.tick(); }, this.timing.render_timeout_ms);
    void this.render(job);
  }

  expire(job) {
    if (this.active.get(job.token) !== job) return;
    // Revocation precedes credit release, including during a stalled bitmap decode.
    this.invalidate(job);
    if (job.stage === "decode") {
      // Decoding cannot be aborted. Hold a separate barrier until its promise settles,
      // even if RENDER is pressed, so retries cannot accumulate unresolved decodes.
      this.decodeBlocked.add(job.token);
      this.pause("Image decoding timed out. Press RENDER to restart.");
    } else {
      this.refresh("Render connection timed out. Refreshing gateway discovery…");
    }
    job.controller.abort();
    this.release(job);
  }

  live(job) {
    if (clock() >= job.deadline) this.expire(job);
    return this.owns(job);
  }

  contentFault(job) {
    if (!this.owns(job)) return;
    job.tile.faults++;
    this.invalidate(job);
    this.cooldowns.set(job.worker.process_id, clock() + this.timing.cooldown_ms);
    if (job.tile.faults > 1) {
      this.pause(`Tile ${job.tile.index + 1} failed image or identity validation twice. Check worker output, then press RENDER.`);
    }
  }

  handleError(job, response, body) {
    if (!this.owns(job)) return;
    if (!body || !["worker", "gateway"].includes(body.scope) || typeof body.message !== "string") {
      this.contentFault(job);
      return;
    }
    const code = body.code;
    if (response.status === 503 && code === "busy") {
      this.invalidate(job);
      const until = clock() + this.timing.cooldown_ms;
      if (body.scope === "gateway") this.sharedCooldown = until;
      else this.cooldowns.set(job.worker.process_id, until);
    } else if ((response.status === 409 && code === "incarnation_mismatch") ||
               (response.status === 404 && code === "unknown_worker")) {
      this.invalidate(job);
      // Discovery can return the same identity again (for example, with a wrong local
      // port mapping). A successful poll must not immediately retry that process.
      this.cooldowns.set(job.worker.process_id, clock() + this.timing.cooldown_ms);
      this.refresh("Worker registration changed. Refreshing discovery…");
    } else if (response.status === 422 && code === "invalid_request") {
      this.pause(`Render request rejected: ${body.message.slice(0, 240)}. Check Settings or use Reset settings, then press RENDER.`);
    } else if ((response.status === 502 && code === "upstream_failure") ||
               (response.status === 504 && code === "upstream_timeout")) {
      this.invalidate(job, true);
      this.cooldowns.set(job.worker.process_id, clock() + this.timing.cooldown_ms);
    } else {
      // upstream_protocol includes identity faults; never turn these into contact loss.
      this.contentFault(job);
    }
  }

  async render(job) {
    let bitmap = null;
    let response = null;
    try {
      const { xmin, ymin, pixel_size, iterations } = this.config.view;
      const { px, py, width, height } = job.tile;
      const query = new URLSearchParams({ xmin, ymin, pixel_size, px, py, width, height,
        iterations, palette: this.palette, expected_process_id: job.worker.process_id });
      response = await fetch(`${job.worker.render_url}?${query}`, {
        cache: "no-store", signal: job.controller.signal,
      });
      if (!this.live(job)) return;
      if (!response.ok) {
        let body;
        try { body = await response.json(); }
        catch (error) {
          if (error instanceof SyntaxError) {
            if (this.live(job)) this.contentFault(job);
          } else {
            throw error;
          }
          return;
        }
        if (this.live(job)) this.handleError(job, response, body);
        return;
      }
      if (response.headers.get("X-Worker-ID") !== job.worker.process_id ||
          response.headers.get("X-Render-Node") !== job.worker.node ||
          response.headers.get("Content-Type")?.split(";", 1)[0].trim().toLowerCase() !== "image/png") {
        this.contentFault(job);
        return;
      }
      const blob = await response.blob();
      if (!this.live(job)) return;
      const signature = new Uint8Array(await blob.slice(0, 8).arrayBuffer());
      if (!this.live(job)) return;
      if (signature.length !== 8 || ![137, 80, 78, 71, 13, 10, 26, 10].every((byte, index) => signature[index] === byte)) {
        this.contentFault(job);
        return;
      }
      job.stage = "decode";
      this.decodes.set(job.token, job);
      try { bitmap = await createImageBitmap(blob); }
      catch (error) {
        if (this.live(job)) this.contentFault(job);
        return;
      }
      if (!this.live(job)) return;
      if (bitmap.width !== width || bitmap.height !== height) {
        this.contentFault(job);
        return;
      }
      // No asynchronous gap between this ownership check and the integer-coordinate paint.
      try { this.hooks.paint(bitmap, job.tile); }
      catch (error) {
        this.pause("The canvas could not paint a tile. Press RENDER to retry.");
        return;
      }
      job.tile.state = "done";
      job.tile.token = null;
      this.completed++;
      this.paintTimes.push(clock());
      if (this.completed === this.tiles.length) this.beginDwell();
    } catch (error) {
      if (this.live(job)) {
        this.invalidate(job);
        this.refresh("Render connection unavailable. Refreshing gateway discovery…");
      }
    } finally {
      if (bitmap) bitmap.close();
      this.decodes.delete(job.token);
      this.decodeBlocked.delete(job.token);
      // Fetch resolves at headers. A stale or rejected response still owns transport
      // capacity until its unread body has been cancelled (or its deadline expires).
      if (response?.body && !response.bodyUsed) {
        try { await response.body.cancel(); } catch (error) { /* Already aborted. */ }
      }
      this.release(job);
      this.tick();
    }
  }

  status() {
    const now = clock();
    if (this.error) return { tone: "error", text: this.error };
    if (this.hidden) return { tone: "waiting", text: "Paused while this window is hidden." };
    if (this.decodeBlocked.size) return { tone: "waiting", text: "Waiting for the previous image decoder to settle…" };
    if (!this.currentDiscovery(now)) return { tone: "waiting", text: this.discoveryMessage || "Waiting for current worker discovery…" };
    if (!this.workers.size) return { tone: "waiting", text: "No workers registered. Keeping the image and waiting for workers…" };
    if (this.completed === this.tiles.length) return { tone: "running", text: "Frame complete. Keeping it visible until the next render can start." };
    if (this.sharedCooldown > now) return { tone: "waiting", text: "Gateway render capacity is busy. Retrying after a short cooldown…" };
    const lost = this.tiles.filter(tile => tile.state === "lost").length;
    if (lost) return { tone: "waiting", text: `Worker contact lost · ${lost} unfinished ${lost === 1 ? "tile" : "tiles"} waiting for reassignment.` };
    if ([...this.active.values()].some(job => this.owns(job))) return { tone: "running", text: "Rendering · Borders show which node computed each tile." };
    if (this.active.size) return { tone: "waiting", text: "Waiting for superseded requests to finish before assigning more work…" };
    return { tone: "waiting", text: "Workers are cooling down after a retry. Pending tiles will resume automatically…" };
  }

  stop() {
    this.stopped = true;
    clearInterval(this.interval);
    document.removeEventListener("visibilitychange", this.visibilityListener);
    if (this.pollJob) {
      clearTimeout(this.pollJob.timer);
      this.pollJob.controller.abort();
      this.pollJob = null;
    }
    for (const job of this.active.values()) {
      clearTimeout(job.timer);
      job.controller.abort();
    }
  }
}
