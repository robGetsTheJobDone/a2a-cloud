from __future__ import annotations

RUNTIME_SECRET_SUFFIX = "agent-secrets"


def agent_runtime_secret_name(agent_name: str) -> str:
    return f"{agent_name}-{RUNTIME_SECRET_SUFFIX}"
