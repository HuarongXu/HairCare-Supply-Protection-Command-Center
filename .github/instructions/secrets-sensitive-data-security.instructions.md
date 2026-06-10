---
applyTo: "**/config/config*.json, **/config/config.example.json, **/config/requester_roles.json, **/server_deploy.bat, **/update_and_start_matres*.bat, **/*.env*"
---

# Secrets and Sensitive Data Security Instructions

Apply these rules when working with configuration, environment variables, secrets, credentials, or PII in this repository.

## OWASP attack scenarios to prevent

- Hardcoded secret: `"admin_password": "HR"` is committed in `config.json` and hardcoded in `server_deploy.bat` → use environment variables or deploy-time prompts.
- PII exposure: `requester_roles.json` contains 14 employee email addresses visible to all LAN users → do not expose in unauthenticated API responses.
- Infrastructure disclosure: internal IP `143.35.13.175` and GitHub repo URL hardcoded in committed files → use environment variables or config.
- Debug disclosure: pipeline errors may expose internal file paths in `pipeline_progress.json` → sanitize error messages.

## MUST

- MUST store admin passwords in environment variables or prompt at deploy time — NOT in config files or batch scripts.
- MUST NOT commit `config/config.json` with real credentials (use `.gitignore` + `config.example.json`).
- MUST protect employee email addresses in `requester_roles.json` from unauthenticated access.
- MUST redact internal file paths, IP addresses, and infrastructure details from error responses.
- MUST use obvious fake placeholders in `config.example.json` and documentation.

## SHOULD

- SHOULD store internal IP addresses and Git repository URLs in environment variables rather than source code.
- SHOULD mask sensitive values in admin panel UI.
- SHOULD sanitize `pipeline_progress.json` error messages to remove internal paths.

## NEVER

- NEVER hardcode production credentials, admin passwords, or API keys in config files or batch scripts.
- NEVER commit `config/config.json` with real passwords.
- NEVER expose employee email addresses, internal IPs, or Git repository URLs in unauthenticated responses.
- NEVER include real PII or live secrets in examples, tests, or documentation.

## HITL triggers

Require human approval before reading, modifying, or exporting sensitive config files, credentials, PII, or security configuration.
