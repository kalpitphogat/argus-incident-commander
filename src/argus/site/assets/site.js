// Argus website: the hero replay, scroll storytelling and small interactions. No framework, no build.
import { AGENT, ANALYST, LOGO, icon } from "./icons.js";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;

// ------------------------------------------------------------------ nav, reveal, spotlight
const nav = $("#nav");
const onScroll = () => nav.classList.toggle("scrolled", scrollY > 8);
addEventListener("scroll", onScroll, { passive: true });
onScroll();

const revealer = new IntersectionObserver((entries) => {
  for (const e of entries) if (e.isIntersecting) { e.target.classList.add("in"); revealer.unobserve(e.target); }
}, { rootMargin: "0px 0px -12% 0px", threshold: 0.08 });
$$(".reveal").forEach((el) => revealer.observe(el));

const sections = $$("main section[id]");
const navLinks = new Map($$(".nav-links a").map((a) => [a.getAttribute("href").slice(1), a]));
const spy = new IntersectionObserver((entries) => {
  for (const e of entries) {
    const link = navLinks.get(e.target.id);
    if (link) link.classList.toggle("active", e.isIntersecting);
  }
}, { rootMargin: "-45% 0px -50% 0px" });
sections.forEach((s) => spy.observe(s));

document.addEventListener("pointermove", (ev) => {
  const card = ev.target.closest?.(".spot");
  if (!card) return;
  const r = card.getBoundingClientRect();
  card.style.setProperty("--mx", `${ev.clientX - r.left}px`);
  card.style.setProperty("--my", `${ev.clientY - r.top}px`);
}, { passive: true });

// ------------------------------------------------------------------ statement: words light up as you read
const statement = $("#statement");
if (statement) {
  const words = statement.textContent.trim().split(/\s+/);
  statement.innerHTML = words.map((w) => `<span class="w">${esc(w)}</span> `).join("");
  const spans = $$(".w", statement);
  const paint = () => {
    const r = statement.getBoundingClientRect();
    const start = innerHeight * 0.88, end = innerHeight * 0.35;
    const p = reduced ? 1 : clamp((start - r.top) / (start - end + r.height * 0.6), 0, 1);
    const lit = p * spans.length;
    spans.forEach((s, i) => s.style.setProperty("--o", (0.18 + 0.82 * clamp(lit - i, 0, 1)).toFixed(3)));
  };
  addEventListener("scroll", () => requestAnimationFrame(paint), { passive: true });
  addEventListener("resize", paint);
  paint();
}

// ------------------------------------------------------------------ hero: tilt that settles as you scroll
const consoleEl = $("#console");
const tilt = () => {
  if (!consoleEl || reduced || innerWidth < 860) { consoleEl?.style.removeProperty("--tilt"); consoleEl?.style.removeProperty("--zoom"); return; }
  const p = clamp(scrollY / 520, 0, 1);
  consoleEl.style.setProperty("--tilt", `${(13 * (1 - p)).toFixed(2)}deg`);
  consoleEl.style.setProperty("--zoom", (0.95 + 0.05 * p).toFixed(4));
};
addEventListener("scroll", () => requestAnimationFrame(tilt), { passive: true });
addEventListener("resize", tilt);
tilt();

// ------------------------------------------------------------------ hero: replay recorded runs
const PHASES = ["Triage", "Investigate", "Diagnose", "Plan", "Approve", "Act", "Verify", "Report"];
const STATUS = {
  triaging: ["Triaging", ""], investigating: ["Investigating", ""], planning: ["Planning", ""],
  awaiting_approval: ["Awaiting approval", "wait"], mitigating: ["Mitigating", ""], verifying: ["Verifying", ""],
  replanning: ["Re-planning", ""], resolved: ["Resolved", "good"], mitigated: ["Mitigated", "good"],
  escalated: ["Escalated", "wait"], failed: ["Failed", "wait"],
};
const PACE = { phase: 0, status: 0, round: 650, tool: 110, find: 600, hyps: 950, crit: 950, ask: 2200, ok: 1100,
  act: 1000, ver: 1500, done: 5200 };
const LOG_PACE = { info: 520, decision: 800, warn: 950, alert: 1100 };

function hhmm(anchor, t) {
  const d = new Date(Date.parse(anchor) + t * 60000);
  return `${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")}`;
}

function fmt(v, unit) {
  if (unit === "%") return `${v < 10 ? v.toFixed(2) : v.toFixed(1)}%`;
  if (unit === "ms") return `${Math.round(v).toLocaleString("en-US")} ms`;
  return `${Math.round(v).toLocaleString("en-US")}${unit ? ` ${unit}` : ""}`;
}

class Reel {
  constructor(root, reels, tabsEl) {
    this.root = root; this.reels = reels; this.tabsEl = tabsEl;
    this.idx = 0; this.timer = 0; this.visible = true; this.pending = false; this.raf = 0;
    root.innerHTML = `
      <div class="c-head">
        <span class="c-brand">${LOGO}<span>Argus</span></span><span class="c-sep"></span>
        <div class="c-title"><b data-r="title"></b><span data-r="sub"></span></div>
        <div class="c-badges"><span class="sev" data-r="sev"></span><span class="status" data-r="status"><i></i><span></span></span></div>
        <div class="c-stats">
          <div class="c-stat"><span>Since alert</span><b data-r="age">0 min</b></div>
          <div class="c-stat"><span>Rounds</span><b data-r="rounds">0</b></div>
          <div class="c-stat"><span>Tool calls</span><b data-r="tools">0</b></div>
          <div class="c-stat"><span>Confidence</span><b data-r="conf">&ndash;</b></div>
        </div>
      </div>
      <div class="c-phases" data-r="phases">${PHASES.map((p, i) => `<span class="ph" data-p="${i}"><span class="ph-n">${i + 1}</span>${p}</span>`).join("")}</div>
      <div class="c-body">
        <div class="c-feed" data-r="feed"></div>
        <div class="c-side">
          <div class="chart">
            <div class="chart-head"><b data-r="svc"></b><span data-r="metric"></span><span class="chart-val" data-r="val"></span></div>
            <div class="chart-plot" data-r="plot">
              <svg viewBox="0 0 1000 300" preserveAspectRatio="none" aria-hidden="true">
                <defs><linearGradient id="cg" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#3d8bea" stop-opacity=".26"/><stop offset="1" stop-color="#3d8bea" stop-opacity="0"/></linearGradient></defs>
                <line class="grid" x1="0" x2="1000" y1="75" y2="75"/><line class="grid" x1="0" x2="1000" y1="150" y2="150"/><line class="grid" x1="0" x2="1000" y1="225" y2="225"/>
                <line class="limit" data-r="limit" x1="0" x2="1000"/>
                <path class="area" data-r="area"/><path class="line" data-r="line"/>
              </svg>
              <span class="limit-label" data-r="limitLabel"></span>
              <span class="dot" data-r="dot"></span>
            </div>
            <div class="chart-axis"><span data-r="ax0"></span><span data-r="ax1"></span><span data-r="ax2"></span></div>
          </div>
          <div class="mod-wrap" data-r="mods"></div>
        </div>
      </div>`;
    this.r = Object.fromEntries($$("[data-r]", root).map((el) => [el.dataset.r, el]));
    tabsEl.innerHTML = reels.map((x, i) => `<button type="button" class="reel-tab" data-i="${i}" aria-pressed="false">${esc(x.tab)}<i></i></button>`).join("");
    tabsEl.addEventListener("click", (e) => { const b = e.target.closest(".reel-tab"); if (b) this.load(Number(b.dataset.i), true); });
    new IntersectionObserver(([e]) => { this.visible = e.isIntersecting; this.wake(); }, { threshold: 0.05 }).observe(root);
    document.addEventListener("visibilitychange", () => this.wake());
  }

  get running() { return this.visible && document.visibilityState === "visible"; }

  wake() { if (this.running && this.pending) { this.pending = false; this.schedule(0); } }

  load(i, user = false) {
    clearTimeout(this.timer);
    this.idx = (i + this.reels.length) % this.reels.length;
    const R = this.reel = this.reels[this.idx];
    this.pos = 0; this.rounds = 0; this.tools = 0; this.phase = -1; this.reached = -1; this.lanes = [];
    this.clock = this.shown = R.steps[0]?.t ?? R.alert_t; this.status = "triaging";
    $$(".reel-tab", this.tabsEl).forEach((b, k) => { b.setAttribute("aria-pressed", String(k === this.idx)); b.style.setProperty("--p", 0); });
    const r = this.r;
    r.title.textContent = R.title;
    r.sub.textContent = `${R.incident_id} · ${R.scenario}`;
    r.sev.textContent = R.severity || "SEV";
    r.sev.className = `sev ${String(R.severity).toLowerCase()}`;
    r.feed.innerHTML = "";
    r.mods.innerHTML = "";
    r.svc.textContent = R.service;
    r.metric.textContent = ` · ${R.metric.label}`;
    r.conf.innerHTML = "&ndash;";
    const M = R.metric;
    this.ymax = Math.max(...M.values, M.limit || 0) * 1.2 || 1;
    if (M.limit != null) {
      const y = this.y(M.limit);
      r.limit.setAttribute("y1", y); r.limit.setAttribute("y2", y);
      r.limitLabel.style.top = `${(y / 300) * 100}%`;
      r.limitLabel.textContent = `SLO ${fmt(M.limit, M.unit)}`;
    }
    $$(".mk", r.plot).forEach((m) => m.remove());
    this.markers = R.markers.map((m) => {
      const el = document.createElement("span");
      const x = this.xPct(m.t);
      el.className = `mk ${m.kind}${x > 66 ? " flip" : ""}`;
      el.style.left = `${x}%`;
      el.innerHTML = `<span>${esc(m.label)}</span>`;
      r.plot.append(el);
      return { ...m, el };
    });
    r.ax0.textContent = hhmm(R.anchor, M.t0);
    r.ax1.textContent = hhmm(R.anchor, (M.t0 + M.t1) / 2);
    r.ax2.textContent = hhmm(R.anchor, M.t1);
    this.setStatus("triaging");
    this.setPhase(0);
    this.draw();
    if (reduced) { this.finish(); return; }
    if (user || this.running) this.schedule(user ? 150 : 400); else this.pending = true;
  }

  finish() { while (this.pos < this.reel.steps.length) this.apply(this.reel.steps[this.pos++], true); this.shown = this.clock; this.draw(); this.progress(); }

  schedule(ms) {
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.tick(), ms);
  }

  tick() {
    if (!this.running) { this.pending = true; return; }
    const R = this.reel;
    if (this.pos >= R.steps.length) { this.load(this.idx + 1); return; }
    const s = R.steps[this.pos++];
    this.apply(s, false);
    this.progress();
    const wait = s.k === "log" ? LOG_PACE[s.l] ?? 650 : PACE[s.k] ?? 300;
    this.schedule(wait);
  }

  progress() {
    const tab = $$(".reel-tab", this.tabsEl)[this.idx];
    if (tab) tab.style.setProperty("--p", (this.pos / this.reel.steps.length).toFixed(3));
  }

  // ---------------------------------------------------------------- state changes
  apply(s, instant) {
    if (s.t != null && s.t > this.clock) { this.clock = s.t; instant ? (this.shown = s.t) : this.tween(); }
    switch (s.k) {
      case "phase": this.setPhase(s.p); break;
      case "status": this.setStatus(s.s); break;
      case "log": this.feed(AGENT[s.a] || { icon: "sparkle", c: "var(--accent)" }, s.a, s.x, s.l, s.t); this.logHook(s); break;
      case "tool": this.tools += 1; this.r.tools.textContent = this.tools; break;
      case "round": this.rounds = s.n; this.r.rounds.textContent = s.n; this.modLanes(s); break;
      case "find": {
        const a = ANALYST[s.a] || { label: s.a, icon: "sparkle", c: "var(--accent)" };
        this.feed(a, a.label, s.x, "find", s.t);
        const lane = this.lanes.find((l) => l.a === s.a && l.g === s.g && !l.el.classList.contains("done"));
        if (lane) { lane.el.classList.add("done"); $(".ln-x", lane.el).textContent = s.x; $(".ln-st", lane.el).innerHTML = icon("check", 14); }
        break;
      }
      case "hyps": this.modHyps(s); this.r.conf.textContent = `${s.h[0]?.c ?? 0}%`; break;
      case "crit": this.verdict(s); break;
      case "ask": this.modAsk(s); break;
      case "ok": this.approve(instant); this.setStatus("mitigating"); break;
      case "act": this.modAct(s); break;
      case "ver": this.modVer(s); break;
      case "done": this.setStatus(s.s); this.setPhase(7); this.doneCard(s); break;
    }
    if (instant) this.draw();
  }

  logHook(s) {
    if (s.a === "Memory" && this.phase <= 0) {
      const m = /(INC-\w+) - (.+)$/.exec(s.x);
      if (m) this.mod(`<div class="mod-title">${icon("database", 13)} <b>Incident memory</b></div>
        <div class="hy lead"><div class="hy-top"><span class="hy-id">${esc(m[1])}</span><span class="hy-x" style="white-space:normal">${esc(m[2])}</span></div></div>
        <div class="verdict good">${icon("check", 14)}<span>Similar past incident recalled before the investigation starts.</span></div>`);
    }
  }

  setPhase(p) {
    this.phase = p; this.reached = Math.max(this.reached, p);
    $$(".ph", this.r.phases).forEach((el) => {
      const i = Number(el.dataset.p);
      el.classList.toggle("on", i === p);
      el.classList.toggle("wait", i === p && this.status === "awaiting_approval");
      el.classList.toggle("done", i !== p && i <= this.reached);
      $(".ph-n", el).innerHTML = i !== p && i <= this.reached ? icon("check", 11) : String(i + 1);
    });
  }

  setStatus(st) {
    this.status = st;
    const [label, cls] = STATUS[st] || [st, ""];
    this.r.status.className = `status ${cls}`;
    $("span", this.r.status).textContent = label;
    $$(".ph.on", this.r.phases).forEach((el) => el.classList.toggle("wait", st === "awaiting_approval"));
  }

  feed(who, name, text, level, t) {
    const el = document.createElement("div");
    el.className = `fi ${level}`;
    el.style.setProperty("--c", who.c);
    el.innerHTML = `<span class="fi-ic">${icon(who.icon, 15)}</span><div><div class="fi-who">${esc(name)}<time>${hhmm(this.reel.anchor, t)}</time></div><div class="fi-x">${esc(text)}</div></div>`;
    this.r.feed.append(el);
    while (this.r.feed.children.length > 14) this.r.feed.firstElementChild.remove();
  }

  // ---------------------------------------------------------------- right-hand modules
  mod(html) {
    const el = document.createElement("div");
    el.className = "mod";
    el.innerHTML = html;
    const old = $$(".mod", this.r.mods);
    this.r.mods.append(el);
    requestAnimationFrame(() => requestAnimationFrame(() => el.classList.add("on")));
    old.forEach((o) => { o.classList.remove("on"); setTimeout(() => o.remove(), 500); });
    return el;
  }

  modLanes(s) {
    const el = this.mod(`<div class="mod-title">${icon("deps", 13)} <b>Round ${s.n}</b> · ${s.lanes.length} investigators in parallel</div>
      ${s.lanes.map(([a, g]) => { const A = ANALYST[a] || { label: a, icon: "sparkle", c: "var(--accent)" };
        return `<div class="ln" style="--c:${A.c}" data-a="${esc(a)}" data-g="${esc(g)}"><span class="ln-ic">${icon(A.icon, 14)}</span>
          <div class="ln-main"><div class="ln-name">${esc(A.label)}<em>${esc(g)}</em></div><div class="ln-x"></div></div>
          <span class="ln-st">${icon("loader", 14)}</span></div>`; }).join("")}`);
    this.lanes = $$(".ln", el).map((n) => ({ a: n.dataset.a, g: n.dataset.g, el: n }));
  }

  modHyps(s) {
    this.hypEl = this.mod(`<div class="mod-title">${icon("sparkle", 13)} <b>Root-cause hypotheses</b></div>
      ${s.h.slice(0, 2).map((h, i) => `<div class="hy${i === 0 ? " lead" : ""}"><div class="hy-top"><span class="hy-id">${esc(h.id)}</span><span class="hy-x">${esc(h.x)}</span><b>${h.c}%</b></div>
        <span class="hy-track"><i style="--w:0%"></i></span></div>`).join("")}`);
    const bars = $$(".hy-track i", this.hypEl);
    requestAnimationFrame(() => requestAnimationFrame(() => bars.forEach((b, i) => b.style.setProperty("--w", `${s.h[i].c}%`))));
  }

  verdict(s) {
    if (!this.hypEl) return;
    const v = document.createElement("div");
    v.className = `verdict ${s.ok ? "good" : "warn"}`;
    v.innerHTML = s.ok ? `${icon("shield", 14)}<span><b>Critic:</b> corroborated by independent sources. Safe to plan.</span>`
      : `${icon("shield", 14)}<span><b>Critic:</b> not yet. An alternative is too close; ${s.n} follow-up task${s.n === 1 ? "" : "s"}.</span>`;
    this.hypEl.append(v);
  }

  modAsk(s) {
    this.askEl = this.mod(`<div class="mod-title">${icon("usercheck", 13)} <b>Human approval</b> · policy: ${esc(s.policy || "medium")}</div>
      <div class="ask"><div class="ask-q">${icon("pause", 13)} Waiting for the on-call engineer</div>
        ${s.acts.map((a) => `<div class="ask-act"><span>${esc(a.x)}</span><span class="risk r-${esc(a.r)}">${esc(a.r)} risk</span></div>`).join("")}
        <div class="ask-btns"><span class="abtn ghost">${icon("x", 13)} Reject</span><span class="abtn primary">${icon("check", 13)} Approve</span></div></div>`);
  }

  approve(instant) {
    const b = this.askEl && $(".abtn.primary", this.askEl);
    if (!b) return;
    const done = () => { b.classList.remove("press"); b.classList.add("done"); b.innerHTML = `${icon("check", 13)} Approved`; $(".ask-q", this.askEl).innerHTML = `${icon("usercheck", 13)} Approved by on-call`; };
    if (instant) { done(); return; }
    b.classList.add("press");
    setTimeout(done, 220);
  }

  modAct(s) {
    this.mod(`<div class="mod-title">${icon("zap", 13)} <b>Remediation</b></div>
      <div class="ln done" style="--c:var(--good)"><span class="ln-ic">${icon("zap", 14)}</span>
        <div class="ln-main"><div class="ln-name">${esc(s.x)}</div><div class="ln-x">${s.ok ? `applied at ${esc(s.at)} · ${esc(s.r)} risk · approved` : "failed"}</div></div>
        <span class="ln-st">${icon(s.ok ? "check" : "x", 14)}</span></div>
      <div class="verdict good">${icon("activity", 14)}<span>Watching the SLOs before calling it resolved.</span></div>`);
  }

  modVer(s) {
    this.verEl = this.mod(`<div class="mod-title">${icon("activity", 13)} <b>Verification</b> · ${esc(s.s)}</div>
      ${s.checks.slice(0, 3).map((c, i) => `<div class="vr" style="animation-delay:${i * 120}ms"><span class="vr-ic">${icon(c.ok ? "check" : "x", 12)}</span><span class="vr-x">${esc(c.x)}</span><span class="vr-n"><s>${esc(c.b)}</s> → <b>${esc(c.a)}</b></span></div>`).join("")}`);
  }

  doneCard(s) {
    const host = this.verEl || this.mod("");
    const n = this.reel.numbers || {};
    const bits = [n.rca != null && `root cause in ${n.rca} min`, n.mitigate != null && `recovered in ${n.mitigate} min`].filter(Boolean).join(" · ");
    const d = document.createElement("div");
    d.className = "done-card";
    d.innerHTML = `<span class="done-ic">${icon("pen", 16)}</span><div><b>${s.s === "mitigated" ? "Mitigated" : "Resolved"} · postmortem ready</b><span>${esc(bits || "Report written")}</span></div>`;
    host.append(d);
  }

  // ---------------------------------------------------------------- chart
  xPct(t) { const M = this.reel.metric; return clamp(((t - M.t0) / (M.t1 - M.t0)) * 100, 0, 100); }
  y(v) { return 300 - (v / this.ymax) * 288; }

  tween() {
    cancelAnimationFrame(this.raf);
    const from = this.shown, to = this.clock, t0 = performance.now(), dur = 700;
    const step = (now) => {
      const k = clamp((now - t0) / dur, 0, 1), e = 1 - (1 - k) ** 3;
      this.shown = from + (to - from) * e;
      this.draw();
      if (k < 1) this.raf = requestAnimationFrame(step);
    };
    this.raf = requestAnimationFrame(step);
  }

  valueAt(t) {
    const M = this.reel.metric, i = t - M.t0, n = M.values.length;
    if (i <= 0) return M.values[0];
    if (i >= n - 1) return M.values[n - 1];
    const a = Math.floor(i), f = i - a;
    return M.values[a] * (1 - f) + M.values[a + 1] * f;
  }

  draw() {
    const R = this.reel, M = R.metric, r = this.r, end = Math.min(this.shown, M.t0 + M.values.length - 1);
    const pts = [];
    for (let t = M.t0; t < end; t++) pts.push([t, M.values[t - M.t0]]);
    pts.push([end, this.valueAt(end)]);
    const X = (t) => ((t - M.t0) / (M.t1 - M.t0)) * 1000;
    const d = pts.map(([t, v], i) => `${i ? "L" : "M"}${X(t).toFixed(1)} ${this.y(v).toFixed(1)}`).join("");
    r.line.setAttribute("d", d);
    r.area.setAttribute("d", `${d}L${X(end).toFixed(1)} 300L${X(M.t0)} 300Z`);
    const v = this.valueAt(end);
    r.dot.style.left = `${this.xPct(end)}%`;
    r.dot.style.top = `${(this.y(v) / 300) * 100}%`;
    r.val.textContent = fmt(v, M.unit);
    r.val.classList.toggle("ok", M.limit != null && v <= M.limit);
    for (const m of this.markers) m.el.classList.toggle("show", this.shown >= m.t - 0.01);
    r.age.textContent = `${Math.max(0, Math.round(this.shown - R.alert_t))} min`;
  }
}

const reelData = (() => { try { return JSON.parse($("#reels")?.textContent || "[]"); } catch { return []; } })();
if (consoleEl && reelData.length) {
  const reel = new Reel(consoleEl, reelData, $("#reelTabs"));
  reel.load(0);
}

// ------------------------------------------------------------------ how it works: graph follows the story
const steps = $$(".how-step");
const graph = $("#howGraph");
function activate(step) {
  const i = steps.indexOf(step);
  steps.forEach((s, k) => s.classList.toggle("on", k === i));
  if (!graph) return;
  const on = new Set(step.dataset.nodes.split(" "));
  const before = new Set(steps.slice(0, i).flatMap((s) => s.dataset.nodes.split(" ")));
  const edges = new Set(step.dataset.edges.split(" "));
  $$(".n", graph).forEach((n) => { n.classList.toggle("on", on.has(n.dataset.n)); n.classList.toggle("done", before.has(n.dataset.n)); });
  $$(".e", graph).forEach((e) => e.classList.toggle("on", edges.has(e.dataset.e)));
  $$(".e-label", graph).forEach((l) => l.classList.toggle("on", l.dataset.e.split(" ").some((k) => edges.has(k))));
}
if (steps.length) {
  activate(steps[0]);
  const io = new IntersectionObserver((entries) => {
    for (const e of entries) if (e.isIntersecting) activate(e.target);
  }, { rootMargin: "-48% 0px -48% 0px" });
  steps.forEach((s) => io.observe(s));
}

// ------------------------------------------------------------------ tabs (tour + code)
$$('[role="tablist"]').forEach((list) => {
  const tabs = $$('[role="tab"]', list);
  const select = (tab, focus) => {
    tabs.forEach((t) => {
      const on = t === tab;
      t.setAttribute("aria-selected", String(on));
      t.tabIndex = on ? 0 : -1;
      const panel = document.getElementById(t.getAttribute("aria-controls"));
      if (panel) panel.hidden = !on;
    });
    if (focus) tab.focus();
  };
  list.addEventListener("click", (e) => { const t = e.target.closest('[role="tab"]'); if (t) select(t, false); });
  list.addEventListener("keydown", (e) => {
    const i = tabs.indexOf(document.activeElement);
    if (i < 0) return;
    const next = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: tabs.length - 1 }[e.key];
    if (next == null) return;
    e.preventDefault();
    select(tabs[(next + tabs.length) % tabs.length], true);
  });
});

// ------------------------------------------------------------------ copy buttons
$$(".copy").forEach((b) => b.addEventListener("click", async () => {
  const text = b.parentElement.querySelector("pre").innerText.split("\n").filter((l) => !l.trim().startsWith("#")).join("\n");
  try {
    await navigator.clipboard.writeText(text);
    b.classList.add("ok");
    b.innerHTML = icon("check", 14);
    setTimeout(() => { b.classList.remove("ok"); b.innerHTML = icon("copy", 14); }, 1600);
  } catch { /* clipboard unavailable (insecure context); the text stays selectable */ }
}));

// ------------------------------------------------------------------ benchmark numbers count up once
const counters = $$("[data-count]");
const counter = new IntersectionObserver((entries) => {
  for (const e of entries) {
    if (!e.isIntersecting) continue;
    counter.unobserve(e.target);
    const el = e.target, m = /^(\d+)(.*)$/.exec(el.textContent.trim());
    if (!m || reduced) continue;
    const to = Number(m[1]), suffix = m[2], t0 = performance.now();
    const step = (now) => {
      const k = clamp((now - t0) / 1200, 0, 1);
      el.textContent = `${Math.round(to * (1 - (1 - k) ** 3))}${suffix}`;
      if (k < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }
}, { threshold: 0.6 });
counters.forEach((c) => counter.observe(c));
