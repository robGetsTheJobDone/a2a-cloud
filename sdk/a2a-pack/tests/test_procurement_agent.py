from __future__ import annotations

import asyncio
import base64
import importlib.util
from pathlib import Path
import sys

import pytest

from a2a_pack import LocalRunContext, LocalWorkspaceClient, PlatformUserAuth, UploadedFile, WorkspaceAccess, WorkspaceMode
from a2a_pack.cli.local import load_local_project
from a2a_pack.openapi import agent_openapi_spec


PROJECT = Path(__file__).resolve().parents[1] / "examples" / "procurement_agent"


@pytest.fixture()
def procurement_agent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("PROCUREMENT_DATABASE_URL", f"sqlite:///{tmp_path / 'procurement.sqlite3'}")
    local = load_local_project(PROJECT)
    return local.agent_cls()


def _ctx(user_id: int, email: str, org_slug: str, files: dict[str, bytes] | None = None):
    workspace = LocalWorkspaceClient(
        files or {},
        access=WorkspaceAccess.dynamic(
            max_files=10,
            allowed_modes=(WorkspaceMode.READ_ONLY,),
            max_total_size_bytes=10_000_000,
        ),
    )
    auth = PlatformUserAuth(
        sub=str(user_id),
        user_id=user_id,
        email=email,
        org_slug=org_slug,
        scopes=["agent:invoke"],
    )
    return LocalRunContext(auth=auth, workspace=workspace)


def _upload(path: str, data: bytes) -> UploadedFile:
    return UploadedFile(
        path=path,
        filename=Path(path).name,
        media_type="application/pdf",
        size_bytes=len(data),
    )


def test_procurement_agent_ingests_compares_and_approves(procurement_agent):
    asyncio.run(_run_procurement_flow(procurement_agent))


async def _run_procurement_flow(procurement_agent):
    files = {
        "inputs/acme.pdf": b"""
Supplier: Acme Bearings
Part: 6205 bearing
SKU: BRG-6205
Quantity: 500
Unit: unit
Unit Price: BRL 10.75
Delivery: 12 days
Payment Terms: Net 30
Valid Until: 2026-07-01
""",
        "inputs/beta.pdf": b"""
Supplier: Beta Industrial
Part: 6205 bearing
SKU: BRG-6205
Quantity: 500
Unit: unit
Unit Price: BRL 9.10
Delivery: 8 days
Payment Terms: Net 15
Valid Until: 2026-07-01
""",
    }
    buyer_ctx = _ctx(1, "buyer@example.com", "acme", files)
    ingested = await procurement_agent.invoke(
        "ingest_quotes",
        buyer_ctx,
        documents=[_upload("inputs/acme.pdf", files["inputs/acme.pdf"]), _upload("inputs/beta.pdf", files["inputs/beta.pdf"])],
    )

    assert ingested["ingested_count"] == 2
    assert ingested["error_count"] == 0

    comparison = await procurement_agent.invoke(
        "compare_quotes",
        buyer_ctx,
        part_query="6205",
        quantity=500,
    )
    assert comparison["recommendation"]["supplier"] == "Beta Industrial"
    quote_line_item_id = comparison["recommendation"]["quote_line_item_id"]

    member = await procurement_agent.invoke(
        "set_member_role",
        buyer_ctx,
        email="approver@example.com",
        user_id=2,
        role="approver",
        approval_limit=25_000,
    )
    assert member["member"]["role"] == "approver"

    request = await procurement_agent.invoke(
        "create_purchase_request",
        buyer_ctx,
        quote_line_item_id=quote_line_item_id,
        quantity=500,
        reason="Stock replenishment",
    )
    assert request["request"]["amount"] == "BRL 4550.00"
    assert request["request"]["status"] == "pending_approval"

    approver_ctx = _ctx(2, "approver@example.com", "acme")
    decision = await procurement_agent.invoke(
        "decide_purchase_request",
        approver_ctx,
        request_id=request["request"]["id"],
        decision="approve",
        note="Best price and acceptable delivery.",
    )
    assert decision["request"]["status"] == "approved"

    await procurement_agent.invoke(
        "record_supplier_outcome",
        buyer_ctx,
        supplier_name="Beta Industrial",
        event_type="purchase",
        purchase_request_id=request["request"]["id"],
        quote_line_item_id=quote_line_item_id,
        quantity=500,
        amount=4550.00,
        delivery_days=8,
        quality_score=91,
    )

    scorecard = await procurement_agent.invoke(
        "supplier_scorecard",
        buyer_ctx,
        supplier_name="Beta Industrial",
    )
    assert scorecard["found"] is True
    assert scorecard["quotes_submitted"] == 1
    assert scorecard["overall_score"] > 80

    dashboard = await procurement_agent.invoke("executive_dashboard", buyer_ctx)
    assert dashboard["purchases_this_month"] == "BRL 4550.00"
    assert dashboard["suppliers_reviewed"] == 2


def test_procurement_agent_ingests_browser_payload(procurement_agent):
    asyncio.run(_run_payload_ingestion(procurement_agent))


async def _run_payload_ingestion(procurement_agent):
    ctx = _ctx(10, "ops@example.com", "payload-co")
    quote = b"""
Supplier: Delta Components
Part: M8 fastener
SKU: FAST-M8
Quantity: 200
Unit: unit
Unit Price: BRL 1.25
Delivery: 4 days
"""
    result = await procurement_agent.invoke(
        "ingest_quote_payloads",
        ctx,
        documents=[
            {
                "filename": "delta.txt",
                "media_type": "text/plain",
                "data_base64": base64.b64encode(quote).decode("ascii"),
            }
        ],
    )

    assert result["ingested_count"] == 1
    assert result["quotes"][0]["line_item"]["supplier_name"] == "Delta Components"


def test_procurement_store_retries_transient_postgres_startup(
    monkeypatch: pytest.MonkeyPatch,
):
    spec = importlib.util.spec_from_file_location("procurement_retry_test", PROJECT / "procurement.py")
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    monkeypatch.setitem(sys.modules, "procurement_retry_test", module)
    spec.loader.exec_module(module)

    class FakePsycopg:
        def __init__(self) -> None:
            self.calls = 0

        def connect(self, database_url: str):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("FATAL: the database system is starting up")
            return {"database_url": database_url}

    fake_psycopg = FakePsycopg()
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)
    monkeypatch.setenv("PROCUREMENT_DATABASE_CONNECT_ATTEMPTS", "2")
    monkeypatch.setenv("PROCUREMENT_DATABASE_CONNECT_RETRY_SECONDS", "0")

    store = module.ProcurementStore("postgresql://agent:secret@db/procurement")

    assert store.connect() == {"database_url": "postgresql://agent:secret@db/procurement"}
    assert fake_psycopg.calls == 2


def test_procurement_project_declares_org_database_and_file_upload_schema():
    local = load_local_project(PROJECT)
    agent = local.agent_cls()

    payload = agent.card().runtime.platform_resources.public_payload()
    assert payload["databases"][0]["name"] == "procurement"
    assert payload["databases"][0]["scope"] == "org"
    assert payload["databases"][0]["migrations"]["path"] == "db/migrations"

    ingest = next(skill for skill in agent.card().skills if skill.name == "ingest_quotes")
    documents = ingest.input_schema["properties"]["documents"]
    assert documents["x-a2a-file-upload"]["multiple"] is True
    assert documents["x-a2a-file-upload"]["accept"] == ["application/pdf", "text/plain"]

    payload_ingest = next(skill for skill in agent.card().skills if skill.name == "ingest_quote_payloads")
    payload_schema = payload_ingest.input_schema["properties"]["documents"]
    assert payload_schema["type"] == "array"


def test_procurement_openapi_spec_inlines_payload_schema_defs():
    local = load_local_project(PROJECT)
    spec = agent_openapi_spec(local.agent_cls())
    payload = str(spec["paths"]["/invoke/ingest_quote_payloads"])

    assert "#/$defs/" not in payload
    assert "$defs" not in payload
    assert "QuotePayload" in payload
