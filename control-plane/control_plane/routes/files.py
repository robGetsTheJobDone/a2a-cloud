"""User file endpoints. Each user has their own MinIO bucket."""
from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from fastapi.responses import StreamingResponse
from fastapi import Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import current_user
from ..db import get_session
from ..gitea import GITEA_INTERNAL, GITEA_PASS, GITEA_USER
from ..models import Agent, User
from ..minio_client import (
    bucket_for_user,
    delete_file,
    iter_file,
    list_children,
    list_files_page,
    move_file,
    stat_file,
    upload_fileobj,
)

router = APIRouter(prefix="/v1/me/files", tags=["files"])
_MAX_UPLOAD_BYTES = int(os.environ.get("A2A_CP_MAX_FILE_UPLOAD_BYTES", str(100 * 1024 * 1024)))
_DEFAULT_FILE_LIST_PAGE_LIMIT = 1000


def _sanitize_key(key: str) -> str:
    """Normalize a workspace key: strip leading slash, collapse, reject ``..``."""
    key = key.strip().lstrip("/").rstrip()
    if not key:
        raise HTTPException(400, "empty path")
    parts = [p for p in key.split("/") if p and p != "."]
    if any(p == ".." for p in parts):
        raise HTTPException(400, "'..' not allowed in path")
    return "/".join(parts)


def _sanitize_prefix(prefix: str | None) -> str:
    if prefix is None:
        return ""
    clean = prefix.strip().lstrip("/").rstrip("/")
    if not clean:
        return ""
    parts = [p for p in clean.split("/") if p and p != "."]
    if any(p == ".." for p in parts):
        raise HTTPException(400, "'..' not allowed in path")
    return "/".join(parts)


def _upload_size(file: UploadFile) -> int:
    try:
        current = file.file.tell()
        file.file.seek(0, os.SEEK_END)
        size = file.file.tell()
        file.file.seek(current)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"could not inspect upload size: {exc}") from exc
    if size > _MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"upload exceeds {_MAX_UPLOAD_BYTES} bytes")
    return size


@router.get("")
async def list_my_files(
    response: Response,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    prefix: str | None = Query(None),
    limit: int | None = Query(default=None, ge=1, le=1000),
    cursor: str | None = Query(default=None),
) -> list[dict[str, Any]]:
    bucket = bucket_for_user(user.id)
    clean_prefix = _sanitize_prefix(prefix)
    if prefix is None:
        rows, next_cursor = list_files_page(
            bucket,
            limit=limit or _DEFAULT_FILE_LIST_PAGE_LIMIT,
            cursor=cursor,
        )
        if next_cursor:
            response.headers["X-A2A-Next-Cursor"] = next_cursor
            response.headers["X-A2A-Has-More"] = "true"
        else:
            response.headers["X-A2A-Has-More"] = "false"
        return rows
    return await _list_view(bucket, user, session, clean_prefix)


@router.post("", status_code=201)
async def upload(
    file: UploadFile = File(...),
    path: str | None = Form(None),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    if not file.filename:
        raise HTTPException(400, "missing filename")
    # `path` is the FULL destination key (e.g. "data/sales.csv"). If absent,
    # use the bare filename.
    key = _sanitize_key(path or file.filename)
    bucket = bucket_for_user(user.id)
    size = _upload_size(file)
    return upload_fileobj(
        bucket,
        path=key,
        fileobj=file.file,
        size=size,
        content_type=file.content_type,
    )


@router.post("/move")
async def move(
    body: dict,
    user: User = Depends(current_user),
) -> dict[str, Any]:
    """Move (rename) a single object inside the user's bucket.

    Body: ``{"from": "<src>", "to": "<dst>"}``. Both paths sanitized.
    """
    src_raw = body.get("from")
    dst_raw = body.get("to")
    if not isinstance(src_raw, str) or not isinstance(dst_raw, str):
        raise HTTPException(400, "from + to are required strings")
    src = _sanitize_key(src_raw)
    dst = _sanitize_key(dst_raw)
    bucket = bucket_for_user(user.id)
    try:
        return move_file(bucket, src, dst)
    except FileNotFoundError as exc:
        raise HTTPException(404, f"source not found: {src}") from exc
    except FileExistsError as exc:
        raise HTTPException(409, f"destination already exists: {dst}") from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"move failed: {exc}") from exc


@router.delete("/{path:path}", status_code=204)
async def remove(
    path: str,
    user: User = Depends(current_user),
) -> None:
    key = _sanitize_key(path)
    bucket = bucket_for_user(user.id)
    try:
        delete_file(bucket, key)
    except FileNotFoundError as exc:
        raise HTTPException(404, f"not found: {key}") from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"delete failed: {exc}") from exc


@router.get("/{path:path}")
async def download(
    path: str,
    user: User = Depends(current_user),
) -> StreamingResponse:
    key = _sanitize_key(path)
    bucket = bucket_for_user(user.id)
    try:
        meta = stat_file(bucket, key)
        chunks, content_type = iter_file(bucket, key)
    except FileNotFoundError as exc:
        raise HTTPException(404, f"not found: {key}") from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"download failed: {exc}") from exc
    filename = key.rsplit("/", 1)[-1]
    headers = {
        "Content-Disposition": f"inline; filename*=UTF-8''{quote(filename)}",
        "X-A2A-File-Path": quote(key, safe="/"),
        "X-A2A-Modified-At": str(meta.get("modified_at") or ""),
    }
    return StreamingResponse(chunks, media_type=content_type, headers=headers)


async def _list_view(
    bucket: str,
    user: User,
    session: AsyncSession,
    prefix: str,
) -> list[dict[str, Any]]:
    if not prefix:
        rows = [
            item
            for item in list_children(bucket, "")
            if (path := str(item.get("path") or "")) != "agents" and not path.startswith("agents/")
        ]
        agent_repos = await _live_agent_repos(session, user.id)
        if agent_repos:
            rows.append(
                {
                    "path": "agents",
                    "size": 0,
                    "modified_at": "",
                    "content_type": "",
                    "is_dir": True,
                }
            )
        rows.sort(key=lambda item: item["path"])
        return rows

    if prefix == "agents":
        repos = await _live_agent_repos(session, user.id)
        return [
            {
                "path": f"agents/{repo}",
                "size": 0,
                "modified_at": "",
                "content_type": "",
                "is_dir": True,
                "writable": False,
            }
            for repo, _owner in repos
        ]

    if prefix.startswith("agents/"):
        repo_part = prefix[len("agents/"):]
        repo, _, inner = repo_part.partition("/")
        if not repo:
            return []
        repos = {name: owner for name, owner in await _live_agent_repos(session, user.id)}
        owner = repos.get(repo)
        if owner is None:
            return []
        return await _gitea_children(owner, repo, inner)

    return list(list_children(bucket, prefix))


async def _live_agent_repos(session: AsyncSession, user_id: int) -> list[tuple[str, str]]:
    rows = (
        await session.execute(
            select(Agent.name, Agent.gitea_owner)
            .where(Agent.owner_id == user_id)
            .where(Agent.gitea_owner.is_not(None))
            .order_by(Agent.name.asc())
        )
    ).all()
    out: list[tuple[str, str]] = []
    for name, owner in rows:
        if not isinstance(name, str) or not name.strip():
            continue
        out.append((name, str(owner or GITEA_USER)))
    return out


async def _gitea_children(owner: str, repo: str, prefix: str) -> list[dict[str, Any]]:
    clean_prefix = prefix.strip("/").rstrip("/")
    contents = await _gitea_contents(owner, repo, clean_prefix)
    if isinstance(contents, dict):
        contents = [contents] if contents.get("type") in {"dir", "tree"} else []
    rows: list[dict[str, Any]] = []
    for item in contents or []:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or item.get("name") or "").strip("/")
        if not path:
            continue
        is_dir = item.get("type") in {"dir", "tree"} or bool(item.get("is_dir"))
        rows.append(
            {
                "path": f"agents/{repo}/{path}",
                "size": 0 if is_dir else int(item.get("size") or 0),
                "modified_at": "",
                "content_type": "",
                "etag": str(item.get("sha") or ""),
                "is_dir": is_dir,
                "writable": False,
            }
        )
    return sorted(rows, key=lambda item: str(item["path"]))


async def _gitea_contents(owner: str, repo: str, prefix: str) -> list[dict[str, Any]] | dict[str, Any]:
    clean_prefix = prefix.strip("/").rstrip("/")
    url = f"{GITEA_INTERNAL.rstrip('/')}/api/v1/repos/{owner}/{repo}/contents"
    if clean_prefix:
        url += "/" + quote(clean_prefix, safe="/")
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url, auth=(GITEA_USER, GITEA_PASS), params={"ref": "main"})
    if resp.status_code == 404:
        return []
    resp.raise_for_status()
    data = resp.json() if resp.content else []
    return data if isinstance(data, (list, dict)) else []
