"""Grant-scoped workspace file access for deployed agents.

Agents should not receive broad MinIO credentials. They receive a signed
workspace grant, and this router performs storage operations server-side after
verifying the grant and enforcing its path/mode policy.
"""
from __future__ import annotations

import asyncio
from fnmatch import fnmatch
import re
import time
from typing import Any
from urllib.parse import quote

from a2a_pack.gitea_backend import GiteaBackend, GiteaAuthError, GiteaError
from a2a_pack.grants import (
    Grant,
    GrantDelegationDenied,
    WorkspaceMode,
    delegate_grant,
)
from fastapi import APIRouter, Body, Depends, Header, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..agent_authorization import require_agent_access
from ..gitea import GITEA_INTERNAL, GITEA_USER, ensure_repo, repo_exists
from ..gitea_meta import mint_scoped_token
from ..gitea_provisioning import resolve_agent_gitea_workspace
from ..grants import GrantInvalid, normalize_write_prefixes, verify_grant_token
from ..minio_client import delete_file, get_file, list_files, stat_file, upload_file
from ..models import Agent, User

router = APIRouter(prefix="/v1/workspace-grants", tags=["workspace-grants"])
_USER_BUCKET_RE = re.compile(r"^user-(\d+)-files$")
_GRANT_FILE_LIST_DEFAULT_LIMIT = 500
_GRANT_FILE_LIST_MAX_LIMIT = 1000


class WorkspaceGrantDelegateIn(BaseModel):
    audience: str = Field(min_length=1, max_length=255)
    allow_patterns: list[str] = Field(default_factory=lambda: ["**"])
    deny_patterns: list[str] = Field(default_factory=list)
    mode: str = "read_only"
    outputs_prefix: str | None = None
    write_prefixes: list[str] = Field(default_factory=list)
    source_grants: list[dict[str, str]] = Field(default_factory=list)
    llm_models: list[str] = Field(default_factory=list)
    llm_max_budget_usd: float | None = Field(default=None, ge=0)
    llm_rpm_limit: int | None = Field(default=None, ge=1)
    llm_tpm_limit: int | None = Field(default=None, ge=1)
    ttl_seconds: int = Field(default=300, ge=1, le=7200)


def _grant_from_header(x_a2a_grant: str | None) -> dict[str, Any]:
    if not x_a2a_grant:
        raise HTTPException(401, "missing X-A2A-Grant")
    try:
        grant = verify_grant_token(x_a2a_grant)
    except GrantInvalid as exc:
        raise HTTPException(403, f"invalid grant: {exc}") from exc
    bucket = str(grant.get("bucket") or "")
    if not bucket:
        raise HTTPException(403, "invalid grant: missing bucket")
    return grant


@router.post("/delegate")
async def delegate_workspace_grant(
    body: WorkspaceGrantDelegateIn,
    x_a2a_grant: str | None = Header(default=None, alias="X-A2A-Grant"),
) -> dict[str, Any]:
    """Mint a mechanically bounded child without exposing the platform key.

    Deployed agents receive only the verifying key. A valid parent capability
    may request a child here; ``delegate_grant`` enforces path, source, LLM,
    mode, depth, and lifetime containment before the control plane signs it.
    """
    parent_payload = _grant_from_header(x_a2a_grant)
    try:
        parent = Grant.model_validate(parent_payload)
        mode = WorkspaceMode(body.mode)
        child, token = delegate_grant(
            parent,
            issuer=parent.audience,
            audience=body.audience,
            mode=mode,
            allow_patterns=tuple(body.allow_patterns),
            deny_patterns=tuple(body.deny_patterns),
            outputs_prefix=body.outputs_prefix,
            write_prefixes=tuple(body.write_prefixes),
            source_grants=tuple(body.source_grants),
            llm_models=tuple(body.llm_models),
            llm_max_budget_usd=body.llm_max_budget_usd,
            llm_rpm_limit=body.llm_rpm_limit,
            llm_tpm_limit=body.llm_tpm_limit,
            ttl_seconds=body.ttl_seconds,
        )
    except (ValueError, GrantDelegationDenied) as exc:
        raise HTTPException(403, f"child grant denied: {exc}") from exc
    return {
        "grant": token,
        "grant_id": child.grant_id,
        "expires_at": child.expires_at,
    }


def _clean_file_list_limit(limit: int | None) -> int:
    try:
        value = int(limit or _GRANT_FILE_LIST_DEFAULT_LIMIT)
    except (TypeError, ValueError):
        value = _GRANT_FILE_LIST_DEFAULT_LIMIT
    return max(1, min(value, _GRANT_FILE_LIST_MAX_LIMIT))


def _sanitize_key(raw: str) -> str:
    value = raw.strip().replace("\\", "/").lstrip("/")
    parts = [part for part in value.split("/") if part and part != "."]
    if not parts or any(part == ".." for part in parts):
        raise HTTPException(400, "invalid workspace path")
    return "/".join(parts)


def _workspace_route(path: str) -> tuple[str, str] | None:
    if not path.startswith("agents/"):
        return None
    rel = path[len("agents/"):]
    repo, _, inner = rel.partition("/")
    if not repo:
        return None
    # Builder bookkeeping is workspace-private state, not agent source. Keep
    # it in the user's object bucket so recording a sandbox result or deploy
    # base cannot create a Gitea commit and immediately stale that same base.
    if inner == ".a2a-builder-state.json" or inner.startswith(".agent-builder/"):
        return None
    return repo, inner


def _grant_user_id(grant: dict[str, Any]) -> int | None:
    value = grant.get("user_id")
    try:
        user_id = int(value)
    except (TypeError, ValueError):
        user_id = 0
    if user_id > 0:
        return user_id
    bucket = str(grant.get("bucket") or "")
    match = _USER_BUCKET_RE.match(bucket)
    if not match:
        return None
    return int(match.group(1))


def _agent_repos_from_grant(grant: dict[str, Any]) -> tuple[str, ...]:
    repos: list[str] = []
    values: list[str] = []
    values.extend(str(p) for p in grant.get("allow_patterns") or ())
    values.extend(str(p) for p in grant.get("write_prefixes") or ())
    outputs_prefix = grant.get("outputs_prefix")
    if isinstance(outputs_prefix, str):
        values.append(outputs_prefix)
    for item in grant.get("source_grants") or ():
        if not isinstance(item, dict):
            continue
        repo = str(item.get("agent") or item.get("repo") or item.get("name") or "").strip()
        if repo and not any(char in repo for char in "*?[]") and repo not in repos:
            repos.append(repo)
    for value in values:
        clean = value.replace("\\", "/").strip("/")
        if not clean.startswith("agents/"):
            continue
        rest = clean[len("agents/"):]
        repo = rest.split("/", 1)[0].strip()
        if not repo or any(char in repo for char in "*?[]"):
            continue
        if repo not in repos:
            repos.append(repo)
    return tuple(repos)


def _pattern_literal_prefix(pattern: str, *, directory_for_wildcard: bool = False) -> str:
    clean = str(pattern or "").replace("\\", "/").strip("/")
    if not clean:
        return ""
    wildcard_indexes = [
        idx
        for idx in (clean.find("*"), clean.find("?"), clean.find("["))
        if idx >= 0
    ]
    if not wildcard_indexes:
        return clean
    prefix = clean[:min(wildcard_indexes)]
    if directory_for_wildcard and prefix and not prefix.endswith("/"):
        if "/" not in prefix:
            return ""
        prefix = prefix.rsplit("/", 1)[0] + "/"
    return prefix.strip("/")


def _collapse_prefixes(prefixes: list[str]) -> tuple[str, ...]:
    normalized = sorted(
        {prefix.strip("/") for prefix in prefixes},
        key=lambda item: (len(item), item),
    )
    collapsed: list[str] = []
    for prefix in normalized:
        if any(
            existing == ""
            or prefix == existing
            or prefix.startswith(existing.rstrip("/") + "/")
            for existing in collapsed
        ):
            continue
        collapsed.append(prefix)
    return tuple(collapsed)


def _grant_minio_prefixes(grant: dict[str, Any]) -> tuple[str, ...]:
    allow_patterns = tuple(str(p) for p in grant.get("allow_patterns") or ("**",))
    return _collapse_prefixes([_pattern_literal_prefix(pattern) for pattern in allow_patterns])


def _repo_wildcard_inner_prefix(pattern: str, repo: str) -> str | None:
    clean = str(pattern or "").replace("\\", "/").strip("/")
    if not clean.startswith("agents/"):
        return None
    rest = clean[len("agents/"):]
    repo_pattern, separator, inner_pattern = rest.partition("/")
    if not repo_pattern or not fnmatch(repo, repo_pattern):
        return None
    if not separator:
        return ""
    return _pattern_literal_prefix(inner_pattern, directory_for_wildcard=True)


def _grant_repo_inner_prefixes(grant: dict[str, Any], repo: str) -> tuple[str, ...]:
    allow_patterns = tuple(str(p) for p in grant.get("allow_patterns") or ("**",))
    prefixes: list[str] = []
    repo_root = f"agents/{repo}"
    repo_prefix = f"{repo_root}/"
    for pattern in allow_patterns:
        prefix = _pattern_literal_prefix(pattern, directory_for_wildcard=True)
        if prefix == "":
            prefixes.append("")
            continue
        if prefix == repo_root:
            prefixes.append("")
        elif prefix.startswith(repo_prefix):
            prefixes.append(prefix[len(repo_prefix):])
        elif any(char in pattern for char in "*?["):
            wildcard_prefix = _repo_wildcard_inner_prefix(pattern, repo)
            if wildcard_prefix is not None:
                prefixes.append(wildcard_prefix)
    return _collapse_prefixes(prefixes)


def _source_grant_scope(grant: dict[str, Any], repo: str) -> str | None:
    """Return the grant's explicit source scope for ``repo``."""
    best_scope: str | None = None
    for item in grant.get("source_grants") or ():
        if not isinstance(item, dict):
            continue
        agent = str(item.get("agent") or item.get("repo") or item.get("name") or "").strip()
        if agent != repo:
            continue
        scope = str(item.get("scope") or "read").strip().lower()
        if scope == "write":
            return "write"
        if scope == "read":
            best_scope = "read"
    if best_scope is not None:
        return best_scope
    return None


def _allowed(grant: dict[str, Any], path: str) -> bool:
    allow_patterns = tuple(str(p) for p in grant.get("allow_patterns") or ("**",))
    deny_patterns = tuple(str(p) for p in grant.get("deny_patterns") or ())
    return any(fnmatch(path, pat) for pat in allow_patterns) and not any(
        fnmatch(path, pat) for pat in deny_patterns
    )


def _writable(grant: dict[str, Any], path: str) -> bool:
    if str(grant.get("mode") or "read_only") == "read_only":
        return False
    write_prefixes = normalize_write_prefixes(
        grant.get("outputs_prefix") if isinstance(grant.get("outputs_prefix"), str) else None,
        tuple(str(p) for p in grant.get("write_prefixes") or ()),
    )
    if write_prefixes:
        clean = path.strip("/")
        return any(
            clean == prefix.strip("/") or clean.startswith(prefix.strip("/") + "/")
            for prefix in write_prefixes
            if prefix.strip("/")
        )
    return _allowed(grant, path)


async def _assert_grant_user_can_access_agent_source(
    session: AsyncSession,
    *,
    user_id: int,
    agent: Agent,
    scope: str,
) -> None:
    if int(agent.owner_id) == int(user_id):
        return
    # The signed grant already binds a positive platform user id.  Construct
    # the minimal actor needed by the central policy so storage adapters do
    # not need a second user query (and remain easy to test in isolation).
    user = User(id=user_id, email="", password_hash="")
    try:
        await require_agent_access(
            session,
            user=user,
            agent=agent,
            action="edit_existing" if scope == "write" else "fork",
        )
    except HTTPException as exc:
        reason = exc.detail.get("reason") if isinstance(exc.detail, dict) else exc.detail
        raise HTTPException(
            403,
            f"grant user is not permitted to access this agent source: {reason}",
        ) from exc


async def _gitea_backend_for_grant(
    session: AsyncSession,
    grant: dict[str, Any],
    *,
    repo: str,
    scope: str,
) -> GiteaBackend:
    agent = (
        await session.execute(select(Agent).where(Agent.name == repo))
    ).scalar_one_or_none()
    issued_by_user_id = _grant_user_id(grant)
    source_scope = _source_grant_scope(grant, repo)
    if source_scope is None:
        raise HTTPException(403, "grant does not include agent source access")
    if scope == "write" and source_scope != "write":
        raise HTTPException(403, "grant does not include write access to agent source")
    if agent is None:
        if issued_by_user_id is None:
            raise HTTPException(403, "grant does not identify a workspace owner")
        user = await session.get(User, issued_by_user_id)
        if user is None:
            raise HTTPException(403, "grant workspace owner not found")
        try:
            _org, workspace = await resolve_agent_gitea_workspace(
                session,
                user,
                source="workspace_grants",
            )
        except ValueError as exc:
            raise HTTPException(403, str(exc)) from exc
        owner = str(workspace.gitea_org_name or GITEA_USER)
        if scope == "write":
            ensure_repo(repo, f"agent {repo}", owner=owner)
        elif not repo_exists(repo, owner=owner):
            raise HTTPException(404, f"agent source repo not found: {repo}")
    else:
        if issued_by_user_id is None:
            raise HTTPException(403, "grant does not identify a workspace owner")
        await _assert_grant_user_can_access_agent_source(
            session,
            user_id=issued_by_user_id,
            agent=agent,
            scope=scope,
        )
        owner = str(agent.gitea_owner or GITEA_USER)
    expires_at = int(grant.get("expires_at") or 0)
    ttl_seconds = max(60, min(900, expires_at - int(time.time()))) if expires_at else 300
    try:
        _row, secret = await mint_scoped_token(
            session,
            scope="write" if scope == "write" else "read",
            owner=owner,
            repo=repo,
            ttl_seconds=ttl_seconds,
            issued_by_user_id=issued_by_user_id,
            purpose=f"workspace-grants:{grant.get('audience') or ''}",
        )
    except RuntimeError as exc:
        raise HTTPException(502, f"gitea token mint failed: {exc}") from exc
    return GiteaBackend(
        gitea_url=GITEA_INTERNAL,
        owner=owner,
        repo=repo,
        token=secret,
        commit_prefix="a2a-source-edit",
    )


async def _write_gitea_bytes_with_retry(
    backend: GiteaBackend,
    path: str,
    body: bytes,
    *,
    action: str = "write",
    attempts: int = 8,
) -> None:
    last_error: GiteaError | None = None
    for attempt in range(attempts):
        try:
            await backend._write_bytes(path, body, action=action)  # type: ignore[attr-defined]
            return
        except GiteaAuthError:
            raise
        except GiteaError as exc:
            last_error = exc
            if attempt >= attempts - 1:
                break
            await asyncio.sleep(min(2.0, 0.25 * (2 ** attempt)))
    if last_error is not None:
        raise last_error


async def _gitea_files_under_prefix(
    backend: GiteaBackend,
    inner_prefix: str,
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    clean_prefix = inner_prefix.strip("/")
    clean_limit = _clean_file_list_limit(limit) if limit is not None else None
    als = getattr(backend, "als", None)
    if not callable(als):
        tree = await backend._load_tree()  # type: ignore[attr-defined]
        prefix = clean_prefix.rstrip("/")
        rows: list[dict[str, Any]] = []
        for item in tree:
            path = str(getattr(item, "path", "") or "").strip("/")
            if (
                getattr(item, "is_dir", False)
                or not path
                or (prefix and path != prefix and not path.startswith(prefix + "/"))
            ):
                continue
            rows.append({
                "path": path,
                "size": int(getattr(item, "size", 0) or 0),
                "modified_at": "",
                "content_type": "",
            })
            if clean_limit is not None and len(rows) >= clean_limit:
                break
        return rows

    rows: list[dict[str, Any]] = []
    pending = ["/" + clean_prefix if clean_prefix else "/"]
    cursor = 0
    seen_dirs: set[str] = set()
    while cursor < len(pending):
        if clean_limit is not None and len(rows) >= clean_limit:
            break
        current = pending[cursor]
        cursor += 1
        if current in seen_dirs:
            continue
        seen_dirs.add(current)
        result = await als(current)
        if getattr(result, "error", None):
            continue
        entries = result.entries if hasattr(result, "entries") else result
        for item in entries or []:
            path = str(item.get("path") or "").strip("/")
            if not path:
                continue
            is_dir = bool(item.get("is_dir")) or str(item.get("path") or "").endswith("/")
            if is_dir:
                pending.append("/" + path.rstrip("/"))
                continue
            rows.append({
                "path": path,
                "size": int(item.get("size") or 0),
                "modified_at": "",
                "content_type": "",
            })
            if clean_limit is not None and len(rows) >= clean_limit:
                break
    return rows


@router.get("/files")
async def list_grant_files(
    x_a2a_grant: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
    limit: int = _GRANT_FILE_LIST_DEFAULT_LIMIT,
) -> list[dict[str, Any]]:
    grant = _grant_from_header(x_a2a_grant)
    bucket = str(grant["bucket"])
    clean_limit = _clean_file_list_limit(limit)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for prefix in _grant_minio_prefixes(grant):
        if len(rows) >= clean_limit:
            break
        for item in list_files(bucket, prefix=prefix):
            path = str(item.get("path") or "")
            if not path or path in seen or not _allowed(grant, path):
                continue
            seen.add(path)
            rows.append(item)
            if len(rows) >= clean_limit:
                break
    for repo in _agent_repos_from_grant(grant):
        if len(rows) >= clean_limit:
            break
        inner_prefixes = _grant_repo_inner_prefixes(grant, repo)
        if not inner_prefixes:
            continue
        try:
            backend = await _gitea_backend_for_grant(session, grant, repo=repo, scope="read")
            repo_rows: list[dict[str, Any]] = []
            for inner_prefix in inner_prefixes:
                remaining = clean_limit - len(rows) - len(repo_rows)
                if remaining <= 0:
                    break
                repo_rows.extend(
                    await _gitea_files_under_prefix(
                        backend,
                        inner_prefix,
                        limit=remaining,
                    )
                )
        except HTTPException as exc:
            if exc.status_code == 404:
                continue
            raise
        except GiteaAuthError as exc:
            raise HTTPException(403, "gitea token denied") from exc
        except GiteaError:
            continue
        for item in repo_rows:
            rel = str(item.get("path") or "").strip("/")
            if not rel:
                continue
            key = f"agents/{repo}/{rel}"
            if key in seen or not _allowed(grant, key):
                continue
            seen.add(key)
            rows.append({
                "path": key,
                "size": int(item.get("size") or 0),
                "modified_at": "",
                "content_type": "",
            })
            if len(rows) >= clean_limit:
                break
    return sorted(rows, key=lambda item: str(item.get("path") or ""))


@router.get("/files/{path:path}")
async def read_grant_file(
    path: str,
    x_a2a_grant: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> Response:
    grant = _grant_from_header(x_a2a_grant)
    key = _sanitize_key(path)
    if not _allowed(grant, key):
        raise HTTPException(403, "path denied by grant")
    route = _workspace_route(key)
    if route is not None:
        repo, inner = route
        if not inner:
            raise HTTPException(404, "not found")
        backend = await _gitea_backend_for_grant(
            session,
            grant,
            repo=repo,
            scope="read",
        )
        try:
            data = await backend._read_bytes(inner)  # type: ignore[attr-defined]
        except GiteaAuthError as exc:
            raise HTTPException(403, "gitea token denied") from exc
        except GiteaError as exc:
            raise HTTPException(404, "not found") from exc
        return Response(content=data, media_type="text/plain; charset=utf-8")
    try:
        data, content_type = get_file(str(grant["bucket"]), key)
    except FileNotFoundError as exc:
        raise HTTPException(404, "not found") from exc
    return Response(content=data, media_type=content_type)


@router.head("/files/{path:path}")
async def stat_grant_file(
    path: str,
    x_a2a_grant: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> Response:
    grant = _grant_from_header(x_a2a_grant)
    key = _sanitize_key(path)
    if not _allowed(grant, key):
        raise HTTPException(403, "path denied by grant")
    route = _workspace_route(key)
    if route is not None:
        repo, inner = route
        if not inner:
            raise HTTPException(404, "not found")
        backend = await _gitea_backend_for_grant(
            session,
            grant,
            repo=repo,
            scope="read",
        )
        try:
            data = await backend._read_bytes(inner)  # type: ignore[attr-defined]
        except GiteaAuthError as exc:
            raise HTTPException(403, "gitea token denied") from exc
        except GiteaError as exc:
            raise HTTPException(404, "not found") from exc
        return Response(
            headers={
                "X-A2A-File-Path": quote(key, safe="/"),
                "X-A2A-File-Size": str(len(data)),
                "X-A2A-Content-Type": "text/plain; charset=utf-8",
            }
        )
    try:
        meta = stat_file(str(grant["bucket"]), key)
    except FileNotFoundError as exc:
        raise HTTPException(404, "not found") from exc
    return Response(
        headers={
            "X-A2A-File-Path": quote(key, safe="/"),
            "X-A2A-File-Size": str(meta.get("size") or 0),
            "X-A2A-Content-Type": str(meta.get("content_type") or ""),
        }
    )


@router.put("/files/{path:path}")
async def write_grant_file(
    path: str,
    body: bytes = Body(...),
    x_a2a_grant: str | None = Header(default=None),
    content_type: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    grant = _grant_from_header(x_a2a_grant)
    key = _sanitize_key(path)
    if not _writable(grant, key):
        raise HTTPException(403, "path is not writable by grant")
    route = _workspace_route(key)
    if route is not None:
        repo, inner = route
        if not inner:
            raise HTTPException(400, "agents/ paths must point to a file")
        backend = await _gitea_backend_for_grant(
            session,
            grant,
            repo=repo,
            scope="write",
        )
        try:
            await _write_gitea_bytes_with_retry(backend, inner, body, action="write")
        except GiteaAuthError as exc:
            raise HTTPException(403, "gitea token denied") from exc
        except GiteaError as exc:
            raise HTTPException(502, str(exc)) from exc
        return {"ok": True, "path": key, "size": len(body), "backend": "gitea"}
    return upload_file(str(grant["bucket"]), key, body, content_type)


@router.delete("/files/{path:path}", status_code=204)
async def delete_grant_file(
    path: str,
    x_a2a_grant: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> None:
    grant = _grant_from_header(x_a2a_grant)
    key = _sanitize_key(path)
    if not _writable(grant, key):
        raise HTTPException(403, "path is not writable by grant")
    route = _workspace_route(key)
    if route is not None:
        repo, inner = route
        if not inner:
            raise HTTPException(400, "agents/ paths must point to a file")
        backend = await _gitea_backend_for_grant(
            session,
            grant,
            repo=repo,
            scope="write",
        )
        try:
            await backend._delete(inner)  # type: ignore[attr-defined]
        except GiteaAuthError as exc:
            raise HTTPException(403, "gitea token denied") from exc
        except GiteaError as exc:
            raise HTTPException(404, "not found") from exc
        return
    try:
        delete_file(str(grant["bucket"]), key)
    except FileNotFoundError as exc:
        raise HTTPException(404, "not found") from exc
