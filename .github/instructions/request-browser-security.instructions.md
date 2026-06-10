---
applyTo: "**/dashboards/matres_app*.py, **/server_deploy.bat, **/update_and_start_matres*.bat"
---

# Request and Browser Security Instructions

Apply these rules when working on request middleware, CORS, CSRF, redirects, headers, cookies, reverse proxy handling, or server bootstrap in this Dash/Flask application.

## OWASP attack scenarios to prevent

- X-Forwarded-For spoofing: `enforce_internal_access()` trusts `X-Forwarded-For` header → attacker spoofs header to bypass IP-based access control → trust forwarded headers only from configured reverse proxies.
- Missing security headers: no CSP, X-Frame-Options, HSTS → clickjacking and content-type sniffing attacks.
- Open network binding: `0.0.0.0:8050` exposes dashboard to entire LAN without TLS → plaintext admin password on wire.

## MUST

- MUST NOT trust `X-Forwarded-For` header unless running behind a configured reverse proxy.
- MUST use `request.remote_addr` for IP-based access control when no reverse proxy is configured.
- MUST set security headers: `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: strict-origin-when-cross-origin`.
- MUST apply rate limiting to authentication and expensive operations (pipeline runs, data exports).

## SHOULD

- SHOULD deploy behind a TLS-terminating reverse proxy (nginx/Caddy) for production use.
- SHOULD restrict binding to specific network interface if public LAN exposure is not needed.
- SHOULD set CSP header appropriate for Dash/Plotly (allow plotly.js CDN if used).

## NEVER

- NEVER expose admin functionality over plaintext HTTP on untrusted networks.
- NEVER accept arbitrary redirect URLs from users.
- NEVER expose debug routes, stack traces, or admin consoles without authorization.

## HITL triggers

Require human approval before changing network binding, security headers, IP-based access control, proxy trust configuration, or TLS settings.
