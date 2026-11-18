---
id: RB-104
title: Payment provider (PayRail) degradation
categories: [external_dependency]
applies_to: [payrail-api, payment-service, checkout-service]
owner: payments
actions:
  - action: toggle_feature_flag
    role: fix
    params: {flag: "payments-failover-backup-provider", enabled: true}
    risk: high
    rationale: "Route authorisations to the backup acquirer while PayRail is degraded."
    expected_effect: "Payment errors fall to baseline within a minute; latency +~150 ms; fees +0.4%."
  - action: enable_circuit_breaker
    role: fallback
    params: {service: "payment-service", dependency: "payrail-api"}
    risk: medium
    rationale: "Fail fast instead of holding checkout threads for 5 s timeouts."
    expected_effect: "Checkout latency recovers; payment errors remain until failover or vendor recovery."
---
# Payment provider (PayRail) degradation

**Symptoms.** `payment-service` logs `PayRail authorize failed: 503` / timeouts; client-side
error rate and latency on `payrail-api` jump together; there is **no internal change** that
lines up. PayRail's status page usually confirms within minutes.

## Confirm
1. `check_vendor_status` for PayRail.
2. Make sure nothing internal changed (deploys, flags, certificates for `payrail-mtls-client`).

## Mitigate
1. Enable the failover flag `payments-failover-backup-provider` (needs payments on-call approval:
   it changes fees and fraud-screening behaviour).
2. If failover is not approved, enable the circuit breaker so checkout fails fast and keeps
   threads free - customers get a clear "try again" instead of a spinner.
3. Post a status-page update: payments degraded, retrying usually works.

## Follow-up
- Automate failover on sustained vendor error rate > 20% for 3 minutes.
