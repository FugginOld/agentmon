/* agentmon web UI. Vanilla JS, no build step. All data reaches the DOM through
 * textContent / createElementNS (never innerHTML), so event text can't inject markup. */
"use strict";

// ------------------------------------------------------------------ constants
const SVGNS = "http://www.w3.org/2000/svg";
const C = { // keep in sync with :root in app.css (SVG markers need literal colours)
  text: "#E7EAF0", text2: "#A0A9B8", text3: "#8B95A6", text4: "#5E6A7E", line: "#2A3240",
  blue: "#6EA8FE", green: "#4CC38A", amber: "#F2A93B", red: "#F2706B", edge: "#5E6A7E",
  edgeHi: "#C3CAD5", pending: "#8C96A6", ink: "#0B0E13",
};
const G = { NODE_W: 192, NODE_H: 58, ROW_H: 88, LANE_W: 228, HEAD_H: 76, PAD: 16 };
G.HG = (G.ROW_H - G.NODE_H) / 2;      // half the free band between node rows (edge routing lane)
G.GH = (G.LANE_W - G.NODE_W) / 2;     // half the free gutter between node columns
const FILTERS = [["all", "All"], ["tools", "Tools"], ["messages", "Messages"], ["thinking", "Thinking"], ["errors", "Errors"]];
const TABS = ["swim", "dag", "seq"];

// ---------------------------------------------------------------------- state
const state = {
  snap: null, recvAt: 0,          // latest snapshot and performance.now() when it arrived
  tab: "swim", selected: null,    // explicit selection (step id) - otherwise the inspector follows snap.focus
  filter: "all", mode: "latest",  // stream mode: "default" (server's choice), "latest", or "run" (a specific run id)
  runId: null,
  runs: [], runsSig: "", es: null, graphSig: {}, eventsSig: "",
};
try { const t = localStorage.getItem("agentmon.tab"); if (TABS.includes(t)) state.tab = t; } catch (_) { /* storage may be blocked */ }
if (TABS.includes(location.hash.slice(1))) state.tab = location.hash.slice(1); // deep link: /#dag

// ------------------------------------------------------------------- helpers
const $ = (id) => document.getElementById(id);

function apply(node, props) {
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  return node;
}
function append(node, kids) {
  for (const k of kids.flat(Infinity)) {
    if (k == null || k === false) continue;
    node.append(k instanceof Node ? k : document.createTextNode(String(k)));
  }
  return node;
}
const h = (tag, props, ...kids) => append(apply(document.createElement(tag), props), kids);
const s = (tag, props, ...kids) => append(apply(document.createElementNS(SVGNS, tag), props), kids);

// Mirrors agentmon/fmt.py so both UIs read the same.
function fDur(sec) {
  if (sec == null || !isFinite(sec)) return "—";
  if (sec < 10) return sec.toFixed(1) + "s";
  if (sec < 60) return Math.round(sec) + "s";
  const m = Math.floor(sec / 60), r = Math.floor(sec % 60);
  if (m < 60) return `${m}:${String(r).padStart(2, "0")}`;
  return `${Math.floor(m / 60)}:${String(m % 60).padStart(2, "0")}:${String(r).padStart(2, "0")}`;
}
function fClock(sec) {
  if (sec == null) return "--:--";
  const t = Math.max(0, Math.floor(sec)), hh = Math.floor(t / 3600), mm = Math.floor((t % 3600) / 60), ss = t % 60;
  const p = (n) => String(n).padStart(2, "0");
  return hh ? `${hh}:${p(mm)}:${p(ss)}` : `${p(mm)}:${p(ss)}`;
}
function fTok(n) {
  if (!n) return "0";
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
  return String(Math.round(n));
}
const fMoney = (x) => (x == null ? "—" : "$" + x.toFixed(2));
function fHms(ts) {
  if (ts == null) return "--:--:--";
  const d = new Date(ts * 1000), p = (n) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
const clip = (text, n) => {
  const t = String(text == null ? "" : text).replace(/\s+/g, " ").trim();
  return t.length <= n ? t : t.slice(0, Math.max(1, n - 1)) + "…";
};

/** Server "now", advanced locally between snapshots while the run is live. */
function liveNow() {
  const snap = state.snap;
  if (!snap) return 0;
  const live = snap.run.status === "running";
  return snap.run.now + (live ? (performance.now() - state.recvAt) / 1000 : 0);
}
function stepDuration(step) {
  if (step.started == null) return null;
  if (step.status === "running") return Math.max(0, liveNow() - step.started);
  return step.duration;
}
const stepsById = () => new Map(state.snap.steps.map((st) => [st.id, st]));
const agentsById = () => new Map(state.snap.agents.map((a) => [a.id, a]));
const STATUS_COLOR = { done: C.green, running: C.amber, error: C.red, queued: C.text3 };

// ------------------------------------------------------------ connection / SSE
function connect() {
  if (state.es) state.es.close();
  const url = state.mode === "default" ? "/api/stream"
    : state.mode === "latest" ? "/api/stream?run=latest" : "/api/stream?run=" + encodeURIComponent(state.runId || "latest");
  const es = new EventSource(url);
  state.es = es;
  es.addEventListener("snapshot", (e) => {
    let snap;
    try { snap = JSON.parse(e.data); } catch (_) { return; }
    onSnapshot(snap);
  });
  es.addEventListener("empty", () => showEmpty(true));
  es.onopen = () => { $("conn").hidden = true; };
  es.onerror = () => { $("conn").hidden = false; }; // EventSource retries by itself
}

function showEmpty(on) {
  $("empty").hidden = !on;
  $("dash").hidden = on;
  $("status-pill").hidden = on;
  if (on) {
    state.snap = null;
    $("run-title").textContent = "Waiting for a run…";
    $("run-meta").textContent = "";
  }
}

async function refreshRuns() {
  try {
    const res = await fetch("/api/runs", { cache: "no-store" });
    if (!res.ok) return;
    const runs = await res.json();
    const sig = JSON.stringify(runs.map((r) => [r.id, r.title]));
    state.runs = runs;
    if (sig !== state.runsSig) { state.runsSig = sig; renderPicker(); }
  } catch (_) { /* server restarting; try again next tick */ }
}

function renderPicker() {
  const sel = $("run-picker");
  const cur = state.snap ? state.snap.run.id : null;
  const ids = state.runs.map((r) => r.id);
  const opts = state.runs.map((r) => h("option", { value: r.id, text: (r.title ? r.title + " · " : "") + r.id }));
  if (cur && !ids.includes(cur)) opts.unshift(h("option", { value: cur, text: cur }));
  sel.replaceChildren(...opts);
  if (cur) sel.value = cur;
}

// ------------------------------------------------------------------- snapshot
function onSnapshot(snap) {
  const prev = state.snap;
  state.snap = snap;
  state.recvAt = performance.now();
  state.runId = snap.run.id;
  if (!prev || prev.run.id !== snap.run.id) { // new run: reset per-run UI state
    state.selected = null;
    state.graphSig = {};
    state.eventsSig = "";
    renderPicker();
    refreshRuns();
  }
  showEmpty(false);
  render();
}

function render() {
  const snap = state.snap;
  if (!snap) return;
  renderTop(snap);
  renderMetrics(snap);
  renderBadges(snap);
  renderGraph();
  renderInspector();
  renderEvents(snap);
  renderContext(snap);
  renderTimeline(snap);
  applySelection();
}

function renderTop(snap) {
  const r = snap.run;
  $("run-title").textContent = r.title || r.id;
  $("run-meta").textContent = ["run " + r.id, "started " + fHms(r.started), r.cwd].filter(Boolean).join(" · ");
  const pill = $("status-pill");
  pill.hidden = false;
  pill.className = "pill " + (["running", "done", "error"].includes(r.status) ? r.status : "");
  $("status-text").textContent = r.status.charAt(0).toUpperCase() + r.status.slice(1);
  $("export").href = `/api/runs/${encodeURIComponent(r.id)}/events.jsonl`;
  const picker = $("run-picker");
  if (picker.value !== r.id && [...picker.options].some((o) => o.value === r.id)) picker.value = r.id;
  document.title = `${r.title || r.id} · Agent Monitor`;
}

// -------------------------------------------------------------------- metrics
function tile(key, value, sub, opts = {}) {
  return h("div", { class: "tile" + (opts.bad ? " bad" : "") },
    h("div", { class: "k", text: key }),
    h("div", { class: "v", id: opts.id }, value),
    h("div", { class: "s", text: sub, title: sub }));
}
function renderMetrics(snap) {
  const m = snap.metrics, st = m.steps || {};
  const finished = (st.done || 0) + (st.error || 0);
  const tools = Object.entries(m.tools || {}).slice(0, 4).map(([k, v]) => `${k} ${v}`).join(" · ") || "none yet";
  const lastErr = [...snap.log].reverse().find((e) => e.level === "error");
  $("metrics").replaceChildren(
    tile("Elapsed", fClock(m.elapsed), snap.run.status === "running" ? "live" : snap.run.status, { id: "elapsed" }),
    tile("Steps", [String(finished), h("small", { text: ` / ${m.steps_total}` })],
      `${st.running || 0} running · ${st.queued || 0} queued`),
    tile("Tool calls", String(m.tool_calls), tools),
    tile("Tokens", fTok((m.tokens.in || 0) + (m.tokens.out || 0)), `${fTok(m.tokens.in)} in · ${fTok(m.tokens.out)} out`),
    tile("Cache hit", m.cache_hit == null ? "—" : Math.round(m.cache_hit * 100) + "%", `${fTok(m.tokens.cache_read)} tokens read`),
    tile("Cost", fMoney(m.cost), m.cost == null ? "no cost reported" : `${m.agents_running}/${m.agents_total} agents running`),
    tile("Errors", String(m.errors), m.errors > 0 && lastErr ? clip(lastErr.text, 40) : "none", { bad: m.errors > 0 }),
  );
}

// ---------------------------------------------------------------- tabs/legend
function renderBadges(snap) {
  const L = snap.layouts;
  $("badge-swim").textContent = `${L.swim.lanes.length} lanes`;
  $("badge-dag").textContent = `${L.dag.nodes.length} nodes`;
  $("badge-seq").textContent = `${L.seq.rows.length} msgs`;
}

function setTab(tab, focus) {
  state.tab = tab;
  try { localStorage.setItem("agentmon.tab", tab); } catch (_) { /* optional */ }
  for (const t of TABS) {
    const btn = $("tab-" + t), on = t === tab;
    btn.setAttribute("aria-selected", String(on));
    btn.tabIndex = on ? 0 : -1;
    $("panel-" + t).hidden = !on;
  }
  if (focus) $("tab-" + tab).focus();
  renderLegend();
  renderGraph();
  applySelection();
}

function renderLegend() {
  const icon = (st) => { const v = s("svg", { width: 16, height: 16, viewBox: "-10 -10 20 20", "aria-hidden": "true" }); v.append(statusIcon(st, 0, 0, 0.8)); return v; };
  const line = (color, dash, anim) => {
    const v = s("svg", { width: 28, height: 8, viewBox: "0 0 28 8", "aria-hidden": "true" });
    v.append(s("path", { d: "M1 4h26", stroke: color, "stroke-width": 2, "stroke-dasharray": dash || null, class: anim ? "seq-flow" : null }));
    return v;
  };
  const items = [[icon("done"), "Done"], [icon("running"), "Running"], [icon("queued"), "Queued"], [icon("error"), "Failed"],
    [line(C.edge), "Hand-off"], [line(C.amber, "6 4", true), "Active"], [line(C.pending, "6 4"), "Pending"], [line(C.red), "Failure route"]];
  if (state.tab === "dag") items.push([line(C.edgeHi), "Critical path"]);
  if (state.tab === "seq") items.push([line(C.text2), "Call"], [line(C.text2, "5 4"), "Return"]);
  $("legend").replaceChildren(...items.map(([ic, label]) => h("li", null, ic, label)));
}

// ---------------------------------------------------------------- svg pieces
const fills = { done: "rgba(76,195,138,.16)", running: "rgba(242,169,59,.14)", error: "rgba(242,112,107,.16)", queued: "rgba(139,149,166,.1)" };

/** Status icon centred on (cx, cy). ✓ done, spinner running, ✕ error, clock queued. */
function statusIcon(st, cx, cy, scale = 1) {
  const g = s("g", { transform: `translate(${cx},${cy}) scale(${scale})`, "aria-hidden": "true" });
  const col = STATUS_COLOR[st] || C.text3;
  if (st === "running") {
    g.append(s("circle", { r: 9, fill: fills.running, stroke: col, "stroke-opacity": 0.25, "stroke-width": 2 }),
      s("g", { class: "spin" }, s("circle", { r: 9, fill: "none", stroke: col, "stroke-width": 2, "stroke-linecap": "round", "stroke-dasharray": "14 43" })));
  } else if (st === "queued") {
    g.append(s("circle", { r: 9, fill: fills.queued, stroke: col, "stroke-width": 1.5, "stroke-dasharray": "2.5 2.5" }),
      s("path", { d: "M0 -4.5V0l3.2 2", fill: "none", stroke: col, "stroke-width": 1.5, "stroke-linecap": "round", "stroke-linejoin": "round" }));
  } else if (st === "error") {
    g.append(s("circle", { r: 9, fill: fills.error, stroke: col, "stroke-width": 1.5 }),
      s("path", { d: "M-3.5 -3.5l7 7M3.5 -3.5l-7 7", stroke: col, "stroke-width": 2, "stroke-linecap": "round" }));
  } else {
    g.append(s("circle", { r: 9, fill: fills.done, stroke: col, "stroke-width": 1.5 }),
      s("path", { d: "M-4 0.5l2.8 3L4.2 -3", fill: "none", stroke: col, "stroke-width": 2, "stroke-linecap": "round", "stroke-linejoin": "round" }));
  }
  return g;
}

function metaText(step) {
  const parts = [step.tool, step.started != null ? fDur(stepDuration(step)) : null, step.summary].filter(Boolean);
  return clip(parts.length ? parts.join(" · ") : step.status, 28);
}

/** A node card at (x, y). Selection ring + running halo are CSS-driven. */
function nodeCard(step, agent, x, y) {
  const { NODE_W: w, NODE_H: hgt } = G;
  const g = s("g", { class: `node st-${step.status}`, transform: `translate(${x},${y})`, "data-step": step.id,
    role: "button", tabindex: 0, "aria-label": `${step.title}, ${step.status}` });
  g.append(
    s("title", null, `${step.title}\n${step.status}${step.tool ? " · " + step.tool : ""}${step.summary ? "\n" + step.summary : ""}`),
    s("rect", { class: "halo", x: -1, y: -1, width: w + 2, height: hgt + 2, rx: 11 }),
    s("rect", { class: "ring", x: -4, y: -4, width: w + 8, height: hgt + 8, rx: 13 }),
    s("rect", { class: "card", width: w, height: hgt, rx: 10 }),
    s("rect", { x: 0, y: 12, width: 3, height: hgt - 24, rx: 1.5, fill: (agent && agent.color) || C.text4 }),
    statusIcon(step.status, 24, 21),
    s("text", { class: "t", x: 42, y: 25, text: clip(step.title, 22) }),
    s("text", { class: "m", x: 14, y: 46, "data-meta": step.id, text: metaText(step) }),
  );
  return g;
}

/** Cached arrowhead markers, one per colour. Returns the url() reference. */
function arrowFor(defs, color) {
  const id = "ah" + color.replace("#", "") + "-" + defs.dataset.view;
  if (!defs.querySelector("#" + id)) {
    defs.append(s("marker", { id, viewBox: "0 0 10 10", refX: 8, refY: 5, markerWidth: 8, markerHeight: 8, markerUnits: "userSpaceOnUse", orient: "auto" },
      s("path", { d: "M0 1L9 5L0 9z", fill: color })));
  }
  return `url(#${id})`;
}

/** Polyline with rounded corners. */
function roundedPath(pts, r = 7) {
  const p = pts.filter((q, i) => i === 0 || q[0] !== pts[i - 1][0] || q[1] !== pts[i - 1][1]);
  const out = p.filter((q, i) => { // drop collinear middle points
    if (i === 0 || i === p.length - 1) return true;
    const a = p[i - 1], b = p[i + 1];
    return !((a[0] === q[0] && q[0] === b[0]) || (a[1] === q[1] && q[1] === b[1]));
  });
  let d = `M${out[0][0]} ${out[0][1]}`;
  for (let i = 1; i < out.length; i++) {
    const prev = out[i - 1], cur = out[i], next = out[i + 1];
    if (!next) { d += ` L${cur[0]} ${cur[1]}`; break; }
    const l1 = Math.hypot(cur[0] - prev[0], cur[1] - prev[1]), l2 = Math.hypot(next[0] - cur[0], next[1] - cur[1]);
    const k = Math.min(r, l1 / 2, l2 / 2);
    const a = [cur[0] - ((cur[0] - prev[0]) / l1) * k, cur[1] - ((cur[1] - prev[1]) / l1) * k];
    const b = [cur[0] + ((next[0] - cur[0]) / l2) * k, cur[1] + ((next[1] - cur[1]) / l2) * k];
    d += ` L${a[0]} ${a[1]} Q${cur[0]} ${cur[1]} ${b[0]} ${b[1]}`;
  }
  return d;
}

/** Orthogonal route: parent bottom -> gap below parent -> gutter beside target -> gap above target -> target top. */
function routeEdge(a, b, rects, slot) {
  const x0 = a.x + a.w / 2, y0 = a.y + a.h, x1 = b.x + b.w / 2, y1 = b.y;
  const blocked = rects.some((r) => r !== a && r !== b && x0 > r.x - 4 && x0 < r.x + r.w + 4 && r.y < y1 && r.y + r.h > y0);
  if (Math.abs(x0 - x1) < 1 && !blocked) return [[x0, y0], [x1, y1]];
  const jitter = (slot % 5 - 2) * 3; // spread edges that share a gutter
  const gx = (x1 > x0 + 1 ? b.x - G.GH : b.x + b.w + G.GH) + jitter;
  const yA = y0 + G.HG, yB = Math.max(yA, y1 - G.HG);
  return [[x0, y0], [x0, yA], [gx, yA], [gx, yB], [x1, yB], [x1, y1]];
}

const EDGE_STYLE = {
  done: { color: C.edge, width: 1.5 }, active: { color: C.amber, width: 2, dash: "6 5", anim: true },
  pending: { color: C.pending, width: 1.5, dash: "6 5" }, error: { color: C.red, width: 2 },
};

/** Draw all edges between already-positioned rects (map step id -> rect). */
function drawEdges(layer, defs, edges, rects, critical) {
  const all = [...rects.values()];
  const crit = new Set();
  if (critical) for (let i = 1; i < critical.length; i++) crit.add(critical[i - 1] + ">" + critical[i]);
  edges.forEach((e, i) => {
    const a = rects.get(e.src), b = rects.get(e.dst);
    if (!a || !b) return;
    const base = EDGE_STYLE[e.kind] || EDGE_STYLE.done;
    const hot = crit.has(e.src + ">" + e.dst);
    const color = hot && e.kind === "done" ? C.edgeHi : base.color;
    layer.append(s("path", {
      d: roundedPath(routeEdge(a, b, all, i)), class: "edge " + e.kind, stroke: color,
      "stroke-width": base.width + (hot ? 1.5 : 0) + (e.cross && !hot ? 0.25 : 0),
      "stroke-dasharray": base.dash || null, "marker-end": arrowFor(defs, color),
    }));
  });
}

function svgRoot(view, w, hgt) {
  const svg = s("svg", { width: w, height: hgt, viewBox: `0 0 ${w} ${hgt}`, role: "img", "aria-label": view + " view" });
  const defs = s("defs", { "data-view": view });
  svg.append(defs);
  return [svg, defs];
}

// -------------------------------------------------------------- graph: render
function stepsSig(snap) {
  return JSON.stringify([
    snap.steps.map((x) => [x.id, x.status, x.title, x.tool, x.summary, x.status === "running" ? 0 : x.duration]),
    snap.agents.map((a) => [a.id, a.name, a.model, a.status, a.color]),
  ]);
}

/** Re-render the active graph view only when its structure changed (keeps CSS animations steady). */
function renderGraph() {
  const snap = state.snap;
  if (!snap) return;
  const tab = state.tab, L = snap.layouts;
  const sig = JSON.stringify([L[tab], tab === "seq" ? snap.run.status : 0]) + stepsSig(snap);
  if (state.graphSig[tab] === sig) return;
  state.graphSig[tab] = sig;
  const root = $("view-" + tab);
  if (tab === "swim") root.replaceChildren(renderSwim(snap));
  else if (tab === "dag") { renderDagStats(snap); root.replaceChildren(renderDag(snap)); }
  else root.replaceChildren(renderSeq(snap));
  applySelection();
}

const noGraph = (msg) => h("div", { class: "graph-empty", text: msg });

function renderSwim(snap) {
  const sw = snap.layouts.swim;
  if (!sw.nodes.length) return noGraph("No steps yet.");
  const steps = stepsById(), agents = agentsById();
  const { PAD, LANE_W, HEAD_H, ROW_H, NODE_W, NODE_H, HG } = G;
  const W = PAD * 2 + sw.lanes.length * LANE_W, H = PAD * 2 + HEAD_H + sw.rows * ROW_H;
  const [svg, defs] = svgRoot("swim", W, H);
  const bands = s("g"), edges = s("g"), nodes = s("g");
  sw.lanes.forEach((id, i) => {
    const a = agents.get(id) || { name: id, color: C.text4 };
    const lx = PAD + i * LANE_W;
    bands.append(s("rect", { class: "lane-band" + (i % 2 ? " alt" : ""), x: lx, y: PAD + 62, width: LANE_W, height: H - PAD * 2 - 62 }));
    bands.append(s("g", null,
      s("rect", { class: "lane-card", x: lx + 10, y: PAD, width: LANE_W - 20, height: 56, rx: 10 }),
      s("circle", { cx: lx + 28, cy: PAD + 20, r: 5, fill: a.color }),
      s("text", { x: lx + 40, y: PAD + 24, "font-size": 13, "font-weight": 600, text: clip(a.name, 20) }),
      s("text", { class: "mono", x: lx + 22, y: PAD + 44, "font-size": 11, fill: C.text3, text: clip(a.model || "model n/a", 18) }),
      s("text", { x: lx + LANE_W - 22, y: PAD + 44, "font-size": 11, "text-anchor": "end", fill: STATUS_COLOR[a.status] || C.text3, text: a.status || "" }),
    ));
  });
  const rects = new Map();
  for (const n of sw.nodes) {
    const st = steps.get(n.step);
    if (!st) continue;
    const r = { x: PAD + n.lane * LANE_W + (LANE_W - NODE_W) / 2, y: PAD + HEAD_H + n.row * ROW_H + HG, w: NODE_W, h: NODE_H };
    rects.set(n.step, r);
    nodes.append(nodeCard(st, agents.get(st.agent), r.x, r.y));
  }
  drawEdges(edges, defs, sw.edges, rects, null);
  svg.append(bands, edges, nodes);
  return svg;
}

function renderDagStats(snap) {
  const d = snap.layouts.dag, steps = stepsById();
  const crit = d.critical.reduce((sum, id) => sum + (steps.get(id) ? stepDuration(steps.get(id)) || 0 : 0), 0);
  const stat = (k, v) => h("span", null, k, h("b", { text: String(v) }));
  $("dag-stats").replaceChildren(stat("nodes", d.nodes.length), stat("edges", d.edges.length), stat("levels", d.levels),
    stat("max parallelism", d.width), stat("critical path", d.critical.length ? fDur(crit) : "—"));
}

function renderDag(snap) {
  const d = snap.layouts.dag;
  if (!d.nodes.length) return noGraph("No steps yet.");
  const steps = stepsById(), agents = agentsById();
  const { PAD, ROW_H, NODE_W, NODE_H, HG } = G, COLW = NODE_W + 2 * G.GH;
  const W = PAD * 2 + Math.max(1, d.width) * COLW, H = PAD * 2 + d.levels * ROW_H;
  const [svg, defs] = svgRoot("dag", W, H);
  const edges = s("g"), nodes = s("g"), rects = new Map();
  for (const n of d.nodes) {
    const st = steps.get(n.step);
    if (!st) continue;
    const cx = W / 2 + (n.col - (n.ncols - 1) / 2) * COLW;
    const r = { x: cx - NODE_W / 2, y: PAD + n.level * ROW_H + HG, w: NODE_W, h: NODE_H };
    rects.set(n.step, r);
    nodes.append(nodeCard(st, agents.get(st.agent), r.x, r.y));
  }
  drawEdges(edges, defs, d.edges, rects, d.critical);
  svg.append(edges, nodes);
  return svg;
}

// ----------------------------------------------------------------- sequence
function renderSeq(snap) {
  const q = snap.layouts.seq, agents = agentsById(), steps = stepsById();
  if (!q.participants.length) return noGraph("No messages yet.");
  const GUT = 74, PAD = 20, HEAD = 50, ROW = 36, HALF = 5;
  const n = q.participants.length;
  // Fit columns to the panel when possible (min 130px each); otherwise the view scrolls horizontally.
  const avail = $("view-seq").clientWidth || 1000;
  const COL = Math.max(130, Math.min(190, Math.floor((avail - PAD * 2 - GUT) / n)));
  const BOX = COL - 20;
  const W = PAD + GUT + n * COL + PAD, y0 = PAD + HEAD + 24;
  const live = snap.run.status === "running";
  const H = y0 + q.rows.length * ROW + (live ? 44 : 24);
  const [svg, defs] = svgRoot("seq", W, H);
  const xs = new Map(q.participants.map((p, i) => [p, PAD + GUT + i * COL + COL / 2]));
  const rowY = (i) => y0 + i * ROW + ROW / 2;
  const colorOf = (p) => (p === "user" ? C.blue : p === "tools" ? C.text3 : (agents.get(p) || {}).color || C.text2);
  const nameOf = (p) => (agents.get(p) || {}).name || p;
  const bottom = H - 14;

  const life = s("g"), heads = s("g"), acts = s("g"), rowsG = s("g"), msgs = s("g");
  q.participants.forEach((p) => {
    const x = xs.get(p), c = colorOf(p), a = agents.get(p);
    life.append(s("line", { x1: x, x2: x, y1: PAD + HEAD, y2: bottom, stroke: C.line, "stroke-width": 1.5, "stroke-dasharray": "4 5" }));
    heads.append(s("g", null,
      s("rect", { x: x - BOX / 2, y: PAD, width: BOX, height: HEAD - 4, rx: 10, fill: "#161B23", stroke: c, "stroke-width": 1.5 }),
      s("text", { x, y: PAD + 21, "text-anchor": "middle", "font-size": 12.5, "font-weight": 600, fill: c, text: clip(nameOf(p), Math.floor(BOX / 8.5)) }),
      s("text", { class: "mono", x, y: PAD + 37, "text-anchor": "middle", "font-size": 10.5, fill: C.text3,
        text: clip(a ? a.model || "agent" : p === "user" ? "human" : "tool calls", 18) })));
  });
  for (const [id, [i0, i1]] of Object.entries(q.activations)) {
    if (!xs.has(id)) continue;
    acts.append(s("rect", { x: xs.get(id) - HALF, y: rowY(i0) - 11, width: HALF * 2, height: rowY(i1) - rowY(i0) + 22, rx: 3,
      fill: colorOf(id), "fill-opacity": 0.22, stroke: colorOf(id), "stroke-width": 1 }));
  }
  const t0 = snap.run.started || 0;
  q.rows.forEach((r, i) => {
    const y = rowY(i), xa = xs.get(r.src), xb = xs.get(r.dst);
    if (xa == null || xb == null) return;
    const attrs = { class: "rowhit" + (r.step ? " step" : ""), x: PAD, y: y - ROW / 2, width: W - PAD * 2, height: ROW };
    if (r.step) Object.assign(attrs, { "data-step": r.step, role: "button", tabindex: 0, "aria-label": `${r.label} (${steps.has(r.step) ? steps.get(r.step).title : r.step})` });
    rowsG.append(s("rect", attrs, s("title", null, `${r.src} → ${r.dst}: ${r.label}`)));
    rowsG.append(s("text", { class: "mono", x: PAD + GUT - 12, y: y + 4, "text-anchor": "end", "font-size": 10.5, fill: C.text3, text: "+" + fDur(Math.max(0, r.ts - t0)) }));

    let color = C.text2, dash = null, anim = false;
    if (r.kind === "return") dash = "5 4";
    else if (r.kind === "error") color = C.red;
    else if (r.kind === "running") { color = C.amber; dash = "6 4"; anim = true; }
    else if (r.kind === "spawn" || r.kind === "message") color = colorOf(r.src);
    if (r.kind === "message" && r.src === "user") color = C.blue;
    const common = { fill: "none", stroke: color, "stroke-width": 1.6, "stroke-dasharray": dash, "stroke-linecap": "round", "marker-end": arrowFor(defs, color), class: anim ? "seq-flow" : null };
    let label, lx, anchor = "middle", maxc;
    if (r.src === r.dst) { // self message: small loop to the right of the lifeline
      msgs.append(s("path", { ...common, d: roundedPath([[xa + HALF, y - 7], [xa + 30, y - 7], [xa + 30, y + 7], [xa + HALF + 2, y + 7]], 4) }));
      lx = xa + 36; anchor = "start"; maxc = 24;
    } else {
      const dir = Math.sign(xb - xa), x1 = xa + dir * HALF, x2 = xb - dir * HALF;
      msgs.append(s("path", { ...common, d: `M${x1} ${y}L${x2} ${y}` }));
      lx = (xa + xb) / 2; maxc = Math.max(6, Math.floor((Math.abs(xb - xa) - 12) / 6.2));
    }
    label = s("text", { x: lx, y: r.src === r.dst ? y : y - 7, "text-anchor": anchor, "font-size": 11.5, fill: r.kind === "error" ? C.red : C.text, text: clip(r.label, maxc) });
    msgs.append(label);
  });
  for (const g of [life, heads, acts, msgs]) g.setAttribute("pointer-events", "none"); // clicks fall through to row hit-areas
  svg.append(life, heads, acts, rowsG, msgs);
  if (live) {
    const ny = y0 + q.rows.length * ROW + 14;
    svg.append(s("line", { class: "now-line", x1: PAD + 4, x2: W - PAD, y1: ny, y2: ny }),
      s("text", { x: PAD + GUT - 12, y: ny + 4, "text-anchor": "end", "font-size": 10.5, "font-weight": 600, fill: C.amber, text: "now" }));
  }
  return svg;
}

// ----------------------------------------------------------------- timeline
function niceStep(span, target = 8) {
  const steps = [0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 14400];
  return steps.find((x) => span / x <= target) || 14400;
}
const tickLabel = (sec) => (sec < 60 ? (Number.isInteger(sec) ? sec : sec.toFixed(1)) + "s" : fClock(sec).replace(/^0/, ""));

function renderTimeline(snap) {
  const tl = snap.layouts.timeline, agents = agentsById(), root = $("timeline-body");
  const live = snap.run.status === "running";
  $("timeline-sub").textContent = `${fDur(tl.t1 - tl.t0)} total`;
  if (!tl.lanes.length) { root.replaceChildren(noGraph("No steps yet.")); return; }
  const LABEL = 130, PADR = 28, AXIS = 28, BAR = 18, GAP = 5;
  const W = Math.max(root.clientWidth || 900, 640), span = tl.t1 - tl.t0;
  const x = (t) => LABEL + ((t - tl.t0) / span) * (W - LABEL - PADR);
  // stack overlapping bars within a lane into sub-rows
  const laid = tl.lanes.map((lane) => {
    const ends = [];
    const bars = lane.bars.map((b) => {
      const xs = x(b.start), xe = Math.max(x(b.end), xs + (b.status === "queued" ? 8 : 3));
      let row = ends.findIndex((e) => e <= xs - 2);
      if (row < 0) row = ends.length;
      ends[row] = xe;
      return { ...b, xs, xe, row };
    });
    return { agent: lane.agent, bars, rows: Math.max(1, ends.length) };
  });
  let y = AXIS;
  const H = AXIS + laid.reduce((sum, l) => sum + l.rows * (BAR + GAP) + 14, 0) + 6;
  const [svg] = svgRoot("timeline", W, H);
  const grid = s("g"), body = s("g");
  const step = niceStep(span);
  for (let t = 0; t <= span + 1e-6; t += step) {
    const gx = x(tl.t0 + t);
    grid.append(s("line", { x1: gx, x2: gx, y1: AXIS - 6, y2: H, stroke: "#1C222C", "stroke-width": 1 }),
      s("text", { class: "mono", x: gx, y: 14, "text-anchor": "middle", "font-size": 10.5, fill: C.text3, text: tickLabel(t) }));
  }
  laid.forEach((lane, li) => {
    const a = agents.get(lane.agent) || { name: lane.agent, color: C.text4 };
    const lh = lane.rows * (BAR + GAP) + 14;
    if (li % 2 === 0) body.append(s("rect", { x: 0, y, width: W, height: lh, fill: "#0F1318" }));
    body.append(s("circle", { cx: 12, cy: y + lh / 2, r: 5, fill: a.color }),
      s("text", { x: 24, y: y + lh / 2 + 4, "font-size": 12.5, "font-weight": 600, text: clip(a.name, 15) }));
    for (const b of lane.bars) {
      const by = y + 7 + b.row * (BAR + GAP), bw = b.xe - b.xs;
      const fill = b.status === "running" ? C.amber : b.status === "error" ? C.red : b.status === "queued" ? "none" : a.color;
      const g = s("g", { class: "bar", "data-step": b.step, role: "button", tabindex: 0, "aria-label": `${b.title}, ${b.status}` },
        s("title", null, `${b.title}\n${b.status} · ${fDur(b.end - b.start)}`),
        s("rect", { x: b.xs, y: by, width: bw, height: BAR, rx: 4, fill, "fill-opacity": b.status === "done" ? 0.85 : 1,
          stroke: b.status === "queued" ? C.text4 : "none", "stroke-dasharray": b.status === "queued" ? "3 3" : null }));
      if (bw > 54) g.append(s("text", { x: b.xs + 6, y: by + 13, "font-size": 11, "font-weight": 500, fill: C.ink, "pointer-events": "none", text: clip(b.title, Math.floor((bw - 10) / 6.2)) }));
      body.append(g);
    }
    y += lh;
  });
  svg.append(grid, body);
  if (live) {
    const nx = x(tl.t1);
    svg.append(s("line", { class: "now-line", x1: nx, x2: nx, y1: AXIS - 8, y2: H }),
      s("text", { x: nx - 4, y: 14, "text-anchor": "end", "font-size": 10.5, "font-weight": 600, fill: C.amber, text: "now" }));
  }
  root.replaceChildren(svg);
}

// ---------------------------------------------------------------- selection
function applySelection() {
  for (const n of document.querySelectorAll("[data-step]")) n.classList.toggle("sel", n.dataset.step === state.selected);
}
function select(id) {
  state.selected = id;
  renderInspector();
  applySelection();
}

// ---------------------------------------------------------------- inspector
function renderInspector() {
  const snap = state.snap, root = $("inspector");
  if (!snap) return;
  const steps = stepsById(), agents = agentsById();
  let id = state.selected && steps.has(state.selected) ? state.selected : null;
  if (!id && state.selected) state.selected = null;
  const explicit = id != null;
  if (!id) id = snap.focus;
  const step = id ? steps.get(id) : null;
  if (!step) { root.replaceChildren(h("p", { class: "muted", text: "No steps yet. The inspector follows the active step once the run starts." })); return; }

  const agent = agents.get(step.agent);
  const chips = (ids) => ids.length
    ? h("div", { class: "chip-list" }, ids.map((sid) => h("button", { type: "button", class: "step-chip", title: sid, text: (steps.get(sid) || { title: sid }).title, onclick: () => select(sid) })))
    : "—";
  const unblocks = snap.steps.filter((x) => x.deps.includes(step.id)).map((x) => x.id);
  const rows = [
    ["Agent", agent ? h("span", { class: "agent-chip" }, h("i", { class: "dotc", style: `background:${agent.color}` }), agent.name, agent.model ? h("span", { class: "muted mono small", text: agent.model }) : null) : step.agent],
    ["Tool", step.tool || "—"],
    ["Started", fHms(step.started)],
    ["Duration", h("span", { "data-insp-dur": "1", text: fDur(stepDuration(step)) })],
    ["Triggered by", chips(step.deps)],
    ["Unblocks", chips(unblocks)],
  ];
  const tk = Object.entries(step.tokens || {}).filter(([, v]) => v);
  if (tk.length) rows.push(["Tokens", tk.map(([k, v]) => `${fTok(v)} ${k}`).join(" · ")]);

  const parts = [
    h("div", { class: "insp-src" }, explicit ? "Selected step" : "Following active step",
      explicit ? h("button", { type: "button", class: "link", text: "clear (Esc)", onclick: () => select(null) }) : null),
    h("div", { class: "insp-top" }, h("div", { class: "insp-title", text: step.title }), h("span", { class: "status-badge " + step.status, text: step.status })),
    h("dl", { class: "kv" }, rows.map(([k, v]) => [h("dt", { text: k }), h("dd", null, v)])),
  ];
  if (step.status === "running" && agent && agent.last_thought) parts.push(h("div", { class: "block-label", text: "Thinking" }), h("p", { class: "thought", text: agent.last_thought }));
  if (step.input) parts.push(h("div", { class: "block-label", text: "Input" }), h("pre", { text: prettyJson(step.input) }));
  if (step.output) parts.push(h("div", { class: "block-label", text: "Output" }), h("pre", { text: step.output }));
  else if (step.summary) parts.push(h("div", { class: "block-label", text: "Summary" }), h("pre", { text: step.summary }));
  const wrap = h("div", { class: "insp" }, parts);
  const keep = root.scrollTop;
  root.replaceChildren(wrap);
  root.scrollTop = keep;
}
function prettyJson(text) {
  try { return JSON.stringify(JSON.parse(text), null, 2); } catch (_) { return text; }
}

// ------------------------------------------------------------------- events
function eventMatches(e, f) {
  if (f === "all") return true;
  if (f === "tools") return e.type === "step_start" || e.type === "step_end";
  if (f === "messages") return e.type === "message";
  if (f === "thinking") return e.type === "thinking";
  return e.level === "error";
}
function renderChips() {
  $("chips").replaceChildren(...FILTERS.map(([key, label]) =>
    h("button", { type: "button", class: "chip", "aria-pressed": String(state.filter === key), text: label,
      onclick: () => { state.filter = key; state.eventsSig = ""; renderChips(); if (state.snap) renderEvents(state.snap); } })));
}
function renderEvents(snap) {
  const log = snap.log, last = log[log.length - 1];
  const sig = `${state.filter}:${log.length}:${last ? last.ts : 0}`;
  if (sig === state.eventsSig) return;
  state.eventsSig = sig;
  const agents = agentsById();
  const items = log.filter((e) => eventMatches(e, state.filter)).reverse().map((e) => {
    const a = e.agent ? agents.get(e.agent) : null;
    const li = h("li", { class: `${e.level === "error" ? "error " : ""}${e.type}` },
      h("time", { text: fHms(e.ts) }),
      h("div", null,
        e.agent ? h("span", { class: "who", style: `color:${a ? a.color : C.text2}`, text: a ? a.name : e.agent }) : null,
        h("span", { class: "txt", text: e.text })));
    return li;
  });
  $("events").replaceChildren(...(items.length ? items : [h("li", null, h("span", { class: "none", text: "No matching events." }))]));
}

// ------------------------------------------------------------------ context
function renderContext(snap) {
  const maxTok = Math.max(1, ...snap.agents.map((a) => (a.tokens.in || 0) + (a.tokens.out || 0)));
  const rows = snap.agents.map((a) => {
    const ctx = a.context && a.context.max ? a.context : null;
    let pct, label, color = a.color;
    if (ctx) {
      pct = Math.min(1, ctx.used / ctx.max);
      label = `${fTok(ctx.used)} / ${fTok(ctx.max)} · ${Math.round(pct * 100)}%`;
      if (pct > 0.85) color = C.red; else if (pct > 0.65) color = C.amber;
    } else {
      const t = (a.tokens.in || 0) + (a.tokens.out || 0);
      pct = t / maxTok;
      label = `${fTok(a.tokens.in)} in · ${fTok(a.tokens.out)} out`;
    }
    return h("div", { class: "ctx-row" },
      h("div", { class: "ctx-top" }, h("span", { class: "nm" }, h("i", { class: "dotc", style: `background:${a.color}` }), a.name), h("span", { class: "num", text: label })),
      h("div", { class: "meter", role: "meter", "aria-label": `${a.name} ${ctx ? "context" : "tokens"}`, "aria-valuemin": 0, "aria-valuemax": 100, "aria-valuenow": Math.round(pct * 100) },
        h("i", { style: `width:${Math.round(pct * 100)}%;background:${color}` })));
  });
  $("context").replaceChildren(...rows, h("div", { class: "ctx-total" }, h("span", { class: "muted", text: "Total cost" }), h("b", { class: "mono", text: fMoney(snap.metrics.cost) })));
}

// -------------------------------------------------------------------- ticker
/** Advances clocks between snapshots (running steps, elapsed tile) without re-rendering. */
function tick() {
  const snap = state.snap;
  if (!snap || snap.run.status !== "running") return;
  const el = $("elapsed");
  if (el) el.textContent = fClock(liveNow() - snap.run.started);
  const steps = stepsById();
  for (const n of document.querySelectorAll("[data-meta]")) {
    const st = steps.get(n.dataset.meta);
    if (st && st.status === "running") { const t = metaText(st); if (n.textContent !== t) n.textContent = t; }
  }
  const dur = document.querySelector("[data-insp-dur]");
  const cur = state.selected && steps.has(state.selected) ? state.selected : snap.focus;
  if (dur && cur && steps.has(cur)) dur.textContent = fDur(stepDuration(steps.get(cur)));
}

// -------------------------------------------------------------------- wiring
function init() {
  renderChips();
  setTab(state.tab, false);
  for (const btn of document.querySelectorAll("[role=tab]")) {
    btn.addEventListener("click", () => setTab(btn.dataset.tab, false));
    btn.addEventListener("keydown", (e) => {
      const i = TABS.indexOf(btn.dataset.tab);
      const next = e.key === "ArrowRight" ? (i + 1) % 3 : e.key === "ArrowLeft" ? (i + 2) % 3 : e.key === "Home" ? 0 : e.key === "End" ? 2 : -1;
      if (next >= 0) { e.preventDefault(); setTab(TABS[next], true); }
    });
  }
  // One delegated handler per graph surface: click/Enter/Space on anything carrying data-step.
  for (const id of ["view-swim", "view-dag", "view-seq", "timeline-body"]) {
    const root = $(id);
    root.addEventListener("click", (e) => {
      const t = e.target.closest("[data-step]");
      if (t) select(t.dataset.step); else if (!e.target.closest("button")) select(null);
    });
    root.addEventListener("keydown", (e) => {
      if (e.key !== "Enter" && e.key !== " ") return;
      const t = e.target.closest("[data-step]");
      if (t) { e.preventDefault(); select(t.dataset.step); }
    });
  }
  document.addEventListener("keydown", (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === "Escape") { if (state.selected) select(null); return; }
    const tag = (e.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "select" || tag === "textarea") return;
    if (e.key >= "1" && e.key <= "3") setTab(TABS[Number(e.key) - 1], false);
  });
  $("run-picker").addEventListener("change", (e) => {
    state.mode = "run";
    state.runId = e.target.value;
    $("follow").setAttribute("aria-pressed", "false");
    connect();
  });
  $("follow").addEventListener("click", () => {
    const on = $("follow").getAttribute("aria-pressed") !== "true";
    state.mode = on ? "latest" : "run";
    $("follow").setAttribute("aria-pressed", String(on));
    connect();
  });
  let resizeTimer = 0;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      if (!state.snap) return;
      state.graphSig.seq = ""; // sequence columns depend on the panel width
      renderGraph();
      renderTimeline(state.snap);
    }, 150);
  });
  setInterval(tick, 250);
  setInterval(refreshRuns, 5000);

  fetch("/api/config", { cache: "no-store" }).then((r) => r.json()).catch(() => ({})).then((cfg) => {
    if (cfg.pinned) { // server was started on one specific run: honour it until the user chooses otherwise
      state.mode = "default";
      $("follow").setAttribute("aria-pressed", "false");
    }
    refreshRuns();
    connect();
  });
}
init();
