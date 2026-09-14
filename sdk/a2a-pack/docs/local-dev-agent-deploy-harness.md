# Local Dev Agent Deploy Harness

Use the A2A Pack local harness when an agent or test needs to upload an agent
to the Docker Compose devcontainer control plane without a browser login.

The harness wraps the regular `a2a deploy` tarball upload path and only changes
credential bootstrap:

- waits for `http://localhost:8000/healthz`;
- uses `A2A_LOCAL_CP_TOKEN`, `A2A_CP_TOKEN`, `A2A_API_TOKEN`, or `A2A_TOKEN` when
  one is set;
- otherwise runs `docker exec a2a-control-plane python -m
  control_plane.e2e_users --email local@example.com --json` to mint a
  short-lived local bearer token for the normal seeded dashboard user from
  `AGENTS.md`;
- compiles the project DSL, packages the source tarball, and posts it to
  `/v1/agents/from-tarball`.

In the Docker Compose devcontainer, the upload commits both the managed source
repo and the hidden runtime repo in Gitea. When no Kubernetes config is present,
the control plane records the ArgoCD stage as skipped instead of failing the
upload. Use `--wait-agent` only with a stack that can actually reconcile and
serve the returned agent URL.

## Command

From the workspace root:

```bash
PYTHONPATH=sdk/a2a-pack python -m a2a_pack.cli.main local-deploy apps/test-helper --json
```

When `a2a-pack` is installed in the active environment, the shorter form is:

```bash
a2a local-deploy apps/test-helper --json
```

Useful options:

- `--api http://localhost:8000` overrides the control-plane URL.
- `--token "$JWT"` uses an explicit bearer token.
- `--token-email test-owner@a2acloud.test` overrides the default local owner
  `local@example.com`.
- `--no-docker-token` fails instead of running `docker exec` when no token env
  var is present.
- `--private` uploads the agent as private.
- `--wait-agent` polls the returned agent URL `/healthz`.

Clean up a local deploy with the same token bootstrap:

```bash
PYTHONPATH=sdk/a2a-pack python -m a2a_pack.cli.main local-cleanup test-helper --json
```

Use `--ignore-missing` for idempotent test teardown.

The JSON output is stable enough for tests:

```json
{
  "agent": "test-helper",
  "version": "0.1.0",
  "status": "building",
  "url": "https://test-helper.a2acloud.io",
  "head_sha": "abc1234",
  "deployment_id": "dep_...",
  "api_url": "http://localhost:8000",
  "owner_email": "local@example.com",
  "tarball_bytes": 12345,
  "agent_ready": null
}
```

## Python Test Fixture

Tests can call the harness directly:

```python
import os
from pathlib import Path

import pytest
from a2a_pack.cli.local_harness import cleanup_local_agent, deploy_local_agent


@pytest.fixture(scope="session")
def deployed_test_helper():
    result = deploy_local_agent(
        Path(os.environ.get("A2A_TEST_HELPER_PROJECT", "apps/test-helper")),
        api_url=os.environ.get("A2A_LOCAL_API_URL", "http://localhost:8000"),
        token_email="test-helper-owner@a2acloud.test",
        wait_agent_ready=False,
    )
    yield result.as_dict()
    cleanup_local_agent(
        result.agent,
        api_url=os.environ.get("A2A_LOCAL_API_URL", "http://localhost:8000"),
        token_email="test-helper-owner@a2acloud.test",
        ignore_missing=True,
    )
```

Set `wait_agent_ready=True` only when the local stack includes a runtime path
that actually serves the deployed agent URL. The default Docker Compose
devcontainer accepts the upload and commits the managed Gitea repos, but it does
not provide Kubernetes/Argo reconciliation by default.

## Local Stack Checklist

The devcontainer Compose file is not checked into this repository. Start the
local stack however your checkout provides it, then confirm the containers the
harness talks to are up:

```bash
docker ps --filter name=a2a-control-plane --filter name=a2a-gitea
```

The harness expects:

- `a2a-control-plane` is running;
- `http://localhost:8000/healthz` returns `200`;
- Gitea is reachable by the control plane, because `/from-tarball` commits the
  uploaded source and hidden runtime repo.

If Docker token minting is not available, run tests with an explicit token:

```bash
export A2A_LOCAL_CP_TOKEN="$(docker exec a2a-control-plane python -m control_plane.e2e_users)"
pytest apps/e2e
```

If uploads fail with Gitea `403 Forbidden`, check the seeded admin account:

```bash
curl -u 'gitea_admin:LocalGitea123!' http://localhost:3002/api/v1/user
```

A `must change your password` response means the seed script was not rerun after
the local fix. Clear the flag with:

```bash
docker exec --user git a2a-gitea gitea admin user change-password \
  --username gitea_admin \
  --password 'LocalGitea123!' \
  --must-change-password=false
```
