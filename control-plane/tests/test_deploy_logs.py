from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

os.environ.setdefault("A2A_CP_DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from control_plane import deployments
from control_plane.db import Base
from control_plane.log_scrub import (
    SecretQueryParamFilter,
    bound_and_scrub,
    install_access_log_scrubbing,
    scrub_query_params,
    scrub_secrets,
)
from control_plane.models import Agent, AgentDeployment, AgentDeploymentLog, User


# ---------------------------------------------------------------------------
# Secret scrubbing / bounding
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw",
    [
        "Authorization: Bearer abcDEF123456ghijkl",
        "clone https://gitea_admin:supersecret@gitea/repo.git",
        "DB_PASSWORD=hunter2taylorswift",
        "token eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV",
        "client_secret: 'sk-live-abcdef....'",
    ],
)
def test_scrub_secrets_redacts_credentials(raw: str) -> None:
    assert "«redacted»" in scrub_secrets(raw)


def test_scrub_secrets_preserves_plain_text() -> None:
    line = "INFO uvicorn running on http://0.0.0.0:8000 ready in 1.2s"
    assert scrub_secrets(line) == line


def test_bound_and_scrub_keeps_tail_and_flags_truncated() -> None:
    text = "\n".join(f"line{i}" for i in range(1000))
    content, truncated = bound_and_scrub(text, max_bytes=40)
    assert truncated is True
    assert content.encode("utf-8").__len__() <= 40
    # Tail is preserved, head dropped.
    assert "line999" in content
    assert "line000" not in content


def test_bound_and_scrub_empty() -> None:
    assert bound_and_scrub("", max_bytes=100) == ("", False)


# ---------------------------------------------------------------------------
# Access-log scrubbing (uvicorn logs the raw request line, query string included)
# ---------------------------------------------------------------------------

def test_scrub_query_params_redacts_integration_token_only() -> None:
    line = (
        "/v1/agents/reporter/api/invoke/build_report"
        "?integration_token=a2a_app_SUPERSECRETVALUE&topic=sales"
    )
    scrubbed = scrub_query_params(line)
    assert "a2a_app_SUPERSECRETVALUE" not in scrubbed
    assert "integration_token=«redacted»" in scrubbed
    # Surrounding request context stays readable.
    assert "topic=sales" in scrubbed
    assert scrubbed.startswith("/v1/agents/reporter/api/invoke/build_report?")


def test_access_log_filter_redacts_uvicorn_request_line() -> None:
    install_access_log_scrubbing(["test.access.scrub"])
    logger = logging.getLogger("test.access.scrub")
    # Installing twice must not stack duplicate filters.
    install_access_log_scrubbing(["test.access.scrub"])
    assert sum(isinstance(f, SecretQueryParamFilter) for f in logger.filters) == 1

    record = logger.makeRecord(
        logger.name,
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        (
            "10.0.0.1:5555",
            "POST",
            "/v1/agents/reporter/mcp?integration_token=a2a_app_LEAKME",
            "1.1",
            200,
        ),
        None,
    )
    for log_filter in logger.filters:
        log_filter.filter(record)
    formatted = record.getMessage()
    assert "a2a_app_LEAKME" not in formatted
    assert "integration_token=«redacted»" in formatted
    assert '"POST /v1/agents/reporter/mcp' in formatted
    assert formatted.endswith('HTTP/1.1" 200')


# ---------------------------------------------------------------------------
# Log formatting helpers
# ---------------------------------------------------------------------------

def test_format_pod_logs_includes_previous_container() -> None:
    pods = {
        "ok": True,
        "pods": [
            {
                "pod": "agent-x-abc",
                "container": "agent",
                "phase": "Running",
                "restarts": 2,
                "log": "listening on 8000",
                "previous_log": "Traceback: boom",
            }
        ],
    }
    out = deployments._format_pod_logs(pods)
    assert "agent-x-abc" in out
    assert "previous container" in out
    assert "Traceback: boom" in out
    assert "listening on 8000" in out


def test_format_pod_logs_reports_unavailable() -> None:
    out = deployments._format_pod_logs({"ok": False, "error": "403 Forbidden"})
    assert "unavailable" in out
    assert "403 Forbidden" in out


def test_format_action_logs_falls_back_to_metadata_and_link() -> None:
    build = {
        "available": True,
        "reason": None,
        "web_url": "http://gitea/u/agent-x/actions",
        "run": {
            "status": "failure",
            "conclusion": "failure",
            "workflow": "build",
            "run_number": 7,
        },
        "log": None,
    }
    out = deployments._format_action_logs(build)
    assert "status=failure" in out
    assert "http://gitea/u/agent-x/actions" in out


def test_format_argo_logs_summarizes_status() -> None:
    out = deployments._format_argo_logs(
        {"exists": True, "health": "Degraded", "sync": "OutOfSync", "revision": "abc"}
    )
    assert "health=Degraded" in out
    assert "sync=OutOfSync" in out
    assert deployments._format_argo_logs({}) == ""


# ---------------------------------------------------------------------------
# Upsert + scrub round-trip through the DB
# ---------------------------------------------------------------------------

async def _seed(session) -> AgentDeployment:
    user = User(email="owner@example.com", password_hash="hash")
    session.add(user)
    await session.flush()
    agent = Agent(
        name="agent-x",
        owner_id=user.id,
        description="Agent X",
        status="building",
        version="0.1.0",
        image="registry.a2acloud.io/agents/agent-x:latest",
        url="https://agent-x.a2acloud.io",
        public=False,
        card={},
    )
    session.add(agent)
    await session.flush()
    deploy = AgentDeployment(
        deploy_id="dpl_test123",
        agent_id=agent.id,
        user_id=user.id,
        agent_name=agent.name,
        status="building",
        started_at=datetime.now(timezone.utc),
    )
    session.add(deploy)
    await session.commit()
    await session.refresh(deploy)
    return deploy


@pytest.mark.asyncio
async def test_upsert_deployment_log_scrubs_and_updates_in_place() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with Session() as session:
        deploy = await _seed(session)

        await deployments._upsert_deployment_log(
            session, deploy, stage="build", source="gitea_actions",
            text="cloning https://user:secretpw@gitea/repo.git\nbuilding image",
        )
        await session.commit()

        rows = (
            await session.execute(
                select(AgentDeploymentLog).where(
                    AgentDeploymentLog.deploy_id == "dpl_test123"
                )
            )
        ).scalars().all()
        assert len(rows) == 1
        assert "secretpw" not in rows[0].content
        assert "«redacted»" in rows[0].content
        assert rows[0].byte_len > 0

        # Same (stage, source) with new content updates in place, no new row.
        await deployments._upsert_deployment_log(
            session, deploy, stage="build", source="gitea_actions",
            text="build complete",
        )
        await session.commit()
        rows = (
            await session.execute(
                select(AgentDeploymentLog).where(
                    AgentDeploymentLog.deploy_id == "dpl_test123"
                )
            )
        ).scalars().all()
        assert len(rows) == 1
        assert "build complete" in rows[0].content

    await engine.dispose()


@pytest.mark.asyncio
async def test_upsert_deployment_log_skips_empty() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with Session() as session:
        deploy = await _seed(session)
        await deployments._upsert_deployment_log(
            session, deploy, stage="runtime", source="pod", text=""
        )
        await session.commit()
        rows = (
            await session.execute(select(AgentDeploymentLog))
        ).scalars().all()
        assert rows == []

    await engine.dispose()
