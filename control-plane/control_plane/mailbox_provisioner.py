from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import logging
import secrets as _secrets
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .agent_secrets import delete_agent_secret_value, upsert_agent_secret_value
from .config import settings
from .db import SessionLocal
from .mailu_client import MailuClient
from .models import AgentMailbox, MailboxProvisionEvent
from .secret_crypto import decrypt_secret, encrypt_secret

log = logging.getLogger(__name__)

PENDING = "pending"
PROVISIONING = "provisioning"
READY = "ready"
DISABLED = "disabled"
REMOVED = "removed"
# Transient claim state during teardown; a crash mid-delete leaves the row
# here (visible in events) rather than letting two workers race the delete.
DELETING = "deleting"
FAILED = "failed"

MAIL_SECRET_KEYS = (
    "A2A_MAIL_ADDRESS",
    "A2A_MAIL_PASSWORD",
    "A2A_MAIL_IMAP_HOST",
    "A2A_MAIL_IMAP_PORT",
    "A2A_MAIL_SMTP_HOST",
    "A2A_MAIL_SMTP_PORT",
)


@dataclass
class MailboxProvisionerMetrics:
    reconcile_runs: int = 0
    queue_depth: int = 0
    create_attempts: int = 0
    remove_attempts: int = 0
    ready_mailboxes: int = 0
    disabled_mailboxes: int = 0
    failures: int = 0
    last_run_at: datetime | None = None
    last_error: str | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "reconcile_runs": self.reconcile_runs,
            "queue_depth": self.queue_depth,
            "create_attempts": self.create_attempts,
            "remove_attempts": self.remove_attempts,
            "ready_mailboxes": self.ready_mailboxes,
            "disabled_mailboxes": self.disabled_mailboxes,
            "failures": self.failures,
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "last_error": self.last_error,
        }


metrics = MailboxProvisionerMetrics()


def _mail_secret_values(address: str, password: str) -> dict[str, str]:
    return {
        "A2A_MAIL_ADDRESS": address,
        "A2A_MAIL_PASSWORD": password,
        "A2A_MAIL_IMAP_HOST": settings.agent_mail_imap_host,
        "A2A_MAIL_IMAP_PORT": str(settings.agent_mail_imap_port),
        "A2A_MAIL_SMTP_HOST": settings.agent_mail_smtp_host,
        "A2A_MAIL_SMTP_PORT": str(settings.agent_mail_smtp_port),
    }


@dataclass
class MailboxProvisioner:
    session_maker: async_sessionmaker[AsyncSession]
    client: MailuClient
    upsert_secret: Callable[..., None] = upsert_agent_secret_value
    delete_secret: Callable[..., None] = delete_agent_secret_value
    worker_name: str = "mailbox-provisioner"
    metrics: MailboxProvisionerMetrics = field(default_factory=lambda: metrics)
    _domain_ensured: bool = False

    async def reconcile_once(self) -> MailboxProvisionerMetrics:
        self.metrics.reconcile_runs += 1
        self.metrics.last_run_at = datetime.now(UTC)
        async with self.session_maker() as session:
            rows = (
                await session.execute(
                    select(AgentMailbox)
                    .where(
                        AgentMailbox.status.in_(
                            [PENDING, FAILED, REMOVED, READY, DISABLED]
                        )
                    )
                    .order_by(AgentMailbox.updated_at, AgentMailbox.id)
                )
            ).scalars().all()
            queue = [
                row
                for row in rows
                if row.status in {PENDING, FAILED, REMOVED}
                or row.status == DISABLED
                or self._retention_expired(row)
            ]
            self.metrics.queue_depth = len(queue)
            for mailbox in queue:
                if mailbox.status == REMOVED or self._retention_expired(mailbox):
                    await self._remove(session, mailbox)
                    continue
                if mailbox.status in {PENDING, FAILED}:
                    await self._provision(session, mailbox)
                elif mailbox.status == DISABLED:
                    # Nothing gates mailboxes any more; re-enable anything a
                    # previous policy had switched off.
                    await self._set_enabled(session, mailbox, enabled=True)
        return self.metrics

    def _retention_expired(self, mailbox: AgentMailbox) -> bool:
        if mailbox.status != DISABLED or mailbox.disabled_at is None:
            return False
        cutoff = datetime.now(UTC) - timedelta(
            days=settings.mailbox_disabled_retention_days
        )
        disabled_at = mailbox.disabled_at
        if disabled_at.tzinfo is None:
            disabled_at = disabled_at.replace(tzinfo=UTC)
        return disabled_at < cutoff

    async def _ensure_domain(self) -> None:
        if self._domain_ensured:
            return
        await self.client.ensure_domain(settings.agent_mail_domain)
        self._domain_ensured = True

    async def _provision(self, session: AsyncSession, mailbox: AgentMailbox) -> None:
        # Several worker processes run this loop concurrently; the conditional
        # UPDATE elects exactly one to provision (a second winner would mint a
        # different password and desync Mailu from the runtime secret).
        claimed = await session.execute(
            update(AgentMailbox)
            .where(
                AgentMailbox.id == mailbox.id,
                AgentMailbox.status.in_([PENDING, FAILED]),
            )
            .values(status=PROVISIONING)
        )
        if claimed.rowcount == 0:
            await session.rollback()
            return
        self.metrics.create_attempts += 1
        mailbox.status = PROVISIONING
        session.add(mailbox)
        await self._event(
            session,
            mailbox,
            event_type="agent_mailbox_provisioning_started",
            status="started",
            message=f"provisioning mailbox {mailbox.address}",
        )
        await session.commit()
        try:
            await self._ensure_domain()
            if mailbox.password_ciphertext:
                password = decrypt_secret(mailbox.password_ciphertext)
            else:
                password = _secrets.token_urlsafe(32)
            await self.client.upsert_user(
                email=mailbox.address,
                password=password,
                quota_bytes=mailbox.quota_bytes,
                enabled=True,
                comment=f"a2a agent mailbox ({mailbox.agent_name})",
            )
            for key, value in _mail_secret_values(mailbox.address, password).items():
                self.upsert_secret(
                    agent_name=mailbox.agent_name,
                    key=key,
                    value=value,
                    owner_id=mailbox.user_id,
                )
            mailbox.password_ciphertext = encrypt_secret(password)
            mailbox.status = READY
            mailbox.disabled_at = None
            mailbox.metadata_json = {
                **(mailbox.metadata_json or {}),
                "provisioned_at": datetime.now(UTC).isoformat(),
            }
            session.add(mailbox)
            await self._event(
                session,
                mailbox,
                event_type="agent_mailbox_ready",
                status="ok",
                message=f"mailbox {mailbox.address} is ready",
            )
            await session.commit()
            self.metrics.ready_mailboxes += 1
        except Exception as exc:  # noqa: BLE001
            await session.rollback()
            await self._fail(session, mailbox, str(exc))

    async def _set_enabled(
        self,
        session: AsyncSession,
        mailbox: AgentMailbox,
        *,
        enabled: bool,
    ) -> None:
        try:
            await self.client.set_user_enabled(mailbox.address, enabled)
            if enabled:
                mailbox.status = READY
                mailbox.disabled_at = None
            else:
                mailbox.status = DISABLED
                mailbox.disabled_at = datetime.now(UTC)
                self.metrics.disabled_mailboxes += 1
            session.add(mailbox)
            await self._event(
                session,
                mailbox,
                event_type=(
                    "agent_mailbox_enabled" if enabled else "agent_mailbox_disabled"
                ),
                status="ok",
                message=(
                    f"mailbox {mailbox.address} re-enabled (plan restored)"
                    if enabled
                    else f"mailbox {mailbox.address} disabled (plan lapsed); "
                    f"mail kept {settings.mailbox_disabled_retention_days}d"
                ),
            )
            await session.commit()
        except Exception as exc:  # noqa: BLE001
            await session.rollback()
            await self._fail(session, mailbox, str(exc), keep_status=True)

    async def _remove(self, session: AsyncSession, mailbox: AgentMailbox) -> None:
        # Same single-winner election as _provision: a losing worker's
        # session.delete on an already-deleted row would raise and then
        # resurrect the mailbox via the failure path.
        claimed = await session.execute(
            update(AgentMailbox)
            .where(
                AgentMailbox.id == mailbox.id,
                AgentMailbox.status.in_([REMOVED, DISABLED]),
            )
            .values(status=DELETING)
        )
        if claimed.rowcount == 0:
            await session.rollback()
            return
        self.metrics.remove_attempts += 1
        try:
            await self.client.delete_user(mailbox.address)
            for key in MAIL_SECRET_KEYS:
                self.delete_secret(agent_name=mailbox.agent_name, key=key)
            await self._event(
                session,
                mailbox,
                event_type="agent_mailbox_deleted",
                status="ok",
                message=f"deleted mailbox {mailbox.address}",
            )
            await session.delete(mailbox)
            await session.commit()
        except Exception as exc:  # noqa: BLE001
            await session.rollback()
            # Release the DELETING claim so a transient Mailu/secret failure
            # is retried on the next reconcile pass.
            mailbox.status = REMOVED
            await self._fail(session, mailbox, str(exc), keep_status=True)

    async def _fail(
        self,
        session: AsyncSession,
        mailbox: AgentMailbox,
        message: str,
        *,
        keep_status: bool = False,
    ) -> None:
        self.metrics.failures += 1
        self.metrics.last_error = message
        if not keep_status:
            mailbox.status = FAILED
        mailbox.metadata_json = {
            **(mailbox.metadata_json or {}),
            "last_error": message[:500],
        }
        session.add(mailbox)
        await self._event(
            session,
            mailbox,
            event_type="agent_mailbox_failed",
            status="failed",
            message=message[:500],
        )
        await session.commit()

    async def _event(
        self,
        session: AsyncSession,
        mailbox: AgentMailbox,
        *,
        event_type: str,
        status: str,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        session.add(
            MailboxProvisionEvent(
                mailbox_id=mailbox.id,
                agent_id=mailbox.agent_id,
                event_type=event_type,
                status=status,
                message=message,
                data={"address": mailbox.address, **(data or {})},
            )
        )


def build_mailbox_provisioner(
    *,
    session_maker: async_sessionmaker[AsyncSession] = SessionLocal,
) -> MailboxProvisioner:
    return MailboxProvisioner(session_maker=session_maker, client=MailuClient())


async def run_mailbox_provisioner_loop(
    *,
    interval_seconds: float | None = None,
    provisioner: MailboxProvisioner | None = None,
) -> None:
    interval = (
        settings.mailbox_provisioner_interval_seconds
        if interval_seconds is None
        else interval_seconds
    )
    provisioner = provisioner or build_mailbox_provisioner()
    while True:
        try:
            await provisioner.reconcile_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            metrics.failures += 1
            metrics.last_error = str(exc)
            log.exception("mailbox provisioner reconcile failed")
        await asyncio.sleep(interval)


def start_mailbox_provisioner(app: Any) -> None:
    if not settings.mailbox_provisioning_enabled:
        return
    if not settings.mailu_api_token:
        metrics.last_error = (
            "A2A_CP_MAILU_API_TOKEN is required when mailbox provisioning is enabled"
        )
        log.error(metrics.last_error)
        return
    task = asyncio.create_task(
        run_mailbox_provisioner_loop(),
        name="mailbox-provisioner",
    )
    app.state.mailbox_provisioner = task


async def stop_mailbox_provisioner(app: Any) -> None:
    task = getattr(app.state, "mailbox_provisioner", None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass


def mailbox_metrics_snapshot() -> dict[str, Any]:
    return metrics.snapshot()


def mailbox_provisioner_health() -> dict[str, Any]:
    enabled = settings.mailbox_provisioning_enabled
    configured = bool(settings.mailu_api_token)
    return {
        "enabled": enabled,
        "configured": configured,
        "ok": (not enabled) or configured,
        "metrics": mailbox_metrics_snapshot(),
    }
