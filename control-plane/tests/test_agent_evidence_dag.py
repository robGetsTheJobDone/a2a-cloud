from __future__ import annotations

import os
from datetime import datetime, timezone

os.environ.setdefault("A2A_CP_DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from control_plane.db import Base
from control_plane.evidence_dag import _review_finding_hash, build_evidence_dag
from control_plane.models import (
    Agent,
    AgentDeployment,
    AgentDeploymentEvent,
    AgentProofRun,
    AgentReceipt,
    AgentReviewRun,
    AgentSession,
    DagRun,
    DagRunNode,
    GrantAudit,
    LLMUsageEvent,
    SubagentRun,
    SubagentRunEvent,
    TrialRoom,
    TrialRun,
    User,
    WorkEvent,
    WorkJob,
)
from control_plane.routes.agent_evidence import (
    get_agent_dossier,
    get_agent_evidence_dag,
    get_agent_evidence_timeline,
    router as agent_evidence_router,
)


@pytest.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with Session() as session:
        owner = User(email="owner@example.com", password_hash="hash")
        other = User(email="other@example.com", password_hash="hash")
        session.add_all([owner, other])
        await session.flush()
        private_agent = Agent(
            owner_id=owner.id,
            name="private-research",
            description="Private research helper",
            version="1.0.0",
            image="registry.a2acloud.io/agents/private-research:latest",
            public=False,
            status="running",
            url="https://private-research.a2acloud.io",
            card={"skills": [{"name": "search"}]},
        )
        public_agent = Agent(
            owner_id=owner.id,
            name="public-research",
            description="Public research helper",
            version="1.0.0",
            image="registry.a2acloud.io/agents/public-research:latest",
            public=True,
            status="running",
            url="https://public-research.a2acloud.io",
            card={"skills": [{"name": "search"}]},
        )
        session.add_all([private_agent, public_agent])
        await session.commit()
        yield session, owner, other, private_agent, public_agent
    await engine.dispose()


def test_agent_evidence_router_exports_expected_v0_paths() -> None:
    paths = {
        (route.path, tuple(sorted(route.methods or ())))
        for route in agent_evidence_router.routes
    }

    assert ("/v1/agents/{name}/evidence-dag", ("GET",)) in paths
    assert ("/v1/agents/{name}/dossier", ("GET",)) in paths
    assert ("/v1/agents/{name}/evidence-timeline", ("GET",)) in paths


def test_v0_does_not_add_authoritative_graph_or_mutation_tables() -> None:
    forbidden_terms = (
        "agent_graph",
        "capability_graph",
        "evidence_graph",
        "graph_node",
        "graph_edge",
        "mutation_run",
        "mutation_proposal",
        "self_improvement",
    )

    shipped_tables = {
        table_name
        for table_name in Base.metadata.tables
        if any(term in table_name for term in forbidden_terms)
    }

    assert shipped_tables == set()


@pytest.mark.asyncio
async def test_owner_gets_private_agent_root_evidence(db_session) -> None:
    session, owner, _other, private_agent, _public_agent = db_session

    out = await get_agent_evidence_dag(
        "private-research",
        include_payloads=False,
        include_warnings=True,
        user=owner,
        session=session,
    )

    assert out.agent["view"] == "owner"
    assert out.agent["id"] == private_agent.id
    assert len(out.nodes) == 1
    assert out.nodes[0].id == f"agent:{private_agent.id}"
    assert out.nodes[0].type == "agent"
    assert out.nodes[0].payload["owner_id"] == owner.id
    assert "card" not in out.nodes[0].payload
    assert out.warnings[0].code == "projection_skeleton"


@pytest.mark.asyncio
async def test_public_agent_returns_public_redacted_view_without_user(db_session) -> None:
    session, _owner, _other, _private_agent, public_agent = db_session

    out = await get_agent_evidence_dag(
        "public-research",
        include_payloads=False,
        include_warnings=False,
        user=None,
        session=session,
    )

    assert out.agent["view"] == "public"
    assert out.agent["id"] is None
    assert out.nodes[0].id == f"agent:{public_agent.id}"
    assert "owner_id" not in out.nodes[0].payload
    assert "image" not in out.nodes[0].payload
    assert out.warnings == []
    assert "owner_private_rows" in out.redaction.omitted_classes


@pytest.mark.asyncio
async def test_other_user_cannot_read_private_agent(db_session) -> None:
    session, _owner, other, _private_agent, _public_agent = db_session

    with pytest.raises(HTTPException) as exc:
        await get_agent_evidence_dag(
            "private-research",
            include_payloads=False,
            include_warnings=True,
            user=other,
            session=session,
        )

    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_anonymous_private_agent_returns_404(db_session) -> None:
    session, _owner, _other, _private_agent, _public_agent = db_session

    with pytest.raises(HTTPException) as exc:
        await get_agent_evidence_dag(
            "private-research",
            include_payloads=False,
            include_warnings=True,
            user=None,
            session=session,
        )

    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_unknown_agent_returns_404(db_session) -> None:
    session, owner, _other, _private_agent, _public_agent = db_session

    with pytest.raises(HTTPException) as exc:
        await get_agent_evidence_dag(
            "does-not-exist",
            include_payloads=False,
            include_warnings=True,
            user=owner,
            session=session,
        )

    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_projection_module_can_be_called_directly(db_session) -> None:
    session, owner, _other, private_agent, _public_agent = db_session

    out = await build_evidence_dag(
        session,
        private_agent,
        view="owner",
        include_payloads=True,
        include_warnings=False,
    )

    assert out.agent["name"] == "private-research"
    assert out.nodes[0].payload["agent_id"] == private_agent.id
    assert out.nodes[0].payload["owner_id"] == owner.id
    assert out.nodes[0].payload["card"]["skills"][0]["name"] == "search"
    assert out.redaction.include_payloads is True
    assert out.watermark.source_row_count == 1


@pytest.mark.asyncio
async def test_owner_dag_projects_strict_deploy_review_proof_chain(db_session) -> None:
    session, owner, _other, private_agent, _public_agent = db_session
    now = datetime.now(timezone.utc)
    deployment = AgentDeployment(
        deploy_id="deploy-1",
        agent_id=private_agent.id,
        user_id=owner.id,
        agent_name=private_agent.name,
        trigger="source-push",
        status="succeeded",
        source_repo_url="https://git.example/research.git",
        head_sha="abc123def456",
        image="registry.example/private-research:abc123",
        agent_url=private_agent.url,
        started_at=now,
        completed_at=now,
    )
    session.add(deployment)
    await session.flush()
    session.add(
        AgentDeploymentEvent(
            deployment_id=deployment.id,
            deploy_id=deployment.deploy_id,
            agent_name=private_agent.name,
            stage="review",
            status="succeeded",
            message="review finished",
            data={"review_id": "review-1"},
        )
    )
    session.add(
        AgentReviewRun(
            review_id="review-1",
            deploy_id=deployment.deploy_id,
            agent_id=private_agent.id,
            agent_name=private_agent.name,
            ref="abc123def456",
            user_id=owner.id,
            status="passed",
            summary="No critical findings.",
            findings=[],
            critical_count=0,
            warning_count=0,
            info_count=0,
            started_at=now,
            completed_at=now,
        )
    )
    session.add(
        AgentProofRun(
            agent_id=private_agent.id,
            agent_name=private_agent.name,
            user_id=owner.id,
            skill_name="search",
            grant_id="grant-1",
            status="passed",
            summary="Live proof passed.",
            head_sha="abc123def456",
            card_hash="cardhash1",
            image="registry.example/private-research:abc123",
            agent_url=private_agent.url,
            started_at=now,
            completed_at=now,
        )
    )
    await session.commit()

    out = await get_agent_evidence_dag(
        private_agent.name,
        include_payloads=False,
        include_warnings=True,
        user=owner,
        session=session,
    )

    node_types = {node.type for node in out.nodes}
    edge_types = {edge.type for edge in out.edges}
    assert {"agent", "version", "deployment", "deployment_event", "review", "proof"} <= node_types
    assert "agent_has_deployment" in edge_types
    assert "deployment_has_event" in edge_types
    assert "deployment_reviewed_by" in edge_types
    assert "proof_tests_version" in edge_types
    assert out.agent["current_version"]["head_sha"] == "abc123def456"
    assert out.agent["current_version"]["deploy_id"] == "deploy-1"
    assert out.watermark.source_row_count == 5
    assert out.chains[0].type == "version_trust"
    assert out.chains[0].confidence == "strict"
    assert "projection_skeleton" not in {warning.code for warning in out.warnings}


@pytest.mark.asyncio
async def test_owner_dag_warns_when_deployment_lacks_review_and_proof(db_session) -> None:
    session, owner, _other, _private_agent, public_agent = db_session
    deployment = AgentDeployment(
        deploy_id="deploy-missing-evidence",
        agent_id=public_agent.id,
        user_id=owner.id,
        agent_name=public_agent.name,
        trigger="deploy",
        status="succeeded",
        head_sha="missing123",
        image="registry.example/public-research:missing123",
        agent_url=public_agent.url,
    )
    session.add(deployment)
    await session.commit()

    out = await get_agent_evidence_dag(
        public_agent.name,
        include_payloads=False,
        include_warnings=True,
        user=owner,
        session=session,
    )

    warning_codes = {warning.code for warning in out.warnings}
    assert "missing_review_runs" in warning_codes
    assert "missing_proof_runs" in warning_codes
    assert out.agent["current_version"]["head_sha"] == "missing123"


@pytest.mark.asyncio
async def test_owner_dag_projects_grant_handoff_proof_and_cost_chain(db_session) -> None:
    session, owner, _other, private_agent, _public_agent = db_session
    now = datetime.now(timezone.utc)
    session.add(
        GrantAudit(
            grant_id="grant-chain-1",
            parent_grant_id=None,
            issuer="control-plane",
            audience=private_agent.name,
            bucket="workspace",
            mode="read_write",
            allow_patterns=["src/**"],
            deny_patterns=[".env"],
            outputs_prefix="s3://private-output/grant-chain-1",
            ttl_seconds=600,
            user_id=owner.id,
            decision="auto_approve",
            decided_by="auto",
            reason="fixture authority grant",
            created_at=now,
        )
    )
    subagent_run = SubagentRun(
        grant_id="grant-chain-1",
        user_id=owner.id,
        thread_id="thread-authority-1",
        agent_name=private_agent.name,
        skill_name="search",
        args_json='{"api_key":"super-secret-handoff"}',
        scopes={"workspace": "read_write"},
        status="completed",
        summary="handoff completed",
        file_ops=[{"path": "src/app.py", "op": "edit"}],
        start_files={"src/app.py": {"sha256": "abc"}},
        created_at=now,
        updated_at=now,
        completed_at=now,
    )
    session.add(subagent_run)
    await session.flush()
    session.add(
        SubagentRunEvent(
            run_id=subagent_run.id,
            grant_id="grant-chain-1",
            user_id=owner.id,
            event_type="completed",
            payload={"secret": "super-secret-event", "result": "ok"},
            created_at=now,
        )
    )
    session.add(
        AgentProofRun(
            agent_id=private_agent.id,
            agent_name=private_agent.name,
            user_id=owner.id,
            skill_name="search",
            grant_id="grant-chain-1",
            status="passed",
            summary="grant-backed proof passed",
            head_sha="grantsha123",
            card_hash="cardhash-grant",
            image="registry.example/private-research:grantsha",
            agent_url=private_agent.url,
            started_at=now,
            completed_at=now,
        )
    )
    session.add(
        LLMUsageEvent(
            user_id=owner.id,
            thread_id="thread-authority-1",
            grant_id="grant-chain-1",
            agent_name=private_agent.name,
            skill_name="search",
            source="subagent_run",
            provider="openai",
            model="gpt-fixture",
            prompt_tokens=100,
            completion_tokens=25,
            total_tokens=125,
            cost_usd=0.0125,
            metadata_json={"request_id": "req-secret-value"},
            created_at=now,
        )
    )
    await session.commit()

    out = await get_agent_evidence_dag(
        private_agent.name,
        include_payloads=False,
        include_warnings=True,
        user=owner,
        session=session,
    )

    node_types = {node.type for node in out.nodes}
    edge_types = {edge.type for edge in out.edges}
    assert {"grant", "subagent_run", "subagent_event", "proof", "llm_usage"} <= node_types
    assert "grant_authorizes_subagent_run" in edge_types
    assert "grant_authorizes_proof" in edge_types
    assert "grant_has_llm_usage" in edge_types
    assert "subagent_run_has_event" in edge_types
    authority_chain = next(chain for chain in out.chains if chain.type == "authority_execution")
    assert authority_chain.confidence == "strict"
    assert out.watermark.source_row_count == 6
    encoded = out.model_dump_json()
    assert "super-secret-handoff" not in encoded
    assert "super-secret-event" not in encoded
    assert "req-secret-value" not in encoded


@pytest.mark.asyncio
async def test_receipt_session_chain_omits_signed_tokens_and_warns_on_missing_session(
    db_session,
) -> None:
    session, owner, _other, private_agent, _public_agent = db_session
    now = datetime.now(timezone.utc)
    session.add_all(
        [
            AgentReceipt(
                receipt_id="receipt-with-session",
                agent_id=private_agent.id,
                agent_name=private_agent.name,
                agent_version="1.0.0",
                caller="tester",
                task_id="task-1",
                skill_name="search",
                status="ok",
                eval_score=0.98,
                started_at=now,
                ended_at=now,
                elapsed_ms=321,
                signed_token="signed-secret-token",
                payload={"private": "receipt-secret-value"},
                created_at=now,
            ),
            AgentReceipt(
                receipt_id="receipt-without-session",
                agent_id=private_agent.id,
                agent_name=private_agent.name,
                agent_version="1.0.0",
                caller="tester",
                task_id="task-2",
                skill_name="search",
                status="ok",
                started_at=now,
                ended_at=now,
                elapsed_ms=100,
                signed_token="another-signed-secret-token",
                payload={},
                created_at=now,
            ),
            AgentSession(
                session_id="session-1",
                agent_id=private_agent.id,
                agent_name=private_agent.name,
                agent_version="1.0.0",
                caller="tester",
                task_id="task-1",
                skill_name="search",
                receipt_id="receipt-with-session",
                started_at=1,
                ended_at=2,
                event_count=3,
                signed_token="signed-session-secret-token",
                events_object_key="s3://private-replay/session-1.ndjson",
                created_at=now,
            ),
        ]
    )
    await session.commit()

    out = await get_agent_evidence_dag(
        private_agent.name,
        include_payloads=False,
        include_warnings=True,
        user=owner,
        session=session,
    )

    edge_types = {edge.type for edge in out.edges}
    assert "agent_has_receipt" in edge_types
    assert "agent_has_session" in edge_types
    assert "receipt_has_session" in edge_types
    assert "receipt_session_missing" in {warning.code for warning in out.warnings}
    encoded = out.model_dump_json()
    assert "signed-secret-token" not in encoded
    assert "signed-session-secret-token" not in encoded
    assert "receipt-secret-value" not in encoded
    assert "s3://private-replay/session-1.ndjson" not in encoded


@pytest.mark.asyncio
async def test_work_and_dag_evidence_are_owner_only_for_public_agent(db_session) -> None:
    session, owner, _other, _private_agent, public_agent = db_session
    now = datetime.now(timezone.utc)
    session.add(
        DagRun(
            dag_run_id="dag-public-1",
            user_id=owner.id,
            thread_id="thread-dag-1",
            goal="patch and review",
            status="completed",
            summary="dag completed",
            nodes_json=[{"node_id": "patch"}],
            created_at=now,
            updated_at=now,
            completed_at=now,
        )
    )
    session.add(
        DagRunNode(
            dag_run_id="dag-public-1",
            node_id="patch",
            user_id=owner.id,
            agent_name=public_agent.name,
            skill_name="patch",
            deps=[],
            args_json='{"token":"dag-arg-secret"}',
            status="succeeded",
            summary="patched source",
            result={"token": "dag-result-secret"},
            file_ops=[{"path": "src/app.py", "op": "edit"}],
            elapsed_ms=200,
            started_at=now,
            completed_at=now,
        )
    )
    session.add(
        WorkJob(
            job_id="work-public-1",
            kind="source_push",
            status="succeeded",
            title="source push",
            summary="pushed source",
            user_id=owner.id,
            thread_id="thread-dag-1",
            correlation_id="corr-public-1",
            source_type="agent",
            source_id=public_agent.name,
            subject_type="agent",
            subject_id=str(public_agent.id),
            worker_type="agent",
            worker_name=public_agent.name,
            input_payload={"token": "work-input-secret"},
            output_payload={"token": "work-output-secret"},
            created_at=now,
            updated_at=now,
            queued_at=now,
            started_at=now,
            completed_at=now,
        )
    )
    session.add(
        WorkEvent(
            event_id="work-event-public-1",
            job_id="work-public-1",
            event_seq=1,
            correlation_id="corr-public-1",
            event_type="source_push",
            stage="deploy",
            status="succeeded",
            severity="info",
            message="source push completed",
            actor_type="agent",
            actor_id=public_agent.name,
            source_type="agent",
            source_id=public_agent.name,
            payload={"token": "work-event-secret"},
            created_at=now,
        )
    )
    await session.commit()

    owner_out = await get_agent_evidence_dag(
        public_agent.name,
        include_payloads=False,
        include_warnings=True,
        user=owner,
        session=session,
    )
    owner_node_types = {node.type for node in owner_out.nodes}
    owner_edge_types = {edge.type for edge in owner_out.edges}
    assert {"dag_run", "dag_node", "work_job", "work_event"} <= owner_node_types
    assert "dag_run_has_node" in owner_edge_types
    assert "agent_executed_dag_node" in owner_edge_types
    assert "work_job_has_event" in owner_edge_types
    owner_json = owner_out.model_dump_json()
    assert "dag-arg-secret" not in owner_json
    assert "dag-result-secret" not in owner_json
    assert "work-input-secret" not in owner_json
    assert "work-output-secret" not in owner_json
    assert "work-event-secret" not in owner_json

    public_out = await get_agent_evidence_dag(
        public_agent.name,
        include_payloads=False,
        include_warnings=False,
        user=None,
        session=session,
    )

    assert public_out.agent["view"] == "public"
    assert {node.type for node in public_out.nodes} == {"agent"}


@pytest.mark.asyncio
async def test_finding_hashes_and_ids_are_stable_and_default_output_redacts_secrets(
    db_session,
) -> None:
    session, owner, _other, private_agent, _public_agent = db_session
    now = datetime.now(timezone.utc)
    deployment = AgentDeployment(
        deploy_id="deploy-redaction",
        agent_id=private_agent.id,
        user_id=owner.id,
        agent_name=private_agent.name,
        trigger="source-push",
        status="succeeded",
        head_sha="redactsha123",
        image="registry.example/private-research:redactsha",
        agent_url=private_agent.url,
        started_at=now,
        completed_at=now,
    )
    session.add(deployment)
    await session.flush()
    session.add(
        AgentReviewRun(
            review_id="review-redaction",
            deploy_id=deployment.deploy_id,
            agent_id=private_agent.id,
            agent_name=private_agent.name,
            ref="redactsha123",
            user_id=owner.id,
            status="failed",
            summary="review saw sk-live-secret1234567890",
            findings=[
                {
                    "severity": "critical",
                    "rule": "no-secrets",
                    "path": "src/app.py",
                    "line": 10,
                    "message": "secret leaked",
                    "api_key": "finding-secret-value",
                    "stdout": "raw stdout should not serialize",
                }
            ],
            critical_count=1,
            warning_count=0,
            info_count=0,
            error="jwt eyJhbGciOiJIUzI1NiJ9.abcdefghijk12345.lmnopqrstuv98765",
            started_at=now,
            completed_at=now,
        )
    )
    await session.commit()

    out1 = await build_evidence_dag(
        session,
        private_agent,
        view="owner",
        include_payloads=False,
        include_warnings=True,
    )
    out2 = await build_evidence_dag(
        session,
        private_agent,
        view="owner",
        include_payloads=False,
        include_warnings=True,
    )

    assert sorted(node.id for node in out1.nodes) == sorted(node.id for node in out2.nodes)
    assert sorted(edge.id for edge in out1.edges) == sorted(edge.id for edge in out2.edges)
    review1 = next(node for node in out1.nodes if node.type == "review")
    review2 = next(node for node in out2.nodes if node.type == "review")
    assert review1.payload["finding_hashes"] == review2.payload["finding_hashes"]
    assert len(review1.payload["finding_hashes"]) == 1
    encoded = out1.model_dump_json()
    assert "sk-live-secret1234567890" not in encoded
    assert "eyJhbGciOiJIUzI1NiJ9" not in encoded
    assert "finding-secret-value" not in encoded
    assert "raw stdout should not serialize" not in encoded

    operator_out = await build_evidence_dag(
        session,
        private_agent,
        view="operator",
        include_payloads=False,
        include_warnings=True,
    )
    assert operator_out.redaction.view == "operator"
    assert "owner_private_rows" in operator_out.redaction.omitted_classes
    assert "sk-live-secret1234567890" not in operator_out.model_dump_json()


@pytest.mark.asyncio
async def test_failure_remediation_chain_joins_finding_patch_deploy_review_and_proof(
    db_session,
) -> None:
    session, owner, _other, private_agent, _public_agent = db_session
    now = datetime.now(timezone.utc)
    failed_deployment = AgentDeployment(
        deploy_id="deploy-failed-review",
        agent_id=private_agent.id,
        user_id=owner.id,
        agent_name=private_agent.name,
        trigger="source-push",
        status="succeeded",
        head_sha="failedsha123",
        image="registry.example/private-research:failedsha",
        agent_url=private_agent.url,
        started_at=now,
        completed_at=now,
    )
    finding = {
        "severity": "critical",
        "rule": "no-secret-env",
        "path": "src/app.py",
        "line": 42,
        "message": "secret env var leaked",
    }
    failed_review = AgentReviewRun(
        review_id="review-failed",
        deploy_id=failed_deployment.deploy_id,
        agent_id=private_agent.id,
        agent_name=private_agent.name,
        ref="failedsha123",
        user_id=owner.id,
        status="failed",
        summary="critical finding",
        findings=[finding],
        critical_count=1,
        warning_count=0,
        info_count=0,
        started_at=now,
        completed_at=now,
    )
    session.add_all([failed_deployment, failed_review])
    await session.flush()
    finding_hash = _review_finding_hash(failed_review, finding)
    fixed_deployment = AgentDeployment(
        deploy_id="deploy-fixed-review",
        agent_id=private_agent.id,
        user_id=owner.id,
        agent_name=private_agent.name,
        trigger="source-push",
        status="succeeded",
        head_sha="fixedsha456",
        image="registry.example/private-research:fixedsha",
        agent_url=private_agent.url,
        started_at=now,
        completed_at=now,
    )
    session.add_all(
        [
            fixed_deployment,
            AgentReviewRun(
                review_id="review-fixed",
                deploy_id=fixed_deployment.deploy_id,
                agent_id=private_agent.id,
                agent_name=private_agent.name,
                ref="fixedsha456",
                user_id=owner.id,
                status="passed",
                summary="finding fixed",
                findings=[],
                critical_count=0,
                warning_count=0,
                info_count=0,
                started_at=now,
                completed_at=now,
            ),
            AgentProofRun(
                agent_id=private_agent.id,
                agent_name=private_agent.name,
                user_id=owner.id,
                skill_name="search",
                grant_id="grant-fixed-proof",
                status="passed",
                summary="fixed proof passed",
                head_sha="fixedsha456",
                card_hash="fixed-card",
                image="registry.example/private-research:fixedsha",
                agent_url=private_agent.url,
                started_at=now,
                completed_at=now,
            ),
            WorkJob(
                job_id="code-editor-fix-1",
                kind="code_editor_patch",
                status="complete",
                title="fix reviewer finding",
                summary="patched critical finding",
                user_id=owner.id,
                thread_id="thread-fix-1",
                correlation_id="corr-fix-1",
                source_type="code_editor",
                source_id=private_agent.name,
                subject_type="agent",
                subject_id=str(private_agent.id),
                worker_type="agent",
                worker_name=private_agent.name,
                input_payload={
                    "target_finding_hash": finding_hash,
                    "target_review_id": "review-failed",
                },
                output_payload={
                    "source_sha": "fixedsha456",
                    "deploy_id": fixed_deployment.deploy_id,
                    "review_id": "review-fixed",
                    "changed_paths": ["src/app.py"],
                },
                created_at=now,
                updated_at=now,
                queued_at=now,
                started_at=now,
                completed_at=now,
            ),
            WorkEvent(
                event_id="code-editor-fix-event-1",
                job_id="code-editor-fix-1",
                event_seq=1,
                correlation_id="corr-fix-1",
                event_type="source_push_deploy_queued",
                stage="deploy",
                status="complete",
                severity="info",
                message="source push deployment queued",
                actor_type="agent",
                actor_id=private_agent.name,
                source_type="code_editor",
                source_id=private_agent.name,
                payload={
                    "target_finding_hash": finding_hash,
                    "source_sha": "fixedsha456",
                    "deploy_id": fixed_deployment.deploy_id,
                },
                created_at=now,
            ),
        ]
    )
    await session.commit()

    out = await get_agent_evidence_dag(
        private_agent.name,
        include_payloads=False,
        include_warnings=True,
        user=owner,
        session=session,
    )

    node_types = {node.type for node in out.nodes}
    edge_types = {edge.type for edge in out.edges}
    assert "review_finding" in node_types
    assert "finding_targeted_by_work_job" in edge_types
    assert "work_job_produced_version" in edge_types
    assert "work_job_triggered_deployment" in edge_types
    assert "work_event_triggered_deployment" in edge_types
    remediation_chain = next(chain for chain in out.chains if chain.type == "failure_remediation")
    assert remediation_chain.confidence == "strict"
    assert f"finding:review-failed:{finding_hash}" in remediation_chain.node_ids
    assert "version:fixedsha456" in remediation_chain.node_ids
    assert "missing_code_editor_correlation_fields" not in {
        warning.code for warning in out.warnings
    }


@pytest.mark.asyncio
async def test_missing_code_editor_payload_warns_without_false_remediation_chain(
    db_session,
) -> None:
    session, owner, _other, private_agent, _public_agent = db_session
    now = datetime.now(timezone.utc)
    session.add(
        WorkJob(
            job_id="code-editor-missing-fields",
            kind="code_editor_patch",
            status="complete",
            title="patch without refs",
            summary="missing correlation payload",
            user_id=owner.id,
            source_type="code_editor",
            source_id=private_agent.name,
            subject_type="agent",
            subject_id=str(private_agent.id),
            worker_type="agent",
            worker_name=private_agent.name,
            input_payload={"prompt": "fix it"},
            output_payload={"summary": "done"},
            created_at=now,
            updated_at=now,
            queued_at=now,
            started_at=now,
            completed_at=now,
        )
    )
    await session.commit()

    out = await get_agent_evidence_dag(
        private_agent.name,
        include_payloads=False,
        include_warnings=True,
        user=owner,
        session=session,
    )

    assert "missing_code_editor_correlation_fields" in {
        warning.code for warning in out.warnings
    }
    assert "failure_remediation" not in {chain.type for chain in out.chains}


@pytest.mark.asyncio
async def test_dossier_summarizes_version_authority_quality_mutation_and_risks(
    db_session,
) -> None:
    session, owner, _other, private_agent, _public_agent = db_session
    now = datetime.now(timezone.utc)
    deployment = AgentDeployment(
        deploy_id="deploy-dossier",
        agent_id=private_agent.id,
        user_id=owner.id,
        agent_name=private_agent.name,
        trigger="source-push",
        status="succeeded",
        head_sha="dossiersha123",
        image="registry.example/private-research:dossiersha",
        agent_url=private_agent.url,
        started_at=now,
        completed_at=now,
    )
    session.add(deployment)
    await session.flush()
    session.add_all(
        [
            GrantAudit(
                grant_id="grant-dossier",
                issuer="control-plane",
                audience=private_agent.name,
                bucket="workspace",
                mode="read",
                allow_patterns=["src/**"],
                deny_patterns=[],
                ttl_seconds=300,
                user_id=owner.id,
                decision="auto_approve",
                decided_by="auto",
                created_at=now,
            ),
            AgentReviewRun(
                review_id="review-dossier",
                deploy_id=deployment.deploy_id,
                agent_id=private_agent.id,
                agent_name=private_agent.name,
                ref="dossiersha123",
                user_id=owner.id,
                status="passed",
                summary="clean",
                findings=[],
                critical_count=0,
                warning_count=0,
                info_count=0,
                started_at=now,
                completed_at=now,
            ),
            AgentProofRun(
                agent_id=private_agent.id,
                agent_name=private_agent.name,
                user_id=owner.id,
                skill_name="search",
                grant_id="grant-dossier",
                status="passed",
                summary="proof passed",
                head_sha="dossiersha123",
                card_hash="dossier-card",
                image="registry.example/private-research:dossiersha",
                agent_url=private_agent.url,
                started_at=now,
                completed_at=now,
            ),
            WorkJob(
                job_id="work-dossier",
                kind="source_push",
                status="complete",
                title="source push",
                summary="queued deploy",
                user_id=owner.id,
                source_type="agent",
                source_id=private_agent.name,
                subject_type="agent",
                subject_id=str(private_agent.id),
                worker_type="agent",
                worker_name=private_agent.name,
                output_payload={
                    "source_sha": "dossiersha123",
                    "deploy_id": deployment.deploy_id,
                },
                created_at=now,
                updated_at=now,
                queued_at=now,
                started_at=now,
                completed_at=now,
            ),
        ]
    )
    await session.commit()

    dossier = await get_agent_dossier(
        private_agent.name,
        include_payloads=False,
        include_warnings=True,
        user=owner,
        session=session,
    )

    assert dossier.current_version["head_sha"] == "dossiersha123"
    assert dossier.authority_summary["grant_count"] == 1
    assert dossier.authority_summary["strict_chain_count"] == 1
    assert dossier.quality_summary["latest_review"]["status"] == "passed"
    assert dossier.quality_summary["proof_count"] == 1
    assert dossier.mutation_summary["work_job_count"] == 1
    assert dossier.mutation_summary["produced_version_edges"] == 1
    assert dossier.trust_profile["strict_version_trust"] is True
    assert dossier.risk_summary["warning_count"] == 0


@pytest.mark.asyncio
async def test_review_loop_dossier_and_timeline_projection(db_session) -> None:
    session, owner, _other, private_agent, _public_agent = db_session
    now = datetime.now(timezone.utc)
    session.add(
        AgentReviewRun(
            review_id="review-loop-dossier",
            agent_id=private_agent.id,
            agent_name=private_agent.name,
            ref="main",
            user_id=owner.id,
            status="failed",
            summary="critical finding",
            findings=[
                {
                    "severity": "critical",
                    "title": "route policy can be edited directly",
                    "finding_hash": "finding-loop-critical",
                }
            ],
            critical_count=1,
            warning_count=0,
            info_count=0,
            started_at=now,
            completed_at=now,
        )
    )
    session.add(
        WorkJob(
            job_id="arl-dossier",
            kind="adversarial_review_loop",
            status="blocked",
            title="Adversarial review loop: private-research",
            summary="critical finding",
            user_id=owner.id,
            source_type="agent",
            source_id="agent-reviewer",
            subject_type="agent",
            subject_id=private_agent.name,
            worker_type="agent",
            worker_name="agent-reviewer",
            input_payload={"target_agent": private_agent.name, "ref": "main"},
            output_payload={
                "latest_review_id": "review-loop-dossier",
                "critical_finding_count": 1,
                "active_apply_enabled": False,
            },
            metadata_json={
                "template_ref": "adversarial_review_loop@v1",
                "active_apply_enabled": False,
            },
            created_at=now,
            updated_at=now,
            queued_at=now,
            started_at=now,
        )
    )
    session.add_all(
        [
            WorkEvent(
                event_id="arl-dossier-created",
                job_id="arl-dossier",
                event_seq=1,
                event_type="loop_created",
                stage="created",
                status="queued",
                severity="info",
                message="created",
                source_type="agent",
                source_id="agent-reviewer",
                payload={"active_apply_enabled": False},
                created_at=now,
            ),
            WorkEvent(
                event_id="arl-dossier-finding",
                job_id="arl-dossier",
                event_seq=2,
                event_type="finding_emitted",
                stage="findings",
                status="failed",
                severity="critical",
                message="critical finding",
                source_type="agent",
                source_id="agent-reviewer",
                payload={
                    "review_id": "review-loop-dossier",
                    "finding_hash": "finding-loop-critical",
                    "active_apply_enabled": False,
                },
                created_at=now,
            ),
            WorkEvent(
                event_id="arl-dossier-freeze",
                job_id="arl-dossier",
                event_seq=3,
                event_type="promotion_frozen",
                stage="promotion_gate",
                status="blocked",
                severity="critical",
                message="promotion frozen",
                source_type="agent",
                source_id="agent-reviewer",
                payload={
                    "review_id": "review-loop-dossier",
                    "finding_hash": "finding-loop-critical",
                    "promotion_frozen": True,
                    "active_apply_enabled": False,
                },
                created_at=now,
            ),
            WorkEvent(
                event_id="arl-dossier-fix",
                job_id="arl-dossier",
                event_seq=4,
                event_type="fix_proposed",
                stage="proposal",
                status="queued",
                severity="info",
                message="fix proposed",
                source_type="agent",
                source_id="agent-reviewer",
                payload={
                    "self_improvement_proposal_ref": "sip:loop-fix",
                    "fixes_enter_self_improvement": True,
                    "active_apply_enabled": False,
                },
                created_at=now,
            ),
        ]
    )
    await session.commit()

    dossier = await get_agent_dossier(
        private_agent.name,
        include_payloads=False,
        include_warnings=True,
        user=owner,
        session=session,
    )

    assert dossier.quality_summary["review_loop_count"] == 1
    assert dossier.quality_summary["active_review_loop_count"] == 1
    assert dossier.mutation_summary["review_loop_event_count"] == 4
    assert dossier.mutation_summary["review_loop_proposed_fix_count"] == 1
    assert dossier.risk_summary["critical_findings"] == 1
    assert dossier.risk_summary["promotion_freeze_count"] == 1

    quality = await get_agent_evidence_timeline(
        private_agent.name,
        limit=100,
        lane="quality",
        include_payloads=False,
        include_warnings=True,
        user=owner,
        session=session,
    )
    assert any(item.payload.get("event_type") == "finding_emitted" for item in quality.items)

    frozen = await get_agent_evidence_timeline(
        private_agent.name,
        limit=100,
        event_type="promotion_frozen",
        include_payloads=False,
        include_warnings=True,
        user=owner,
        session=session,
    )
    assert [item.lane for item in frozen.items] == ["control"]


@pytest.mark.asyncio
async def test_trial_and_protocol_simulation_projection(db_session) -> None:
    session, owner, _other, private_agent, _public_agent = db_session
    now = datetime.now(timezone.utc)
    room = TrialRoom(
        slug="red-team-room",
        user_id=owner.id,
        title="Red team comparison",
        goal="Compare agents on adversarial prompts.",
        acceptance_criteria="Must preserve authority boundaries.",
        input_paths=["inputs/prompt.md"],
        output_schema={"required": ["summary"]},
        status="open",
        created_at=now,
        updated_at=now,
    )
    session.add(room)
    await session.flush()
    session.add_all(
        [
            TrialRun(
                trial_room_id=room.id,
                user_id=owner.id,
                agent_id=private_agent.id,
                agent_name=private_agent.name,
                skill_name="search",
                grant_id="trial-grant",
                status="passed",
                score=91,
                summary="passed adversarial trial",
                evaluator_notes="good boundary handling",
                receipt_json={"receipt_id": "trial-receipt"},
                created_at=now,
                started_at=now,
                completed_at=now,
            ),
            GrantAudit(
                grant_id="trial-grant",
                issuer="trial-room:red-team-room",
                audience=private_agent.name,
                bucket="user-1-files",
                mode="read_write_overlay",
                allow_patterns=["inputs/*"],
                ttl_seconds=600,
                user_id=owner.id,
                decision="allowed",
                decided_by="policy",
            ),
            WorkJob(
                job_id="psim-dossier",
                kind="protocol_simulation",
                status="running",
                title="Protocol simulation: Red team / private-research",
                summary="running protocol simulation",
                user_id=owner.id,
                source_type="protocol_pack",
                source_id="red_team",
                subject_type="agent",
                subject_id=private_agent.name,
                worker_type="simulator",
                worker_name="red_team@v1",
                input_payload={"target_agent": private_agent.name},
                metadata_json={
                    "protocol_ref": {
                        "id": "red_team",
                        "version": 1,
                        "display_name": "Red team",
                        "template_refs": ["red_team@v1"],
                    },
                    "template_ref": "red_team@v1",
                    "simulation_only": True,
                    "proposal_only": True,
                    "active_apply_enabled": False,
                    "kill_switch_available": True,
                },
                created_at=now,
                updated_at=now,
                queued_at=now,
                started_at=now,
            ),
            WorkEvent(
                event_id="psim-dossier-invariant",
                job_id="psim-dossier",
                event_seq=1,
                event_type="invariant_checked",
                stage="invariants",
                status="running",
                severity="info",
                message="authority invariant held",
                source_type="protocol_pack",
                source_id="red_team",
                payload={
                    "invariant": "signals cannot grant authority",
                    "active_apply_enabled": False,
                },
                created_at=now,
            ),
            WorkEvent(
                event_id="psim-dossier-scenario-trace",
                job_id="psim-dossier",
                event_seq=2,
                event_type="scenario_trace_recorded",
                stage="scenario_trace",
                status="running",
                severity="info",
                message="graph-kernel scenario trace recorded",
                source_type="protocol_pack",
                source_id="red_team",
                payload={
                    "passed": True,
                    "trace_summary": {
                        "scenario_count": 2,
                        "scenario_ids": ["s8_child_scope", "s12_rank_no_authority"],
                        "event_count": 14,
                        "invariant_pass_count": 4,
                        "invariant_fail_count": 0,
                        "replay_pass_count": 2,
                        "alert_count": 1,
                        "alerts": ["protocol_simulation_direct_apply_forbidden"],
                        "violation_count": 1,
                        "violations": ["review_loop_direct_apply_forbidden"],
                        "passed": True,
                        "active_apply_enabled": False,
                    },
                    "active_apply_enabled": False,
                },
                created_at=now,
            ),
            WorkEvent(
                event_id="psim-dossier-arena-suite",
                job_id="psim-dossier",
                event_seq=3,
                event_type="arena_suite_recorded",
                stage="arena_suite",
                status="complete",
                severity="info",
                message="arena suite scoreboard recorded",
                source_type="protocol_pack",
                source_id="custom_kernel_suite",
                payload={
                    "suite_id": "arena-suite-drill",
                    "title": "Arena suite scoreboard drill",
                    "passed": True,
                    "episode_count": 2,
                    "scoreboard": {
                        "participants": [
                            {
                                "participant_id": "alpha",
                                "wins": 2,
                                "losses": 0,
                                "exclusions": 0,
                                "evidence_refs": ["evt-1", "evt-2"],
                            },
                            {
                                "participant_id": "beta",
                                "wins": 0,
                                "losses": 1,
                                "exclusions": 1,
                                "exclusion_reasons": {"participant_frozen": 1},
                                "evidence_refs": ["evt-3"],
                            },
                        ],
                        "winner_events": [{"event_id": "evt-w1"}, {"event_id": "evt-w2"}],
                        "rejected_winner_events": [],
                        "episode_count": 2,
                        "passed_episode_count": 2,
                        "failed_episode_count": 0,
                        "invariant_failures": [],
                        "active_apply_enabled": False,
                    },
                    "active_apply_enabled": False,
                },
                created_at=now,
            ),
        ]
    )
    await session.commit()

    dag = await build_evidence_dag(
        session,
        private_agent,
        view="owner",
        include_payloads=False,
        include_warnings=True,
    )
    assert any(node.type == "trial" and node.payload["score"] == 91 for node in dag.nodes)
    assert any(edge.type == "grant_authorizes_trial" for edge in dag.edges)
    assert any(
        node.type == "work_job"
        and node.payload["kind"] == "protocol_simulation"
        and node.payload["protocol_ref"]["id"] == "red_team"
        for node in dag.nodes
    )

    dossier = await get_agent_dossier(
        private_agent.name,
        include_payloads=False,
        include_warnings=True,
        user=owner,
        session=session,
    )
    assert dossier.quality_summary["trial_count"] == 1
    assert dossier.quality_summary["latest_trial_score"] == 91
    assert dossier.quality_summary["protocol_simulation_count"] == 1
    assert dossier.quality_summary["active_protocol_simulation_count"] == 1
    assert dossier.quality_summary["protocol_scenario_trace_count"] == 1
    assert dossier.quality_summary["protocol_scenario_count"] == 2
    assert dossier.quality_summary["protocol_invariant_pass_count"] == 4
    assert dossier.quality_summary["protocol_invariant_fail_count"] == 0
    assert dossier.quality_summary["protocol_replay_pass_count"] == 2
    assert dossier.quality_summary["protocol_arena_suite_count"] == 1
    assert dossier.quality_summary["protocol_arena_suite_episode_count"] == 2
    assert dossier.quality_summary["protocol_arena_suite_winner_count"] == 2
    assert dossier.quality_summary["protocol_arena_suite_exclusion_count"] == 1
    assert dossier.mutation_summary["protocol_scenario_event_count"] == 14
    assert dossier.mutation_summary["protocol_arena_suite_event_count"] == 1
    assert dossier.risk_summary["protocol_scenario_alert_count"] == 1
    assert dossier.risk_summary["protocol_scenario_violation_count"] == 1
    assert dossier.risk_summary["protocol_arena_suite_invariant_failure_count"] == 0
    assert dossier.risk_summary["protocol_arena_suite_failed_episode_count"] == 0

    timeline = await get_agent_evidence_timeline(
        private_agent.name,
        lane="quality",
        include_payloads=False,
        include_warnings=True,
        user=owner,
        session=session,
    )
    assert any(item.type == "trial" for item in timeline.items)
    assert any(item.type == "work_event" and item.label == "invariant_checked" for item in timeline.items)
    assert any(item.type == "work_event" and item.label == "scenario_trace_recorded" for item in timeline.items)
    assert any(item.type == "work_event" and item.label == "arena_suite_recorded" for item in timeline.items)
    assert any(
        item.payload.get("scenario_trace_summary", {}).get("scenario_count") == 2
        for item in timeline.items
        if item.label == "scenario_trace_recorded"
    )
    assert any(
        item.payload.get("arena_suite_summary", {}).get("winner_count") == 2
        for item in timeline.items
        if item.label == "arena_suite_recorded"
    )

    process_timeline = await get_agent_evidence_timeline(
        private_agent.name,
        lane="process",
        include_payloads=False,
        include_warnings=True,
        user=owner,
        session=session,
    )
    assert any(item.type == "trial_room" for item in process_timeline.items)

    invariant_timeline = await get_agent_evidence_timeline(
        private_agent.name,
        event_type="invariant_checked",
        include_payloads=False,
        include_warnings=True,
        user=owner,
        session=session,
    )
    assert [item.label for item in invariant_timeline.items] == ["invariant_checked"]
    assert [item.lane for item in invariant_timeline.items] == ["quality"]


@pytest.mark.asyncio
async def test_evidence_timeline_filters_lanes_head_sha_and_event_type(db_session) -> None:
    session, owner, _other, private_agent, _public_agent = db_session
    now = datetime.now(timezone.utc)
    deployment = AgentDeployment(
        deploy_id="deploy-timeline",
        agent_id=private_agent.id,
        user_id=owner.id,
        agent_name=private_agent.name,
        trigger="source-push",
        status="succeeded",
        head_sha="timelinesha123",
        image="registry.example/private-research:timelinesha",
        agent_url=private_agent.url,
        started_at=now,
        completed_at=now,
    )
    session.add(deployment)
    await session.flush()
    session.add_all(
        [
            GrantAudit(
                grant_id="grant-timeline",
                issuer="control-plane",
                audience=private_agent.name,
                bucket="workspace",
                mode="read",
                allow_patterns=[],
                deny_patterns=[],
                ttl_seconds=300,
                user_id=owner.id,
                decision="auto_approve",
                decided_by="auto",
                created_at=now,
            ),
            AgentReviewRun(
                review_id="review-timeline",
                deploy_id=deployment.deploy_id,
                agent_id=private_agent.id,
                agent_name=private_agent.name,
                ref="timelinesha123",
                user_id=owner.id,
                status="passed",
                summary="clean",
                findings=[],
                critical_count=0,
                warning_count=0,
                info_count=0,
                started_at=now,
                completed_at=now,
            ),
            AgentProofRun(
                agent_id=private_agent.id,
                agent_name=private_agent.name,
                user_id=owner.id,
                skill_name="search",
                grant_id="grant-timeline",
                status="passed",
                summary="proof passed",
                head_sha="timelinesha123",
                card_hash="timeline-card",
                image="registry.example/private-research:timelinesha",
                agent_url=private_agent.url,
                started_at=now,
                completed_at=now,
            ),
            LLMUsageEvent(
                user_id=owner.id,
                grant_id="grant-timeline",
                agent_name=private_agent.name,
                skill_name="search",
                source="proof",
                provider="openai",
                model="gpt-fixture",
                prompt_tokens=10,
                completion_tokens=5,
                total_tokens=15,
                cost_usd=0.001,
                created_at=now,
            ),
            WorkJob(
                job_id="work-timeline",
                kind="source_push",
                status="complete",
                title="source push",
                user_id=owner.id,
                source_type="agent",
                source_id=private_agent.name,
                subject_type="agent",
                subject_id=str(private_agent.id),
                worker_type="agent",
                worker_name=private_agent.name,
                output_payload={
                    "source_sha": "timelinesha123",
                    "deploy_id": deployment.deploy_id,
                },
                created_at=now,
                updated_at=now,
                queued_at=now,
                started_at=now,
                completed_at=now,
            ),
            WorkEvent(
                event_id="work-event-timeline",
                job_id="work-timeline",
                event_seq=1,
                event_type="source_push_deploy_queued",
                stage="deploy",
                status="complete",
                severity="info",
                message="queued",
                source_type="agent",
                source_id=private_agent.name,
                payload={
                    "source_sha": "timelinesha123",
                    "deploy_id": deployment.deploy_id,
                },
                created_at=now,
            ),
        ]
    )
    await session.commit()

    timeline = await get_agent_evidence_timeline(
        private_agent.name,
        limit=100,
        include_payloads=False,
        include_warnings=True,
        user=owner,
        session=session,
    )

    lanes = {item.lane for item in timeline.items}
    assert {"version", "authority", "quality", "mutation", "cost"} <= lanes

    quality = await get_agent_evidence_timeline(
        private_agent.name,
        limit=100,
        lane="quality",
        include_payloads=False,
        include_warnings=True,
        user=owner,
        session=session,
    )
    assert quality.items
    assert {item.lane for item in quality.items} == {"quality"}

    head_filtered = await get_agent_evidence_timeline(
        private_agent.name,
        limit=100,
        head_sha="timelinesha123",
        include_payloads=False,
        include_warnings=True,
        user=owner,
        session=session,
    )
    assert head_filtered.items
    assert all("timelinesha123" in item.model_dump_json() for item in head_filtered.items)

    event_filtered = await get_agent_evidence_timeline(
        private_agent.name,
        limit=100,
        event_type="source_push_deploy_queued",
        include_payloads=False,
        include_warnings=True,
        user=owner,
        session=session,
    )
    assert [item.type for item in event_filtered.items] == ["work_event"]
