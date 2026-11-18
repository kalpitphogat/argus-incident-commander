---
id: RB-107
title: Downstream timeouts and cascading failures
categories: [dependency_failure]
applies_to: ["*"]
owner: platform-sre
actions:
  - action: enable_circuit_breaker
    role: mitigation
    params: {service: "{caller}", dependency: "{component}"}
    risk: medium
    rationale: "Contain the blast radius: stop waiting on a failing dependency."
    expected_effect: "Caller latency recovers; calls to the dependency fail fast until it recovers."
---
# Downstream timeouts and cascading failures

Follow the hot path: use trace breakdowns to find the hop whose latency or error contribution
grew the most, then check that component's **own** golden signals. If the dependency itself
looks healthy (normal latency, errors and saturation) but calls to it fail, suspect the
*client side*: credentials, certificates, pool configuration or network policy of the caller.

Contain with circuit breakers and timeouts; fix the origin, not the symptoms.
