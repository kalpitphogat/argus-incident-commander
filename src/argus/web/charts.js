// SVG time-series charts: one series per chart (small multiples), zero-based y, hairline grid,
// SLO/limit line, event markers, and a crosshair tooltip that snaps to the nearest minute.
import { esc } from "./markdown.js";

const UNITS = {
  latency_p99_ms: ["p99 latency", "ms"], query_latency_p95_ms: ["p95 query latency", "ms"],
  error_rate_pct: ["Error rate", "%"], throughput_rps: ["Throughput", "rps"], throughput_qps: ["Queries", "qps"],
  throughput_ops: ["Ops", "ops/s"], throughput_msgs: ["Messages", "msg/s"], cpu_pct: ["CPU", "%"],
  memory_pct: ["Memory", "%"], restarts: ["Restarts", ""], connections: ["Connections", ""], consumer_lag: ["Consumer lag", ""],
};

export function metricLabel(m) { return (UNITS[m] || [m, ""])[0]; }

export function fmtValue(metric, v) {
  if (v == null || Number.isNaN(v)) return "–";
  const unit = (UNITS[metric] || ["", ""])[1];
  let s;
  if (Math.abs(v) >= 1000) s = Math.round(v).toLocaleString("en-US");
  else if (Math.abs(v) >= 100) s = v.toFixed(0);
  else if (Math.abs(v) >= 10) s = v.toFixed(1);
  else s = v.toFixed(2);
  return unit === "%" ? `${s}%` : unit ? `${s} ${unit}` : s;
}

function niceStep(max, ticks = 3) {
  const raw = max / ticks;
  const mag = 10 ** Math.floor(Math.log10(raw || 1));
  const n = raw / mag;
  return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * mag;
}

function tickLabel(v) {
  if (v >= 10000) return `${Math.round(v / 1000)}K`;
  if (v >= 1000) return v.toLocaleString("en-US");
  return Number.isInteger(v) ? String(v) : v.toFixed(1);
}

const tip = () => document.getElementById("tooltip");

export function showTip(html, x, y) {
  const t = tip();
  t.innerHTML = html;
  t.classList.add("show");
  const r = t.getBoundingClientRect();
  let left = x + 14, top = y - r.height - 10;
  if (left + r.width > window.innerWidth - 8) left = x - r.width - 14;
  if (top < 8) top = y + 16;
  t.style.left = `${left}px`;
  t.style.top = `${top}px`;
}
export function hideTip() { tip().classList.remove("show"); }

/**
 * opts: { metric, points: [[t, v]], limit, limitLabel, markers: [{t, kind, label, text}], anomalyT, fmtT }
 */
export function lineChart(container, opts) {
  const draw = () => {
    const W = Math.max(240, container.clientWidth || 320);
    const H = 132;
    const m = { l: 40, r: 10, t: 14, b: 20 };
    const pts = opts.points || [];
    if (!pts.length) { container.innerHTML = '<div class="empty">No data</div>'; return; }
    const t0 = pts[0][0], t1 = pts[pts.length - 1][0];
    const lp = opts.limitPoints;
    const vmax = Math.max(...pts.map((p) => p[1]), opts.limit || 0, ...(lp || []).map((p) => p[1])) * 1.12 || 1;
    const step = niceStep(vmax, 3);
    const ymax = Math.ceil(vmax / step) * step;
    const x = (t) => m.l + ((t - t0) / Math.max(1, t1 - t0)) * (W - m.l - m.r);
    const y = (v) => m.t + (1 - v / ymax) * (H - m.t - m.b);
    const parts = [];
    for (let v = 0; v <= ymax + 1e-9; v += step) {
      parts.push(`<line class="${v === 0 ? "axis-line" : "grid-line"}" x1="${m.l}" x2="${W - m.r}" y1="${y(v)}" y2="${y(v)}"/>`);
      parts.push(`<text class="tick" x="${m.l - 6}" y="${y(v) + 3}" text-anchor="end">${tickLabel(v)}</text>`);
    }
    const span = t1 - t0;
    const xstep = span > 60 ? 20 : span > 30 ? 10 : 5;
    for (let t = Math.ceil(t0 / xstep) * xstep; t <= t1; t += xstep) {
      parts.push(`<text class="tick" x="${x(t)}" y="${H - 4}" text-anchor="middle">${esc(opts.fmtT(t))}</text>`);
    }
    if (opts.anomalyT != null && opts.anomalyT <= t1) {
      const ax = x(Math.max(t0, opts.anomalyT));
      const bx = opts.anomalyEnd != null && opts.anomalyEnd > opts.anomalyT ? x(Math.min(t1, opts.anomalyEnd)) : W - m.r;
      parts.push(`<rect class="anomaly-band" x="${ax}" y="${m.t}" width="${Math.max(0, bx - ax)}" height="${H - m.t - m.b}"/>`);
    }
    for (const mk of opts.markers || []) {
      if (mk.t < t0 || mk.t > t1) continue;
      const cls = mk.kind === "alert" ? "alert" : mk.kind === "action" ? "action" : "";
      parts.push(`<line class="marker-line ${cls}" x1="${x(mk.t)}" x2="${x(mk.t)}" y1="${m.t - 4}" y2="${H - m.b}"/>`);
    }
    if (lp && lp.length > 1) {
      const d = lp.map((p, i) => i ? `H${x(p[0]).toFixed(1)}V${y(p[1]).toFixed(1)}` : `M${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join("") + `H${(W - m.r).toFixed(1)}`;
      parts.push(`<path class="limit" fill="none" d="${d}"/>`);
      parts.push(`<text class="limit-label" x="${m.l + 4}" y="${y(lp[0][1]) - 4}" text-anchor="start">max ${esc(lp[0][1])}</text>`);
      parts.push(`<text class="limit-label" x="${W - m.r}" y="${y(lp[lp.length - 1][1]) - 4}" text-anchor="end">max ${esc(lp[lp.length - 1][1])}</text>`);
    } else if (opts.limit) {
      parts.push(`<line class="limit" x1="${m.l}" x2="${W - m.r}" y1="${y(opts.limit)}" y2="${y(opts.limit)}"/>`);
      parts.push(`<text class="limit-label" x="${m.l + 4}" y="${y(opts.limit) - 4}" text-anchor="start">${esc(opts.limitLabel || "limit")}</text>`);
    }
    const line = pts.map((p, i) => `${i ? "L" : "M"}${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join("");
    const area = `${line}L${x(t1).toFixed(1)},${y(0)}L${x(t0).toFixed(1)},${y(0)}Z`;
    parts.push(`<path class="area" d="${area}"/><path class="line" d="${line}"/>`);
    const last = pts[pts.length - 1];
    parts.push(`<circle class="end-dot" cx="${x(last[0])}" cy="${y(last[1])}" r="4"/>`);
    parts.push(`<line class="xhair" x1="0" x2="0" y1="${m.t}" y2="${H - m.b}" visibility="hidden"/><circle class="hover-dot" r="4.5" visibility="hidden"/>`);
    parts.push(`<rect class="hit" x="${m.l}" y="0" width="${W - m.l - m.r}" height="${H}" fill="transparent"/>`);
    container.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="${esc(opts.aria || "")}">${parts.join("")}</svg>`;
    const svg = container.querySelector("svg");
    const xh = svg.querySelector(".xhair"), hd = svg.querySelector(".hover-dot");
    const move = (ev) => {
      const r = svg.getBoundingClientRect();
      const px = ((ev.clientX - r.left) / r.width) * W;
      const t = t0 + ((px - m.l) / (W - m.l - m.r)) * (t1 - t0);
      let best = pts[0];
      for (const p of pts) if (Math.abs(p[0] - t) < Math.abs(best[0] - t)) best = p;
      xh.setAttribute("x1", x(best[0])); xh.setAttribute("x2", x(best[0])); xh.setAttribute("visibility", "visible");
      hd.setAttribute("cx", x(best[0])); hd.setAttribute("cy", y(best[1])); hd.setAttribute("visibility", "visible");
      const near = (opts.markers || []).filter((mk) => Math.abs(mk.t - best[0]) <= 1);
      showTip(`<div class="tv">${esc(fmtValue(opts.metric, best[1]))}</div><div><span class="tk"></span>${esc(opts.title || "")} · ${esc(opts.fmtT(best[0]))} UTC</div>` +
        near.map((mk) => `<div class="tm"><b>${esc(mk.label)}</b> ${esc(mk.text || "")}</div>`).join(""), ev.clientX, ev.clientY);
    };
    const leave = () => { xh.setAttribute("visibility", "hidden"); hd.setAttribute("visibility", "hidden"); hideTip(); };
    svg.addEventListener("pointermove", move);
    svg.addEventListener("pointerleave", leave);
  };
  draw();
  if (!container._ro) {
    container._ro = new ResizeObserver(() => draw());
    container._ro.observe(container);
  }
  container._draw = draw;
}

export function sparkline(values, w = 120, h = 28) {
  if (!values || values.length < 2) return "";
  const max = Math.max(...values) || 1, min = Math.min(0, ...values);
  const x = (i) => (i / (values.length - 1)) * (w - 4) + 2;
  const y = (v) => h - 3 - ((v - min) / (max - min || 1)) * (h - 6);
  const d = values.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("");
  return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" aria-hidden="true"><path d="${d}L${x(values.length - 1)},${h - 2}L2,${h - 2}Z" fill="var(--chart-wash)"/><path d="${d}" fill="none" stroke="var(--chart)" stroke-width="1.6" stroke-linejoin="round"/></svg>`;
}
