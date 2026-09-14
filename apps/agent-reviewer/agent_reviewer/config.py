"""Runtime config — env-driven."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    sandbox_url: str
    sandbox_timeout_s: int
    sandbox_token: str | None
    gitea_internal: str
    gitea_public: str
    litellm_url: str
    litellm_key: str
    litellm_model: str
    cp_url: str
    image: str


def load_settings() -> Settings:
    return Settings(
        sandbox_url=(
            os.environ.get("A2A_SANDBOX_URL")
            or os.environ.get("SANDBOX_URL", "http://sandbox.sandbox.svc.cluster.local:8000")
        ),
        sandbox_timeout_s=int(
            os.environ.get("A2A_SANDBOX_TIMEOUT_S")
            or os.environ.get("SANDBOX_TIMEOUT_S", "600")
        ),
        sandbox_token=os.environ.get("A2A_SANDBOX_TOKEN") or os.environ.get("SANDBOX_TOKEN"),
        gitea_internal=os.environ.get(
            "A2A_GITEA_INTERNAL", "http://gitea-http.gitea.svc.cluster.local:3000"
        ),
        gitea_public=os.environ.get("A2A_GITEA_PUBLIC", "http://gitea.a2acloud.io"),
        litellm_url=os.environ.get(
            "A2A_LITELLM_URL", "http://litellm.llm.svc.cluster.local:4000"
        ),
        litellm_key=os.environ.get("A2A_LITELLM_KEY", ""),
        litellm_model=os.environ.get("A2A_LITELLM_MODEL", "gpt-5.5"),
        cp_url=os.environ.get(
            "A2A_CP_URL", "http://control-plane.control-plane.svc.cluster.local"
        ),
        image=os.environ.get("AGENT_REVIEWER_SANDBOX_IMAGE", "python:3.11-slim"),
    )
