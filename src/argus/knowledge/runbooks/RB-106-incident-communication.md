---
id: RB-106
title: Incident severity and communication guide
categories: [process]
applies_to: ["*"]
owner: incident-management
actions: []
---
# Incident severity and communication guide

| Severity | Definition | Comms cadence |
|---|---|---|
| SEV1 | Revenue path down or >20% of a critical flow failing | Status page within 10 min, updates every 15 min |
| SEV2 | Critical flow degraded (latency > 2x SLO or errors > SLO) | Status page within 20 min, updates every 30 min |
| SEV3 | Non-critical feature degraded | Internal channel only |
| SEV4 | No customer impact | Ticket |

**Customer-facing language.** Plain words, no internal service names, no blame, say what
customers can do (retry, use another method) and when the next update is.

**Approval policy.** Low-risk actions (restarts, scaling) may be automated. Medium and high
risk actions (config, feature flags, rollbacks, failovers) need the incident commander's
approval.
