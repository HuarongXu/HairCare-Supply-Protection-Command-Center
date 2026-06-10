---
applyTo: "**/scripts/matres_pipeline*.py, **/scripts/create_dashboard_snapshot.py, **/scripts/generate_weekly_mail_preview.py, **/dashboards/matres_app*.py, **/0.Data Base/**"
---

# File, Storage, and Network Boundary Security Instructions

Apply these rules when working with file reading/writing, Excel/CSV ingestion, snapshot exports, or network boundaries in this application.

## OWASP attack scenarios to prevent

- Path traversal: user-controlled values in file names or paths reach `open()` or `Path()` → canonicalize paths and restrict to approved directories.
- Unsafe file serving: `/docs/user-guide` serves HTML files directly via `Response(path.read_text())` without auth → add authorization or sanitize content.
- Internal network exposure: dashboard on `0.0.0.0:8050` without TLS → all data visible on LAN in plaintext.

## MUST

- MUST canonicalize file paths and restrict file access to approved directories (`data/`, `0.Data Base/`, `docs/`).
- MUST validate file extensions and content type for any file serving endpoint.
- MUST prevent path traversal by rejecting `..` and encoded path separators in any user-influenced path component.
- MUST store exported snapshots in server-controlled directories with server-generated file names.

## SHOULD

- SHOULD add authentication to file-serving endpoints like `/docs/user-guide` and `/mail-preview/latest`.
- SHOULD add audit logs for data exports and snapshot generation.
- SHOULD deploy behind TLS for production use.

## NEVER

- NEVER trust user-supplied file names or paths.
- NEVER serve arbitrary files from the file system without path validation.
- NEVER expose internal file paths in error messages or API responses.

## HITL triggers

Require human approval before adding file serving endpoints, bulk export features, or changing file access patterns.
