"""Gitea service-user lifecycle for meta-agents.

Meta-agents (reviewer, patcher, migrator, composer) need to read or modify
another agent's source. They never receive admin credentials. Instead the
platform keeps two dedicated service users in Gitea — one read-only, one
writer — and mints short-lived tokens *as those users* via admin's
``Sudo`` header.

Per request the service user is also added as a collaborator on the target
repository with the requested permission. The effective authority of a
returned token is::

    (service user's repo perms)  ∩  (token scopes)

Both halves are bounded: the read-only service user can never write, and a
write token only works on repos the writer user has been explicitly granted
write access to.

State:

* All issuance lives in the :class:`GiteaTokenAudit` SQL table — survives
  control-plane restarts and lets operators audit "who minted what".
* The sweeper task queries the table directly, so unreleased tokens get
  revoked even if the issuing route handler died.
* Collaborator entries are removed once the *last* active token for a
  ``(service user, owner, repo)`` triple is revoked — multiple meta-agents
  hitting the same repo concurrently are safe.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import secrets
from datetime import datetime, timezone
from typing import Literal

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .db import SessionLocal
from .gitea import GITEA_INTERNAL, GITEA_PASS, GITEA_USER
from .models import GiteaTokenAudit

logger = logging.getLogger(__name__)

META_READER_USER = os.environ.get("A2A_CP_GITEA_META_READER", "meta-agent-reader")
META_WRITER_USER = os.environ.get("A2A_CP_GITEA_META_WRITER", "meta-agent-writer")
META_USER_EMAIL_DOMAIN = os.environ.get(
    "A2A_CP_GITEA_META_EMAIL_DOMAIN", "meta.a2a.local"
)

_ADMIN_AUTH = (GITEA_USER, GITEA_PASS)
_users_ensured: set[str] = set()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def scope_user(scope: Literal["read", "write"]) -> str:
    return META_READER_USER if scope == "read" else META_WRITER_USER


def scope_token_scopes(scope: Literal["read", "write"]) -> list[str]:
    if scope == "read":
        return ["read:repository"]
    return ["read:repository", "write:repository"]


def collaborator_permission(scope: Literal["read", "write"]) -> str:
    return "read" if scope == "read" else "write"


def _fingerprint(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()[:32]


async def _ensure_user(client: httpx.AsyncClient, username: str) -> None:
    """Idempotently create a Gitea service user via the admin API."""
    if username in _users_ensured:
        return
    check = await client.get(
        f"{GITEA_INTERNAL.rstrip('/')}/api/v1/users/{username}",
        auth=_ADMIN_AUTH,
    )
    if check.status_code == 200:
        _users_ensured.add(username)
        return
    if check.status_code != 404:
        raise RuntimeError(
            f"gitea user lookup failed: {check.status_code}: {check.text[:200]}"
        )
    password = secrets.token_urlsafe(32)
    body = {
        "username": username,
        "email": f"{username}@{META_USER_EMAIL_DOMAIN}",
        "password": password,
        "must_change_password": False,
        "send_notify": False,
        "source_id": 0,
        "login_name": username,
    }
    create = await client.post(
        f"{GITEA_INTERNAL.rstrip('/')}/api/v1/admin/users",
        json=body,
        auth=_ADMIN_AUTH,
    )
    if create.status_code >= 400 and create.status_code != 422:
        # 422 = concurrent create race; safe to treat as already-exists.
        raise RuntimeError(
            f"gitea user create failed: {create.status_code}: {create.text[:200]}"
        )
    _users_ensured.add(username)
    logger.info("gitea meta service user ensured: %s", username)


async def _ensure_collaborator(
    client: httpx.AsyncClient,
    owner: str,
    repo: str,
    collaborator: str,
    permission: str,
) -> None:
    url = (
        f"{GITEA_INTERNAL.rstrip('/')}/api/v1/repos/{owner}/{repo}/collaborators/"
        f"{collaborator}"
    )
    resp = await client.put(url, json={"permission": permission}, auth=_ADMIN_AUTH)
    if resp.status_code in (200, 201, 204):
        return
    if _is_duplicate_collaborator_error(resp) and await _collaborator_exists(
        client, owner, repo, collaborator,
    ):
        logger.warning(
            "gitea collaborator add returned duplicate-row error; treating existing "
            "collaborator as success for %s/%s/%s",
            owner,
            repo,
            collaborator,
        )
        return
    raise RuntimeError(
        f"collaborator add failed for {owner}/{repo}/{collaborator}: "
        f"{resp.status_code}: {resp.text[:200]}"
    )


async def _collaborator_exists(
    client: httpx.AsyncClient,
    owner: str,
    repo: str,
    collaborator: str,
) -> bool:
    url = (
        f"{GITEA_INTERNAL.rstrip('/')}/api/v1/repos/{owner}/{repo}/collaborators/"
        f"{collaborator}"
    )
    resp = await client.get(url, auth=_ADMIN_AUTH)
    if resp.status_code in (200, 204):
        return True
    if resp.status_code == 404:
        return False
    logger.warning(
        "collaborator existence check returned status=%s body=%s for %s/%s/%s",
        resp.status_code,
        resp.text[:200],
        owner,
        repo,
        collaborator,
    )
    return False


def _is_duplicate_collaborator_error(resp: httpx.Response) -> bool:
    if resp.status_code not in (409, 422, 500):
        return False
    text = resp.text.lower()
    return "uqe_collaboration" in text or (
        "duplicate key" in text and "collaboration" in text
    )


async def _remove_collaborator(
    client: httpx.AsyncClient,
    owner: str,
    repo: str,
    collaborator: str,
) -> None:
    url = (
        f"{GITEA_INTERNAL.rstrip('/')}/api/v1/repos/{owner}/{repo}/collaborators/"
        f"{collaborator}"
    )
    resp = await client.delete(url, auth=_ADMIN_AUTH)
    if resp.status_code in (200, 204, 404):
        return
    logger.warning(
        "collaborator remove returned status=%s body=%s for %s/%s/%s",
        resp.status_code,
        resp.text[:200],
        owner,
        repo,
        collaborator,
    )


async def _mint_token_as(
    client: httpx.AsyncClient,
    username: str,
    token_name: str,
    scopes: list[str],
) -> str:
    url = f"{GITEA_INTERNAL.rstrip('/')}/api/v1/users/{username}/tokens"
    resp = await client.post(
        url,
        json={"name": token_name, "scopes": scopes},
        auth=_ADMIN_AUTH,
        headers={"Sudo": username},
    )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"gitea token mint failed: {resp.status_code}: {resp.text[:200]}"
        )
    data = resp.json()
    secret = data.get("sha1") or data.get("token")
    if not secret:
        raise RuntimeError(f"gitea token response missing secret; keys={list(data.keys())}")
    return secret


async def _delete_token_as(
    client: httpx.AsyncClient,
    username: str,
    token_name: str,
) -> bool:
    url = f"{GITEA_INTERNAL.rstrip('/')}/api/v1/users/{username}/tokens/{token_name}"
    resp = await client.delete(url, auth=_ADMIN_AUTH, headers={"Sudo": username})
    if resp.status_code in (200, 204, 404):
        return resp.status_code != 404
    logger.warning(
        "gitea token delete returned status=%s body=%s",
        resp.status_code,
        resp.text[:200],
    )
    return False


async def _count_active_for_repo(
    session: AsyncSession,
    *,
    username: str,
    owner: str,
    repo: str,
    exclude_token_name: str | None = None,
) -> int:
    """Count *other* active tokens that still need the collaborator entry."""
    stmt = select(func.count(GiteaTokenAudit.id)).where(
        GiteaTokenAudit.username == username,
        GiteaTokenAudit.owner == owner,
        GiteaTokenAudit.repo == repo,
        GiteaTokenAudit.revoked_at.is_(None),
        GiteaTokenAudit.expires_at > _utcnow(),
    )
    if exclude_token_name is not None:
        stmt = stmt.where(GiteaTokenAudit.token_name != exclude_token_name)
    return int((await session.execute(stmt)).scalar_one() or 0)


async def mint_scoped_token(
    session: AsyncSession,
    *,
    scope: Literal["read", "write"],
    owner: str,
    repo: str,
    ttl_seconds: int,
    issued_by_user_id: int,
    purpose: str | None,
) -> tuple[GiteaTokenAudit, str]:
    """Mint a Gitea token scoped to ``{owner}/{repo}``.

    Returns ``(audit_row, token_secret)``. The secret is only available
    here — it is never stored in the row (only a fingerprint is) so a
    leaked DB dump cannot replay tokens.
    """
    username = scope_user(scope)
    scopes = scope_token_scopes(scope)
    permission = collaborator_permission(scope)
    nonce = secrets.token_hex(6)
    token_name = f"a2a-meta-{issued_by_user_id}-{nonce}"

    async with httpx.AsyncClient(timeout=10.0) as client:
        await _ensure_user(client, username)
        await _ensure_collaborator(client, owner, repo, username, permission)
        secret = await _mint_token_as(client, username, token_name, scopes)

    expires_at = _utcnow() + _timedelta_seconds(ttl_seconds)
    row = GiteaTokenAudit(
        token_name=token_name,
        username=username,
        scopes=list(scopes),
        owner=owner,
        repo=repo,
        permission=permission,
        issued_by_user_id=issued_by_user_id,
        purpose=purpose,
        token_secret_hash=_fingerprint(secret),
        expires_at=expires_at,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    logger.info(
        "gitea token issued user_id=%s token_name=%s username=%s owner=%s repo=%s "
        "scope=%s expires_at=%s purpose=%s",
        issued_by_user_id,
        token_name,
        username,
        owner,
        repo,
        scope,
        expires_at.isoformat(),
        purpose or "",
    )
    return row, secret


async def revoke_token(
    session: AsyncSession,
    token_name: str,
    *,
    allow_collaborator_cleanup: bool = True,
) -> bool:
    """Revoke a token by name. Idempotent — returns ``False`` if unknown."""
    row = (
        await session.execute(
            select(GiteaTokenAudit).where(GiteaTokenAudit.token_name == token_name)
        )
    ).scalar_one_or_none()
    if row is None:
        return False
    if row.revoked_at is not None:
        return True
    async with httpx.AsyncClient(timeout=10.0) as client:
        await _delete_token_as(client, row.username, row.token_name)
        await session.execute(
            update(GiteaTokenAudit)
            .where(GiteaTokenAudit.id == row.id)
            .values(revoked_at=_utcnow())
        )
        await session.commit()
        if allow_collaborator_cleanup:
            remaining = await _count_active_for_repo(
                session,
                username=row.username,
                owner=row.owner,
                repo=row.repo,
                exclude_token_name=row.token_name,
            )
            if remaining == 0:
                await _remove_collaborator(client, row.owner, row.repo, row.username)
                logger.info(
                    "gitea collaborator removed: %s from %s/%s (no active tokens)",
                    row.username,
                    row.owner,
                    row.repo,
                )
    return True


async def get_token_record(
    session: AsyncSession,
    token_name: str,
) -> GiteaTokenAudit | None:
    return (
        await session.execute(
            select(GiteaTokenAudit).where(GiteaTokenAudit.token_name == token_name)
        )
    ).scalar_one_or_none()


async def sweep_expired_tokens() -> int:
    """Revoke any tokens whose TTL has elapsed. Returns count."""
    async with SessionLocal() as session:
        expired_rows = (
            await session.execute(
                select(GiteaTokenAudit.token_name).where(
                    GiteaTokenAudit.revoked_at.is_(None),
                    GiteaTokenAudit.expires_at <= _utcnow(),
                )
            )
        ).scalars().all()
        revoked = 0
        for name in expired_rows:
            try:
                if await revoke_token(session, name):
                    revoked += 1
            except Exception:  # noqa: BLE001
                logger.exception("sweeper failed to revoke %s", name)
        if revoked:
            logger.info("gitea token sweeper revoked %d expired tokens", revoked)
        return revoked


async def run_sweeper_loop(interval_seconds: float = 30.0) -> None:
    """Long-lived task: periodically revoke expired tokens."""
    while True:
        try:
            await sweep_expired_tokens()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("gitea token sweeper iteration failed")
        await asyncio.sleep(interval_seconds)


def _timedelta_seconds(seconds: int):  # type: ignore[no-untyped-def]
    from datetime import timedelta

    return timedelta(seconds=seconds)
