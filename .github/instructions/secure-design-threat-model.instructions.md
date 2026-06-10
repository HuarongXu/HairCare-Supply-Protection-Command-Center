---
applyTo: "**/docs/**, **/REQUIREMENTS*.md, **/README*"
---

# Secure Design and Threat Modeling Instructions

Apply these rules when designing or implementing new features, architecture changes, or production-intended functionality.

## OWASP attack scenarios to prevent

- Insecure design: sensitive pipeline execution lacks abuse prevention → identify abuse cases and add authorization, rate limits, audit logs.
- Prototype drift: demo code with weak controls becomes production code → mark prototype assumptions and require review.
- Missing idempotency: repeated pipeline trigger causes data corruption → design idempotency and duplicate protection.

## MUST

- MUST identify trust boundaries, data flows, sensitive assets, and privileged actions for new features.
- MUST define authentication, authorization, logging, and error handling expectations for production-intended features.
- MUST treat pipeline execution, data exports, and admin actions as sensitive flows.
- MUST document owner, data sensitivity, and exposure level for production-intended changes.

## SHOULD

- SHOULD use secure defaults and deny-by-default assumptions.
- SHOULD propose threat scenarios and mitigations before implementing high-risk features.

## NEVER

- NEVER treat working code as production-ready solely because it passes functional tests.
- NEVER omit authorization or audit logging from sensitive flows.

## HITL triggers

Require human approval before implementing production-intended sensitive flows, new trust boundaries, or autonomous automation.
