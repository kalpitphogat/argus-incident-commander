// Argus web UI: landing (scenario launcher) + live incident view driven by Server-Sent Events.
import { AGENT, ANALYST, LOGO, icon } from "./icons.js";
import { esc, renderMarkdown } from "./markdown.js";
import { fmtValue, lineChart, metricLabel, sparkline } from "./charts.js";
import { NODE_META, renderAgentGraph } from "./graph.js";
import { renderServiceMap } from "./servicemap.js";

const $ = (s, el = document) => el.querySelector(s);
const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
};

const App = {
  health: null, scenarios: [], shape: null, topo: null, incidents: [],
  settings: { reasoning: "auto", approval_policy: "medium", pace: 0.6 },
  cur: null, es: null, telemetryTimer: null,
};

const PHASES = ["Triage", "Investigate", "Diagnose", "Plan", "Approve", "Execute", "Verify", "Report"];
const NODE_PHASE = { intake: 0, triage: 0, dispatch: 1, investigate: 1, synthesize: 2, critique: 2, plan: 3,
  approval: 4, execute: 5, verify: 6, report: 7 };
const STATUS = {
  starting: ["Starting", "st-live"], running: ["Running", "st-live"], triaging: ["Triaging", "st-live"],
  investigating: ["Investigating", "st-live"], planning: ["Planning", "st-live"], awaiting_approval: ["Awaiting approval", "st-wait"],
  mitigating: ["Mitigating", "st-live"], verifying: ["Verifying", "st-live"], replanning: ["Re-planning", "st-live"],
  resolved: ["Resolved", "st-good"], mitigated: ["Mitigated", "st-good"], escalated: ["Escalated", "st-bad"],
  failed: ["Failed", "st-bad"], unknown: ["Unknown", ""],
};
const SCEN_ICON = { rocket: "rocket", flame: "flame", memory: "memory", card: "card", lock: "lock", cart: "cart" };
const ACTION_ICON = { rollback_deploy: "refresh", restart_service: "refresh", scale_service: "layers",
  increase_connection_pool: "database", toggle_feature_flag: "flag", rotate_certificate: "lock", enable_circuit_breaker: "shield" };

// ------------------------------------------------------------------ utilities
async function api(path, opts = {}) {
  if (App.demo) return App.demo.api(path, opts);
  const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  const token = store.get("argus.token");
  if (token) headers.Authorization = `Bearer ${token}`;
  const r = await fetch(path, { ...opts, headers });
  if (r.status === 401) {
    const t = window.prompt("This Argus server requires an API token:");
    if (t) { store.set("argus.token", t); return api(path, opts); }
  }
  if (!r.ok) throw new Error(`${r.status} ${(await r.text()).slice(0, 200)}`);
  return r.json();
}
const hhmmss = (iso) => { try { return new Date(iso).toLocaleTimeString([], { hour12: false }); } catch { return ""; } };
const pretty = (s) => String(s || "").replace(/_/g, " ");
const pct = (v) => `${Math.round((v || 0) * 100)}%`;

function toast(kind, title, body) {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  const ic = { good: "check", warn: "pause", bad: "alert", info: "sparkle" }[kind] || "sparkle";
  el.innerHTML = `<div class="ti">${icon(ic, 16)}</div><div><div class="tt">${esc(title)}</div><div class="tb">${esc(body || "")}</div></div>`;
  $("#toasts").appendChild(el);
  setTimeout(() => { el.style.opacity = "0"; el.style.transition = "opacity .3s"; }, 5200);
  setTimeout(() => el.remove(), 5600);
}

function effectiveTheme() {
  const set = document.documentElement.dataset.theme;
  if (set) return set;
  return matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}
function paintThemeBtn() { $("#themeBtn").innerHTML = icon(effectiveTheme() === "dark" ? "sun" : "moon", 17); }

// --------------------------------------------------------------------- shell
async function boot() {
  $("#brand").innerHTML = `${LOGO}<span class="brand-name">Argus</span><span class="brand-sub">AI Incident Commander</span>`;
  $("#newBtn").innerHTML = `${icon("plus", 16)} New incident`;
  $("#menuBtn").innerHTML = icon("menu", 18);
  $("#menuBtn").onclick = () => $("#rail").classList.toggle("open");
  paintThemeBtn();
  $("#themeBtn").onclick = () => {
    const next = effectiveTheme() === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    store.set("argus.theme", next);
    paintThemeBtn();
    if (App.cur) { App.cur.dirty.add("charts"); schedule(); }
  };
  const saved = store.get("argus.settings");
  if (saved) { try { Object.assign(App.settings, JSON.parse(saved)); } catch { /* ignore */ } }
  const demoTag = document.querySelector('meta[name="argus-demo"]');
  if (demoTag) {
    const { initDemo } = await import("./demo.js");
    App.demo = await initDemo(new URL(demoTag.content, document.baseURI).href);
    App.repo = document.querySelector('meta[name="argus-repo"]')?.content || App.demo.meta.repo_url;
    App.settings.reasoning = "deterministic";
    App.settings.approval_policy = "medium";
  }
  const [health, sc, shape, topo] = await Promise.all([api("/api/health"), api("/api/scenarios"), api("/api/graph"), api("/api/topology")]);
  App.health = health; App.scenarios = sc.scenarios; App.shape = shape; App.topo = topo;
  const pill = $("#enginePill");
  pill.classList.add(health.llm_available ? "llm" : "offline");
  pill.querySelector("span:last-child").textContent = App.demo ? "Live demo · recorded runs"
    : health.llm_available ? `${health.model_label} ready` : "Offline reasoner · no API key";
  if (!health.llm_available && App.settings.reasoning === "llm") App.settings.reasoning = "auto";
  window.addEventListener("hashchange", route);
  await refreshList();
  setInterval(refreshList, 8000);
  route();
}

async function refreshList() {
  try { App.incidents = (await api("/api/incidents")).incidents; } catch { return; }
  const cur = App.cur?.id;
  $("#incList").innerHTML = App.incidents.length ? App.incidents.map((i) => {
    const [label, cls] = STATUS[i.status] || [pretty(i.status), ""];
    const dot = cls === "st-live" ? "live" : cls === "st-wait" ? "wait" : cls === "st-good" ? "good" : cls === "st-bad" ? "bad" : "";
    return `<li class="inc-item${i.incident_id === cur ? " active" : ""}"><a href="#/incident/${esc(i.incident_id)}">
      <span class="status-dot ${dot}"></span><span class="t">${esc(i.title)}</span>
      <span class="s">${esc(i.incident_id)} · ${esc(i.severity || "…")} · ${esc(label)}</span></a></li>`;
  }).join("") : `<li class="rail-empty">No incidents yet. Start one from the scenario library.</li>`;
}

function route() {
  $("#rail").classList.remove("open");
  const m = location.hash.match(/^#\/incident\/([\w-]+)/);
  if (App.es) { App.es.close(); App.es = null; }
  clearInterval(App.telemetryTimer);
  if (m) openIncident(m[1]); else { App.cur = null; renderLanding(); }
  refreshList();
}

// ------------------------------------------------------------------- landing
function seg(name, options, value) {
  return `<div class="seg" role="group" data-setting="${name}">${options.map(([v, label, disabled, title]) =>
    `<button type="button" data-v="${esc(v)}" aria-pressed="${String(v) === String(value)}" ${disabled ? "disabled" : ""} title="${esc(title || "")}">${esc(label)}</button>`).join("")}</div>`;
}

function renderLanding() {
  const h = App.health, s = App.settings;
  const llmTitle = h.llm_available ? `Use ${h.model_label}` : "Set ANTHROPIC_API_KEY (or another provider key) on the server to enable";
  const cards = App.scenarios.map((c) => `
    <button class="scenario" data-scenario="${esc(c.id)}">
      <div class="sc-top"><span class="sc-icon">${icon(SCEN_ICON[c.icon] || "sparkle", 20)}</span><span class="sc-diff">${esc(c.difficulty)}</span></div>
      <h3>${esc(c.title)}</h3><p>${esc(c.teaser)}</p>
      <div class="tags">${c.tags.map((t) => `<span class="tag">${esc(t)}</span>`).join("")}</div>
      <span class="sc-go">Simulate incident ${icon("arrow", 14)}</span>
    </button>`).join("") + `
    <button class="scenario chaos" data-scenario="chaos">
      <div class="sc-top"><span class="sc-icon">${icon("dice", 20)}</span><span class="sc-diff">Unknown</span></div>
      <h3>Chaos monkey</h3><p>Inject a random fault into a random component. Nobody - including Argus - knows what broke.</p>
      <div class="tags"><span class="tag">random</span><span class="tag">20+ fault variants</span></div>
      <span class="sc-go">Break something ${icon("arrow", 14)}</span>
    </button>`;
  $("#main").innerHTML = `
    <section class="hero">
      <span class="eyebrow">${icon("sparkle", 14)} LangGraph · LangChain · ${esc(h.model_label)}</span>
      ${App.demo ? demoCallout() : ""}
      <h1>Your <span class="grad">AI incident commander</span>.</h1>
      <p class="lead">Argus investigates production incidents like a seasoned SRE team: parallel specialists dig through
        metrics, logs, changes and traces; a critic challenges every conclusion; risky fixes wait for a human; and nothing is
        called resolved until the telemetry says so.</p>
      <div class="hero-stats"><span><b>4</b> parallel investigators</span><span><b>18</b>-component estate</span>
        <span><b>6</b> curated incidents + chaos</span><span><b>0</b> API keys needed to try it</span></div>
    </section>
    <div class="settings">
      <label class="field"><span>Reasoning engine</span>${App.demo
        ? seg("reasoning", [["auto", "Auto", true, DEMO_LOCK], ["llm", "Claude", true, DEMO_LOCK], ["deterministic", "Offline (recorded)"]], "deterministic")
        : seg("reasoning", [["auto", "Auto"], ["llm", "Claude", !h.llm_available, llmTitle], ["deterministic", "Offline"]], s.reasoning)}</label>
      <label class="field"><span>Human approval required for</span>${seg("approval_policy", [["low", "Every action", !!App.demo, DEMO_LOCK], ["medium", "Medium + high risk"], ["high", "High risk only", !!App.demo, DEMO_LOCK], ["never", "Nothing (autonomous)", !!App.demo, DEMO_LOCK]], App.demo ? "medium" : s.approval_policy)}</label>
      <label class="field"><span>Playback</span>${seg("pace", [["0.6", "Cinematic"], ["0", "Instant"]], s.pace)}</label>
    </div>
    <div class="section-title"><h2>Simulate an incident</h2><span class="muted">Each run builds a fresh, seeded production estate with its own telemetry.</span></div>
    <div class="scenarios">${cards}</div>
    <div class="section-title"><h2>How Argus works</h2><span class="muted">The exact LangGraph that runs every incident.</span></div>
    <div class="how">
      <div class="card"><div class="card-h"><h2>${icon("graph", 16)} Agent graph</h2><span class="aside">compiled StateGraph</span></div>
        <svg class="agent-graph" id="howGraph"></svg></div>
      <div class="features">
        ${feature("deps", "Parallel specialists", "Triage fans out with <code>Send</code>: metrics, logs, change and dependency analysts each run a tool-calling sub-graph and compress what they found.")}
        ${feature("shield", "A critic that pushes back", "An RCA lead ranks hypotheses; a critic demands direct evidence from the suspected component and loops back for more rounds.")}
        ${feature("usercheck", "Human in the loop", "Risky actions pause the graph with <code>interrupt()</code>. Approve, edit or reject with feedback - state is checkpointed, so it can wait.")}
        ${feature("activity", "Closed-loop remediation", "Actions run against the control plane, then a verifier watches SLOs. Temporary fixes are caught and Argus re-plans.")}
        ${feature("database", "Learns from history", "Every postmortem is stored in a LangGraph <code>Store</code>; triage recalls similar incidents next time.")}
        ${feature("cpu", "Graceful degradation", "Runs on Claude when a key is present and falls back, per step, to a deterministic reasoner - incidents never stall.")}
      </div>
    </div>`;
  renderAgentGraph($("#howGraph"), App.shape, { nodes: {}, edges: new Set(), flowing: null, lanes: [] });
  document.querySelectorAll(".seg").forEach((g) => g.addEventListener("click", (ev) => {
    const b = ev.target.closest("button"); if (!b || b.disabled) return;
    const key = g.dataset.setting;
    App.settings[key] = key === "pace" ? Number(b.dataset.v) : b.dataset.v;
    store.set("argus.settings", JSON.stringify(App.settings));
    g.querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
  }));
  document.querySelectorAll(".scenario").forEach((c) => c.addEventListener("click", () => startIncident(c.dataset.scenario, c)));
}

const DEMO_EDIT = "Partial approvals and parameter edits need the live agent.";
const DEMO_LOCK = "The demo replays recordings made with this setting. Run Argus yourself to change it.";

function demoCallout() {
  const repo = App.repo ? esc(App.repo) : "";
  const cs = App.repo ? esc(App.repo.replace("https://github.com/", "https://codespaces.new/")) : "";
  return `<div class="demo-callout">${icon("play", 16)}<div><b>This is a static demo on GitHub Pages.</b> Every incident replays a
    recording of the real LangGraph agent (offline reasoner): its actual tool calls, findings, hypotheses and fixes.
    At the approval step you choose between two recorded branches: approve, or reject with feedback and watch it re-plan.
    ${repo ? `<div class="demo-links"><a class="btn" href="${repo}" target="_blank" rel="noopener">${icon("graph", 15)} Source on GitHub</a>
    <a class="btn" href="${cs}" target="_blank" rel="noopener">${icon("cpu", 15)} Run the live agent in Codespaces</a></div>` : ""}</div></div>`;
}

function feature(ic, title, body) {
  return `<div class="feature"><div class="f-ic">${icon(ic, 16)}</div><h4>${esc(title)}</h4><p>${body}</p></div>`;
}

async function startIncident(scenario, el) {
  el.style.opacity = ".6";
  try {
    const s = App.settings;
    const r = await api("/api/incidents", { method: "POST", body: JSON.stringify({ scenario, reasoning: s.reasoning, approval_policy: s.approval_policy, pace: Number(s.pace) }) });
    location.hash = `#/incident/${r.incident_id}`;
  } catch (e) {
    el.style.opacity = "";
    toast("bad", "Could not start the incident", e.message);
  }
}

// ------------------------------------------------------------ incident state
function freshState(id) {
  return { id, meta: null, status: "starting", engine: "", alert: null, brief: null, round: 0, lanes: {}, rounds: {},
    feed: [], calls: {}, findings: [], hyps: [], critique: null, plan: null, approval: null, interrupt: null, actions: [],
    verifications: [], report: null, reportText: "", streaming: false, nodes: {}, edges: new Set(), flowing: null,
    lastEnded: null, activeInv: 0, phase: -1, reached: -1, toolCalls: 0, world: null, telemetry: null, done: false,
    dirty: new Set(["all"]), feedRendered: 0, seen: 0 };
}

let listTimer = null;
function refreshSoon() { clearTimeout(listTimer); listTimer = setTimeout(refreshList, 400); }

function apply(S, ev) {
  if (ev.seq && ev.seq <= S.seen) return;
  if (["brief", "approval_required", "decision", "done", "error"].includes(ev.type)) refreshSoon();
  if (ev.seq) S.seen = ev.seq;
  const D = S.dirty;
  switch (ev.type) {
    case "run_started": S.meta = ev.meta; S.engine = ev.meta.engine; D.add("header"); break;
    case "node": {
      const n = ev.node, st = S.nodes[n] || (S.nodes[n] = { status: "idle", count: 0 });
      if (ev.phase === "start") {
        if (n === "investigate") { if (S.activeInv === 0) st.count += 1; S.activeInv += 1; } else st.count += 1;
        st.status = "active";
        if (S.lastEnded && S.lastEnded !== n) { S.edges.add(`${S.lastEnded}>${n}`); S.flowing = `${S.lastEnded}>${n}`; }
        S.phase = NODE_PHASE[n] ?? S.phase; S.reached = Math.max(S.reached, S.phase);
      } else {
        if (n === "investigate") { S.activeInv = Math.max(0, S.activeInv - 1); if (S.activeInv === 0) st.status = "done"; }
        else st.status = "done";
        if (n !== "investigate" || S.activeInv === 0) S.lastEnded = n;
        if (ev.error) S.feed.push({ kind: "log", agent: "Engine", level: "error", text: `${n} failed: ${ev.error}`, ts: ev.ts });
      }
      D.add("graph"); D.add("phases"); break;
    }
    case "incident": S.alert = ev.alert; S.engine = ev.engine; S.world = ev.world; D.add("header"); D.add("charts"); D.add("map"); break;
    case "log": S.feed.push({ kind: "log", agent: ev.agent, level: ev.level, text: ev.text, ts: ev.ts }); break;
    case "brief": S.brief = ev.brief; D.add("header"); break;
    case "round":
      S.round = ev.round; S.rounds[ev.round] = ev.tasks.map((t) => t.id);
      for (const t of ev.tasks) S.lanes[t.id] = { ...t, status: "queued", tools: [], headline: null };
      D.add("lanes"); D.add("graph"); D.add("charts"); D.add("header"); break;
    case "investigator": {
      const l = S.lanes[ev.task_id]; if (l) { l.status = ev.status === "done" ? "done" : "running"; if (ev.headline) l.headline = ev.headline; }
      D.add("lanes"); D.add("graph"); break;
    }
    case "thought": S.feed.push({ kind: "thought", analyst: ev.analyst, task_id: ev.task_id, text: ev.text, ts: ev.ts }); break;
    case "tool_call":
      S.calls[ev.call_id] = { ...ev, status: "running" }; S.toolCalls += 1;
      if (S.lanes[ev.task_id]) S.lanes[ev.task_id].tools.push(ev.call_id);
      S.feed.push({ kind: "tool", call_id: ev.call_id, analyst: ev.analyst, ts: ev.ts }); D.add("lanes"); D.add("header"); break;
    case "tool_result": {
      const c = S.calls[ev.call_id]; if (c) { c.status = ev.status === "error" ? "error" : "ok"; c.preview = ev.preview; c.charts = ev.charts; }
      D.add("lanes"); D.add("feedUpdate"); break;
    }
    case "finding": S.findings.push(ev.finding); S.feed.push({ kind: "finding", finding: ev.finding, ts: ev.ts }); break;
    case "hypotheses": S.hyps = ev.hypotheses; S.hypRound = ev.round; D.add("hyps"); D.add("map"); D.add("charts"); D.add("header"); break;
    case "critique": S.critique = { ...ev.critique, round: ev.round }; D.add("hyps"); break;
    case "plan": S.plan = ev.plan; S.approval = null; D.add("plan"); break;
    case "approval_required":
      S.interrupt = ev.interrupt; S.status = "awaiting_approval"; S.nodes.approval = { ...(S.nodes.approval || { count: 1 }), status: "wait" };
      D.add("plan"); D.add("header"); D.add("graph"); D.add("phases"); D.add("banner");
      if (!S.replaying) toast("warn", "Approval needed", ev.interrupt.question);
      break;
    case "decision":
      S.interrupt = null; S.status = "running";
      S.feed.push({ kind: "log", agent: "You", level: "decision", ts: ev.ts,
        text: `${ev.decision.decision === "approve" ? "Approved" : "Rejected"} by ${ev.decision.by}${ev.decision.feedback ? `: “${ev.decision.feedback}”` : ""}` });
      D.add("plan"); D.add("banner"); D.add("header"); break;
    case "approval": S.approval = ev.decision; D.add("plan"); break;
    case "action": S.actions.push(ev.result); D.add("plan"); D.add("charts"); break;
    case "verification": S.verifications.push(ev.verification); D.add("plan"); D.add("charts"); D.add("map"); break;
    case "world": S.world = ev.world; D.add("map"); break;
    case "report_start": S.streaming = true; S.reportText = ""; D.add("report"); break;
    case "report_token": S.reportText += ev.text; D.add("report"); break;
    case "report": S.report = ev.report; S.streaming = false; D.add("report"); D.add("header"); break;
    case "status": if (S.status !== "awaiting_approval" || ev.status !== "awaiting_approval") S.status = ev.status; D.add("header"); break;
    case "done":
      S.done = true; S.status = ev.status; S.flowing = null; S.phase = 7; S.reached = 7;
      D.add("header"); D.add("graph"); D.add("phases"); D.add("charts"); D.add("map"); D.add("banner");
      if (!S.replaying) {
        const good = ["resolved", "mitigated"].includes(ev.status);
        toast(good ? "good" : "bad", good ? `Incident ${pretty(ev.status)}` : "Incident escalated",
          good ? "Telemetry confirms recovery. The postmortem is ready." : "Argus could not verify a fix; humans are on it.");
        refreshList();
      }
      break;
    case "error": S.status = "failed"; S.feed.push({ kind: "log", agent: "Engine", level: "error", text: ev.message, ts: ev.ts }); D.add("header");
      if (!S.replaying) toast("bad", "Run failed", ev.message); break;
    default: break;
  }
}

// --------------------------------------------------------------- incident UI
function incidentSkeleton() {
  return `
  <section class="incident">
    <div class="card inc-header" id="hdr"></div>
    <nav class="phases" id="phases" aria-label="Incident phases"></nav>
    <div id="banner"></div>
    <div class="grid">
      <section class="card c-graph"><div class="card-h"><h2>${icon("graph", 16)} Agent graph</h2><span class="aside" id="graphAside">live</span></div>
        <svg class="agent-graph" id="agentGraph" role="img" aria-label="Agent graph execution state"></svg></section>
      <section class="card c-feed"><div class="card-h"><h2>${icon("message", 16)} Live activity</h2>
        <div class="feed-filters" id="feedFilters"><button class="chip-btn" data-f="all" aria-pressed="true">All</button><button class="chip-btn" data-f="decisions" aria-pressed="false">Decisions</button><button class="chip-btn" data-f="tools" aria-pressed="false">Tool calls</button></div></div>
        <div class="feed" id="feed" aria-live="polite"></div></section>
      <section class="card c-lanes"><div class="card-h"><h2>${icon("deps", 16)} Investigators</h2><span class="aside" id="lanesAside"></span></div><div id="lanes"></div></section>
      <section class="card c-signals"><div class="card-h"><h2>${icon("metrics", 16)} Signals</h2><span class="aside" id="sigAside"></span></div>
        <div class="charts" id="charts"><div class="empty">Waiting for telemetry…</div></div>
        <div class="legend-row"><span><i class="lk lk-slo"></i>SLO / limit</span><span><i class="lk lk-alert"></i>Alert fired</span><span><i class="lk lk-change"></i>Change (deploy, flag, config)</span><span><i class="lk lk-action"></i>Argus action</span></div></section>
      <section class="card c-hyps"><div class="card-h"><h2>${icon("sparkle", 16)} Root-cause hypotheses</h2><span class="aside" id="hypAside"></span></div><div id="hyps"><div class="empty">Hypotheses appear after the first investigation round.</div></div></section>
      <section class="card c-plan" id="planCard"><div class="card-h"><h2>${icon("route", 16)} Remediation</h2><span class="aside" id="planAside"></span></div><div id="plan"><div class="empty">No plan yet - Argus does not act before the critic is satisfied.</div></div></section>
      <section class="card c-map"><div class="card-h"><h2>${icon("map", 16)} Service map</h2><span class="aside">live health · hover for details</span></div>
        <svg class="svc-map" id="svcMap" role="img" aria-label="Service dependency map with health"></svg></section>
      <section class="card c-report" id="reportCard"><div class="card-h"><h2>${icon("pen", 16)} Postmortem</h2><div class="report-actions" id="reportActions"></div></div><div id="report"><div class="empty">Written automatically when the incident closes.</div></div></section>
    </div>
  </section>`;
}

async function openIncident(id) {
  const S = freshState(id);
  App.cur = S;
  $("#main").innerHTML = incidentSkeleton();
  $("#feedFilters").addEventListener("click", (ev) => {
    const b = ev.target.closest("button"); if (!b) return;
    $("#feedFilters").querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
    $("#feed").dataset.filter = b.dataset.f;
    applyFeedFilter();
  });
  let snap;
  try { snap = await api(`/api/incidents/${id}`); } catch (e) {
    $("#main").innerHTML = App.demo
      ? `<div class="card empty">Demo incidents live in this browser tab only, so ${esc(id)} was cleared when the page reloaded. <a href="#/">Start a new one</a></div>`
      : `<div class="card empty">Incident ${esc(id)} not found. <a href="#/">Back</a></div>`;
    return;
  }
  S.meta = snap.meta; S.engine = snap.meta?.engine || "";
  if (!snap.has_events) fromSnapshot(S, snap);
  S.replaying = true;
  schedule();
  const es = App.demo ? App.demo.openStream(id) : new EventSource(`/api/incidents/${id}/events?after=0`);
  App.es = es;
  es.onerror = () => { if (S.done) es.close(); };
  es.onmessage = (m) => {
    if (App.cur !== S) return;
    const ev = JSON.parse(m.data);
    apply(S, ev);
    if (S.replaying && ev.seq >= snap.last_seq) S.replaying = false;
    if (ev.type === "done" || ev.type === "error") { S.replaying = false; es.close(); }
    schedule();
  };
  App.telemetryTimer = setInterval(() => { if (!S.done && App.cur === S) loadTelemetry(S); }, 6000);
}

function fromSnapshot(S, snap) {
  const v = snap.values || {};
  S.alert = v.alert; S.brief = v.brief; S.status = snap.status; S.findings = v.findings || []; S.hyps = v.hypotheses || [];
  S.critique = v.critique; S.plan = v.plan; S.approval = v.approval; S.actions = v.executed || []; S.verifications = v.verifications || [];
  S.report = v.report; S.round = v.round || 0; S.interrupt = snap.interrupt; S.engine = v.engine || S.engine;
  for (const t of v.task_log || []) S.lanes[t.id] = { ...t, status: "done", tools: [], headline: null };
  for (const f of S.findings) if (S.lanes[f.task_id]) S.lanes[f.task_id].headline = f.headline;
  for (const t of v.task_log || []) (S.rounds[t.round] = S.rounds[t.round] || []).push(t.id);
  const visited = { intake: v.alert, triage: v.brief, dispatch: v.round, investigate: S.findings.length, synthesize: S.hyps.length,
    critique: v.critique, plan: v.plan, approval: v.approval || snap.interrupt, execute: S.actions.length, verify: S.verifications.length, report: v.report };
  for (const [n, ok] of Object.entries(visited)) if (ok) S.nodes[n] = { status: n === "approval" && snap.interrupt ? "wait" : "done", count: 1 };
  for (const t of v.timeline || []) S.feed.push({ kind: "log", agent: "Timeline", level: "info", text: t.text, ts: t.time });
  S.done = ["resolved", "mitigated", "escalated", "failed"].includes(snap.status);
  S.reached = S.done ? 7 : Math.max(...Object.keys(S.nodes).map((n) => NODE_PHASE[n] ?? 0), 0);
  S.phase = S.reached;
}

let raf = null;
function schedule() { if (!raf) raf = requestAnimationFrame(() => { raf = null; render(); }); }

function render() {
  const S = App.cur; if (!S) return;
  const D = S.dirty, all = D.has("all");
  if (all || D.has("header")) renderHeader(S);
  if (all || D.has("phases") || D.has("header")) renderPhases(S);
  if (all || D.has("banner")) renderBanner(S);
  if (all || D.has("graph")) renderGraphPanel(S);
  renderFeed(S, all || D.has("feedUpdate"));
  if (all || D.has("lanes")) renderLanes(S);
  if (all || D.has("hyps")) renderHyps(S);
  if (all || D.has("plan")) renderPlan(S);
  if (all || D.has("report")) renderReport(S);
  if (all || D.has("charts") || D.has("map")) loadTelemetry(S);
  D.clear();
}

function renderHeader(S) {
  const sev = S.brief?.severity;
  const [label, cls] = STATUS[S.status] || [pretty(S.status), ""];
  const top = S.hyps[0];
  const title = S.brief?.title || S.alert?.title || S.meta?.title || "Opening incident…";
  const alert = S.alert ? `${esc(S.alert.rule)} · fired ${hhmmss(S.alert.fired_at)} · scenario “${esc(S.meta?.title || "")}”` : "";
  const elapsed = S.telemetry && S.alert ? Math.max(0, Math.round((new Date(S.telemetry.now) - new Date(S.alert.fired_at)) / 60000)) : 0;
  $("#hdr").innerHTML = `
    <div>
      <div class="badges">
        ${sev ? `<span class="sev ${esc(sev)}">${icon("alert", 13)}${esc(sev)}</span>` : `<span class="sev none">Triage…</span>`}
        <span class="status-pill ${cls}"><span class="status-dot"></span>${esc(label)}</span>
        <span class="muted mono">${esc(S.id)}</span>
        <span class="chip">${icon("cpu", 12)} ${esc(S.engine || "…")}</span>
      </div>
      <h1 class="inc-title">${esc(title)}</h1>
      <p class="inc-sub">${alert}</p>
    </div>
    <div class="kpis">
      <div class="kpi"><div class="l">Since alert</div><div class="v">${elapsed} min</div></div>
      <div class="kpi"><div class="l">Rounds</div><div class="v">${S.round}</div></div>
      <div class="kpi"><div class="l">Tool calls</div><div class="v">${S.toolCalls || S.findings.reduce((a, f) => a + (f.tool_calls || 0), 0)}</div></div>
      <div class="kpi"><div class="l">Confidence</div><div class="v">${top ? pct(top.confidence) : "–"}</div></div>
    </div>`;
}

function renderPhases(S) {
  $("#phases").innerHTML = PHASES.map((p, i) => {
    let cls = "";
    if (S.done) cls = "done";
    else if (i === 4 && S.status === "awaiting_approval") cls = "wait";
    else if (i === S.phase) cls = "active";
    else if (i <= S.reached) cls = "done";
    return `<div class="phase ${cls}"><span class="n">${cls === "done" ? icon("check", 12) : i + 1}</span>${esc(p)}</div>`;
  }).join("");
}

function renderBanner(S) {
  const el = $("#banner");
  if (S.status !== "awaiting_approval" || !S.interrupt) { el.innerHTML = ""; return; }
  const iv = S.interrupt;
  const gated = (iv.plan?.actions || []).filter((a) => iv.gated.includes(a.id));
  el.innerHTML = `<div class="banner"><div class="b-ic">${icon("pause", 18)}</div><div class="grow">
    <div class="b-t">Argus is waiting for your approval</div>
    <div class="b-s">${gated.length} action(s) need a human under the “${esc(pretty(iv.policy))}” policy: ${gated.map((a) => `${esc(pretty(a.action))} (${esc(a.risk)} risk)`).join(", ")}</div></div>
    <button class="btn btn-primary" id="reviewBtn">Review plan ${icon("arrow", 14)}</button></div>`;
  $("#reviewBtn").onclick = () => $("#planCard").scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderGraphPanel(S) {
  const lanes = (S.rounds[S.round] || []).map((id) => S.lanes[id]).filter(Boolean).map((l) => ({ analyst: l.analyst, status: l.status }));
  renderAgentGraph($("#agentGraph"), App.shape, { nodes: S.nodes, edges: S.edges, flowing: S.done ? null : S.flowing, lanes });
  const active = Object.entries(S.nodes).find(([, v]) => v.status === "active" || v.status === "wait");
  $("#graphAside").textContent = S.done ? "finished" : active ? `running: ${NODE_META[active[0]]?.label || active[0]}` : "live";
}

// ---------------------------------------------------------------------- feed
function agentStyle(item) {
  if (item.analyst) return ANALYST[item.analyst] || { icon: "sparkle", c: "var(--accent)", label: item.analyst };
  if (item.kind === "finding") return ANALYST[item.finding.analyst];
  const a = AGENT[item.agent] || { icon: "sparkle", c: "var(--accent)" };
  return { ...a, label: item.agent };
}

function feedItemHTML(S, item) {
  const st = agentStyle(item);
  const who = st.label || item.agent;
  let body = "", kind = "log", lvl = item.level || "info";
  if (item.kind === "log") {
    body = `<div class="ev-text">${esc(item.text)}</div>`;
    kind = ["decision", "warn", "alert", "error"].includes(lvl) ? "decisions" : "log";
  } else if (item.kind === "thought") {
    body = `<div class="ev-text">${esc(item.text)}</div>`; kind = "tools";
  } else if (item.kind === "tool") {
    body = toolCallHTML(S.calls[item.call_id]); kind = "tools";
  } else if (item.kind === "finding") {
    const f = item.finding;
    body = `<div class="finding-card" style="--c:${st.c}"><div class="h">${esc(f.headline)}</div><div class="b">${esc(f.summary).slice(0, 400)}</div></div>`;
    kind = "decisions"; lvl = "decision";
  }
  return `<div class="ev lvl-${esc(lvl)}" data-kind="${kind}" ${item.call_id ? `data-call="${esc(item.call_id)}"` : ""}>
    <div class="ev-av" style="--c:${st.c}">${icon(st.icon, 16)}</div>
    <div><div class="ev-meta"><span class="ev-agent">${esc(who)}</span>${item.kind === "tool" ? `<span class="muted">→ ${esc(S.calls[item.call_id]?.args?.component || S.calls[item.call_id]?.args?.service || "")}</span>` : ""}<span class="ev-time">${esc(hhmmss(item.ts))}</span></div>${body}</div></div>`;
}

function toolCallHTML(c) {
  if (!c) return "";
  const args = Object.entries(c.args || {}).map(([k, v]) => `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`).join(", ");
  const state = c.status === "running" ? `<span class="tc-state">${icon("loader", 14, "spin")}</span>`
    : c.status === "error" ? `<span class="tc-state err">${icon("x", 14)}</span>` : `<span class="tc-state ok">${icon("check", 14)}</span>`;
  const sparks = (c.charts || []).filter((ch) => ch.values?.length > 1).map((ch) =>
    `<figure>${sparkline(ch.values)}<figcaption>${esc(ch.component)} · ${esc(metricLabel(ch.metric))}</figcaption></figure>`).join("");
  return `<details class="toolcall"><summary>${state}<span class="fn">${esc(c.tool)}</span><span class="args">(${esc(args)})</span></summary>
    ${sparks ? `<div class="spark">${sparks}</div>` : ""}${c.preview ? `<pre>${esc(c.preview)}</pre>` : ""}</details>`;
}

function renderFeed(S, updateCalls) {
  const el = $("#feed");
  if (!el._wired) {
    el._wired = true;
    el.addEventListener("scroll", () => { S.stick = el.scrollHeight - el.scrollTop - el.clientHeight < 60; });
  }
  const nearBottom = S.stick !== false;
  const frag = [];
  for (let i = S.feedRendered; i < S.feed.length; i++) frag.push(feedItemHTML(S, S.feed[i]));
  if (frag.length) {
    if (!S.feedRendered) el.innerHTML = "";
    el.insertAdjacentHTML("beforeend", frag.join(""));
    S.feedRendered = S.feed.length;
  }
  if (updateCalls) {
    el.querySelectorAll(".ev[data-call]").forEach((node) => {
      const c = S.calls[node.dataset.call];
      const tc = node.querySelector(".toolcall");
      if (c && tc && tc.dataset.status !== c.status) {
        const open = tc.open;
        tc.outerHTML = toolCallHTML(c);
        const fresh = node.querySelector(".toolcall");
        fresh.dataset.status = c.status;
        fresh.open = open;
      }
    });
  }
  if (!S.feed.length) el.innerHTML = `<div class="empty">Connecting to the incident stream…</div>`;
  applyFeedFilter();
  if (frag.length && (nearBottom || S.replaying)) el.scrollTop = el.scrollHeight;
}

function applyFeedFilter() {
  const el = $("#feed"); if (!el) return;
  const f = el.dataset.filter || "all";
  el.querySelectorAll(".ev").forEach((n) => { n.style.display = f === "all" || n.dataset.kind === f ? "" : "none"; });
}

// --------------------------------------------------------------- investigators
function renderLanes(S) {
  const rounds = Object.keys(S.rounds).map(Number).sort((a, b) => b - a);
  $("#lanesAside").textContent = rounds.length ? `${Object.keys(S.lanes).length} tasks across ${rounds.length} round(s)` : "";
  if (!rounds.length) { $("#lanes").innerHTML = `<div class="empty">Investigators are dispatched right after triage.</div>`; return; }
  $("#lanes").innerHTML = rounds.map((r) => {
    const lanes = (S.rounds[r] || []).map((id) => S.lanes[id]).filter(Boolean);
    return `<div class="round"><div class="round-h">Round ${r}${r > 1 ? " · requested by the critic" : " · dispatched by triage"}</div><div class="lanes">${lanes.map((l) => {
      const a = ANALYST[l.analyst] || { label: l.analyst, icon: "sparkle", c: "var(--accent)" };
      const steps = l.tools.map((cid) => {
        const c = S.calls[cid] || {};
        const st = c.status === "running" ? icon("loader", 12, "spin") : c.status === "error" ? icon("x", 12) : icon("check", 12);
        return `<li>${st}<span>${esc(c.tool || "")}</span></li>`;
      }).join("");
      const state = l.status === "done" ? `<span class="lane-state ok">${icon("check", 16)}</span>` : l.status === "running" ? `<span class="lane-state">${icon("loader", 16, "spin")}</span>` : `<span class="lane-state">${icon("clock", 16)}</span>`;
      return `<div class="lane ${l.status}" style="--c:${a.c}"><div class="lane-h"><span class="lane-ic">${icon(a.icon, 16)}</span><div><div class="lane-name">${esc(a.label)}</div><div class="lane-target">${esc(l.target)}</div></div>${state}</div>
        <div class="lane-obj">${esc(l.objective)}</div><ul class="steps">${steps}</ul>${l.headline ? `<div class="lane-result">${esc(l.headline)}</div>` : ""}</div>`;
    }).join("")}</div></div>`;
  }).join("");
}

// ----------------------------------------------------------------- hypotheses
const SRC_COLOR = { metrics: "var(--s1)", logs: "var(--s2)", changes: "var(--s3)", dependencies: "var(--s4)", memory: "var(--accent)" };

function renderHyps(S) {
  $("#hypAside").textContent = S.hyps.length ? `${S.hyps.length} candidate(s)` : "";
  if (!S.hyps.length) return;
  const hyps = S.hyps.map((h, i) => {
    const ev = (h.supporting || []).map((s) => {
      const m = String(s).match(/^\[(\w+)\]\s*(.*)$/);
      const src = m ? m[1] : "metrics";
      return `<li><span class="src" style="--c:${SRC_COLOR[src] || "var(--accent)"}" title="${esc(src)}"></span><span><b>${esc(src)}</b> · ${esc(m ? m[2] : s)}</span></li>`;
    }).join("") + (h.contradicting || []).map((s) => `<li class="against"><span>${icon("alert", 13)}</span><span>${esc(s)}</span></li>`).join("");
    return `<div class="hyp${i === 0 ? " top" : ""}"><div class="hyp-h"><span class="rank">${esc(h.id)}</span><div style="min-width:0;flex:1">
      <div class="hyp-t">${esc(h.title)}</div>
      <div class="chips"><span class="chip">${esc(pretty(h.category))}</span><span class="chip mono">${esc(h.component)}</span>${h.change_id ? `<span class="chip mono">${esc(h.change_id)}</span>` : ""}</div>
      <div class="meter" title="Confidence ${pct(h.confidence)}"><div class="meter-track"><div class="meter-fill" style="width:${Math.round(h.confidence * 100)}%"></div></div><span class="meter-v">${pct(h.confidence)}</span></div>
      <details class="evidence"${i === 0 ? " open" : ""}><summary>Evidence (${(h.supporting || []).length} for${(h.contradicting || []).length ? `, ${(h.contradicting || []).length} against` : ""})</summary><ul>${ev}</ul></details>
    </div></div></div>`;
  }).join("");
  const c = S.critique && (S.critique.round == null || S.hypRound == null || S.critique.round >= S.hypRound) ? S.critique : null;
  const pending = S.critique && !c ? `<div class="critique more"><span class="ci">${icon("loader", 16, "spin")}</span><div><div class="ct">Critic is reviewing round ${esc(S.hypRound)}…</div></div></div>` : "";
  const crit = c ? `<div class="critique ${c.proceed ? "ok" : "more"}"><span class="ci">${icon(c.proceed ? "check" : "refresh", 16)}</span><div>
      <div class="ct">Critic: ${c.proceed ? "evidence is sufficient" : "needs more evidence"}</div><div class="cb">${esc(c.reasoning)}</div>
      ${(c.follow_up_tasks || []).length && !c.proceed ? `<div class="chips">${c.follow_up_tasks.map((t) => `<span class="chip">${esc(ANALYST[t.analyst]?.label || t.analyst)} → <span class="mono">${esc(t.target)}</span></span>`).join("")}</div>` : ""}
    </div></div>` : "";
  $("#hyps").innerHTML = hyps + (crit || pending);
}

// ------------------------------------------------------------------- plan
function riskBadge(r) {
  const ic = { low: "check", medium: "alert", high: "alert" }[r] || "alert";
  return `<span class="risk ${esc(r)}">${icon(ic, 12)}${esc(r)} risk</span>`;
}

function renderPlan(S) {
  const p = S.plan, el = $("#plan");
  if (!p) return;
  const iv = S.status === "awaiting_approval" ? S.interrupt : null;
  const gated = new Set(iv?.gated || []);
  $("#planAside").textContent = `plan v${p.revision || 1}${p.runbook_ids?.length ? ` · runbooks ${p.runbook_ids.join(", ")}` : ""}`;
  const done = Object.fromEntries(S.actions.map((a) => [a.action_id, a]));
  const actions = (p.actions || []).map((a) => {
    const res = done[a.id];
    const params = Object.entries(a.params || {}).map(([k, v]) => {
      if (iv && gated.has(a.id) && ["replicas", "max_connections", "to_version"].includes(k)) {
        return `<label class="chip mono">${esc(k)} <input class="param-edit" data-action="${esc(a.id)}" data-param="${esc(k)}" value="${esc(v)}" ${k === "to_version" ? "" : 'inputmode="numeric"'} ${App.demo ? `disabled title="${esc(DEMO_EDIT)}"` : ""}></label>`;
      }
      return `<span class="chip mono">${esc(k)}=${esc(v)}</span>`;
    }).join("");
    const gate = iv ? (gated.has(a.id) ? `<span class="gate need">needs your approval</span>` : `<span class="gate">auto-approved by policy</span>`) : "";
    const resHTML = res ? `<div class="a-e" style="color:var(${res.ok ? "--good-ink" : "--crit-ink"})">${icon(res.ok ? "check" : "x", 12)} ${esc(res.message)} · ${esc(res.approved_by || "")}</div>` : "";
    return `<div class="action">${iv && gated.has(a.id) ? `<input type="checkbox" checked data-approve="${esc(a.id)}" aria-label="Approve ${esc(a.action)}" ${App.demo ? `disabled title="${esc(DEMO_EDIT)}"` : ""}>` : `<span class="a-ic">${icon(ACTION_ICON[a.action] || "zap", 16)}</span>`}
      <div><div class="a-t">${esc(pretty(a.action))} ${riskBadge(a.risk)} ${gate}</div><div class="chips">${params}</div>
      <div class="a-r">${esc(a.rationale)}</div><div class="a-e">Expected: ${esc(a.expected_effect)}</div>${resHTML}</div></div>`;
  }).join("") || `<div class="empty">No automated action fits - escalating to the owning team.</div>`;
  const approve = iv ? `<div class="approve-box"><b>${icon("usercheck", 15)} ${esc(iv.question)}</b>
      <div class="row"><input class="who" id="who" placeholder="Your name" value="${esc(store.get("argus.name") || "")}">
      <button class="btn btn-primary" id="approveBtn">${icon("check", 15)} Approve selected</button>
      <button class="btn btn-danger" id="rejectBtn">${icon("x", 15)} Reject…</button></div>
      <textarea id="feedback" placeholder="Optional feedback for Argus - e.g. “Don't fail over, it changes our fees. Contain the blast radius first.”"></textarea>
      ${App.demo ? `<div class="demo-note">${icon("play", 13)} Demo: approving or rejecting picks a recorded branch of the real agent. ${esc(DEMO_EDIT)}</div>` : ""}</div>` : "";
  const ap = S.approval && !iv ? `<dl class="kv"><dt>Decision</dt><dd>${esc(S.approval.decision)} by ${esc(S.approval.by)}${S.approval.comment ? ` - ${esc(S.approval.comment)}` : ""}</dd></dl>` : "";
  const ver = S.verifications.map((v) => {
    const cls = v.recovered ? "good" : v.status === "partially_mitigated" || v.status === "temporarily_mitigated" ? "mid" : "bad";
    return `<div class="subhead">Verification #${esc(v.attempt)} <span class="verdict ${cls}">${icon(v.recovered ? "check" : "alert", 12)} ${esc(pretty(v.status))}</span></div>
      <div class="a-r">${esc(v.summary)}</div>
      <table class="checks"><thead><tr><th>Check</th><th>Before</th><th>After</th><th></th></tr></thead><tbody>${(v.checks || []).map((c) =>
        `<tr><td>${esc(c.label)}${c.note ? `<div class="muted">${esc(c.note)}</div>` : ""}</td><td>${esc(fmtValue(c.metric, c.before))}</td><td>${esc(fmtValue(c.metric, c.after))}</td><td class="${c.ok ? "ok" : "no"}">${icon(c.ok ? "check" : "x", 14)}</td></tr>`).join("")}</tbody></table>`;
  }).join("");
  el.innerHTML = `<p class="plan-sum">${esc(p.summary)}</p>${actions}${approve}${ap}
    ${p.fallback ? `<dl class="kv"><dt>Fallback</dt><dd>${esc(p.fallback)}</dd></dl>` : ""}${ver}`;
  if (iv) {
    $("#approveBtn").onclick = () => sendDecision(S, "approve");
    $("#rejectBtn").onclick = () => {
      const fb = $("#feedback");
      if (!fb.value.trim()) { fb.focus(); fb.placeholder = "Tell Argus why - it will re-plan with your feedback."; toast("info", "Add a reason", "Feedback lets Argus re-plan instead of escalating."); return; }
      sendDecision(S, "reject");
    };
  }
}

async function sendDecision(S, kind) {
  const by = ($("#who")?.value || "").trim() || "on-call engineer";
  store.set("argus.name", by);
  const approved = [...document.querySelectorAll("[data-approve]")].filter((x) => x.checked).map((x) => x.dataset.approve);
  const edits = {};
  document.querySelectorAll(".param-edit").forEach((inp) => {
    const v = inp.value.trim(); if (!v) return;
    (edits[inp.dataset.action] = edits[inp.dataset.action] || {})[inp.dataset.param] = /^\d+$/.test(v) ? Number(v) : v;
  });
  const body = { decision: kind === "approve" && approved.length ? "approve" : "reject", approved_ids: approved, edits,
    feedback: ($("#feedback")?.value || "").trim() || null, by };
  ["#approveBtn", "#rejectBtn"].forEach((s) => { const b = $(s); if (b) b.disabled = true; });
  try { await api(`/api/incidents/${S.id}/decision`, { method: "POST", body: JSON.stringify(body) }); }
  catch (e) { toast("bad", "Decision failed", e.message); ["#approveBtn", "#rejectBtn"].forEach((s) => { const b = $(s); if (b) b.disabled = false; }); }
}

// --------------------------------------------------------------- telemetry
let telemetryInflight = false;
async function loadTelemetry(S) {
  if (telemetryInflight || !S.alert) return;
  telemetryInflight = true;
  try {
    const root = S.hyps[0]?.component;
    const suspects = [root, S.hyps[1]?.component, ...S.findings.map((f) => f.target)];
    const second = suspects.find((c) => c && c !== S.alert.service);
    const comps = [S.alert.service, second].filter((c, i, a) => c && a.indexOf(c) === i);
    const t = await api(`/api/incidents/${S.id}/telemetry?components=${encodeURIComponent(comps.join(","))}`);
    if (App.cur !== S) return;
    S.telemetry = t;
    renderCharts(S, t);
    renderServiceMap($("#svcMap"), App.topo, t.health, root || null);
    renderHeader(S);
  } catch { /* transient */ } finally { telemetryInflight = false; }
}

function pickMetrics(name, comp, isAlert, alertMetric) {
  const ms = Object.entries(comp.metrics);
  if (isAlert) {
    const pref = [alertMetric, alertMetric === "latency_p99_ms" ? "error_rate_pct" : "latency_p99_ms"];
    return pref.filter((m) => comp.metrics[m]);
  }
  const anomalous = ms.filter(([, v]) => v.anomaly).map(([m]) => m);
  const order = ["memory_pct", "connections", "throughput_qps", "query_latency_p95_ms", "error_rate_pct", "latency_p99_ms", "restarts", "cpu_pct"];
  anomalous.sort((a, b) => (order.indexOf(a) + 99) % 99 - (order.indexOf(b) + 99) % 99);
  return (anomalous.length ? anomalous : ms.map(([m]) => m)).slice(0, 2);
}

function renderCharts(S, t) {
  const start = new Date(t.window_start).getTime();
  const fmtT = (min) => new Date(start + min * 60000).toISOString().slice(11, 16);
  const items = [];
  Object.entries(t.components).forEach(([name, comp], idx) => {
    for (const m of pickMetrics(name, comp, idx === 0, S.alert.metric)) items.push({ name, m, d: comp.metrics[m] });
  });
  const grid = $("#charts");
  const key = items.map((i) => `${i.name}:${i.m}`).join("|");
  if (grid.dataset.key !== key) {
    grid.dataset.key = key;
    grid.innerHTML = items.map((i, n) => `<div class="chart"><div class="chart-h"><span class="chart-t"><b>${esc(i.name)}</b> · ${esc(metricLabel(i.m))}</span><span class="chart-v" id="cv${n}"></span></div><div id="cc${n}"></div></div>`).join("");
  }
  $("#sigAside").textContent = `now ${fmtT(t.clock)} UTC (simulated)`;
  const fixed = ["resolved", "mitigated"].includes(S.status);
  const lastAction = Math.max(-1, ...t.markers.filter((mk) => mk.kind === "action").map((mk) => mk.t));
  items.forEach((i, n) => {
    const pts = i.d.points;
    const last = pts.length ? pts[pts.length - 1][1] : null;
    const cv = $(`#cv${n}`);
    cv.textContent = fmtValue(i.m, last);
    cv.className = `chart-v${i.d.limit && last > i.d.limit ? " bad" : ""}`;
    lineChart($(`#cc${n}`), {
      metric: i.m, points: pts, limit: i.d.limit, title: `${i.name} ${metricLabel(i.m)}`,
      limitLabel: i.m === "connections" ? `max ${i.d.limit}` : i.d.limit ? `SLO ${fmtValue(i.m, i.d.limit)}` : "",
      markers: t.markers, anomalyT: i.d.anomaly?.onset_t, anomalyEnd: fixed && lastAction >= 0 ? lastAction : null,
      limitPoints: i.d.limit_points, fmtT, aria: `${i.name} ${metricLabel(i.m)} over time`,
    });
  });
}

// --------------------------------------------------------------------- report
function renderReport(S) {
  const el = $("#report");
  const text = S.report?.postmortem || S.reportText;
  if (!text) return;
  const su = S.report?.status_update;
  el.innerHTML = `${su ? `<div class="status-update"><span class="si">${icon("message", 16)}</span><div><div class="st">Customer status update</div><div class="sb">${esc(su)}</div></div></div>` : ""}
    <div class="md${S.streaming ? " caret" : ""}">${renderMarkdown(text)}</div>`;
  if (S.report && !$("#dlBtn")) {
    $("#reportActions").innerHTML = `<button class="btn" id="copyBtn">${icon("copy", 15)} Copy</button><a class="btn" id="dlBtn" href="${App.demo ? (App.demo.postmortemUrl(S.id) || "#") : `/api/incidents/${esc(S.id)}/postmortem.md`}" download="${esc(S.id)}-postmortem.md">${icon("download", 15)} Download .md</a>`;
    $("#copyBtn").onclick = async () => { try { await navigator.clipboard.writeText(S.report.postmortem); toast("good", "Copied", "Postmortem copied as Markdown."); } catch { toast("bad", "Copy failed", "Clipboard not available."); } };
  }
}

boot().catch((e) => { $("#main").innerHTML = `<div class="card empty">Failed to start the UI: ${esc(e.message)}</div>`; });
