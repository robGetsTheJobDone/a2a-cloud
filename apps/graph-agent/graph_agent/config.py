"""Runtime config — env-driven, matches the rest of the cluster."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    sandbox_url: str
    sandbox_timeout_s: int
    sandbox_token: str | None
    litellm_url: str
    litellm_key: str
    litellm_model: str
    image: str


def load_settings() -> Settings:
    return Settings(
        sandbox_url=(
            os.environ.get("A2A_SANDBOX_URL")
            or os.environ.get("SANDBOX_URL", "http://sandbox.sandbox.svc.cluster.local:8000")
        ),
        sandbox_timeout_s=int(
            os.environ.get("A2A_SANDBOX_TIMEOUT_S")
            or os.environ.get("SANDBOX_TIMEOUT_S", "240")
        ),
        sandbox_token=os.environ.get("A2A_SANDBOX_TOKEN") or os.environ.get("SANDBOX_TOKEN"),
        litellm_url=os.environ.get(
            "A2A_LITELLM_URL", "http://litellm.llm.svc.cluster.local:4000"
        ),
        litellm_key=os.environ.get("A2A_LITELLM_KEY", ""),
        litellm_model=os.environ.get("A2A_LITELLM_MODEL", "gpt-5.5"),
        image=os.environ.get("GRAPH_AGENT_IMAGE", "python:3.11-slim"),
    )
