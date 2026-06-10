---
applyTo: "**/dashboards/matres_app*.py, **/dashboards/assets/**, **/docs/*.html, **/scripts/generate_weekly_mail_preview.py, **/scripts/create_dashboard_snapshot.py"
---

# Frontend and XSS Security Instructions

Apply these rules when working on Dash layout components, HTML responses, assets, templates, or browser-facing code in this repository.

## OWASP attack scenarios to prevent

- DOM XSS: user-controlled data rendered via `dash_dangerously_set_inner_html` or raw `Response(html_text)` → use Dash's safe rendering or sanitize HTML.
- URL injection: attacker sets `href`, redirect, or callback URL to `javascript:` or malicious domain → validate with allow-listed schemes and hosts.
- Client-side auth bypass: frontend hides admin panel but server lacks authorization → keep frontend checks as UX only; enforce security server-side.

## MUST

- MUST use Dash's safe component rendering by default.
- MUST sanitize any raw HTML before passing to `Response()` or `dash_dangerously_set_inner_html`.
- MUST validate URL schemes and hosts for links and redirects.
- MUST treat frontend authorization checks (hiding/showing admin panel) as UX only — protected actions require server-side authorization.

## SHOULD

- SHOULD set security headers: `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Content-Security-Policy`.
- SHOULD avoid storing secrets or privileged data in frontend code or browser storage.

## NEVER

- NEVER use `dash_dangerously_set_inner_html` with untrusted content.
- NEVER trust data rendered from CSV/Excel if users can modify the source files.
- NEVER expose secrets, internal paths, or infrastructure details in frontend code.

## HITL triggers

Require human approval before adding raw HTML rendering, third-party scripts, iframe integrations, or browser storage of sensitive data.
