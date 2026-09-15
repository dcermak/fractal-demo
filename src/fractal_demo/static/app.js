import { Scheduler } from "/static/scheduler.js";

const $ = id => document.getElementById(id);
const canvas = $("fractal-canvas");
const context = canvas.getContext("2d", { alpha: false });
const overlay = $("tile-overlay");
const legend = $("worker-legend");
const renderButton = $("render-button");
const paletteSelect = $("palette-select");
const colors = [
  { name: "Sky", value: "#22a9df" },
  { name: "Violet", value: "#af83f5" },
  { name: "Emerald", value: "#29c898" },
  { name: "Amber", value: "#f7b842" },
  { name: "Rose", value: "#fb7296" },
  { name: "Cyan", value: "#57d9df" },
  { name: "Orange", value: "#ff965b" },
  { name: "Indigo", value: "#818cf8" },
];
const nodeStyles = new Map();
let tileElements = [];
let scheduler = null;
let loading = false;

function text(element, value) {
  if (element.textContent !== value) element.textContent = value;
}

function shortLabels(names) {
  const lengths = new Map(names.map(name => [name, 8]));
  const parts = new Map(names.map(name => {
    const uuid = name.match(/^(.*-)([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$/i);
    const split = name.lastIndexOf("-");
    return [name, uuid ? [uuid[1], uuid[2]] : [name.slice(0, split + 1), name.slice(split + 1)]];
  }));
  const shorten = name => {
    // Match the sibling dashboard: recognizable prefix plus eight identifier characters.
    const [prefix, identifier] = parts.get(name);
    return prefix ? prefix + identifier.slice(0, lengths.get(name)) : name;
  };
  let labels;
  let collision;
  do {
    labels = new Map(names.map(name => [name, shorten(name)]));
    const groups = new Map();
    for (const [name, label] of labels) {
      if (!groups.has(label)) groups.set(label, []);
      groups.get(label).push(name);
    }
    collision = false;
    for (const group of groups.values()) {
      if (group.length > 1) {
        collision = true;
        for (const name of group) lengths.set(name, lengths.get(name) + 1);
      }
    }
  } while (collision);
  return labels;
}

function updateLegend(state, now) {
  const references = new Map();
  const entry = node => {
    if (!references.has(node)) references.set(node, { registered: false, painted: 0, requests: 0, arrival: 0 });
    return references.get(node);
  };
  for (const worker of state.workers.values()) {
    const item = entry(worker.node);
    item.registered = true;
    item.arrival = Math.max(item.arrival, state.arrivals.get(worker.process_id) || 0);
  }
  // Outstanding stale requests retain their node colors and legend entries too.
  for (const job of state.active.values()) entry(job.worker.node).requests++;
  for (const job of state.decodes.values()) entry(job.worker.node);
  for (const tile of state.tiles) {
    if (tile.node) {
      const item = entry(tile.node);
      if (tile.state === "done") item.painted++;
    }
  }
  for (const [node, style] of nodeStyles) {
    if (!references.has(node)) {
      style.card.remove();
      nodeStyles.delete(node);
    }
  }
  const labels = shortLabels([...references.keys()]);
  for (const [node, item] of references) {
    if (!nodeStyles.has(node)) {
      const usage = colors.map(color => [...nodeStyles.values()].filter(style => style.color === color).length);
      const color = colors[usage.indexOf(Math.min(...usage))];
      const card = document.createElement("li");
      card.className = "worker-card";
      card.style.setProperty("--node-color", color.value);
      const name = document.createElement("span");
      name.className = "node-name";
      const detail = document.createElement("div");
      detail.className = "node-detail";
      const status = document.createElement("span");
      status.className = "node-status";
      const counts = document.createElement("span");
      counts.className = "node-counts";
      detail.append(status, counts);
      card.append(name, detail);
      legend.append(card);
      nodeStyles.set(node, { color, card, name, status, counts });
    }
    const style = nodeStyles.get(node);
    text(style.name, labels.get(node));
    style.name.title = node;
    const registration = item.registered ? (state.currentDiscovery(now) ? "Registered" : "Last seen registered") : "Contact lost";
    text(style.status, `${style.color.name} · ${registration}`);
    text(style.counts, `${item.painted} tiles in frame · ${item.requests} requests`);
    style.card.classList.toggle("new-worker", item.arrival > now);
  }
  $("pool-empty").hidden = references.size > 0;
  text($("pool-empty"), state.currentDiscovery(now) ?
    "No workers registered. New workers will appear here automatically." : "Waiting for current worker discovery.");
}

function update(state) {
  const now = performance.now();
  updateLegend(state, now);
  for (const tile of state.tiles) {
    const element = tileElements[tile.index];
    if (element.dataset.state !== tile.state) element.dataset.state = tile.state;
    const color = nodeStyles.get(tile.node)?.color.value || "transparent";
    if (element.style.getPropertyValue("--node-color") !== color) element.style.setProperty("--node-color", color);
    element.classList.toggle("new-worker", tile.pulseUntil > now);
  }
  const current = state.currentDiscovery(now);
  text($("worker-count"), `${state.workers.size}${current ? "" : " ?"}`);
  $("worker-count").title = current ? "Currently registered workers" : "Last known worker count; discovery is not current";
  text($("active-count"), `${state.active.size} / ${state.limit}`);
  text($("paint-rate"), (state.paintTimes.length * 1000 / state.timing.rate_window_ms).toFixed(1));
  $("paint-rate").title = `Successfully painted tiles over the last ${state.timing.rate_window_ms / 1000} seconds`;
  const percent = Math.floor(100 * state.completed / state.tiles.length);
  text($("progress-text"), `${state.completed} / ${state.tiles.length} · ${percent}%`);
  $("render-progress").max = state.tiles.length;
  $("render-progress").value = state.completed;
  text($("frame-text"), `Frame ${state.frame}`);
  const status = state.status();
  $("status").dataset.tone = status.tone;
  text($("status-text"), status.text);
  $("empty-hint").hidden = state.completed > 0 || state.active.size > 0;
  text($("empty-hint"), state.error ? "Rendering paused · See the message below" :
    !current ? "Waiting for gateway discovery…" : !state.workers.size ? "Waiting for workers…" : "Waiting for render capacity…");
}

async function initialize() {
  if (loading) return;
  loading = true;
  renderButton.disabled = true;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 5000);
  try {
    if (!context || typeof createImageBitmap !== "function") throw new Error("This browser needs Canvas 2D and createImageBitmap support.");
    const response = await fetch("/api/config", { cache: "no-store", signal: controller.signal });
    if (!response.ok) throw new Error(`Configuration request failed (HTTP ${response.status}).`);
    const config = await response.json();
    const { width, height, columns, rows } = config.geometry;
    // The gateway validates settings. Reject unusable geometry before allocating the UI.
    if (![width, height, columns, rows].every(value => Number.isInteger(value) && value > 0) ||
        width % columns || height % rows || !Array.isArray(config.palettes) ||
        !config.palettes.some(palette => palette.id === config.default_palette)) {
      throw new Error("Gateway returned unusable renderer configuration.");
    }
    canvas.width = width;
    canvas.height = height;
    $("viewport").style.aspectRatio = `${width} / ${height}`;
    overlay.style.gridTemplateColumns = `repeat(${columns}, minmax(0, 1fr))`;
    overlay.style.gridTemplateRows = `repeat(${rows}, minmax(0, 1fr))`;
    tileElements = Array.from({ length: columns * rows }, () => {
      const tile = document.createElement("div");
      tile.className = "tile";
      tile.dataset.state = "pending";
      return tile;
    });
    overlay.replaceChildren(...tileElements);
    paletteSelect.replaceChildren(...config.palettes.map(palette => {
      const option = document.createElement("option");
      option.value = palette.id;
      option.textContent = palette.label;
      return option;
    }));
    paletteSelect.value = config.default_palette;
    paletteSelect.disabled = false;
    text($("geometry-text"), `${width} × ${height} px · ${columns * rows} tiles · ${config.view.iterations} iterations`);
    scheduler = new Scheduler(config, {
      clear() { context.fillStyle = "#090f20"; context.fillRect(0, 0, canvas.width, canvas.height); },
      paint(bitmap, tile) { context.drawImage(bitmap, tile.px, tile.py); },
      change: update,
    });
    scheduler.start();
  } catch (error) {
    if (scheduler) { scheduler.stop(); scheduler = null; }
    $("status").dataset.tone = "error";
    text($("status-text"), `Could not start: ${error.name === "AbortError" ? "Gateway configuration timed out." : error.message} Press RENDER to retry.`);
    text($("empty-hint"), "Renderer unavailable · See the message below");
    $("empty-hint").hidden = false;
    paletteSelect.disabled = true;
  } finally {
    clearTimeout(timeout);
    controller.abort();
    loading = false;
    renderButton.disabled = false;
  }
}

renderButton.addEventListener("click", () => {
  if (scheduler) scheduler.restart(paletteSelect.value);
  else void initialize();
});
paletteSelect.addEventListener("change", () => scheduler?.restart(paletteSelect.value));
// BFCache restores must refresh discovery through visibility handling; leaving the
// page permanently needs no replacements or late paints.
window.addEventListener("pagehide", event => { if (!event.persisted) scheduler?.stop(); });
window.addEventListener("pageshow", event => { if (event.persisted) scheduler?.visibilityChanged(); });
void initialize();
