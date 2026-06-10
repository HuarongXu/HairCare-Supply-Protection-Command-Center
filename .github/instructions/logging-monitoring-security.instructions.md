---
applyTo: "**/dashboards/matres_app*.py, **/scripts/matres_pipeline*.py, **/scripts/generate_weekly_mail_preview.py"
---

# Logging and Monitoring Security Instructions

Apply these rules when working with logs, audit trails, monitoring, error handling, or telemetry in this application.

## OWASP attack scenarios to prevent

- Missing auth signal: repeated login failures are not logged → record authentication failures with source IP.
- Missing audit trail: pipeline runs, data exports, and admin actions cannot be investigated → create audit logs.
- Sensitive log leakage: logs contain file paths, passwords, or PII → redact sensitive values.
- Log injection: attacker adds CR/LF or control characters via data fields → use structured logging.

## MUST

- MUST log authentication success/failure with source IP address.
- MUST log pipeline execution start/stop with initiator identity.
- MUST log admin actions: data export, pipeline trigger, config changes.
- MUST avoid logging passwords, secrets, employee email addresses, or full file paths to sensitive data.
- MUST return generic errors to users and log diagnostics server-side only.
- MUST use structured logging for security-relevant events.

## SHOULD

- SHOULD add correlation IDs to track requests across dashboard and pipeline.
- SHOULD make logs actionable for incident response.
- SHOULD sanitize `pipeline_progress.json` error messages to remove internal file paths.

## NEVER

- NEVER log admin passwords, credentials, or full employee email lists.
- NEVER suppress security-relevant errors without recovery logic and logging.
- NEVER expose stack traces or internal paths in user-facing error messages.

## HITL triggers

Require human approval before reducing security-event logging, changing audit log behavior, or logging data that may contain PII.
