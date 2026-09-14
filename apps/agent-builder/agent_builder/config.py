"""Runtime config — env-driven, matches the rest of the cluster."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    sandbox_url: str
    sandbox_timeout_s: int
    sandbox_token: str | None
    deploy_wait_timeout_s: int
    litellm_url: str
    litellm_key: str
    litellm_model: str
    cp_url: str
    minio_endpoint: str
    minio_access_key: str
    minio_secret_key: str
    image: str


def load_settings() -> Settings:
    return Settings(
        sandbox_url=(
            os.environ.get("A2A_SANDBOX_URL")
            or os.environ.get("SANDBOX_URL", "http://sandbox.sandbox.svc.cluster.local:8000")
        ),
        sandbox_timeout_s=int(
            os.environ.get("A2A_SANDBOX_TIMEOUT_S")
            or os.environ.get("SANDBOX_TIMEOUT_S", "1200")
        ),
        sandbox_token=os.environ.get("A2A_SANDBOX_TOKEN") or os.environ.get("SANDBOX_TOKEN"),
        deploy_wait_timeout_s=int(os.environ.get("DEPLOY_WAIT_TIMEOUT_S", "900")),
        litellm_url=os.environ.get(
            "A2A_LITELLM_URL", "http://litellm.llm.svc.cluster.local:4000"
        ),
        litellm_key=os.environ.get("A2A_LITELLM_KEY", ""),
        litellm_model=os.environ.get("A2A_LITELLM_MODEL", "gpt-5.5"),
        cp_url=os.environ.get(
            "A2A_CP_URL", "http://control-plane.control-plane.svc.cluster.local"
        ),
        minio_endpoint=os.environ.get(
            "A2A_MINIO_ENDPOINT",
            "http://a2a-infra-minio.a2a-infra.svc.cluster.local:9000",
        ),
        minio_access_key=os.environ.get("A2A_MINIO_ACCESS_KEY", ""),
        minio_secret_key=os.environ.get("A2A_MINIO_SECRET_KEY", ""),
        image=os.environ.get("AGENT_BUILDER_IMAGE", "python:3.11-slim"),
    )
