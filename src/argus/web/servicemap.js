// Service map of the simulated estate: tiers top -> bottom, live health (with icon + label in the
// tooltip, never colour alone), the root cause ringed, and the blast path animated.
import { esc } from "./markdown.js";
import { hideTip, showTip } from "./charts.js";

const POS = {
  "web-frontend": [360, 28], "api-gateway": [360, 86],
  "auth-service": [60, 152], "catalog-service": [180, 152], "search-service": [300, 152],
  "recommendation-service": [420, 152], "cart-service": [540, 152], "checkout-service": [662, 152],
  "notification-service": [420, 218], "inventory-service": [572, 218], "payment-service": [676, 218],
  "users-db": [60, 286], "catalog-db": [166, 286], "search-index": [272, 286], "redis-cache": [378, 286],
  "event-bus": [480, 286], "orders-db": [580, 286], "payrail-api": [680, 286],
};
const NW = 102, NH = 28, W = 736, H = 314;
const HEALTH = { healthy: ["var(--good)", "Healthy", "✓"], degraded: ["var(--warn)", "Degraded", "!"],
  critical: ["var(--crit)", "Critical", "✕"], unknown: ["var(--text-3)", "Unknown", "?"] };

function short(name) { return name.replace(/-service$/, ""); }

export function renderServiceMap(svgEl, topo, health = {}, root = null) {
  const comps = Object.fromEntries(topo.components.map((c) => [c.name, c]));
  const pos = (n) => POS[n] || [W / 2, H / 2];
  const affected = new Set(Object.entries(health).filter(([, h]) => h && h !== "healthy").map(([n]) => n));
  if (root) affected.add(root);
  const parts = [];
  parts.push(`<text class="sm-tier" x="4" y="14">EDGE</text><text class="sm-tier" x="4" y="130">SERVICES</text><text class="sm-tier" x="4" y="264">DATA &amp; THIRD-PARTY</text>`);
  for (const e of topo.edges) {
    const [x1, y1] = pos(e.caller), [x2, y2] = pos(e.callee);
    const hot = affected.has(e.caller) && affected.has(e.callee);
    let d;
    if (Math.abs(y1 - y2) < 2) {
      const dir = x2 > x1 ? 1 : -1;
      d = `M${x1 + dir * NW / 2},${y1} L${x2 - dir * NW / 2},${y2}`;
    } else {
      const sy = y1 + NH / 2, ty = y2 - NH / 2, my = (sy + ty) / 2;
      d = `M${x1},${sy} C${x1},${my} ${x2},${my} ${x2},${ty}`;
    }
    parts.push(`<path class="sm-edge${hot ? " hot" : ""}" d="${d}"/>`);
  }
  for (const [name, c] of Object.entries(comps)) {
    const [x, y] = pos(name);
    const h = health[name] || "unknown";
    const [col] = HEALTH[h] || HEALTH.unknown;
    const isRoot = root === name;
    parts.push(`<g class="sm-node ${esc(h)}${isRoot ? " root" : ""}" data-name="${esc(name)}" tabindex="0">
      ${isRoot ? `<rect class="ring" x="${x - NW / 2 - 4}" y="${y - NH / 2 - 4}" width="${NW + 8}" height="${NH + 8}" rx="11"/><text class="tag" x="${x}" y="${y - NH / 2 - 8}" text-anchor="middle">ROOT CAUSE</text>` : ""}
      <rect x="${x - NW / 2}" y="${y - NH / 2}" width="${NW}" height="${NH}" rx="8"/>
      <circle class="hdot" cx="${x - NW / 2 + 12}" cy="${y}" r="4.5" fill="${col}"/>
      <text x="${x - NW / 2 + 22}" y="${y + 4}">${esc(short(name))}</text></g>`);
  }
  svgEl.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svgEl.innerHTML = parts.join("");
  svgEl.querySelectorAll(".sm-node").forEach((g) => {
    const name = g.dataset.name, c = comps[name];
    const [, label, glyph] = HEALTH[health[name] || "unknown"] || HEALTH.unknown;
    const html = `<div class="tv">${esc(name)}</div><div>${glyph} ${esc(label)}${root === name ? " · <b>root cause</b>" : ""}</div>
      <div class="tm">${esc(c.kind)} · team ${esc(c.team)}<br>${esc(c.tech)}${c.version ? ` · v${esc(c.version)}` : ""}</div>`;
    const show = (ev) => { const r = g.getBoundingClientRect(); showTip(html, ev.clientX || r.right, ev.clientY || r.top); };
    g.addEventListener("pointermove", show);
    g.addEventListener("focus", show);
    g.addEventListener("pointerleave", hideTip);
    g.addEventListener("blur", hideTip);
  });
}
