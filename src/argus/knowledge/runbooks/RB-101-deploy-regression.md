---
id: RB-101
title: Regression after a deploy (latency, errors, N+1 queries, exceptions)
categories: [bad_deploy, code_exception, query_regression]
applies_to: ["*-service"]
owner: platform-sre
actions:
  - action: rollback_deploy
    role: fix
    params: {service: "{component}", to_version: "{previous_version}"}
    risk: high
    rationale: "Roll back to the last known-good version; the deploy is the trigger."
    expected_effect: "Error rate and latency return to baseline within 2-3 minutes of the rollout finishing."
---
# Regression after a deploy

**Symptoms.** A step change in p99 latency or error rate on one service within minutes of
a rollout. Often accompanied by new exception signatures tagged with the new build, or by a
burst of queries from the new version against its database (classic N+1: one query per item
instead of one per request).

## Confirm
1. Correlate the anomaly onset with the deploy timestamp (`list_recent_changes`).
2. Look for **new** log templates that carry the new build/version.
3. If a datastore is involved, check its slow-query log: `application_name=<service>@<version>`
   shows which client release is sending the extra load.

## Mitigate
- **Roll back first, debug later.** `rollback_deploy(service, to_version=<previous>)`.
- Do *not* scale the service up to "absorb" an N+1 regression: every extra replica sends more
  queries to the same database.
- Restarting pods does not help: the bad code is redeployed.

## Follow-up
- Add a query-count assertion / budget to the service's integration tests.
- Gate the next rollout behind a canary with automated latency analysis.
