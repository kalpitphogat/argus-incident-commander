---
id: RB-102
title: PostgreSQL connection-pool exhaustion
categories: [resource_exhaustion, traffic_surge]
applies_to: [orders-db, catalog-db, users-db]
owner: data-platform
actions:
  - action: increase_connection_pool
    role: fix
    params: {datastore: "{component}", max_connections: "{suggested_max_connections}"}
    risk: medium
    rationale: "Restore headroom so queued requests can get a connection again."
    expected_effect: "Pool wait errors stop within a minute; latency normalises as the queue drains."
  - action: toggle_feature_flag
    role: fallback
    params: {flag: "{flag}", enabled: false}
    risk: medium
    rationale: "Shed the surge traffic that is driving connection demand (business decision)."
    expected_effect: "Traffic returns to normal within ~3 minutes and connection demand falls."
---
# PostgreSQL connection-pool exhaustion

**Symptoms.** Callers log `could not acquire connection from pool` / timeouts; the database logs
`FATAL: sorry, too many clients already`; `connections` sits flat at `max_connections`.
Latency on callers jumps by the pool timeout (5 s) and errors climb with traffic.

## Typical triggers
- A traffic surge (campaign, flash sale, bot traffic) - check feature flags and edge traffic.
- A slow query holding connections longer than usual.
- Callers scaled out: **every replica opens its own pool**.

## Mitigate (in this order)
1. Raise `max_connections` (PgBouncer + Postgres) to ~1.5x current peak demand.
   The DB has CPU headroom up to ~600 connections.
2. If the surge is from a feature flag or campaign, ask the flag owner to disable it.
3. **Never scale the callers up** during pool exhaustion - it makes it worse.

## Follow-up
- Capacity test campaigns before launch; pre-scale pools.
- Add an alert on `connections / max_connections > 85%`.
