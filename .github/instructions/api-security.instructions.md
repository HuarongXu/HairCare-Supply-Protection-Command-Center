---
applyTo: "**/dashboards/matres_app*.py, **/scripts/matres_pipeline*.py, **/scripts/generate_weekly_mail_preview.py, **/scripts/create_dashboard_snapshot.py"
---

# API Security Instructions

Apply these rules when generating, modifying, or reviewing Dash callbacks, Flask routes, API handlers, or any server-side endpoint in this repository.

## OWASP attack scenarios to prevent

- API1 BOLA / IDOR: attacker changes object identifier in a callback parameter to access another user's data → verify object ownership and authorization before every data operation.
- API3 BOPLA: Dash callback returns raw DataFrames with internal columns → filter response data to only required fields.
- API5 BFLA: unauthenticated user calls admin callback directly → enforce server-side auth on every admin-gated callback.
- API4 unrestricted resource consumption: attacker triggers unlimited pipeline runs or data exports → add rate limits, cooldowns, and quotas.

## MUST

- MUST enforce server-side authentication on every admin callback and pipeline trigger.
- MUST validate Dash callback inputs (dropdown values, date ranges) against allow-listed values.
- MUST add rate limiting / cooldown to pipeline execution and data export endpoints.
- MUST return only required data fields in callback responses.

## SHOULD

- SHOULD separate admin callbacks from public dashboard callbacks with clear naming and auth checks.
- SHOULD add tests for unauthenticated access to admin endpoints.
- SHOULD return generic errors for auth failures and log details server-side.

## NEVER

- NEVER rely on `dcc.Store` or client-side session state as authorization.
- NEVER expose raw DataFrames with internal columns to unauthenticated users.
- NEVER allow unlimited pipeline subprocess invocations without rate limiting.

## HITL triggers

Require human approval before changing authentication, authorization, pipeline subprocess triggers, data export callbacks, or externally reachable endpoint behavior.
