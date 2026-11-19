"""The website generator: reels, highlighting, templating and the published benchmark."""

import html
import re

import pytest

from argus import demo, sitegen
from argus.evaluate import evaluate
from argus.sim import BY_ID
from conftest import ANCHOR


@pytest.fixture(scope="module")
def recording():
    import asyncio

    return asyncio.run(demo.export_incident("auth-cert-expiry", BY_ID["auth-cert-expiry"].seed, ANCHOR, warmup=True))


def test_reel_is_a_faithful_script_of_the_recording(recording):
    reel = sitegen.extract_reel(BY_ID["auth-cert-expiry"], recording)
    kinds = [s["k"] for s in reel["steps"]]
    assert kinds[0] == "phase" and reel["steps"][0]["p"] == 0 and kinds[-1] == "done"
    for k in ("log", "round", "tool", "find", "hyps", "crit", "ask", "ok", "act", "ver"):
        assert k in kinds, k
    assert kinds.index("ask") < kinds.index("ok") < kinds.index("act") < kinds.index("ver")
    clocks = [s["t"] for s in reel["steps"]]
    assert clocks == sorted(clocks)
    m = reel["metric"]
    tel = recording["approve"]["telemetry"]
    assert len(m["values"]) == tel["end"] - m["t0"] and m["t0"] < reel["alert_t"] < m["t1"]
    assert {"alert", "action"} <= {mk["kind"] for mk in reel["markers"]}
    assert set(reel["numbers"]) == {"detect", "rca", "mitigate"}
    assert not any(s["k"] == "log" and s["a"] == "Executor" and s["x"].startswith("Executing") for s in reel["steps"])


def test_scenario_card_quotes_the_recorded_outcome(recording):
    card = BY_ID["auth-cert-expiry"]
    html_ = sitegen.scenario_cards([card], {card.id: recording})
    stats = sitegen.run_stats(recording)
    assert f'{stats["tool_calls"]} tool calls' in html_ and f'root cause in {stats["rca"]} min' in html_
    assert 'href="demo/#/start/auth-cert-expiry"' in html_


def test_tiles_use_real_text(recording):
    reel = sitegen.extract_reel(BY_ID["auth-cert-expiry"], recording)
    tiles = sitegen.tile_fragments(reel)
    assert tiles["tile_memory_id"].startswith("INC-")
    assert "Safe to plan" in tiles["tile_critic"] and "vrow" in tiles["tile_verify"]


def test_highlighter_round_trips_source():
    src = ('@decorator\nasync def f(x: int = 3) -> str:\n    """Doc."""\n    # comment\n'
           '    return f"{x!r} and {g(x)}" + Send("investigate", {"a": [1, 2.5]})\n')
    out = sitegen.highlight_python(src)
    assert html.unescape(re.sub(r"<[^>]+>", "", out)) == src
    assert '<span class="tk-k">async</span>' in out and '<span class="tk-f">f</span>' in out
    assert '<span class="tk-c"># comment</span>' in out and '<span class="tk-t">Send</span>' in out
    assert '<span class="tk-s">f&quot;{x!r} and {g(x)}&quot;</span>' in out


def test_code_samples_come_from_the_package():
    samples = sitegen.code_samples()
    assert [s["tab"] for s in samples] == ["Agent graph", "Parallel fan-out", "Human approval"]
    assert "interrupt" in samples[2]["html"] and samples[2]["html"].rstrip().endswith("...")


def test_render_fails_loudly_on_unknown_placeholders():
    assert "<svg" in sitegen.render("{{icon:check:12}}", {})
    with pytest.raises(KeyError):
        sitegen.render("{{nope}}", {})


def test_pages_url():
    assert sitegen.pages_url("https://github.com/Kalpit/argus-x") == "https://kalpit.github.io/argus-x/"
    assert sitegen.pages_url("https://gitlab.com/a/b") == "" and sitegen.pages_url(None) == ""


def test_graph_svg_matches_the_compiled_graph():
    from argus.graph import build_graph

    svg = sitegen.how_graph_svg()
    real = {n for n in build_graph().get_graph().nodes if not n.startswith("__")}
    assert set(re.findall(r'data-n="([\w]+)"', svg)) == real


async def test_benchmark_summary():
    stats = await evaluate([1000, 1001])
    assert stats["n"] == 2 and stats["rca"] == 2 and stats["fixed"] == 2 and not stats["misses"]
    assert sum(f["n"] for f in stats["families"]) == 2
