# a2a-e2e

End-to-end black-box tests for the a2a platform API. No SDK imports, no
control-plane internals — just HTTP against `api.a2acloud.io` (or any
deployment via `A2A_E2E_API_URL`).

## Coverage

| File | Tests |
|---|---|
| `test_auth.py` | signup, login, wrong password, 401 paths |
| `test_files.py` | upload (root + prefix), read, move, delete, dot-dot traversal, cross-user isolation |
| `test_discovery.py` | `/v1/agents` returns cards, test-helper discoverable, skill schema is strict |
| `test_chart_chat.py` | full chart flow: SSE chat → main-agent sandbox tools → matplotlib → PNG lands in MinIO (slow, ~1-2min) |

## Run

```bash
cd apps/e2e
python3 -m venv .venv
.venv/bin/pip install -e '.[]'
.venv/bin/pip install pytest pytest-asyncio httpx

# Production cluster (default)
.venv/bin/pytest -v

# Local docker-desktop cluster
A2A_E2E_API_URL=http://api.127-0-0-1.nip.io .venv/bin/pytest -v

# Fast tests only (skip the chart flow)
.venv/bin/pytest -v -m "not slow"
```

Each session spins up a fresh `e2e-<timestamp>@example.com` test user via
`/v1/auth/signup`. Tests share the user's bucket; CSV paths and output
names use unique suffixes so re-runs don't collide.

## What it does NOT cover (yet)

- Approval-mode handoff modal (would need a parallel WebSocket / SSE
  consumer to click approve).
- Scope-request mid-skill flow (would need a callee agent that calls
  `ctx.request_scope` to exercise).
- Grant-audit DB chain verification (no public read endpoint; would need
  `/v1/me/grants` or psql access).
- Dashboard UI itself — only the API surface.
