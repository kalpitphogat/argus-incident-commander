"""Command-line interface.

    argus serve                       # web UI on http://127.0.0.1:8000
    argus run checkout-deploy-regression
    argus run chaos --engine deterministic --policy never
    argus scenarios
    argus eval --n 50                 # accuracy on random, unseen faults
    argus export-demo --out _site     # static demo site (recorded runs) for GitHub Pages
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from dataclasses import asdict

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

console = Console()

AGENT_STYLE = {"Pager": "bold red", "Triage": "magenta", "Commander": "bold magenta", "RCA lead": "bold cyan",
               "Critic": "yellow", "Planner": "green", "Approval": "bold yellow", "Executor": "bold green",
               "Verifier": "cyan", "Scribe": "blue", "Memory": "magenta", "Engine": "red"}


def _cmd_scenarios(_: argparse.Namespace) -> None:
    from .sim import CATALOG

    t = Table(title="Incident scenarios", show_lines=False, header_style="bold")
    t.add_column("id", style="cyan")
    t.add_column("title")
    t.add_column("difficulty")
    t.add_column("fault", style="dim")
    for c in CATALOG:
        t.add_row(c.id, c.title, c.difficulty, c.fault["kind"])
    t.add_row("chaos[-<seed>]", "A random fault in a random component", "?", "random")
    console.print(t)


async def _run(args: argparse.Namespace) -> dict:
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.store.memory import InMemoryStore
    from langgraph.types import Command

    from .context import RunContext
    from .graph import build_graph, new_incident_input
    from .sim import resolve

    spec = resolve(args.scenario, args.seed)
    app = build_graph(checkpointer=InMemorySaver(), store=InMemoryStore())
    inp = new_incident_input(asdict(spec))
    ctx = RunContext(reasoning=args.engine, approval_policy=args.policy, pace=0.0, max_rounds=args.rounds)
    inp["settings"] = asdict(ctx)
    cfg = {"configurable": {"thread_id": inp["incident_id"]}, "recursion_limit": 200}
    console.print(Panel.fit(f"[bold]{spec.title}[/]\n{spec.description}", title=f"Argus · {inp['incident_id']}",
                            border_style="magenta"))
    payload = inp
    while True:
        async for _ns, _mode, ev in app.astream(payload, cfg, context=ctx, stream_mode=["custom"], subgraphs=True):
            _print_event(ev, args.verbose)
        st = await app.aget_state(cfg)
        if not st.interrupts:
            return st.values
        payload = Command(resume=_ask(st.interrupts[0].value, args.yes))


def _print_event(ev: dict, verbose: bool) -> None:
    kind = ev.get("type")
    if kind == "log":
        style = AGENT_STYLE.get(ev["agent"], "white")
        console.print(f"[{style}]{ev['agent']:>10}[/] {ev['text']}")
    elif kind == "tool_call" and verbose:
        args = ", ".join(f"{k}={v}" for k, v in ev["args"].items())
        console.print(f"[dim]{'':>10} ↳ {ev['analyst']}: {ev['tool']}({args})[/]")
    elif kind == "finding":
        f = ev["finding"]
        console.print(f"[bold]{'Finding':>10}[/] [dim]{f['analyst']}@{f['target']}[/] {f['headline']}")
    elif kind == "hypotheses":
        t = Table(show_header=True, header_style="bold cyan", box=None, padding=(0, 1))
        t.add_column("#")
        t.add_column("hypothesis")
        t.add_column("conf", justify="right")
        for h in ev["hypotheses"]:
            t.add_row(h["id"], h["title"], f"{h['confidence']:.0%}")
        console.print(t)


def _ask(iv: dict, auto_yes: bool) -> dict:
    plan = iv["plan"]
    t = Table(title=f"[bold yellow]Approval needed[/] - {iv['question']}", header_style="bold")
    t.add_column("id")
    t.add_column("action")
    t.add_column("params")
    t.add_column("risk")
    t.add_column("gate")
    for a in plan["actions"]:
        gated = a["id"] in iv["gated"]
        t.add_row(a["id"], a["action"], str(a["params"]), a["risk"], "[yellow]needs you[/]" if gated else "auto")
    console.print(t)
    if auto_yes:
        console.print("[dim]--yes: approving[/]")
        return {"decision": "approve", "by": os.environ.get("USER", "cli")}
    if Confirm.ask("Approve the gated action(s)?", default=True):
        return {"decision": "approve", "by": os.environ.get("USER", "cli")}
    fb = Prompt.ask("Feedback for Argus (empty = escalate)", default="")
    return {"decision": "reject", "feedback": fb or None, "by": os.environ.get("USER", "cli")}


def _cmd_run(args: argparse.Namespace) -> None:
    t0 = time.time()
    values = asyncio.run(_run(args))
    rep = values.get("report") or {}
    console.rule("[bold]Postmortem")
    console.print(Markdown(rep.get("postmortem", "_no report_")))
    status = values.get("status", "?")
    color = "green" if status in ("resolved", "mitigated") else "red"
    console.print(Panel.fit(f"[bold {color}]{status.upper()}[/] in {time.time() - t0:.1f}s · "
                            f"{values.get('round', 0)} rounds · {len(values.get('findings', []))} findings",
                            border_style=color))


def _cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    from .server import create_app

    console.print(f"[bold magenta]Argus[/] mission control on [link]http://{args.host}:{args.port}[/link]")
    uvicorn.run(create_app(args.data_dir), host=args.host, port=args.port, log_level="warning")


def _cmd_eval(args: argparse.Namespace) -> None:
    """Root-cause accuracy and recovery rate on N random chaos faults."""
    from .evaluate import evaluate

    with console.status("Running chaos evaluation..."):
        stats = asyncio.run(evaluate(range(args.start, args.start + args.n), args.engine))
    t = Table(title=f"Chaos evaluation · engine={args.engine} · {stats['n']} incidents", header_style="bold")
    for col in ("fault family", "incidents", "root cause correct", "recovered"):
        t.add_column(col, justify="right" if col != "fault family" else "left")
    for f in stats["families"]:
        t.add_row(f["kind"], str(f["n"]), f"{f['rca'] / f['n']:.0%}", f"{f['fixed'] / f['n']:.0%}")
    n = stats["n"]
    t.add_row("[bold]all", f"[bold]{n}", f"[bold]{stats['rca'] / n:.0%}", f"[bold]{stats['fixed'] / n:.0%}")
    console.print(t)
    for m in stats["misses"][:10]:
        console.print(f"[red]miss[/] seed={m['seed']} fault={m['fault']} -> {m['top']['category']}@{m['top']['component']} "
                      f"(truth {m['truth']['category']}@{m['truth']['component']})")
    if stats["misses"] and args.strict:
        sys.exit(1)


def _cmd_export_demo(args: argparse.Namespace) -> None:
    from .demo import main as export_main

    console.print(f"[bold magenta]Recording demo incidents[/] -> {args.out}")
    export_main(args.out, args.repo_url, args.chaos, args.bench)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="argus", description="Argus - AI incident commander (LangGraph + LangChain)")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="start the web UI and API")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--data-dir", default=os.environ.get("ARGUS_DATA_DIR", ".argus"))
    s.set_defaults(fn=_cmd_serve)
    r = sub.add_parser("run", help="run one incident in the terminal")
    r.add_argument("scenario", help="scenario id (see `argus scenarios`) or chaos[-seed]")
    r.add_argument("--seed", type=int, default=None)
    r.add_argument("--engine", choices=("auto", "llm", "deterministic"), default="auto")
    r.add_argument("--policy", choices=("low", "medium", "high", "never"), default="medium",
                   help="lowest risk that needs human approval (never = autonomous)")
    r.add_argument("--rounds", type=int, default=3, help="max investigation rounds")
    r.add_argument("--yes", action="store_true", help="approve everything (non-interactive)")
    r.add_argument("-v", "--verbose", action="store_true", help="show every tool call")
    r.set_defaults(fn=_cmd_run)
    sc = sub.add_parser("scenarios", help="list scenarios")
    sc.set_defaults(fn=_cmd_scenarios)
    e = sub.add_parser("eval", help="accuracy on random chaos faults")
    e.add_argument("--n", type=int, default=30)
    e.add_argument("--start", type=int, default=1)
    e.add_argument("--engine", choices=("auto", "llm", "deterministic"), default="deterministic")
    e.add_argument("--strict", action="store_true", help="exit 1 on any miss (for CI)")
    e.set_defaults(fn=_cmd_eval)
    d = sub.add_parser("export-demo", help="build a static, serverless demo site (e.g. for GitHub Pages)")
    d.add_argument("--out", default="_site", help="output directory (replaced)")
    d.add_argument("--repo-url", default=None, help="link shown in the demo for running Argus yourself")
    d.add_argument("--chaos", type=int, default=4, help="number of recorded chaos incidents")
    d.add_argument("--bench", type=int, default=100,
                   help="chaos incidents to evaluate for the published benchmark (0 to skip)")
    d.set_defaults(fn=_cmd_export_demo)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
