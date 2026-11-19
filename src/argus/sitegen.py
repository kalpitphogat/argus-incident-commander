"""Build the project website: a product landing page in front of the static demo.

Everything on the page that looks like data is data. The hero console replays recordings of the
real agent, the feature tiles quote those recordings, the scenario cards show their recorded
outcomes, the code excerpts are read from the package source, and the benchmark is measured while
the site is built.
"""

from __future__ import annotations

import html
import inspect
import io
import json
import keyword
import re
import shutil
import tokenize
from pathlib import Path
from urllib.parse import urlparse

from .evaluate import FAMILY

SITE_DIR = Path(__file__).parent / "site"
WEB_DIR = Path(__file__).parent / "web"
RUNBOOK_DIR = Path(__file__).parent / "knowledge" / "runbooks"

REELS = ("checkout-deploy-regression", "flash-sale-pool-exhaustion", "payment-provider-outage")
REEL_TABS = {"checkout-deploy-regression": "Bad deploy", "flash-sale-pool-exhaustion": "Flash-sale overload",
             "payment-provider-outage": "Payment provider down"}
PHASES = ("Triage", "Investigate", "Diagnose", "Plan", "Approve", "Act", "Verify", "Report")
NODE_PHASE = {"intake": 0, "triage": 0, "dispatch": 1, "investigate": 1, "synthesize": 2, "critique": 2,
              "plan": 3, "approval": 4, "execute": 5, "verify": 6, "report": 7}
METRIC = {"latency_p99_ms": ("p99 latency", "ms"), "error_rate_pct": ("Error rate", "%"),
          "throughput_rps": ("Throughput", "rps"), "cpu_pct": ("CPU", "%"), "memory_pct": ("Memory", "%"),
          "connections": ("Connections", ""), "query_latency_p95_ms": ("Query p95", "ms"),
          "throughput_qps": ("Queries", "qps")}
CHANGE_LABEL = {"deploy": "Deploy", "feature_flag": "Flag change", "config": "Config change",
                "certificate": "Cert change", "scaling": "Scaling", "rollback": "Rollback", "restart": "Restart"}
FINAL = {"resolved", "mitigated", "escalated", "failed"}


# ----------------------------------------------------------------------------- icons
def load_icons() -> dict[str, str]:
    """The stroke icons of the web UI (web/icons.js is the single source of truth)."""
    return dict(re.findall(r"^\s*(\w+): '(.*)',$", (WEB_DIR / "icons.js").read_text(encoding="utf-8"), re.M))


_ICONS: dict[str, str] | None = None


def icon(name: str, size: int = 16, cls: str = "") -> str:
    global _ICONS
    if _ICONS is None:
        _ICONS = load_icons()
    body = _ICONS.get(name) or _ICONS["sparkle"]
    c = f' class="{cls}"' if cls else ""
    return (f'<svg{c} width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
            f'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{body}</svg>')


GITHUB = ('<svg width="{s}" height="{s}" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><path d="M8 0C3.58 0 '
          '0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23'
          '-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78'
          '-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 '
          '1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29'
          '.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"/></svg>')

LOGO = ('<svg class="logo-mark" width="26" height="26" viewBox="0 0 32 32" aria-hidden="true"><defs><linearGradient id="lg{id}" x1="0" y1="0" '
        'x2="1" y2="1"><stop offset="0" stop-color="#6250d6"/><stop offset=".55" stop-color="#2a78d6"/><stop offset="1" '
        'stop-color="#1baf7a"/></linearGradient></defs><rect x="1" y="1" width="30" height="30" rx="9" fill="url(#lg{id})"/>'
        '<path d="M5.5 16S9.5 9 16 9s10.5 7 10.5 7-4 7-10.5 7S5.5 16 5.5 16z" fill="none" stroke="#fff" stroke-width="1.9" '
        'stroke-linejoin="round"/><circle cx="16" cy="16" r="3.6" fill="#fff"/><circle cx="16" cy="16" r="1.5" '
        'fill="#2a78d6"/></svg>')


def esc(v: object) -> str:
    return html.escape(str(v), quote=True)


# ----------------------------------------------------------------------------- formatting
def fmt_value(v: float, metric: str) -> str:
    unit = METRIC.get(metric, ("", ""))[1]
    if unit == "%":
        return f"{v:.2f}%" if v < 10 else f"{v:.1f}%"
    if unit == "ms":
        return f"{v:,.0f} ms"
    return f"{v:,.0f}" + (f" {unit}" if unit else "")


def action_label(action: str, p: dict) -> str:
    if action == "rollback_deploy":
        return f"Roll back {p.get('service')}" + (f" to {p['to_version']}" if p.get("to_version") else "")
    if action == "restart_service":
        return f"Restart {p.get('service')}"
    if action == "scale_service":
        return f"Scale {p.get('service')} to {p.get('replicas')} replicas"
    if action == "increase_connection_pool":
        return f"Raise the {p.get('datastore')} pool to {p.get('max_connections')}"
    if action == "toggle_feature_flag":
        return f"Turn {'on' if p.get('enabled') else 'off'} flag {p.get('flag')}"
    if action == "rotate_certificate":
        return f"Rotate certificate {p.get('certificate')}"
    if action == "enable_circuit_breaker":
        return f"Open a circuit breaker {p.get('service')} → {p.get('dependency')}"
    return action.replace("_", " ")


def key_numbers(postmortem: str | None) -> dict:
    """The 'Key numbers' the Scribe wrote into the postmortem (simulated minutes)."""
    out = {}
    for key, label in (("detect", "Time to detect"), ("rca", "Time to root cause"), ("mitigate", "Time to mitigate")):
        m = re.search(re.escape(label) + r"[^*\n]*\*\*(\d+) min\*\*", postmortem or "")
        if m:
            out[key] = int(m.group(1))
    return out


def _sig(v: float) -> float:
    return float(f"{v:.3g}")


# ----------------------------------------------------------------------------- hero reels
def extract_reel(card, data: dict) -> dict:
    """A compact, replayable script of one recorded run for the hero console."""
    run = data["approve"]
    ev, tel = run["events"], run["telemetry"]
    alert = next(e["alert"] for e in ev if e["type"] == "incident")
    brief = next((e["brief"] for e in ev if e["type"] == "brief"), {})
    svc, metric = alert["service"], alert["metric"]
    label, unit = METRIC.get(metric, (metric, ""))
    end, alert_t = tel["end"], tel["alert_t"]
    t0 = max(0, alert_t - 34)
    markers = [{"t": alert_t, "kind": "alert", "label": "Alert"}]
    for c in tel["changes"]:
        ours = c["author"] == "argus-agent"
        if t0 <= c["t"] <= end and (ours or c["component"] in tel["kinds"] or c["kind"] == "feature_flag"):
            markers.append({"t": c["t"], "kind": "action" if ours else "change",
                            "label": "Fix applied" if ours else CHANGE_LABEL.get(c["kind"], c["kind"])})
    steps: list[dict] = []
    phase = None
    for e in ev:
        k, t = e["type"], e["_clock"]
        if k == "node" and e.get("phase") == "start":
            p = NODE_PHASE.get(e["node"])
            if p is not None and p != phase:
                phase = p
                steps.append({"k": "phase", "p": p, "t": t})
        elif k == "status" and e["status"] not in FINAL:
            steps.append({"k": "status", "s": e["status"], "t": t})
        elif k == "log":
            if e["agent"] == "Executor":
                continue  # the action step below says it better
            text = re.sub(r"^you approved", "On-call approved", e["text"])
            steps.append({"k": "log", "a": e["agent"], "l": e["level"], "x": text, "t": t})
        elif k == "round":
            steps.append({"k": "round", "n": e["round"], "lanes": [[x["analyst"], x["target"]] for x in e["tasks"]],
                          "t": t})
        elif k == "tool_call":
            steps.append({"k": "tool", "t": t})
        elif k == "finding":
            f = e["finding"]
            steps.append({"k": "find", "a": f["analyst"], "g": f["target"], "x": f["headline"], "t": t})
        elif k == "hypotheses":
            steps.append({"k": "hyps", "h": [{"id": h["id"], "x": h["title"], "c": round(h["confidence"] * 100)}
                                             for h in e["hypotheses"][:3]], "t": t})
        elif k == "critique":
            c = e["critique"]
            steps.append({"k": "crit", "ok": bool(c.get("proceed")),
                          "n": len(c.get("follow_up_tasks") or []), "t": t})
        elif k == "approval_required":
            it = e["interrupt"]
            gated = set(it.get("gated") or [])
            steps.append({"k": "ask", "policy": it.get("policy"), "t": t,
                          "acts": [{"x": action_label(a["action"], a["params"]), "r": a["risk"],
                                    "gated": a["id"] in gated} for a in it["plan"]["actions"]]})
        elif k == "approval":
            steps.append({"k": "ok", "t": t})
        elif k == "action":
            r = e["result"]
            x = action_label(r["action"], r["params"])
            steps.append({"k": "act", "x": x, "ok": r["ok"], "r": r.get("risk"), "at": (r.get("time") or "")[11:16],
                          "t": t})
            steps.append({"k": "log", "a": "Executor", "l": "decision" if r["ok"] else "warn",
                          "x": f"{x}: {'applied' if r['ok'] else 'failed'}" + (f" at {r['time'][11:16]}"
                                                                                   if r.get("time") else ""), "t": t})
        elif k == "verification":
            v = e["verification"]
            steps.append({"k": "ver", "s": v["status"], "t": t,
                          "checks": [{"x": c["label"], "b": fmt_value(c["before"], c["metric"]),
                                      "a": fmt_value(c["after"], c["metric"]), "ok": c["ok"]} for c in v["checks"]]})
        elif k == "done":
            steps.append({"k": "done", "s": e["status"], "t": t})
    return {
        "id": card.id, "tab": REEL_TABS.get(card.id, card.title), "scenario": card.title,
        "incident_id": data["incident_id"], "title": brief.get("title") or alert["title"],
        "severity": brief.get("severity"), "service": svc, "anchor": tel["anchor"], "alert_t": alert_t,
        "metric": {"label": label, "unit": unit, "limit": tel["limits"].get(svc, {}).get(metric), "t0": t0,
                   "t1": end + 2, "values": [_sig(v) for v in tel["series"][svc][metric][t0:end]]},
        "markers": sorted(markers, key=lambda m: m["t"]),
        "numbers": key_numbers(run.get("postmortem")), "steps": steps,
    }


# ----------------------------------------------------------------------------- scenario cards
def run_stats(data: dict) -> dict:
    run = data["approve"]
    ev = run["events"]
    count = lambda kind: sum(1 for e in ev if e["type"] == kind)  # noqa: E731
    return {"status": run["status"], "rounds": count("round"), "tool_calls": count("tool_call"),
            "findings": count("finding"), "approvals": run.get("decisions", 0),
            "reject_status": (data.get("reject") or {}).get("status"), **key_numbers(run.get("postmortem"))}


def scenario_cards(cards, recordings: dict[str, dict]) -> str:
    out = []
    for i, card in enumerate(cards):
        s = run_stats(recordings[card.id])
        stats = [f'<span class="stat ok">{esc(s["status"].capitalize())}</span>']
        if "rca" in s:
            stats.append(f'<span class="stat">root cause in {s["rca"]} min</span>')
        stats.append(f'<span class="stat">{s["tool_calls"]} tool calls</span>')
        tags = " · ".join(esc(t) for t in card.tags)
        out.append(f'''
      <a class="scard reveal spot" style="--i:{i}" href="demo/#/start/{esc(card.id)}">
        <div class="scard-top"><span class="scard-ic">{icon(card.icon, 18)}</span>
          <span class="scard-family">{esc(FAMILY.get(card.fault["kind"], card.fault["kind"]))}</span>
          <span class="scard-diff d-{esc(card.difficulty.lower())}">{esc(card.difficulty)}</span></div>
        <h3>{esc(card.title)}</h3>
        <p>{esc(card.teaser)}</p>
        <div class="scard-stats" aria-label="Recorded outcome">{"".join(stats)}</div>
        <div class="scard-foot"><span class="scard-facts">{tags}</span>
          <span class="scard-go">Run it {icon("arrow", 15)}</span></div>
      </a>''')
    return "".join(out)


# ----------------------------------------------------------------------------- benchmark
def bench_block(bench: dict | None) -> dict:
    if not bench or not bench.get("n"):
        return {"bench_n": "100", "bench_rca": "&ndash;", "bench_fixed": "&ndash;", "bench_rounds": "&ndash;",
                "bench_rows": '<p class="muted">Benchmark skipped in this build. Run <code>argus eval --n 100</code>.</p>',
                "bench_seeds": "", "bench_families": str(len(FAMILY)), "bench_secs": "&ndash;"}
    n = bench["n"]
    rows = []
    for f in bench["families"]:
        pct = 100 * f["rca"] / f["n"]
        rows.append(f'''<div class="brow"><span class="brow-name">{esc(f["label"])}</span>
          <span class="brow-bar" aria-hidden="true"><i style="--w:{pct:.1f}%"></i></span>
          <span class="brow-num">{f["rca"]}/{f["n"]}</span></div>''')
    pct = lambda k: f"{100 * bench[k] / n:.0f}%"  # noqa: E731
    return {"bench_n": str(n), "bench_rca": pct("rca"), "bench_fixed": pct("fixed"),
            "bench_rounds": f"{bench['rounds_avg']:.1f}", "bench_rows": "".join(rows),
            "bench_seeds": f"seeds {bench['seeds'][0]}–{bench['seeds'][1]}" if bench.get("seeds") else "",
            "bench_families": str(len(bench["families"])), "bench_secs": f"{bench['secs_median']:.2f}"}


# ----------------------------------------------------------------------------- code excerpts
def highlight_python(src: str) -> str:
    """Syntax-highlight Python with the tokenizer (no third-party highlighter)."""
    lines = src.splitlines(keepends=True)
    offs = [0]
    for line in lines:
        offs.append(offs[-1] + len(line))

    def at(rc: tuple[int, int]) -> int:
        return offs[rc[0] - 1] + rc[1] if rc[0] - 1 < len(offs) else len(src)

    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenError, SyntaxError):
        return esc(src)
    fs_start, fs_end = getattr(tokenize, "FSTRING_START", None), getattr(tokenize, "FSTRING_END", None)
    quiet = {tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.COMMENT, tokenize.ENDMARKER}
    out, cur, prev, i = [], 0, "", 0
    while i < len(toks):
        tok = toks[i]
        if tok.type == tokenize.ENDMARKER:
            break
        a, b, cls = at(tok.start), at(tok.end), None
        if fs_start is not None and tok.type == fs_start:  # Python 3.12+: f-strings are token streams
            depth, j = 1, i + 1
            while depth and j < len(toks):
                depth += (toks[j].type == fs_start) - (toks[j].type == fs_end)
                j += 1
            b, cls, i = at(toks[j - 1].end), "s", j - 1
        elif tok.type == tokenize.STRING:
            cls = "s"
        elif tok.type == tokenize.COMMENT:
            cls = "c"
        elif tok.type == tokenize.NUMBER:
            cls = "n"
        elif tok.type == tokenize.NAME:
            nxt = next((t for t in toks[i + 1:] if t.type not in quiet), None)
            if keyword.iskeyword(tok.string):
                cls = "k"
            elif prev in ("def", "class"):
                cls = "f"
            elif prev == "@":
                cls = "d"
            elif tok.string[:1].isupper():
                cls = "t"
            elif nxt is not None and nxt.string == "(":
                cls = "fn"
        elif tok.type == tokenize.OP and tok.string == "@":
            cls = "d"
        out.append(esc(src[cur:a]))
        text = esc(src[a:b])
        out.append(f'<span class="tk-{cls}">{text}</span>' if cls else text)
        cur = b
        if tok.type not in quiet:
            prev = tok.string
        i += 1
    out.append(esc(src[cur:]))
    return "".join(out)


def code_samples() -> list[dict]:
    from . import graph

    def excerpt(fn, stop: str | None = None) -> str:
        src = inspect.getsource(fn)
        if stop and stop in src:
            src = src[: src.index("\n", src.index(stop)) + 1] + "        ...\n"
        return src

    samples = [
        ("Agent graph", "graph.py", "The whole incident loop, wired as a LangGraph state machine.",
         excerpt(graph.build_graph)),
        ("Parallel fan-out", "graph.py", "One Send per investigation task: the investigators run concurrently.",
         excerpt(graph.fan_out) + "\n\n" + excerpt(graph.after_critique)),
        ("Human approval", "graph.py", "interrupt() pauses the run until a person approves, edits or rejects.",
         excerpt(graph.approval, "decision = _normalize_decision(")),
    ]
    out = []
    for tab, file, caption, src in samples:
        src = src.rstrip("\n")
        out.append({"tab": tab, "file": file, "caption": caption, "lines": src.count("\n") + 1,
                    "html": highlight_python(src)})
    return out


def code_block(samples: list[dict]) -> dict:
    tabs = "".join(f'<button type="button" role="tab" id="code-tab-{i}" aria-controls="code-panel-{i}" '
                   f'aria-selected="{str(i == 0).lower()}" tabindex="{0 if i == 0 else -1}">{esc(s["tab"])}</button>'
                   for i, s in enumerate(samples))
    panels = "".join(
        f'<div class="code-panel" role="tabpanel" id="code-panel-{i}" aria-labelledby="code-tab-{i}"{"" if i == 0 else " hidden"}>'
        f'<div class="code-cap"><span class="mono">src/argus/{esc(s["file"])}</span><span>{esc(s["caption"])}</span></div>'
        f'<div class="code-scroll"><pre class="code-ln" aria-hidden="true">'
        f'{chr(10).join(str(n) for n in range(1, s["lines"] + 1))}</pre><pre class="code"><code>{s["html"]}</code></pre></div></div>'
        for i, s in enumerate(samples))
    return {"code_tabs": tabs, "code_panels": panels}


# ----------------------------------------------------------------------------- feature tiles (real data)
def tile_fragments(reel: dict) -> dict:
    """Feature-tile visuals filled with text from a real recording (the first hero reel)."""
    steps = reel["steps"]
    hyps = [s["h"] for s in steps if s["k"] == "hyps" and s["h"]]
    crits = [s for s in steps if s["k"] == "crit"]
    rounds = sum(1 for s in steps if s["k"] == "round")
    critic_logs = [s["x"] for s in steps if s["k"] == "log" and s["a"] == "Critic" and s["l"] in ("warn", "decision")]
    memory = next((s["x"] for s in steps if s["k"] == "log" and s["a"] == "Memory"), "")
    m = re.search(r"(INC-\w+) - (.+)$", memory)
    ver = next((s for s in steps if s["k"] == "ver"), {"checks": []})
    ask = next((s for s in steps if s["k"] == "ask" and s["acts"]), None)
    gated = next((a for a in ask["acts"] if a["gated"]), ask["acts"][0]) if ask else {"x": "Roll back the deploy",
                                                                                         "r": "high"}

    def bar(h: dict, cls: str = "") -> str:
        return (f'<div class="hbar {cls}"><div class="hbar-top"><span class="hbar-id">{esc(h["id"])}</span>'
                f'<span class="hbar-x">{esc(h["x"])}</span><b>{h["c"]}%</b></div>'
                f'<span class="hbar-track"><i style="--w:{h["c"]}%"></i></span></div>')

    critic = '<div class="crit-stage">'
    if hyps:
        first, last = hyps[0], hyps[-1]
        doubted = bool(crits) and not crits[0]["ok"]
        critic += f'<div class="crit-label">Round 1</div>{bar(first[0], "lead" if doubted else "lead win")}'
        if doubted:
            critic += "".join(bar(h) for h in first[1:2])
            n = crits[0]["n"]
            critic += (f'<div class="stamp warn">{icon("shield", 14)} Not yet: {n} follow-up task{"s" if n != 1 else ""}'
                       f'</div><div class="crit-label">Round {rounds}</div>{bar(last[0], "lead win")}')
        critic += f'<div class="stamp good">{icon("check", 14)} Safe to plan</div>'
    critic += "</div>"
    checks = "".join(f'<div class="vrow"><span class="vrow-ic">{icon("check" if c["ok"] else "x", 14)}</span>'
                     f'<span class="vrow-x">{esc(c["x"])}</span><span class="vrow-n"><s>{esc(c["b"])}</s> '
                     f'{icon("arrow", 12)} <b>{esc(c["a"])}</b></span></div>' for c in ver["checks"])
    return {
        "tile_critic": critic,
        "tile_critic_quote": esc(critic_logs[0] if critic_logs else ""),
        "tile_verify": checks,
        "tile_memory_id": esc(m.group(1) if m else "INC-\u2026"),
        "tile_memory_text": esc(m.group(2) if m else memory),
        "tile_approval_action": esc(gated["x"]),
        "tile_approval_risk": esc(gated["r"]),
    }


def runbook_tile(rb_prefix: str = "RB-102") -> str:
    path = next(RUNBOOK_DIR.glob(f"{rb_prefix}-*.md"))
    text = path.read_text(encoding="utf-8")
    front = text.split("---")[1]
    title = re.search(r"^title: (.+)$", front, re.M).group(1)
    acts = re.findall(r"- action: (\w+)\n\s+role: (\w+)\n\s+params: .+\n\s+risk: (\w+)", front)
    rows = "".join(f'<div class="rb-row"><span class="mono">{esc(a)}</span><span class="rb-role r-{esc(role)}">'
                   f'{esc(role)}</span><span class="risk r-{esc(risk)}">{esc(risk)}</span></div>'
                   for a, role, risk in acts)
    return (f'<div class="rb-head"><span class="mono">{esc(rb_prefix)}</span><span>{esc(title)}</span></div>'
            f'{rows}')


# ----------------------------------------------------------------------------- how-it-works graph
GRAPH_NODES = (("intake", "Intake", "bell"), ("triage", "Triage", "triage"), ("dispatch", "Dispatch", "flag"),
               ("investigate", "Investigators", "deps"), ("synthesize", "RCA synthesis", "sparkle"),
               ("critique", "Critic", "shield"), ("plan", "Plan", "route"), ("approval", "Human approval", "usercheck"),
               ("execute", "Execute", "zap"), ("verify", "Verify", "activity"), ("report", "Postmortem", "pen"))


def how_graph_svg() -> str:
    """The real topology of build_graph(), laid out vertically, with its three loops and the escalation path."""
    global _ICONS
    if _ICONS is None:
        _ICONS = load_icons()
    x, w, h, gap, top = 84, 196, 40, 64, 18
    y = {n: top + i * gap for i, (n, _, _) in enumerate(GRAPH_NODES)}
    cx, right, left = x + w // 2, x + w, x
    parts = ['<svg class="g" viewBox="0 0 372 714" role="img" aria-label="Argus agent graph: intake, triage, '
             'dispatch, parallel investigators, RCA synthesis, critic, plan, human approval, execute, verify and '
             'postmortem, with loops from the critic back to dispatch, from approval and verify back to plan, and an '
             'escalation path to the postmortem.">',
             '<defs><marker id="ah" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="5.5" markerHeight="5.5" '
             'orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" class="ah"/></marker>'
             '<marker id="ah-on" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="5.5" markerHeight="5.5" '
             'orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" class="ah-on"/></marker></defs>']
    names = [n for n, _, _ in GRAPH_NODES]
    for a, b in zip(names, names[1:]):
        parts.append(f'<path class="e" data-e="{a}-{b}" d="M{cx} {y[a] + h} V{y[b] - 1}"/>')

    def loop(key: str, src: str, dst: str, lx: int, sy: int, dy: int, label: str, tx: int) -> None:
        y1, y2 = y[src] + sy, y[dst] + dy
        parts.append(f'<path class="e loop" data-e="{key}" d="M{right} {y1} H{lx - 8} Q{lx} {y1} {lx} {y1 - 8} '
                     f'V{y2 + 8} Q{lx} {y2} {lx - 8} {y2} H{right + 3}"/>')
        mid = (y1 + y2) / 2
        parts.append(f'<text class="e-label" data-e="{key}" transform="translate({tx} {mid:.0f}) rotate(90)" '
                     f'text-anchor="middle">{label}</text>')

    loop("critique-dispatch", "critique", "dispatch", 306, 20, 20, "needs evidence", 318)
    loop("approval-plan", "approval", "plan", 300, 20, 27, "rejected", 312)
    loop("verify-plan", "verify", "plan", 342, 20, 13, "not recovered", 354)
    ey1, ey2, ey3, lx = y["plan"] + 20, y["approval"] + 20, y["report"] + 20, 50
    parts.append(f'<path class="e loop" data-e="plan-report" d="M{left} {ey1} H{lx + 8} Q{lx} {ey1} {lx} {ey1 + 8} '
                 f'V{ey3 - 8} Q{lx} {ey3} {lx + 8} {ey3} H{left - 3}"/>')
    parts.append(f'<path class="e loop" data-e="approval-report" d="M{left} {ey2} H{lx}"/>')
    parts.append(f'<text class="e-label" data-e="plan-report approval-report" transform="translate({lx - 12} '
                 f'{(ey1 + ey3) / 2:.0f}) rotate(-90)" text-anchor="middle">escalate</text>')
    for n, label, ic in GRAPH_NODES:
        dots = ""
        if n == "investigate":
            dots = "".join(f'<circle cx="{w - 46 + i * 9}" cy="20" r="3.2" style="fill:var(--s{i + 1})"/>'
                           for i in range(4))
        parts.append(f'<g class="n" data-n="{n}" transform="translate({x} {y[n]})"><rect width="{w}" height="{h}" '
                     f'rx="11"/><svg class="n-ic" x="14" y="11" width="18" height="18" viewBox="0 0 24 24" '
                     f'fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" '
                     f'stroke-linejoin="round">{_ICONS[ic]}</svg><text x="42" y="25">{label}</text>{dots}</g>')
    parts.append("</svg>")
    return "".join(parts)


def effort_chips() -> str:
    from .brains.llm import EFFORT

    return "".join(f'<span class="chip">{esc(role)} <b class="e-{esc(level)}">{esc(level)}</b></span>'
                   for role, level in EFFORT.items())


# ----------------------------------------------------------------------------- render
_PLACEHOLDER = re.compile(r"\{\{\s*([\w:.-]+)\s*\}\}")


def render(template: str, ctx: dict[str, str]) -> str:
    def sub(m: re.Match) -> str:
        key = m.group(1)
        if key.startswith("icon:"):
            name, _, size = key[5:].partition(":")
            return icon(name, int(size or 16))
        if key.startswith("github"):
            return GITHUB.format(s=key.partition(":")[2] or "16")
        if key.startswith("logo"):
            return LOGO.format(id=key.partition(":")[2] or "0")
        if key not in ctx:
            raise KeyError(f"site template placeholder '{key}' has no value")
        return ctx[key]

    return _PLACEHOLDER.sub(sub, template)


def pages_url(repo_url: str | None) -> str:
    """https://github.com/<owner>/<repo> -> https://<owner>.github.io/<repo>/ (GitHub Pages project site)."""
    if not repo_url:
        return ""
    u = urlparse(repo_url)
    parts = [p for p in u.path.split("/") if p]
    if u.netloc != "github.com" or len(parts) < 2:
        return ""
    return f"https://{parts[0].lower()}.github.io/{parts[1]}/"


def build_landing(out: Path, *, meta: dict, cards, recordings: dict[str, dict], bench: dict | None,
                  repo_url: str | None) -> dict:
    """Write index.html, 404.html and assets/ into ``out`` (the demo lives in ``out/demo``)."""
    from . import __version__
    from .sim import Topology

    out = Path(out)
    assets = out / "assets"
    shutil.copytree(SITE_DIR / "assets", assets)
    shutil.copy2(WEB_DIR / "icons.js", assets / "icons.js")

    by_id = {c.id: c for c in cards}
    reels = [extract_reel(by_id[i], recordings[i]) for i in REELS if i in recordings]
    if not reels:  # e.g. a trimmed catalog in tests: replay whatever was recorded
        reels = [extract_reel(c, recordings[c.id]) for c in cards[:1]]
    repo = repo_url or "https://github.com/"
    owner = [p for p in urlparse(repo).path.split("/") if p][:1]
    site_url = pages_url(repo_url)
    topo = Topology()
    ctx = {
        "repo_url": esc(repo), "codespaces_url": esc(repo.replace("https://github.com/", "https://codespaces.new/")),
        "clone_url": esc(repo.rstrip("/") + ".git"), "repo_dir": esc(repo.rstrip("/").rsplit("/", 1)[-1]),
        "owner": esc(owner[0] if owner else "argus"), "site_url": esc(site_url), "version": esc(__version__),
        "built": esc(meta["generated_at"][:10]), "model_label": esc(meta["health"]["model_label"]),
        "reels_json": json.dumps(reels, separators=(",", ":")).replace("<", "\\u003c").replace(">", "\\u003e")
        .replace("&", "\\u0026"),
        "scenario_cards": scenario_cards(cards, recordings),
        "n_scenarios": str(len(cards)), "n_components": str(len(topo.components)),
        "n_runbooks": str(len(list(RUNBOOK_DIR.glob("RB-*.md")))), "n_families": str(len(FAMILY)),
        "runbook_tile": runbook_tile(), "how_graph": how_graph_svg(), "effort_chips": effort_chips(),
        **tile_fragments(reels[0]), **bench_block(bench), **code_block(code_samples()),
    }
    index = render((SITE_DIR / "index.html").read_text(encoding="utf-8"), ctx)
    (out / "index.html").write_text(index, encoding="utf-8")
    (out / "404.html").write_text(render((SITE_DIR / "404.html").read_text(encoding="utf-8"), ctx), encoding="utf-8")
    return {"reels": len(reels), "bytes": len(index.encode())}
