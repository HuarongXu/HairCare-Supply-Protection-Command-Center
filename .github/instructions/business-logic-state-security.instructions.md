---
applyTo: "**/dashboards/matres_app*.py, **/scripts/matres_pipeline*.py"
---

# Business Logic and State Integrity Security Instructions

Apply these rules when working on pipeline execution, data refresh, data export, admin workflows, or state transitions in this application.

## OWASP attack scenarios to prevent

- Replay / duplicate action: attacker triggers pipeline execution repeatedly → use cooldown/lockout and idempotency checks.
- Broken business authorization: unauthenticated user triggers admin workflow → verify server-side auth on all admin actions.
- Abuse at scale: automated requests trigger unlimited pipeline runs consuming server resources → add rate limits and quotas.

## MUST

- MUST enforce server-side authorization for pipeline execution and data export triggers.
- MUST add cooldown / rate limiting to prevent rapid successive pipeline executions.
- MUST validate pipeline state before allowing new execution (no concurrent runs).
- MUST add audit logs for pipeline start/stop, data exports, and admin actions.

## SHOULD

- SHOULD add idempotency protection against duplicate pipeline triggers.
- SHOULD log meaningful audit events for state transitions.
- SHOULD add tests for unauthorized pipeline execution and concurrent run protection.

## NEVER

- NEVER rely on frontend state or hidden UI to enforce business rules.
- NEVER allow unlimited pipeline execution without rate limiting and auth checks.
- NEVER allow bulk data operations without authorization and audit logging.

## HITL triggers

Require human approval before changing pipeline execution logic, data export behavior, admin workflow, or rate limiting controls.
