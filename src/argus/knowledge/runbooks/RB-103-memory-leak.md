---
id: RB-103
title: Memory leak / OOMKilled crash loop
categories: [memory_leak]
applies_to: ["*-service"]
owner: platform-sre
actions:
  - action: restart_service
    role: mitigation
    params: {service: "{component}"}
    risk: low
    rationale: "Immediate relief: frees memory and stops the GC thrash. Temporary only."
    expected_effect: "Memory drops to baseline; it will climb again at the same rate if the leak remains."
  - action: rollback_deploy
    role: fix
    params: {service: "{component}", to_version: "{previous_version}"}
    risk: high
    rationale: "Remove the leaking code introduced by the most recent deploy."
    expected_effect: "Memory stays flat after the rollout; OOMKills stop."
---
# Memory leak / OOMKilled crash loop

**Symptoms.** `memory_pct` climbs linearly then drops when the container is OOMKilled
(a saw-tooth), `restarts` increments, GC pauses grow as the heap fills, and callers see
bursts of 502 / connection resets every time a pod dies.

## Confirm
1. Is memory growth linear since a specific deploy? Correlate with `list_recent_changes`
   using a **wide window (2-4 h)** - leaks take time to bite, so the deploy is well before
   the first crash.
2. Look for unbounded caches (`eviction policy: none`) or growing collections in logs.

## Mitigate
- `restart_service` buys time only. Note the time of the next expected OOM.
- `rollback_deploy` to the previous version is the real fix.
- Scaling out spreads the load and slows the leak per pod but does not fix it.

## Follow-up
- Bound every cache (size + TTL). Add a heap-growth alert (slope over 30 min).
