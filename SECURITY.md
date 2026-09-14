# Security Policy

## Reporting a vulnerability

Please **do not** open a public issue for security problems.

Use GitHub's private vulnerability reporting ("Report a vulnerability" under
the Security tab) for this repository. You will get an acknowledgement within
a few days and a fix or mitigation plan as soon as the issue is confirmed.

## Scope

- `sdk/a2a-pack` (Python + TypeScript SDK and CLI)
- `control-plane/` (API and orchestration)
- `apps/*` first-party services
- `web/*` dashboard, docs, landing

Vulnerabilities in third-party dependencies should be reported upstream to that project.

## Supported versions

Only the `main` branch and the latest published SDK release receive fixes.
