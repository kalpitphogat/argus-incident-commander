---
id: RB-105
title: Expired TLS / mTLS certificate
categories: [certificate_expiry]
applies_to: ["*"]
owner: security-platform
actions:
  - action: rotate_certificate
    role: fix
    params: {certificate: "{certificate}"}
    risk: medium
    rationale: "Issue a fresh certificate from the internal CA; services hot-reload it."
    expected_effect: "TLS handshakes succeed within a minute; errors drop to baseline."
---
# Expired TLS / mTLS certificate

**Symptoms.** A near-total error rate starting at an exact minute; logs show
`x509: certificate has expired or is not yet valid`; the server side may log
`certificate verify failed`. The downstream datastore itself is healthy - its connection
count simply *drops* because clients cannot complete the handshake.

## Confirm
1. `list_certificates` and compare `not_after` with the onset minute.
2. Check which component owns the expired certificate (client vs server side).

## Mitigate
- `rotate_certificate(<name>)`. Restarting or rolling back does **not** help.

## Follow-up
- Alert 30/14/7 days before expiry; automate renewal via cert-manager.
