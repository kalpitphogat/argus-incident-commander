"""The static demo is built from real recordings; make sure they are complete and self-consistent."""

import json
import re

from argus import demo, sitegen
from argus.sim import BY_ID, chaos_fault
from conftest import ANCHOR


async def test_records_both_branches_of_an_incident():
    data = await demo.export_incident("payment-provider-outage", BY_ID["payment-provider-outage"].seed, ANCHOR,
                                      warmup=True)
    approve, reject = data["approve"], data["reject"]
    assert approve["status"] == "mitigated" and reject["status"] == "mitigated"
    events = approve["events"]
    assert events[0]["type"] == "run_started" and events[-1]["type"] == "done"
    clocks = [e["_clock"] for e in events]
    assert clocks == sorted(clocks), "the simulated clock must never go backwards"
    # a third-party outage is mitigated, never shown as resolved (not even between verify and report)
    assert {e["status"] for e in events if e["type"] == "status"}.isdisjoint({"resolved"})
    assert any(e["type"] == "log" and e["agent"] == "Memory" for e in events), "warm-up should be recalled"
    # the reject branch starts at the human's decision and re-plans (circuit breaker before failover)
    assert reject["events"][0]["type"] == "decision" and reject["events"][0]["decision"]["decision"] == "reject"
    actions = [e["result"]["action"] for e in reject["events"] if e["type"] == "action"]
    assert actions == ["enable_circuit_breaker", "toggle_feature_flag"]
    tel = approve["telemetry"]
    assert {"checkout-service", "payrail-api"} <= set(tel["series"])
    assert len(tel["series"]["checkout-service"]["error_rate_pct"]) == tel["end"]
    assert str(max(clocks)) in tel["health"]


def test_chaos_picks_cover_distinct_fault_families():
    seeds = demo._pick_chaos(4)
    kinds = [chaos_fault(s)["kind"] for s in seeds]
    assert len(seeds) == 4 and len(set(kinds)) == 4


async def test_site_assembly(tmp_path, monkeypatch):
    monkeypatch.setattr(demo, "CATALOG", (BY_ID["cart-bad-release"],))
    out = tmp_path / "site"
    summary = await demo.export_site(out, repo_url="https://github.com/example/argus", chaos=1, log=lambda *_: None,
                                     bench=2)
    assert summary["incidents"] == 2 and summary["benchmark"]["n"] == 2
    # the replayable app lives under /demo/ and links back to the project site
    app = (out / "demo" / "index.html").read_text()
    assert '<meta name="argus-demo" content="data/">' in app and 'src="static/app.js"' in app
    assert '<meta name="argus-home" content="../">' in app
    assert (out / "demo" / "static" / "demo.js").exists() and (out / ".nojekyll").exists()
    meta = json.loads((out / "demo" / "data" / "meta.json").read_text())
    assert meta["health"]["demo"] is True and meta["repo_url"] == "https://github.com/example/argus"
    assert meta["benchmark"]["n"] == 2 and meta["benchmark"]["rca"] == 2
    assert {n["id"] for n in meta["graph"]["nodes"]} >= {"dispatch", "investigate", "approval"}
    assert (out / "demo" / "data" / "scenarios" / "cart-bad-release.json").exists()
    # the landing page at the root is fully rendered from real data
    index = (out / "index.html").read_text()
    assert not sitegen._PLACEHOLDER.search(index), "every template placeholder is rendered"
    assert 'href="demo/#/start/cart-bad-release"' in index and 'href="demo/#/start/chaos"' in index
    assert 'content="https://example.github.io/argus/assets/og.png"' in index
    assert '<span class="tk-f">build_graph</span>' in index, "the code excerpt is read from graph.py"
    assert 'data-n="investigate"' in index and 'data-e="critique-dispatch"' in index
    raw = re.search(r'<script type="application/json" id="reels">(.*?)</script>', index, re.S).group(1)
    assert "<" not in raw
    reel = json.loads(raw)[0]
    assert reel["id"] == "cart-bad-release" and reel["steps"][-1]["k"] == "done"
    for name in ("site.css", "site.js", "icons.js", "favicon.svg", "og.png", "fonts/inter-latin-opsz-normal.woff2",
                 "img/tour-investigation.webp"):
        assert (out / "assets" / name).exists(), name
    assert not sitegen._PLACEHOLDER.search((out / "404.html").read_text())
