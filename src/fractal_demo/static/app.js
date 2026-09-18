import { Scheduler } from "/static/scheduler.js";

const $ = id => document.getElementById(id);
const canvas = $("fractal-canvas");
const context = canvas.getContext("2d", { alpha: false });
const overlay = $("tile-overlay");
const legend = $("worker-legend");
const renderButton = $("render-button");
const paletteSelect = $("palette-select");
const storageKey = "fractal-demo.ui-settings.v1";
const inputKeys = ["columns", "rows", "iterations", "dwell_ms", "lost_ms"];
const navigationIDs = ["pan-left", "pan-right", "pan-up", "pan-down", "zoom-in", "zoom-out"];
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
let defaults = null;
let settings = null;

function text(element, value) {
  if (element.textContent !== value) element.textContent = value;
}

function defaultSettings() {
  return {
    columns: defaults.geometry.columns, rows: defaults.geometry.rows,
    ...defaults.view, dwell_ms: defaults.timing.dwell_ms, lost_ms: defaults.timing.lost_ms,
  };
}

function gridError(columns, rows) {
  const { width, height } = defaults.geometry;
  if (![columns, rows].every(value => Number.isInteger(value) && value > 0) ||
      width % columns || height % rows) {
    return `Columns and rows must be positive integers dividing ${width} × ${height} evenly.`;
  }
  if (columns * rows > 4096) return "Use at most 4,096 tiles.";
  if (width / columns > 256 || height / rows > 256) return "Each tile must be at most 256 × 256 pixels.";
  return null;
}

function validateSettings(candidate) {
  const error = gridError(candidate.columns, candidate.rows);
  if (error) return error;
  if (!Number.isInteger(candidate.iterations) || candidate.iterations < 1 || candidate.iterations > 10000) {
    return "Iterations must be an integer from 1 through 10,000.";
  }
  for (const key of ["dwell_ms", "lost_ms"]) {
    if (!Number.isInteger(candidate[key]) || candidate[key] < 1 || candidate[key] > 300000) {
      return "Dwell and LOST durations must be whole milliseconds from 1 through 300,000.";
    }
  }
  const { xmin, ymin, pixel_size } = candidate;
  if (![xmin, ymin, pixel_size].every(Number.isFinite) || pixel_size < 1e-13 || pixel_size > 4) {
    return "Zoom scale must be a finite number from 1e-13 through 4.";
  }
  const { width, height } = defaults.geometry;
  if (![xmin, ymin, xmin + (width - 1) * pixel_size, ymin + (height - 1) * pixel_size]
    .every(value => value >= -4 && value <= 4)) {
    return "The view must stay within coordinates −4 through 4.";
  }
  return null;
}

function notice(id, message) {
  text($(id), message || "");
  $(id).hidden = !message;
}

function loadSettings() {
  const initial = defaultSettings();
  let saved;
  try { saved = localStorage.getItem(storageKey); }
  catch (error) {
    notice("storage-notice", "Browser storage is unavailable. Using configured defaults.");
    return initial;
  }
  if (saved === null) return initial;
  try {
    const data = JSON.parse(saved);
    if (!data || typeof data !== "object" || Array.isArray(data)) throw new Error("Invalid settings record");
    // Never merge arbitrary saved properties into the gateway configuration.
    const candidate = Object.fromEntries(Object.keys(initial).map(key => [key, data[key]]));
    if (validateSettings(candidate)) throw new Error("Invalid saved settings");
    return candidate;
  } catch (error) {
    notice("storage-notice", "Saved settings are invalid for this image. Using configured defaults; Reset settings clears the saved values.");
    return initial;
  }
}

function persistSettings(reset) {
  try {
    if (reset) localStorage.removeItem(storageKey);
    else localStorage.setItem(storageKey, JSON.stringify(settings));
    notice("storage-notice", "");
  } catch (error) {
    notice("storage-notice", reset ?
      "Defaults restored for this page, but saved settings could not be cleared. They may return after reload." :
      "Settings applied, but could not be saved in this browser. They may be lost after reload.");
  }
}

function rendererConfig(candidate) {
  return {
    ...defaults,
    geometry: { ...defaults.geometry, columns: candidate.columns, rows: candidate.rows },
    view: { xmin: candidate.xmin, ymin: candidate.ymin, pixel_size: candidate.pixel_size,
      iterations: candidate.iterations },
    timing: { ...defaults.timing, dwell_ms: candidate.dwell_ms, lost_ms: candidate.lost_ms },
  };
}

function setGrid({ columns, rows }) {
  const elements = Array.from({ length: columns * rows }, () => {
    const tile = document.createElement("div");
    tile.className = "tile";
    tile.dataset.state = "pending";
    return tile;
  });
  overlay.style.gridTemplateColumns = `repeat(${columns}, minmax(0, 1fr))`;
  overlay.style.gridTemplateRows = `repeat(${rows}, minmax(0, 1fr))`;
  overlay.replaceChildren(...elements);
  tileElements = elements;
}

function navigation(action) {
  const next = { ...settings };
  const { width, height } = defaults.geometry;
  if (action === "pan-left" || action === "pan-right") {
    next.xmin += (action === "pan-left" ? -1 : 1) * 0.25 * (width - 1) * settings.pixel_size;
  } else if (action === "pan-up" || action === "pan-down") {
    // Sampled y increases down the canvas, so moving the view up decreases ymin.
    next.ymin += (action === "pan-up" ? -1 : 1) * 0.25 * (height - 1) * settings.pixel_size;
  } else {
    next.pixel_size *= action === "zoom-in" ? 0.5 : 2;
    next.xmin += (width - 1) * (settings.pixel_size - next.pixel_size) / 2;
    next.ymin += (height - 1) * (settings.pixel_size - next.pixel_size) / 2;
  }
  return next;
}

function updateControls() {
  for (const id of navigationIDs) $(id).disabled = !!validateSettings(navigation(id));
  const { width, height } = defaults.geometry;
  const { columns, rows, iterations } = settings;
  text($("geometry-text"), `${width} × ${height} px · ${columns * rows} tiles (${width / columns} × ${height / rows} px) · ${iterations} iterations`);
}

function updateGridPreview() {
  const columns = $("setting-columns").valueAsNumber;
  const rows = $("setting-rows").valueAsNumber;
  const { width, height } = defaults.geometry;
  text($("grid-preview"), gridError(columns, rows) ||
    `${columns * rows} tiles · ${width / columns} × ${height / rows} pixels per tile · image ${width} × ${height}`);
}

function fillInputs() {
  for (const key of inputKeys) $("setting-" + key).value = settings[key];
  updateGridPreview();
}

function applySettings(candidate, reset = false) {
  const error = validateSettings(candidate);
  if (error) { notice("settings-error", error); return false; }
  scheduler.applySettings(rendererConfig(candidate));
  settings = { ...candidate };
  updateControls();
  persistSettings(reset);
  return true;
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
    if (![width, height, columns, rows].every(value => Number.isInteger(value) && value > 0 && value <= 8192) ||
        width % columns || height % rows || !Array.isArray(config.palettes) ||
        !config.palettes.some(palette => palette.id === config.default_palette)) {
      throw new Error("Gateway returned unusable renderer configuration.");
    }
    defaults = structuredClone(config);
    const error = validateSettings(defaultSettings());
    if (error) throw new Error(error);
    notice("storage-notice", "");
    settings = loadSettings();
    canvas.width = width;
    canvas.height = height;
    $("viewport").style.aspectRatio = `${width} / ${height}`;
    setGrid(settings);
    paletteSelect.replaceChildren(...config.palettes.map(palette => {
      const option = document.createElement("option");
      option.value = palette.id;
      option.textContent = palette.label;
      return option;
    }));
    paletteSelect.value = config.default_palette;
    paletteSelect.disabled = false;
    scheduler = new Scheduler(rendererConfig(settings), {
      clear() { context.fillStyle = "#090f20"; context.fillRect(0, 0, canvas.width, canvas.height); },
      paint(bitmap, tile) { context.drawImage(bitmap, tile.px, tile.py); },
      grid: setGrid,
      change: update,
    });
    fillInputs();
    updateControls();
    $("settings-fields").disabled = false;
    scheduler.start();
  } catch (error) {
    if (scheduler) { scheduler.stop(); scheduler = null; }
    $("status").dataset.tone = "error";
    text($("status-text"), `Could not start: ${error.name === "AbortError" ? "Gateway configuration timed out." : error.message} Press RENDER to retry.`);
    text($("empty-hint"), "Renderer unavailable · See the message below");
    $("empty-hint").hidden = false;
    paletteSelect.disabled = true;
    $("settings-fields").disabled = true;
    for (const id of navigationIDs) $(id).disabled = true;
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
for (const id of navigationIDs) {
  $(id).addEventListener("click", () => { if (scheduler) applySettings(navigation(id)); });
}
$("settings-form").addEventListener("submit", event => {
  event.preventDefault();
  if (!scheduler) return;
  const candidate = { ...settings };
  for (const key of inputKeys) candidate[key] = $("setting-" + key).valueAsNumber;
  if (applySettings(candidate)) {
    notice("settings-error", "");
    fillInputs();
  }
});
$("reset-settings").addEventListener("click", () => {
  if (scheduler && applySettings(defaultSettings(), true)) {
    notice("settings-error", "");
    fillInputs();
  }
});
for (const key of ["columns", "rows"]) {
  $("setting-" + key).addEventListener("input", () => { if (defaults) updateGridPreview(); });
}
// BFCache restores must refresh discovery through visibility handling; leaving the
// page permanently needs no replacements or late paints.
window.addEventListener("pagehide", event => { if (!event.persisted) scheduler?.stop(); });
window.addEventListener("pageshow", event => { if (event.persisted) scheduler?.visibilityChanged(); });
void initialize();
