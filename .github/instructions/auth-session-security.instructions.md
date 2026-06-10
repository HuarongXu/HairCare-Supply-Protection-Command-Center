---
applyTo: "**/dashboards/matres_app*.py, **/config/config*.json, **/server_deploy.bat, **/update_and_start_matres*.bat"
---

# Authentication, Authorization, and Session Security Instructions

Apply these rules when working on admin login, session management, password handling, or access control in this Dash/Flask application.

## OWASP attack scenarios to prevent

- Client-side auth bypass: auth state stored in `dcc.Store(storage_type="session")` (browser sessionStorage) → attacker sets `{"authenticated": true}` in DevTools → use server-side Flask sessions instead.
- Hardcoded password: `"admin_password": "HR"` in config.json and server_deploy.bat → attacker reads source or guesses trivially → use environment variables or deploy-time prompts.
- Unauthenticated pipeline execution: admin callbacks lack server-side auth verification → any LAN user can trigger subprocess → gate all admin callbacks with server-side session check.
- No login rate limiting: unlimited password attempts → brute force → add failed login attempt limiting.

## MUST

- MUST use server-side Flask sessions (with `app.server.secret_key` set to a cryptographically random value) for admin authentication — NOT `dcc.Store` or browser `sessionStorage`.
- MUST store admin passwords in environment variables or prompt at deploy time — NOT in config.json, batch scripts, or source code.
- MUST verify server-side session on every admin callback before executing side effects (pipeline runs, data exports, config changes).
- MUST limit failed login attempts and log authentication failures with source IP.
- MUST set Flask `SECRET_KEY` to a cryptographically random value, loaded from environment.

## SHOULD

- SHOULD use generic error messages for login failures ("Invalid credentials") and log details server-side.
- SHOULD regenerate session after successful login.
- SHOULD add a session timeout / expiration mechanism.
- SHOULD add tests for unauthenticated admin access and client-side auth bypass.

## NEVER

- NEVER store auth state in `dcc.Store`, browser `sessionStorage`, `localStorage`, or cookies without server-side validation.
- NEVER hardcode passwords in config files, batch scripts, or Python source.
- NEVER use a trivially guessable password like "HR", "admin", "password", "changeme".
- NEVER expose the admin password in error messages, logs, or API responses.

## HITL triggers

Require human approval before modifying authentication, session, password storage, admin access control, or deployment scripts that set credentials.
