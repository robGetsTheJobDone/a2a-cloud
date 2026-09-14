# Contributing to A2A Cloud

Thanks for your interest. This is a monorepo; each subsystem has its own `agent.md` with layout, commands, and
conventions. Read the one for the area you are touching before opening a PR.

## Quick start

```bash
# SDK (Python 3.11+, uv)
cd sdk/a2a-pack && uv sync --all-extras && uv run pytest -q

# TypeScript sidecar
cd sdk/a2a-pack/typescript && npm ci && npm test

# Control plane (FastAPI)
cd control-plane && uv sync --all-extras && uv run pytest -q

# Web workspace (pnpm 10, Node 22): dashboard + docs
cd web && pnpm install && pnpm typecheck && pnpm test
```

## Pull requests

- Keep PRs scoped to one change. Say *why* in the description.
- Add or update tests next to the code you change. CI must be green.
- Python: `ruff` formatting and linting. TypeScript: `pnpm lint` and `pnpm typecheck`.
- Docs reference pages are generated. If you change the SDK, CLI, or control-plane routes, run
  `python web/apps/docs/scripts/gen.py` and `python web/apps/docs/scripts/gen_control_plane_api.py` from the SDK
  environment; CI runs `scripts/check.py` and fails on drift.
- The embedded TypeScript sidecar under `sdk/a2a-pack/a2a_pack/typescript` is generated from
  `sdk/a2a-pack/typescript`; run `sdk/a2a-pack/scripts/sync-ts-sidecar.sh` after editing the source.
- No secrets, private hostnames, or build output in commits.

## Reporting bugs / proposing features

Use the issue templates. For security issues see [SECURITY.md](SECURITY.md).

## License

By contributing you agree that your contributions are licensed under the [MIT License](LICENSE).
