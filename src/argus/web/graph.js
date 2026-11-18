// Live diagram of the compiled LangGraph: nodes light up as they run, traversed edges glow,
// loops are drawn as labelled arcs. Edges come from /api/graph, so the picture is the real graph.
import { icon } from "./icons.js";
import { esc } from "./markdown.js";

export const NODE_META = {
  intake: { label: "Intake", icon: "bell" },
  triage: { label: "Triage", icon: "triage" },
  dispatch: { label: "Dispatch", icon: "flag" },
  investigate: { label: "Investigators", icon: "deps" },
  synthesize: { label: "RCA synthesis", icon: "sparkle" },
  critique: { label: "Critic", icon: "shield" },
  plan: { label: "Plan", icon: "route" },
  approval: { label: "Human approval", icon: "usercheck" },
  execute: { label: "Execute", icon: "zap" },
  verify: { label: "Verify", icon: "activity" },
  report: { label: "Postmortem", icon: "pen" },
};

const LOOP_LABEL = { "critique>dispatch": "needs evidence", "approval>plan": "rejected", "verify>plan": "not recovered",
  "plan>report": "escalate", "approval>report": "escalate" };

/**
 * state: { nodes: {name: {status: idle|active|done|wait, count}}, edges: Set("a>b"), flowing: "a>b"|null,
 *          lanes: [{analyst, status}] }
 */
export function renderAgentGraph(svgEl, shape, state) {
  const order = shape.nodes.map((n) => n.id).filter((n) => !n.startsWith("__"));
  const idx = Object.fromEntries(order.map((n, i) => [n, i]));
  const W = 320, rowH = 46, top = 22, nodeW = 168, nodeH = 32, cx = 132;
  const H = top + order.length * rowH;
  const yOf = (n) => top + idx[n] * rowH + nodeH / 2;
  const left = cx - nodeW / 2, right = cx + nodeW / 2;
  const edges = shape.edges.filter((e) => idx[e.source] != null && idx[e.target] != null);
  const parts = [`<defs>
    <marker id="ah" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M0,0L10,5L0,10z" fill="var(--line-strong)"/></marker>
    <marker id="ah-on" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M0,0L10,5L0,10z" fill="var(--accent)"/></marker></defs>`];
  let rightLane = 0, leftLane = 0;
  const sorted = [...edges].sort((a, b) => Math.abs(idx[a.target] - idx[a.source]) - Math.abs(idx[b.target] - idx[b.source]));
  for (const e of sorted) {
    const key = `${e.source}>${e.target}`;
    const on = state.edges.has(key);
    const flow = state.flowing === key;
    const cls = `ag-edge${e.conditional ? " cond" : ""}${on ? " on" : ""}${flow ? " flow" : ""}`;
    const mk = on || flow ? "url(#ah-on)" : "url(#ah)";
    const ys = yOf(e.source), yt = yOf(e.target);
    const d = idx[e.target] - idx[e.source];
    if (d === 1) {
      parts.push(`<path class="${cls}" d="M${cx},${ys + nodeH / 2} L${cx},${yt - nodeH / 2 - 1}" marker-end="${mk}"/>`);
    } else if (d < 0) {
      rightLane += 1;
      const lx = right + 14 + rightLane * 16, r = 8;
      parts.push(`<path class="${cls}" d="M${right},${ys} H${lx - r} Q${lx},${ys} ${lx},${ys - r} V${yt + r} Q${lx},${yt} ${lx - r},${yt} H${right + 2}" marker-end="${mk}"/>`);
      const lab = LOOP_LABEL[key];
      if (lab) parts.push(`<text class="ag-edge-label" x="${lx + 9}" y="${(ys + yt) / 2}" transform="rotate(90 ${lx + 9} ${(ys + yt) / 2})" text-anchor="middle">${esc(lab)}</text>`);
    } else {
      leftLane += 1;
      const lx = left - 12 - leftLane * 14, r = 8;
      parts.push(`<path class="${cls}" d="M${left},${ys} H${lx + r} Q${lx},${ys} ${lx},${ys + r} V${yt - r} Q${lx},${yt} ${lx + r},${yt} H${left - 2}" marker-end="${mk}"/>`);
    }
  }
  for (const n of order) {
    const meta = NODE_META[n] || { label: n, icon: "sparkle" };
    const st = state.nodes[n] || { status: "idle", count: 0 };
    const y = yOf(n) - nodeH / 2;
    const cnt = st.count > 1 ? `<text class="ag-count" x="${right - 10}" y="${y + 20}" text-anchor="end">×${st.count}</text>` : "";
    let mini = "";
    if (n === "investigate" && state.lanes && state.lanes.length) {
      const colors = { metrics: "var(--s1)", logs: "var(--s2)", changes: "var(--s3)", dependencies: "var(--s4)" };
      mini = state.lanes.slice(0, 6).map((l, i) =>
        `<circle class="ag-mini" cx="${right - 12 - i * 11 - (st.count > 1 ? 18 : 0)}" cy="${y + nodeH / 2}" r="4.5" fill="${l.status === "done" ? colors[l.analyst] : "var(--surface-3)"}" stroke="${colors[l.analyst]}"><title>${esc(l.analyst)}</title></circle>`).join("");
    }
    parts.push(`<g class="ag-node ${st.status}" data-node="${esc(n)}">
      <rect class="halo" x="${left}" y="${y}" width="${nodeW}" height="${nodeH}" rx="10"/>
      <rect class="box" x="${left}" y="${y}" width="${nodeW}" height="${nodeH}" rx="10"/>
      <g class="ic" transform="translate(${left + 10},${y + 8})">${icon(meta.icon, 16)}</g>
      <text x="${left + 34}" y="${y + 20.5}">${esc(meta.label)}</text>${cnt}${mini}
      <title>${esc(meta.label)}${st.count ? ` - ran ${st.count}×` : ""}</title></g>`);
  }
  svgEl.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svgEl.innerHTML = parts.join("");
}
