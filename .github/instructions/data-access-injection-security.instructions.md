---
applyTo: "**/scripts/matres_pipeline*.py, **/scripts/compare_*.py, **/scripts/trace_*.py, **/scripts/check_dates.py, **/scripts/count_cb.py"
---

# Data Access and Injection Security Instructions

Apply these rules when working with CSV/Excel data ingestion, pandas pipelines, data filtering, sorting, subprocess command construction, or any code that converts input into commands or file paths.

## OWASP attack scenarios to prevent

- OS command injection: user-controlled values reach `subprocess.Popen` command arguments → use argument arrays, never shell=True with user input.
- Path traversal: user-controlled file names used to construct file paths → validate and canonicalize paths, restrict to approved directories.
- Log injection: attacker includes CR/LF or control characters in data fields that are logged → normalize log fields.

## MUST

- MUST use argument arrays (lists) for `subprocess.Popen`, never `shell=True` with dynamic input.
- MUST validate `--stages` and `--group` subprocess arguments against allow-listed values before passing to subprocess.
- MUST canonicalize file paths and restrict file access to approved directories (`data/`, `0.Data Base/`).
- MUST normalize log fields to prevent log injection via CR/LF or control characters.

## SHOULD

- SHOULD centralize path construction and validation patterns.
- SHOULD add tests for path traversal payloads in file-handling functions.

## NEVER

- NEVER concatenate user-controlled input into OS commands, shell commands, or file path strings without validation.
- NEVER use `shell=True` in `subprocess.Popen` or `subprocess.run`.
- NEVER write unnormalized user-controlled values into log lines.
- NEVER use `eval`, `exec`, or dynamic code execution with external input.

## HITL triggers

Require human approval before adding raw subprocess calls, new file path construction, or any feature that translates user input into executable syntax.
