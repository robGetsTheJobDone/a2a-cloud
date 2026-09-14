"""Runtime configuration — read from env, mirrors the control plane."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    litellm_url: str
    litellm_key: str
    litellm_model: str
    sandbox_url: str
    sandbox_timeout_s: float
    sandbox_token: str | None
    minio_endpoint: str
    minio_access_key: str
    minio_secret_key: str
    cp_url: str
    agents_namespace_dns: str
    platform_llm_models: tuple[str, ...] = ()
    platform_llm_max_budget_usd: float = 1.0
    platform_llm_rpm_limit: int = 60
    platform_llm_tpm_limit: int = 200000
    repo_mounts: str = ""


def load_settings() -> Settings:
    litellm_model = os.environ.get("A2A_LITELLM_MODEL", "gpt-5.5")
    platform_models = tuple(
        item.strip()
        for item in os.environ.get("A2A_PLATFORM_LLM_MODELS", "").split(",")
        if item.strip()
    )
    return Settings(
        litellm_url=os.environ.get(
            "A2A_LITELLM_URL", "http://litellm.llm.svc.cluster.local:4000"
        ),
        litellm_key=os.environ.get("A2A_LITELLM_KEY", ""),
        litellm_model=litellm_model,
        platform_llm_models=platform_models,
        platform_llm_max_budget_usd=float(
            os.environ.get("A2A_PLATFORM_LLM_GRANT_MAX_BUDGET_USD", "1.0")
        ),
        platform_llm_rpm_limit=int(
            os.environ.get("A2A_PLATFORM_LLM_GRANT_RPM_LIMIT", "60")
        ),
        platform_llm_tpm_limit=int(
            os.environ.get("A2A_PLATFORM_LLM_GRANT_TPM_LIMIT", "200000")
        ),
        repo_mounts=os.environ.get(
            "A2A_REPO_MOUNTS",
            os.environ.get("A2A_CP_REPO_MOUNTS", ""),
        ),
        sandbox_url=os.environ.get(
            "A2A_SANDBOX_URL", "http://sandbox.sandbox.svc.cluster.local:8000"
        ),
        sandbox_timeout_s=float(os.environ.get("A2A_SANDBOX_TIMEOUT_S", "180")),
        sandbox_token=os.environ.get("A2A_SANDBOX_TOKEN"),
        minio_endpoint=os.environ.get(
            "A2A_MINIO_ENDPOINT",
            "http://a2a-infra-minio.a2a-infra.svc.cluster.local:9000",
        ),
        minio_access_key=os.environ.get("A2A_MINIO_ACCESS_KEY", ""),
        minio_secret_key=os.environ.get("A2A_MINIO_SECRET_KEY", ""),
        cp_url=(
            os.environ.get("A2A_CP_URL_INTERNAL")
            or os.environ.get("A2A_CP_URL")
            or "http://control-plane.control-plane.svc.cluster.local"
        ),
        # Pattern for in-cluster agent service DNS. Used to resolve a
        # discovered agent name to its cluster URL when minting a handoff.
        agents_namespace_dns=os.environ.get(
            "A2A_AGENTS_DNS", "{name}.agents.svc.cluster.local"
        ),
    )
