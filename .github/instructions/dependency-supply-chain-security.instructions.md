---
applyTo: "**/requirements*.txt, **/pyproject.toml, **/server_deploy.bat, **/update_and_start_matres*.bat"
---

# Dependency and Supply Chain Security Instructions

Apply these rules when adding, removing, upgrading, or replacing Python packages, or modifying batch scripts that install dependencies.

## OWASP attack scenarios to prevent

- Slopsquatting / hallucinated package: AI suggests a non-existent or malicious package → verify package name, publisher, and reputation on PyPI.
- Auto-update code execution: `server_deploy.bat` runs `git reset --hard origin/main` and auto-restarts → compromised GitHub repo leads to automatic RCE → protect the git auto-update mechanism.
- Malicious install scripts: dependency executes code during `pip install` → review package before adding.

## MUST

- MUST prefer existing project dependencies and well-maintained libraries.
- MUST justify every new dependency: purpose, source, maintenance state, and safer alternatives.
- MUST review `requirements.txt` changes and explain dependency purpose.
- MUST use pinned versions in `requirements.txt` where practical.
- MUST protect the git auto-update mechanism in deployment scripts against supply chain attacks.

## SHOULD

- SHOULD avoid new dependencies for small utilities that can be implemented with standard library.
- SHOULD prefer trusted registries and widely-used packages.

## NEVER

- NEVER invent package names.
- NEVER install unknown, abandoned, or unverified packages without review.
- NEVER auto-execute remote scripts (`git reset --hard` + auto-restart) without integrity checks.

## HITL triggers

Require human approval before adding, upgrading, or removing dependencies; modifying deployment scripts; or changing the auto-update mechanism.
