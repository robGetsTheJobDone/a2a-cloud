"""Control plane FastAPI app: auth + agent registry + deploy."""
from __future__ import annotations

import logging
from contextlib import AsyncExitStack, asynccontextmanager
from typing import AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import asyncio

from .agent_schedules import start_schedule_loop, stop_schedule_loop
from .agent_studio_autopilot import start_autopilot_loop, stop_autopilot_loop
from .agent_seo import run_agent_seo_backfill_loop
from .auth import enforce_browser_session_csrf
from .checkpointer import open_checkpointer
from .config import settings
from .db import init_models
from .database_provisioner import (
    start_database_provisioner,
    stop_database_provisioner,
)
from .gitea_meta import run_sweeper_loop as run_gitea_token_sweeper
from .log_scrub import install_access_log_scrubbing
from .mail_ingress import start_mail_ingress, stop_mail_ingress
from .mailbox_provisioner import (
    start_mailbox_provisioner,
    stop_mailbox_provisioner,
)
from .pending_actions import create_pending_actions_store
from .metrics import install_http_metrics
from .openapi import install_openapi_schema
from .rate_limit import close_limiter, rate_limit_guard
from .orchestrator_mcp import mount_orchestrator_mcp
from .routes.agents import router as agents_router
from .routes.agent_proofs import (
    owner_router as agent_proofs_router,
    public_agent_router as public_agent_proofs_router,
    public_router as public_agent_proofs_index_router,
)
from .routes.agent_receipts import router as agent_receipts_router
from .routes.agent_evidence import router as agent_evidence_router
from .routes.agent_sessions import (
    agent_router as agent_sessions_router,
    session_router as sessions_router,
)
from .routes.agent_insights import router as agent_insights_router
from .routes.agent_auth import router as agent_auth_router
from .routes.agent_secrets import router as agent_secrets_router
from .routes.adversarial_review_loops import router as adversarial_review_loops_router
from .routes.auth import me_router, router as auth_router
from .routes.bounties import router as bounties_router
from .routes.chat import router as chat_router
from .routes.compliance import router as compliance_router
from .routes.collective_runtime import router as collective_runtime_router
from .routes.control_room import router as control_room_router
from .routes.consumer_setup import (
    installed_router as installed_agents_router,
    router as consumer_setup_router,
)
from .routes.dag_runs import router as dag_runs_router
from .routes.databases import router as databases_router
from .routes.files import router as files_router
from .routes.feature_flags import (
    admin_router as feature_flags_admin_router,
    router as feature_flags_router,
)
from .routes.gitea_webhooks import router as gitea_webhooks_router
from .routes.grants import router as grants_router
from .routes.llm_creds import router as llm_creds_router
from .routes.llm_usage import router as llm_usage_router
from .routes.mailboxes import (
    health_router as mailboxes_health_router,
    router as mailboxes_router,
)
from .routes.memory import router as memory_router
from .routes.meta_runs import router as meta_runs_router
from .routes.onboarding import router as onboarding_router
from .routes.organizations import router as organizations_router
from .routes.organizations import scim_router
from .routes.admin import router as admin_router
from .routes.agent_reviews import router as agent_reviews_router
from .routes.agent_studio import router as agent_studio_router
from .routes.platform import router as platform_router
from .routes.protocol_simulations import router as protocol_simulations_router
from .routes.public import (
    bounties_router as public_bounties_router,
    receipt_keys_router as public_receipt_keys_router,
    router as public_router,
)
from .routes.service_access import router as service_access_router
from .routes.schedules import router as schedules_router
from .routes.subagent_runs import router as subagent_runs_router
from .routes.threads import router as threads_router
from .routes.trial_rooms import router as trial_rooms_router
from .routes.user_kernel_evolution import router as user_kernel_evolution_router
from .routes.user_kernel_simulations import router as user_kernel_simulations_router
from .routes.work_ledger import router as work_ledger_router
from .routes.workspace_grants import router as workspace_grants_router

log = logging.getLogger(__name__)

# Integration links can be handed to clients that only accept a URL, so tokens
# still reach us as ``?integration_token=...``. Uvicorn's access logger prints
# the raw request line; redact those values before anything is emitted. Done at
# import time so every uvicorn worker process gets it.
install_access_log_scrubbing()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await init_models()
    async with AsyncExitStack() as stack:
        try:
            app.state.checkpointer = await open_checkpointer(stack)
        except Exception:  # noqa: BLE001
            # Don't crash the control plane if Postgres-checkpointer
            # setup fails; chat just won't persist threads until fixed.
            log.exception("checkpointer setup failed; chat threads disabled")
            app.state.checkpointer = None
        app.state.pending_actions = create_pending_actions_store(settings.redis_url)
        app.state.agent_seo_backfill_loop = asyncio.create_task(
            run_agent_seo_backfill_loop(),
            name="agent-seo-backfill",
        )
        if settings.agent_scheduler_enabled:
            start_schedule_loop(
                app,
                interval_seconds=settings.agent_scheduler_interval_seconds,
            )
        if settings.agent_studio_autopilot_worker_enabled:
            start_autopilot_loop(
                app,
                interval_seconds=settings.agent_studio_autopilot_interval_seconds,
            )
        if settings.gitea_token_sweeper_enabled:
            app.state.gitea_token_sweeper = asyncio.create_task(
                run_gitea_token_sweeper(
                    interval_seconds=settings.gitea_token_sweeper_interval_seconds,
                ),
                name="gitea-token-sweeper",
            )
        start_database_provisioner(app)
        start_mailbox_provisioner(app)
        start_mail_ingress(app)
        try:
            yield
        finally:
            await stop_database_provisioner(app)
            await stop_mailbox_provisioner(app)
            await stop_mail_ingress(app)
            sweeper = getattr(app.state, "gitea_token_sweeper", None)
            if sweeper is not None:
                sweeper.cancel()
                try:
                    await sweeper
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            for tasks_attr in ("agent_review_tasks",):
                bg_tasks = getattr(app.state, tasks_attr, None)
                if not bg_tasks:
                    continue
                for task in list(bg_tasks):
                    task.cancel()
                for task in list(bg_tasks):
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass
            seo_loop = getattr(app.state, "agent_seo_backfill_loop", None)
            if seo_loop is not None:
                seo_loop.cancel()
                try:
                    await seo_loop
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            for task in list(getattr(app.state, "agent_seo_tasks", set())):
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            await stop_schedule_loop(app)
            await stop_autopilot_loop(app)
            pending_actions = getattr(app.state, "pending_actions", None)
            if pending_actions is not None and hasattr(pending_actions, "close"):
                await pending_actions.close()
            await close_limiter()


app = FastAPI(
    title="A2A Control Plane",
    version="0.1.2",
    lifespan=lifespan,
    # Installed on every route, enforced on the few the policy in
    # ``control_plane.rate_limit`` names. It runs after routing (so the policy
    # keys on the route template, not a path regex) and before the route's own
    # dependencies (so a throttled request never opens a database session).
    # Everything else short-circuits on a dict lookup.
    dependencies=[Depends(rate_limit_guard)],
)
app.add_middleware(
    CORSMiddleware,
    # Vite dev server + the deployed dashboard origin. Tighten in prod.
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://app.a2acloud.io",
        "https://app.a2acloud.io",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)


@app.middleware("http")
async def browser_session_csrf_middleware(request: Request, call_next):
    try:
        enforce_browser_session_csrf(request)
    except HTTPException as exc:
        return JSONResponse(
            status_code=exc.status_code,
            headers=exc.headers,
            content={
                "detail": exc.detail,
                "error": {
                    "code": f"http_{exc.status_code}",
                    "message": str(exc.detail),
                    "status": exc.status_code,
                },
            },
        )
    return await call_next(request)
install_http_metrics(app)
app.include_router(auth_router)
app.include_router(me_router)
app.include_router(agents_router)
app.include_router(agent_insights_router)
app.include_router(agent_auth_router)
app.include_router(installed_agents_router)
app.include_router(consumer_setup_router)
app.include_router(agent_secrets_router)
app.include_router(files_router)
app.include_router(gitea_webhooks_router)
app.include_router(chat_router)
app.include_router(compliance_router)
app.include_router(collective_runtime_router)
app.include_router(control_room_router)
app.include_router(grants_router)
app.include_router(llm_creds_router)
app.include_router(llm_usage_router)
app.include_router(memory_router)
app.include_router(meta_runs_router)
app.include_router(onboarding_router)
app.include_router(organizations_router)
app.include_router(scim_router)
app.include_router(public_router)
app.include_router(bounties_router)
app.include_router(feature_flags_router)
app.include_router(feature_flags_admin_router)
app.include_router(public_bounties_router)
app.include_router(public_receipt_keys_router)
app.include_router(agent_proofs_router)
app.include_router(public_agent_proofs_index_router)
app.include_router(public_agent_proofs_router)
app.include_router(agent_evidence_router)
app.include_router(agent_receipts_router)
app.include_router(agent_sessions_router)
app.include_router(sessions_router)
app.include_router(threads_router)
app.include_router(subagent_runs_router)
app.include_router(dag_runs_router)
app.include_router(databases_router)
app.include_router(mailboxes_router)
app.include_router(mailboxes_health_router)
app.include_router(trial_rooms_router)
app.include_router(work_ledger_router)
app.include_router(workspace_grants_router)
app.include_router(service_access_router)
app.include_router(schedules_router)
app.include_router(platform_router)
app.include_router(agent_reviews_router)
app.include_router(agent_studio_router)
app.include_router(adversarial_review_loops_router)
app.include_router(protocol_simulations_router)
app.include_router(user_kernel_evolution_router)
app.include_router(user_kernel_simulations_router)
app.include_router(admin_router)
mount_orchestrator_mcp(app)
# Must run after every router is mounted: the security requirements and the
# shared error responses are derived from the mounted routes.
install_openapi_schema(app)


def _error_message(detail: object) -> str:
    if isinstance(detail, str):
        return detail
    # Structured details (a failed agent invoke, a setup gate) carry
    # the sentence a developer can act on. Surfacing it beats "request failed",
    # which is what a client that only renders ``error.message`` used to show.
    if isinstance(detail, dict):
        message = detail.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
    return "request failed"


@app.exception_handler(HTTPException)
async def http_exception_handler(
    _request: Request,
    exc: HTTPException,
) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        headers=exc.headers,
        content={
            "detail": exc.detail,
            "error": {
                "code": f"http_{exc.status_code}",
                "message": _error_message(exc.detail),
                "status": exc.status_code,
            },
        },
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    _request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    detail = jsonable_encoder(exc.errors())
    return JSONResponse(
        status_code=422,
        content={
            "detail": detail,
            "error": {
                "code": "validation_error",
                "message": "request validation failed",
                "status": 422,
            },
        },
    )


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"ok": "true"}


def run() -> None:
    import os

    import uvicorn

    workers = max(1, int(os.environ.get("A2A_CP_UVICORN_WORKERS", "4")))
    graceful_shutdown = max(
        1,
        int(os.environ.get("A2A_CP_UVICORN_GRACEFUL_SHUTDOWN_TIMEOUT", "1800")),
    )
    install_access_log_scrubbing()
    uvicorn.run(
        "control_plane.main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
        workers=workers,
        log_level="info",
        timeout_graceful_shutdown=graceful_shutdown,
    )
