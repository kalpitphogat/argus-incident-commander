// Static demo mode (e.g. GitHub Pages): replays recordings of the real Argus agent in the browser.
//
// `argus export-demo` records every scenario through the real LangGraph runner: the full UI event
// stream, the telemetry behind the charts, and two branches at the first approval (approve, or reject
// with feedback). This module stands in for the HTTP API and the SSE stream, so app.js runs unchanged.

const DELAY = {
  run_started: 0, node: 70, status: 0, incident: 250, log: 520, brief: 380, round: 450, investigator: 170,
  thought: 380, tool_call: 230, tool_result: 330, finding: 520, hypotheses: 650, critique: 600, plan: 650,
  approval_required: 0, decision: 150, approval: 350, action: 850, verification: 1000, world: 0,
  report_start: 300, report_token: 9, report: 250, done: 0, error: 0,
};

class HttpError extends Error {}

function isoAt(anchor, minutes) {
  return new Date(Date.parse(anchor) + minutes * 60000).toISOString().replace(".000Z", "Z");
}

class Player {
  constructor(id, data, speed) {
    this.id = id;
    this.data = data;
    this.speed = speed;
    this.branch = "approve";
    this.queue = data.approve.events;
    this.pos = 0;
    this.seq = 0;
    this.emitted = [];
    this.sub = null;
    this.status = "running";
    this.waiting = false;
    this.decisions = 0;
    this.patch = null;
    this.clock = data.approve.events[0]?._clock ?? 0;
    this.meta = { ...data.approve.events[0].meta, incident_id: id };
    this.timer = setTimeout(() => this.step(), 0);
  }

  get telemetry() { return this.data[this.branch].telemetry; }

  step() {
    if (this.pos >= this.queue.length) return;
    const ev = JSON.parse(JSON.stringify(this.queue[this.pos++]));
    if (ev._clock != null) this.clock = ev._clock;
    delete ev._clock;
    ev.seq = ++this.seq;
    ev.ts = new Date().toISOString();
    if (ev.type === "run_started") ev.meta = this.meta;
    if (ev.type === "brief") this.meta.severity = ev.brief.severity;
    if (ev.type === "decision" && this.patch) { ev.decision = { ...ev.decision, ...this.patch }; this.patch = null; }
    if (ev.type === "status" && !this.waiting) this.status = ev.status;
    if (ev.type === "approval_required") { this.waiting = true; this.status = "awaiting_approval"; }
    if (ev.type === "done") this.status = ev.status;
    if (ev.type === "error") this.status = "failed";
    this.meta.status = this.status;
    this.emitted.push(ev);
    if (this.sub) this.sub(ev);
    if (this.waiting || ev.type === "done" || ev.type === "error") return;  // a "resolved" status precedes the report
    const next = this.queue[this.pos];
    if (next) this.timer = setTimeout(() => this.step(), (DELAY[next.type] ?? 200) * this.speed);
  }

  decide(d) {
    if (!this.waiting) throw new HttpError("409 This incident is not waiting for a decision.");
    if (d.decision === "reject") {
      if (this.decisions > 0 || !this.data.reject) {
        throw new HttpError("This branch was not recorded for the demo. Approve to continue, or run Argus yourself for the live agent.");
      }
      this.branch = "reject";
      this.queue = this.data.reject.events;
      this.pos = 0;
    }
    this.patch = { decision: d.decision, by: d.by || "you", feedback: d.feedback || null, comment: d.comment || null };
    this.decisions += 1;
    this.waiting = false;
    this.status = "running";
    this.timer = setTimeout(() => this.step(), 120);
  }

  snapshot() {
    return { incident_id: this.id, status: this.status, values: { incident_id: this.id }, interrupt: null, next: [],
      has_events: true, last_seq: this.emitted.length, meta: this.meta };
  }

  telemetryFor(components) {
    const T = this.telemetry, clock = this.clock, start = Math.max(0, clock - 75);
    const out = { window_start: T.window_start, clock, now: isoAt(T.anchor, clock), components: {}, markers: [], health: {} };
    const keys = Object.keys(T.health).map(Number).sort((a, b) => a - b);
    let hk = keys[0];
    for (const k of keys) if (k <= clock) hk = k;
    out.health = T.health[String(hk)] || {};
    for (const name of components.slice(0, 4)) {
      const S = T.series[name];
      if (!S) continue;
      const metrics = {};
      for (const [m, vals] of Object.entries(S)) {
        const points = [];
        for (let t = start; t < Math.min(clock, vals.length); t++) points.push([t, vals[t]]);
        const onset = T.anomaly[name]?.[m];
        const entry = { points, limit: (T.limits[name] || {})[m] ?? null, anomaly: onset != null && onset <= clock ? { onset_t: onset } : null };
        if (m === "connections" && T.caps[name]) {
          const capAt = (t) => T.caps[name].reduce((c, [at, v]) => (t >= at ? v : c), T.caps[name][0][1]);
          entry.limit = capAt(clock - 1);
          const steps = points.map(([t]) => [t, capAt(t)]);
          if (new Set(steps.map((s) => s[1])).size > 1) entry.limit_points = steps;
        }
        metrics[m] = entry;
      }
      out.components[name] = { kind: T.kinds[name], health: out.health[name], metrics };
    }
    out.markers.push({ t: T.alert_t, kind: "alert", label: "Alert" });
    const rel = new Set(Object.keys(out.components));
    for (const c of T.changes) {
      if (c.t >= clock - 75 && c.t <= clock && (rel.has(c.component) || c.kind === "feature_flag" || c.author === "argus-agent")) {
        out.markers.push({ t: c.t, kind: c.author === "argus-agent" ? "action" : c.kind, label: c.id, text: c.summary });
      }
    }
    return out;
  }
}

export async function initDemo(base) {
  const get = async (file) => {
    const r = await fetch(new URL(file, base));
    if (!r.ok) throw new HttpError(`could not load ${file} (${r.status})`);
    return r.json();
  };
  const meta = await get("meta.json");
  const cache = new Map();
  const players = new Map();
  const load = (file) => { if (!cache.has(file)) cache.set(file, get(file)); return cache.get(file); };
  const newId = () => `INC-${Math.random().toString(16).slice(2, 8).toUpperCase()}`;
  const ok = (v) => JSON.parse(JSON.stringify(v));

  async function api(path, opts = {}) {
    const method = (opts.method || "GET").toUpperCase();
    if (path === "/api/health") return ok(meta.health);
    if (path === "/api/scenarios") return ok({ scenarios: meta.scenarios });
    if (path === "/api/graph") return ok(meta.graph);
    if (path === "/api/topology") return ok(meta.topology);
    if (path === "/api/incidents" && method === "GET") {
      const list = [...players.values()].map((p) => ({ ...p.meta, status: p.status })).reverse();
      return { incidents: list };
    }
    if (path === "/api/incidents" && method === "POST") {
      const body = JSON.parse(opts.body || "{}");
      let file;
      if (String(body.scenario).startsWith("chaos")) {
        file = `chaos/${meta.chaos[Math.floor(Math.random() * meta.chaos.length)]}.json`;
      } else if (meta.scenarios.some((s) => s.id === body.scenario)) {
        file = `scenarios/${body.scenario}.json`;
      } else throw new HttpError(`404 unknown scenario '${body.scenario}'`);
      const data = await load(file);
      const id = newId();
      const speed = Number(body.pace) > 0 ? 1 : 0.06;
      const p = new Player(id, data, speed);
      players.set(id, p);
      return { incident_id: id, meta: p.meta };
    }
    let m = path.match(/^\/api\/incidents\/([\w-]+)$/);
    if (m) {
      const p = players.get(m[1]);
      if (!p) throw new HttpError("404 This demo incident is no longer in memory (the page was reloaded). Start a new one.");
      return p.snapshot();
    }
    m = path.match(/^\/api\/incidents\/([\w-]+)\/decision$/);
    if (m && method === "POST") {
      const p = players.get(m[1]);
      if (!p) throw new HttpError("404 incident not found");
      p.decide(JSON.parse(opts.body || "{}"));
      return { ok: true };
    }
    m = path.match(/^\/api\/incidents\/([\w-]+)\/telemetry\?components=(.*)$/);
    if (m) {
      const p = players.get(m[1]);
      if (!p) throw new HttpError("404 incident not found");
      return p.telemetryFor(decodeURIComponent(m[2]).split(",").filter(Boolean));
    }
    throw new HttpError(`404 ${path} is not available in the static demo`);
  }

  function openStream(id) {
    const p = players.get(id);
    const es = { onmessage: null, onerror: null, closed: false,
      close() { this.closed = true; if (p && p.sub === deliver) p.sub = null; } };
    const deliver = (ev) => { if (!es.closed && es.onmessage) es.onmessage({ data: JSON.stringify(ev) }); };
    setTimeout(() => {
      if (!p || es.closed) return;
      for (const ev of p.emitted) deliver(ev);
      p.sub = deliver;
    }, 0);
    return es;
  }

  function postmortemUrl(id) {
    const p = players.get(id);
    const md = p?.emitted.find((e) => e.type === "report")?.report?.postmortem;
    return md ? URL.createObjectURL(new Blob([md], { type: "text/markdown" })) : null;
  }

  return { api, openStream, postmortemUrl, meta };
}
