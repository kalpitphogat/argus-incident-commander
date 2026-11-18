"""Signal-processing primitives shared by the tools and the deterministic reasoner.

Nothing in here knows which fault was injected.  These are the same techniques an
SRE (or an AIOps product) applies to real telemetry: robust anomaly detection,
log-template mining, change correlation and trace attribution.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass
from statistics import median
from typing import Iterable, Sequence

# --------------------------------------------------------------------------
# Anomaly detection
# --------------------------------------------------------------------------

# direction, minimum absolute change, minimum relative change
METRIC_RULES: dict[str, tuple[str, float, float]] = {
    "latency_p99_ms": ("up", 25.0, 0.35),
    "query_latency_p95_ms": ("up", 2.0, 0.40),
    "error_rate_pct": ("up", 0.6, 1.0),
    "throughput_rps": ("both", 0.0, 0.30),
    "throughput_qps": ("both", 0.0, 0.40),
    "throughput_ops": ("both", 0.0, 0.40),
    "throughput_msgs": ("both", 0.0, 0.40),
    "cpu_pct": ("up", 12.0, 0.30),
    "memory_pct": ("up", 12.0, 0.20),
    "restarts": ("up", 0.5, 0.0),
    "connections": ("both", 8.0, 0.30),
    "consumer_lag": ("up", 50.0, 1.0),
}


@dataclass
class Anomaly:
    metric: str
    onset_t: int
    direction: str  # up | down
    baseline: float
    current: float
    peak: float
    change_pct: float
    pattern: str  # step | spike | ramp | sawtooth | drop
    z: float

    @property
    def headline_value(self) -> float:
        return self.peak if self.pattern in ("sawtooth", "spike") else self.current

    def describe(self, fmt_time) -> str:
        arrow = "↑" if self.direction == "up" else "↓"
        value = self.headline_value
        change = f"{self.change_pct:+.0f}%" if abs(self.change_pct) < 10_000 else "×{:.0f}".format(
            value / max(self.baseline, 1e-9))
        label = "peak " if self.pattern in ("sawtooth", "spike") else ""
        extra = ", then reset" if self.pattern == "sawtooth" else ""
        return (f"{self.metric} {arrow} {fmt_num(self.baseline)} → {label}{fmt_num(value)} ({change}){extra}, "
                f"{self.pattern} since {fmt_time(self.onset_t)}")


def fmt_num(v: float) -> str:
    if abs(v) >= 1000:
        return f"{v:,.0f}"
    if abs(v) >= 100:
        return f"{v:.0f}"
    if abs(v) >= 10:
        return f"{v:.1f}"
    return f"{v:.2f}"


def robust_stats(values: Sequence[float]) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    med = median(values)
    mad = median([abs(v - med) for v in values])
    return med, mad


def detect_anomaly(points: Sequence[tuple[int, float]], metric: str, baseline_end: int = 60,
                   z_threshold: float = 5.0) -> Anomaly | None:
    """Robust (median/MAD) detector with a sustained-run requirement and pattern classification."""
    direction, min_abs, min_rel = METRIC_RULES.get(metric, ("up", 0.0, 0.3))
    base_vals = [v for t, v in points if t < baseline_end]
    test = [(t, v) for t, v in points if t >= baseline_end]
    if len(base_vals) < 10 or len(test) < 2:
        return None
    med, mad = robust_stats(base_vals)
    scale = max(1.4826 * mad, abs(med) * 0.03, 1e-6)
    if metric == "restarts":
        scale = max(scale, 0.25)

    def significant(v: float) -> str | None:
        dz = (v - med) / scale
        rel = abs(v - med) / max(abs(med), 1e-9)
        big = abs(v - med) >= min_abs and (rel >= min_rel or min_rel == 0)
        if dz >= z_threshold and big and direction in ("up", "both"):
            return "up"
        if dz <= -z_threshold and big and direction in ("down", "both"):
            return "down"
        return None

    flags = [significant(v) for _, v in test]
    onset_idx = None
    run_dir = None
    for i, f in enumerate(flags):
        if f is None:
            continue
        # sustained for 3 points, or a strong spike (2x threshold) of at least 1 point
        window = flags[i:i + 3]
        sustained = len(window) >= 2 and all(x == f for x in window)
        strong = abs((test[i][1] - med) / scale) >= 2 * z_threshold
        if sustained or strong or metric == "restarts":
            onset_idx, run_dir = i, f
            break
    if onset_idx is None:
        return None
    post = [v for _, v in test[onset_idx:]]
    current = sum(post[-3:]) / len(post[-3:])
    peak = max(post) if run_dir == "up" else min(post)
    pattern = classify_pattern(test, onset_idx, med, scale, run_dir, flags)
    # refine onset for ramps: walk back while the series is still rising above baseline noise
    onset_t = test[onset_idx][0]
    if pattern in ("ramp", "sawtooth"):
        vals = [v for _, v in points]
        ts = [t for t, _ in points]
        j = ts.index(onset_t)
        while j > 0 and vals[j - 1] > med + 1.5 * scale and ts[j - 1] >= baseline_end - 30:
            j -= 1
        onset_t = ts[j]
    change_pct = (current - med) / max(abs(med), 1e-9) * 100
    if pattern in ("spike", "sawtooth"):
        change_pct = (peak - med) / max(abs(med), 1e-9) * 100
    return Anomaly(metric, onset_t, run_dir, med, current, peak, change_pct, pattern,
                   round((peak - med) / scale, 1))


def classify_pattern(test, onset_idx, med, scale, direction, flags) -> str:
    vals = [v for _, v in test[onset_idx:]]
    if direction == "down":
        return "drop"
    tail_flags = flags[onset_idx:]
    if len(vals) >= 3:
        # sawtooth: climbs then falls back by a large amount at least once
        drops = sum(1 for a, b in zip(vals, vals[1:]) if a - b > 0.35 * max(abs(max(vals) - med), 1e-9))
        rises = sum(1 for a, b in zip(vals, vals[1:]) if b > a)
        if drops >= 1 and rises >= 3:
            return "sawtooth"
    recent = tail_flags[-3:]
    if len(tail_flags) <= 4 and recent and recent[-1] is None:
        return "spike"
    if len(vals) >= 5:
        n = len(vals)
        xs = list(range(n))
        mx, my = sum(xs) / n, sum(vals) / n
        slope = sum((x - mx) * (y - my) for x, y in zip(xs, vals)) / max(sum((x - mx) ** 2 for x in xs), 1e-9)
        if slope * n > 0.6 * max(abs(max(vals) - med), 1e-9) and vals[0] < vals[-1] and _monotone_ratio(vals) > 0.7:
            return "ramp"
    if tail_flags and tail_flags[-1] is None and sum(1 for f in tail_flags if f) <= 3:
        return "spike"
    return "step"


def _monotone_ratio(vals: Sequence[float]) -> float:
    ups = sum(1 for a, b in zip(vals, vals[1:]) if b >= a)
    return ups / max(1, len(vals) - 1)


# --------------------------------------------------------------------------
# Log template mining (a light-weight cousin of Drain)
# --------------------------------------------------------------------------

_PROTECT = re.compile(r"(?:[A-Za-z][\w.-]*@\d+\.\d+\.\d+|\bv?\d+\.\d+\.\d+\b|\bTLSv\d\.\d\b|\bHTTP/\d\.\d\b|\b[245]xx\b)")
_MASKS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"), "<uuid>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\b"), "<ts>"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b"), "<ip>"),
    (re.compile(r"\b(?:ord|u|req|sess)_[0-9a-zA-Z]+\b"), "<id>"),
    (re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{5,}\b"), "<hex>"),
    (re.compile(r"\b\d+(?:\.\d+)?(?=(?:ms|s|%|x|Gi|Mi)\b)"), "<n>"),
    (re.compile(r"(?<![\w.<])\d+(?:\.\d+)?(?![\w.])"), "<n>"),
)


def to_template(message: str) -> str:
    protected: list[str] = []

    def keep(m: re.Match) -> str:
        protected.append(m.group(0))
        return f"\x00{_letters(len(protected) - 1)}\x00"

    s = _PROTECT.sub(keep, message)
    for pattern, repl in _MASKS:
        s = pattern.sub(repl, s)
    return re.sub(r"\x00([A-Z]+)\x00", lambda m: protected[_unletters(m.group(1))], s)


def _letters(i: int) -> str:
    out = ""
    while True:
        out = chr(65 + i % 26) + out
        i = i // 26 - 1
        if i < 0:
            return out


def _unletters(s: str) -> int:
    n = 0
    for ch in s:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


@dataclass
class LogTemplate:
    template: str
    level: str
    count: int
    before: int
    after: int
    first_seen_t: int
    last_seen_t: int
    sample: str
    status: str  # new | surging | steady | fading
    signature: str | None = None
    signature_label: str | None = None
    refers_to: list[str] | None = None


LEVEL_RANK = {"DEBUG": 0, "INFO": 1, "LOG": 1, "WARN": 2, "ERROR": 3, "FATAL": 4}


def mine_templates(lines: Iterable, split_t: int, known_components: Iterable[str] = ()) -> list[LogTemplate]:
    """Group raw log lines into templates and compare before/after `split_t`."""
    groups: dict[tuple[str, str], dict] = {}
    span_before: set[int] = set()
    span_after: set[int] = set()
    for line in lines:
        (span_before if line.t < split_t else span_after).add(line.t)
        key = (to_template(line.message), line.level)
        g = groups.get(key)
        if g is None:
            g = groups[key] = {"count": 0, "before": 0, "after": 0, "first": line.t, "last": line.t,
                               "sample": line.message}
        g["count"] += 1
        g["before" if line.t < split_t else "after"] += 1
        g["first"] = min(g["first"], line.t)
        g["last"] = max(g["last"], line.t)
        if line.t >= split_t:
            g["sample"] = line.message
    minutes_before = max(1, len(span_before))
    minutes_after = max(1, len(span_after))
    components = list(known_components)
    out: list[LogTemplate] = []
    for (tpl, level), g in groups.items():
        rate_b = g["before"] / minutes_before
        rate_a = g["after"] / minutes_after
        if g["before"] == 0 and (g["after"] >= 2 or (g["after"] >= 1 and LEVEL_RANK.get(level, 1) >= 3)):
            status = "new"
        elif rate_a >= 4 * max(rate_b, 0.02) and g["after"] >= 3:
            status = "surging"
        elif rate_b >= 4 * max(rate_a, 0.02) and g["before"] >= 3:
            status = "fading"
        else:
            status = "steady"
        sig = classify_signature(g["sample"])
        refers = [c for c in components if c in g["sample"]]
        out.append(LogTemplate(tpl, level, g["count"], g["before"], g["after"], g["first"], g["last"], g["sample"],
                               status, sig[0] if sig else None, sig[1] if sig else None, refers or None))
    out.sort(key=lambda x: (x.status in ("new", "surging"), LEVEL_RANK.get(x.level, 1), x.after), reverse=True)
    return out


# Known failure signatures: (category, label, regex).  Order matters: most specific first.
SIGNATURES: tuple[tuple[str, str, re.Pattern], ...] = (
    ("certificate_expiry", "expired / invalid TLS certificate",
     re.compile(r"x509|certificate has expired|certificate expired|certificate verify failed", re.I)),
    ("resource_exhaustion", "connection pool exhausted",
     re.compile(r"could not acquire connection|too many clients|pool exhausted|remaining connection slots", re.I)),
    ("memory_leak", "memory exhaustion (OOM / GC thrash)",
     re.compile(r"OutOfMemoryError|OOMKilled|GC pause|GC overhead|heap \d+% used|eviction policy: none", re.I)),
    ("external_dependency", "third-party API failing",
     re.compile(r"PayRail .*fail|PayRail .*latency|upstream_request_id|vendor api", re.I)),
    ("query_regression", "slow or excessive database queries",
     re.compile(r"statement: SELECT|query rate from|slow call to", re.I)),
    ("code_exception", "unhandled exception in application code",
     re.compile(r"NullPointerException|panic|Exception\b|KeyError|ClassCastException|unwrap\(\)", re.I)),
    ("dependency_failure", "timeouts / errors from a dependency",
     re.compile(r"timeout after|waiting for|upstream .* returned 5\d\d|returned 50[234]|connection reset", re.I)),
    ("traffic_surge", "traffic surge", re.compile(r"traffic surge|utm_campaign", re.I)),
)


def classify_signature(message: str) -> tuple[str, str] | None:
    for category, label, rx in SIGNATURES:
        if rx.search(message):
            return category, label
    return None


_COMPONENT_HINTS = (
    re.compile(r"pool=(?P<c>[a-z][a-z-]+)"),
    re.compile(r"waiting for (?P<c>[a-z][a-z-]+)"),
    re.compile(r"slow call to (?P<c>[a-z][a-z-]+)"),
    re.compile(r"tls handshake with (?P<c>[a-z][a-z-]+)"),
    re.compile(r"upstream (?P<c>[a-z][a-z-]+(?:-service|-api))"),
    re.compile(r"application_name=(?P<c>[a-z][a-z-]+)@(?P<v>\d+\.\d+\.\d+)"),
    re.compile(r"\(client=(?P<c>[a-z][a-z-]+)\)"),
    re.compile(r"query rate from (?P<c>[a-z][a-z-]+)"),
)
_BUILD = re.compile(r"\[build (?P<v>\d+\.\d+\.\d+)\]")


def extract_refs(message: str) -> dict:
    """Pull components / versions referenced by a log line."""
    refs: dict = {}
    for rx in _COMPONENT_HINTS:
        m = rx.search(message)
        if m:
            refs.setdefault("component", m.group("c"))
            if "v" in m.groupdict() and m.group("v"):
                refs["version"] = m.group("v")
                refs["client"] = m.group("c")
    m = _BUILD.search(message)
    if m:
        refs["build"] = m.group("v")
    if re.search(r"PayRail", message):
        refs["vendor"] = "payrail-api"
    return refs


# --------------------------------------------------------------------------
# Change correlation
# --------------------------------------------------------------------------

KIND_WEIGHT = {"deploy": 1.0, "feature_flag": 0.9, "config": 0.75, "certificate": 0.55, "scaling": 0.45,
               "rollback": 0.6, "restart": 0.2}


def correlate_change(change, onset_t: int, focus: set[str], related: set[str], gradual: bool) -> tuple[float, str]:
    """Score how plausibly a change explains an anomaly that started at `onset_t`."""
    dt = onset_t - change.t
    tau = 40.0 if gradual else 10.0
    if dt < -2:
        return 0.0, "happened after the anomaly started"
    temporal = math.exp(-max(0.0, dt) / tau)
    affects = set((change.details or {}).get("affects", []))
    if change.component in focus:
        relevance, why = 1.0, "changed the affected component itself"
    elif affects & focus:
        relevance, why = 0.85, "flag routes traffic through the affected component"
    elif change.component in related:
        relevance, why = 0.7, "changed a direct dependency / caller"
    elif change.kind == "feature_flag":
        relevance, why = 0.35, "global feature flag"
    else:
        relevance, why = 0.12, "unrelated component"
    score = temporal * relevance * KIND_WEIGHT.get(change.kind, 0.5)
    when = f"{abs(dt):.0f} min {'before' if dt >= 0 else 'after'} onset"
    return score, f"{when}; {why}"
