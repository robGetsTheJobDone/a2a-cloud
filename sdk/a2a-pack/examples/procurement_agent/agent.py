from __future__ import annotations

import base64
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

import a2a_pack as a2a
from a2a_pack import (
    A2AAgent,
    AgentDatabase,
    AgentDatabaseEnv,
    AgentDatabaseMigrations,
    AgentPlatformResources,
    FileType,
    FileUpload,
    PlatformUserAuth,
    RunContext,
    UploadedFile,
    WorkspaceAccess,
    WorkspaceMode,
)

from procurement import (
    Actor,
    ExtractedQuote,
    ProcurementStore,
    extract_text_from_document,
    money_to_cents,
    parse_quote_text,
)


def _actor(ctx: RunContext[PlatformUserAuth]) -> Actor:
    auth = ctx.auth
    org_slug = auth.org_slug or auth.org_id or "local-dev"
    email = (auth.email or auth.sub or "unknown@example.local").lower()
    return Actor(org_slug=str(org_slug), user_id=auth.user_id, email=email)


async def _read_upload(ctx: RunContext[PlatformUserAuth], document: UploadedFile) -> bytes:
    reader = getattr(ctx.workspace, "read_bytes", None)
    if callable(reader):
        return reader(document.path)
    view = await ctx.workspace.open_view(
        purpose="Read uploaded procurement quote",
        hints=[document.path, document.filename],
        file_types=[FileType.OTHER],
        max_files=1,
        mode=WorkspaceMode.READ_ONLY,
        reason="Extract supplier quote details",
    )
    return await view.read(document.path)


class QuotePayload(BaseModel):
    filename: str
    data_base64: str = Field(description="Base64-encoded PDF or text quote bytes.")
    media_type: str = "application/pdf"


class ProcurementAgent(A2AAgent[Any, PlatformUserAuth]):
    name = "procurement-agent"
    description = "Turns supplier quotes into durable procurement memory, approvals, and recommendations."
    version = "0.1.0"
    auth_model = PlatformUserAuth
    input_modes = ("application/json", "multipart/form-data")
    output_modes = ("application/json",)
    tools_used = ("sqlite", "postgres", "pypdf")
    workspace_access = WorkspaceAccess.dynamic(
        max_files=20,
        allowed_modes=(WorkspaceMode.READ_ONLY,),
        deny_patterns=("secrets/**", ".env", "**/.env"),
        max_total_size_bytes=50_000_000,
    )
    platform_resources = AgentPlatformResources(
        databases=(
            AgentDatabase(
                name="procurement",
                scope="org",
                access_mode="read_write",
                env=AgentDatabaseEnv(url="DATABASE_URL"),
                migrations=AgentDatabaseMigrations(path="db/migrations"),
            ),
        )
    )

    def store(self) -> ProcurementStore:
        store = ProcurementStore()
        store.ensure_schema()
        return store

    def _insert_extracted_quote(
        self,
        store: ProcurementStore,
        conn: Any,
        *,
        actor: Actor,
        filename: str,
        workspace_path: str,
        media_type: str | None,
        size_bytes: int,
        quote: ExtractedQuote,
    ) -> dict[str, Any]:
        return store.insert_quote(
            conn,
            actor=actor,
            filename=filename,
            workspace_path=workspace_path,
            media_type=media_type,
            size_bytes=size_bytes,
            quote=quote,
        )

    @a2a.tool(description="Assign a procurement role to a user in the current organization.")
    async def set_member_role(
        self,
        ctx: RunContext[PlatformUserAuth],
        email: str,
        role: Literal["procurement_admin", "buyer", "requester", "approver", "finance", "viewer", "owner"],
        approval_limit: float | None = None,
        user_id: int | None = None,
    ) -> dict[str, Any]:
        actor = _actor(ctx)
        limit_cents = None
        if approval_limit is not None:
            limit_cents = money_to_cents(str(approval_limit))[0]
        store = self.store()
        with store.connect() as conn:
            member = store.set_member_role(
                conn,
                actor=actor,
                email=email,
                role=role,
                approval_limit_cents=limit_cents,
                user_id=user_id,
            )
        return {
            "member": member,
            "org_slug": actor.org_slug,
        }

    @a2a.tool(description="Ingest one or more supplier quote PDFs into structured procurement memory.")
    async def ingest_quotes(
        self,
        ctx: RunContext[PlatformUserAuth],
        documents: Annotated[
            list[UploadedFile],
            FileUpload(
                accept=("application/pdf", "text/plain"),
                multiple=True,
                max_bytes=20_000_000,
                description="Supplier quote PDFs or text exports",
            ),
        ],
        requested_part: str = "",
        requested_quantity: int | None = None,
    ) -> dict[str, Any]:
        actor = _actor(ctx)
        store = self.store()
        ingested: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        with store.connect() as conn:
            store.ensure_actor_member(conn, actor)
            for document in documents:
                try:
                    raw = await _read_upload(ctx, document)
                    text = extract_text_from_document(raw, document.filename)
                    quote = parse_quote_text(
                        text,
                        filename=document.filename,
                        requested_part=requested_part,
                        requested_quantity=requested_quantity,
                    )
                    record = self._insert_extracted_quote(
                        store,
                        conn,
                        actor=actor,
                        filename=document.filename,
                        workspace_path=document.path,
                        media_type=document.media_type,
                        size_bytes=document.size_bytes,
                        quote=quote,
                    )
                    ingested.append(record)
                    await ctx.emit_progress(
                        f"structured quote from {quote.supplier_name}: {quote.part_name}"
                    )
                except Exception as exc:
                    errors.append({"filename": document.filename, "error": str(exc)})
        return {
            "org_slug": actor.org_slug,
            "ingested_count": len(ingested),
            "error_count": len(errors),
            "quotes": ingested,
            "errors": errors,
        }

    @a2a.tool(description="Ingest browser-uploaded quote payloads into structured procurement memory.")
    async def ingest_quote_payloads(
        self,
        ctx: RunContext[PlatformUserAuth],
        documents: list[QuotePayload],
        requested_part: str = "",
        requested_quantity: int | None = None,
    ) -> dict[str, Any]:
        actor = _actor(ctx)
        store = self.store()
        ingested: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        with store.connect() as conn:
            store.ensure_actor_member(conn, actor)
            for document in documents:
                try:
                    raw = base64.b64decode(document.data_base64)
                    text = extract_text_from_document(raw, document.filename)
                    quote = parse_quote_text(
                        text,
                        filename=document.filename,
                        requested_part=requested_part,
                        requested_quantity=requested_quantity,
                    )
                    record = self._insert_extracted_quote(
                        store,
                        conn,
                        actor=actor,
                        filename=document.filename,
                        workspace_path=f"browser-upload/{document.filename}",
                        media_type=document.media_type,
                        size_bytes=len(raw),
                        quote=quote,
                    )
                    ingested.append(record)
                    await ctx.emit_progress(
                        f"structured quote from {quote.supplier_name}: {quote.part_name}"
                    )
                except Exception as exc:
                    errors.append({"filename": document.filename, "error": str(exc)})
        return {
            "org_slug": actor.org_slug,
            "ingested_count": len(ingested),
            "error_count": len(errors),
            "quotes": ingested,
            "errors": errors,
        }

    @a2a.tool(description="Compare current and historical quotes for a part.")
    async def compare_quotes(
        self,
        ctx: RunContext[PlatformUserAuth],
        part_query: str,
        quantity: int | None = None,
    ) -> dict[str, Any]:
        actor = _actor(ctx)
        store = self.store()
        with store.connect() as conn:
            store.ensure_actor_member(conn, actor)
            return store.compare_quotes(
                conn,
                org_slug=actor.org_slug,
                part_query=part_query,
                quantity=Decimal(quantity) if quantity is not None else None,
            )

    @a2a.tool(description="Create a purchase request from a recommended or selected quote.")
    async def create_purchase_request(
        self,
        ctx: RunContext[PlatformUserAuth],
        quote_line_item_id: int,
        quantity: int,
        reason: str = "",
    ) -> dict[str, Any]:
        actor = _actor(ctx)
        store = self.store()
        with store.connect() as conn:
            request = store.create_purchase_request(
                conn,
                actor=actor,
                quote_line_item_id=quote_line_item_id,
                quantity=Decimal(quantity),
                reason=reason,
            )
        return {
            "request": request,
            "approval_actions": ["approve", "reject", "request_more_quotes"],
        }

    @a2a.tool(description="Approve, reject, or request more quotes for a purchase request.")
    async def decide_purchase_request(
        self,
        ctx: RunContext[PlatformUserAuth],
        request_id: int,
        decision: Literal["approve", "reject", "request_more_quotes"],
        note: str = "",
    ) -> dict[str, Any]:
        actor = _actor(ctx)
        store = self.store()
        with store.connect() as conn:
            return store.decide_purchase_request(
                conn,
                actor=actor,
                request_id=request_id,
                decision=decision,
                note=note,
            )

    @a2a.tool(description="Record purchase, delivery, late delivery, or quality outcome for a supplier.")
    async def record_supplier_outcome(
        self,
        ctx: RunContext[PlatformUserAuth],
        supplier_name: str,
        event_type: Literal["purchase", "delivery", "late_delivery", "quality_issue", "quality_ok"],
        purchase_request_id: int | None = None,
        quote_line_item_id: int | None = None,
        quantity: int | None = None,
        amount: float | None = None,
        delivery_days: int | None = None,
        quality_score: int | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        actor = _actor(ctx)
        amount_cents = money_to_cents(str(amount))[0] if amount is not None else None
        store = self.store()
        with store.connect() as conn:
            event = store.record_supplier_event(
                conn,
                actor=actor,
                supplier_name=supplier_name,
                event_type=event_type,
                purchase_request_id=purchase_request_id,
                quote_line_item_id=quote_line_item_id,
                quantity=Decimal(quantity) if quantity is not None else None,
                amount_cents=amount_cents,
                delivery_days=delivery_days,
                quality_score=quality_score,
                note=note,
            )
        return {"event": event}

    @a2a.tool(description="Return a supplier scorecard based on quotes and recorded outcomes.")
    async def supplier_scorecard(
        self,
        ctx: RunContext[PlatformUserAuth],
        supplier_name: str,
    ) -> dict[str, Any]:
        actor = _actor(ctx)
        store = self.store()
        with store.connect() as conn:
            store.ensure_actor_member(conn, actor)
            return store.supplier_scorecard(conn, org_slug=actor.org_slug, supplier_name=supplier_name)

    @a2a.tool(description="Return owner-level procurement KPIs for the current organization.")
    async def executive_dashboard(self, ctx: RunContext[PlatformUserAuth]) -> dict[str, Any]:
        actor = _actor(ctx)
        store = self.store()
        with store.connect() as conn:
            store.ensure_actor_member(conn, actor)
            return store.executive_dashboard(conn, org_slug=actor.org_slug)
