"""File CRUD tools for the orchestrator workspace.

Most paths stay in MinIO, but ``agents/<name>/...`` is routed to the managed
Gitea source repo for that agent, and ``repos/<name>/...`` is routed to
configured first-party Gitea repo mounts.

Edits to managed source repos do not auto-deploy. Call ``deploy_agent_source``
after source changes are complete.
"""
from __future__ import annotations

import base64
import asyncio
import json
import posixpath
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import boto3
import httpx
from botocore.config import Config as _BotoConfig
from botocore.exceptions import ClientError
from langchain_core.tools import tool

from a2a_pack.gitea_backend import GiteaAuthError, GiteaBackend
from control_plane.gitea import GITEA_INTERNAL, GITEA_USER
from control_plane.repo_mounts import RepoMount, parse_repo_mounts

if TYPE_CHECKING:
    from ..orchestrator import OrchestratorContext

LIST_FILES_DEFAULT_LIMIT = 80
LIST_FILES_MAX_LIMIT = 500


def _s3_client(ctx: "OrchestratorContext") -> Any:
    return boto3.client(
        "s3",
        endpoint_url=ctx.settings.minio_endpoint,
        aws_access_key_id=ctx.settings.minio_access_key,
        aws_secret_access_key=ctx.settings.minio_secret_key,
        region_name="us-east-1",
        config=_BotoConfig(s3={"addressing_style": "path"}),
    )


def _ensure_bucket(s3: Any, bucket: str) -> None:
    try:
        s3.head_bucket(Bucket=bucket)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code in {"404", "NoSuchBucket", "NotFound"}:
            s3.create_bucket(Bucket=bucket)
        elif code != "403":
            raise


def _normalize_workspace_path(path: str) -> str:
    raw = str(path or "").strip()
    if raw == "/workspace":
        raw = ""
    elif raw.startswith("/workspace/"):
        raw = raw[len("/workspace/"):]
    raw = raw.lstrip("/")
    normalized = posixpath.normpath(raw)
    if normalized in {"", ".", "/"}:
        raise ValueError("refusing to delete the workspace root")
    if normalized == ".." or normalized.startswith("../"):
        raise ValueError("path must stay inside the workspace")
    return normalized


def _normalize_workspace_listing_path(path: str = "") -> str:
    raw = str(path or "").strip()
    if raw == "/workspace":
        raw = ""
    elif raw.startswith("/workspace/"):
        raw = raw[len("/workspace/"):]
    raw = raw.lstrip("/")
    normalized = posixpath.normpath(raw)
    if normalized in {"", ".", "/"}:
        return ""
    if normalized == ".." or normalized.startswith("../"):
        raise ValueError("path must stay inside the workspace")
    return normalized.rstrip("/")


def _clean_list_limit(limit: int | None) -> int:
    try:
        value = int(limit or LIST_FILES_DEFAULT_LIMIT)
    except (TypeError, ValueError):
        value = LIST_FILES_DEFAULT_LIMIT
    return max(1, min(value, LIST_FILES_MAX_LIMIT))


def _json(data: Any) -> str:
    return json.dumps(data, separators=(",", ":"))


def _delete_workspace_path(s3: Any, bucket: str, path: str) -> dict[str, Any]:
    key = _normalize_workspace_path(path)
    keys: list[str] = []
    seen: set[str] = set()

    def add(candidate: str) -> None:
        if candidate and candidate not in seen:
            seen.add(candidate)
            keys.append(candidate)

    prefix = key.rstrip("/")
    child_prefix = f"{prefix}/"
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents") or ():
            obj_key = obj.get("Key")
            if isinstance(obj_key, str) and (
                obj_key == prefix or obj_key.startswith(child_prefix)
            ):
                add(obj_key)
    if not keys:
        add(prefix)

    deleted = 0
    for obj_key in keys:
        s3.delete_object(Bucket=bucket, Key=obj_key)
        deleted += 1
    return {
        "ok": True,
        "path": prefix,
        "deleted": deleted,
        "mode": "prefix" if len(keys) > 1 or path.strip().endswith("/") else "file",
    }


def _delete_workspace_prefix(s3: Any, bucket: str, prefix: str) -> int:
    raw = prefix.rstrip("/") + "/"
    keys: list[str] = []
    seen: set[str] = set()
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=raw):
        for obj in page.get("Contents") or ():
            obj_key = obj.get("Key")
            if isinstance(obj_key, str) and obj_key.startswith(raw) and obj_key not in seen:
                seen.add(obj_key)
                keys.append(obj_key)
    for obj_key in keys:
        s3.delete_object(Bucket=bucket, Key=obj_key)
    return len(keys)


@dataclass(frozen=True)
class WorkspaceRoute:
    collection: str
    repo: str
    inner_path: str

    @property
    def mount_path(self) -> str:
        return f"{self.collection}/{self.repo}/"


def _workspace_route(path: str) -> WorkspaceRoute | None:
    """Return the Gitea route for mounted workspace paths.

    Paths are expected to look like ``agents/<repo>/<file>`` or
    ``repos/<repo>/<file>``. Collection roots are handled separately.
    """
    for collection in ("agents", "repos"):
        prefix = f"{collection}/"
        if not path.startswith(prefix):
            continue
        rel = path[len(prefix):]
        if not rel:
            return None
        repo, _, inner = rel.partition("/")
        if not repo:
            return None
        return WorkspaceRoute(collection=collection, repo=repo, inner_path=inner)
    return None


def _mint_gitea_token(
    ctx: "OrchestratorContext",
    repo: str,
    *,
    owner: str | None = None,
    scope: str = "read",
    ttl_seconds: int = 900,
) -> dict[str, Any]:
    if not ctx.jwt:
        raise RuntimeError("Gitea-backed paths require cp_jwt so the Gitea token can be minted")
    url = f"{ctx.settings.cp_url.rstrip('/')}/v1/platform/gitea-token"
    body: dict[str, Any] = {"repo": repo, "scope": scope, "ttl_seconds": ttl_seconds}
    if owner:
        body["owner"] = owner
    with httpx.Client(timeout=10.0) as client:
        resp = client.post(
            url,
            json=body,
            headers={"authorization": f"bearer {ctx.jwt}"},
        )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"gitea token mint failed: {resp.status_code}: {resp.text[:300]}"
        )
    return resp.json()


class _WorkspaceFileStore:
    def __init__(self, ctx: "OrchestratorContext", *, s3: Any | None = None) -> None:
        self._ctx = ctx
        self._s3 = s3 or _s3_client(ctx)
        self._bucket = ctx.bucket
        self._gitea_repo_owners: dict[str, str] = {}
        self._known_gitea_repo_mounts: list[RepoMount] | None = None
        self._gitea_backends: dict[tuple[str, str, str], GiteaBackend] = {}

    def _configured_repo_mounts(self) -> list[RepoMount]:
        if not self._ctx.jwt:
            return []
        raw = str(getattr(self._ctx.settings, "repo_mounts", "") or "")
        return list(parse_repo_mounts(raw, default_owner=GITEA_USER))

    def _live_gitea_repos(self) -> list[RepoMount]:
        if not self._ctx.jwt:
            return []
        url = f"{self._ctx.settings.cp_url.rstrip('/')}/v1/me/service-access"
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.get(url, headers={"authorization": f"bearer {self._ctx.jwt}"})
        except httpx.HTTPError:
            return []
        if resp.status_code >= 400:
            return []
        payload = resp.json() if resp.content else {}
        gitea = payload.get("gitea") if isinstance(payload, dict) else None
        repos = gitea.get("repositories") if isinstance(gitea, dict) else None
        out: list[RepoMount] = []
        for item in repos or []:
            if not isinstance(item, dict):
                continue
            repo = str(item.get("repo") or item.get("agent_name") or "").strip()
            if not repo:
                continue
            owner = str(item.get("owner") or GITEA_USER).strip() or GITEA_USER
            mount_path = str(item.get("mount_path") or "").strip()
            collection = "repos" if mount_path.startswith("repos/") else "agents"
            out.append(RepoMount(repo=repo, owner=owner, mount_path=f"{collection}/{repo}/"))
        return out

    def _known_gitea_repos(self) -> list[RepoMount]:
        if self._known_gitea_repo_mounts is None:
            live = {(mount.mount_path, mount.repo): mount for mount in self._live_gitea_repos()}
            for mount in self._configured_repo_mounts():
                live.setdefault((mount.mount_path, mount.repo), mount)
            self._known_gitea_repo_mounts = sorted(
                live.values(),
                key=lambda item: item.mount_path,
            )
        repos = {
            (mount.mount_path, mount.repo): mount
            for mount in self._known_gitea_repo_mounts
        }
        for repo, owner in self._gitea_repo_owners.items():
            repos.setdefault(
                (f"agents/{repo}/", repo),
                RepoMount(repo=repo, owner=owner, mount_path=f"agents/{repo}/"),
            )
        return sorted(repos.values(), key=lambda item: item.mount_path)

    def _gitea_owner(self, repo: str, *, mount_path: str | None = None) -> str:
        for mount in self._known_gitea_repos():
            if mount.repo == repo and (mount_path is None or mount.mount_path == mount_path):
                return mount.owner
        return self._gitea_repo_owners.get(repo, GITEA_USER)

    def _gitea_backend(
        self,
        repo: str,
        *,
        scope: str = "read",
        owner: str | None = None,
    ) -> GiteaBackend:
        owner = owner or self._gitea_owner(repo)
        cache_key = (repo, owner, scope)
        cached = self._gitea_backends.get(cache_key)
        if cached is not None:
            return cached
        token = _mint_gitea_token(
            self._ctx,
            repo,
            owner=owner,
            scope=scope,
            ttl_seconds=900,
        )
        backend = GiteaBackend(
            gitea_url=GITEA_INTERNAL,
            owner=str(token.get("owner") or owner or GITEA_USER),
            repo=repo,
            token=str(token["token"]),
            commit_prefix="a2a-source-edit",
        )
        self._gitea_repo_owners[repo] = str(token.get("owner") or owner or GITEA_USER)
        self._gitea_backends[cache_key] = backend
        return backend

    def _with_gitea_backend(
        self,
        repo: str,
        *,
        scope: str,
        fn: Any,
        owner: str | None = None,
    ) -> Any:
        owner = owner or self._gitea_owner(repo)
        cache_key = (repo, owner, scope)
        try:
            backend = self._gitea_backend(repo, scope=scope, owner=owner)
            return fn(backend)
        except GiteaAuthError:
            self._gitea_backends.pop(cache_key, None)
            backend = self._gitea_backend(repo, scope=scope, owner=owner)
            return fn(backend)

    def _invalidate_gitea_backends(self, repo: str) -> None:
        for key in list(self._gitea_backends):
            if key[0] == repo:
                self._gitea_backends.pop(key, None)

    def _list_gitea_recursive_items(
        self,
        backend: GiteaBackend,
        root_path: str,
        *,
        limit: int,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Walk repo directories until enough file rows have been collected."""
        root = f"/{root_path.strip('/')}" if root_path.strip("/") else "/"
        pending = [root]
        cursor = 0
        out: list[dict[str, Any]] = []
        while cursor < len(pending):
            current = pending[cursor]
            cursor += 1
            result = backend.ls(current)
            if getattr(result, "error", None):
                continue
            entries = result.entries if hasattr(result, "entries") else result
            dirs: list[str] = []
            for item in entries or []:
                rel = str(item.get("path") or "").lstrip("/").rstrip("/")
                if not rel:
                    continue
                is_dir = bool(item.get("is_dir")) or str(item.get("path") or "").endswith("/")
                if is_dir:
                    dirs.append(f"/{rel}")
                    continue
                out.append(item)
                if len(out) >= limit:
                    return out, True
            pending.extend(sorted(dirs))
        return out, False

    def _list_minio_files(
        self,
        *,
        prefix: str = "",
        recursive: bool = False,
        limit: int = LIST_FILES_DEFAULT_LIMIT,
    ) -> tuple[list[dict[str, Any]], bool]:
        paginator = self._s3.get_paginator("list_objects_v2")
        out: list[dict[str, Any]] = []
        route_enabled = bool(self._ctx.jwt or self._gitea_repo_owners)
        list_prefix = prefix.rstrip("/")
        if list_prefix:
            list_prefix += "/"
        paginate_kwargs: dict[str, Any] = {"Bucket": self._bucket, "Prefix": list_prefix}
        if not recursive:
            paginate_kwargs["Delimiter"] = "/"
        truncated = False
        page_size = max(1, min(limit + 1, 1000))
        for page in paginator.paginate(
            **paginate_kwargs,
            PaginationConfig={"PageSize": page_size},
        ):
            if not recursive:
                for common in page.get("CommonPrefixes") or ():
                    child = str(common.get("Prefix") or "")
                    if not child:
                        continue
                    if route_enabled and (
                        child in {"agents/", "repos/"}
                        or child.startswith("agents/")
                        or child.startswith("repos/")
                    ):
                        continue
                    out.append({"path": child, "size": 0, "modified_at": None})
                    if len(out) > limit:
                        truncated = True
                        return out[:limit], truncated
            for obj in page.get("Contents") or ():
                key = str(obj["Key"])
                if route_enabled and (
                    key in {"agents", "repos"}
                    or key.startswith("agents/")
                    or key.startswith("repos/")
                ):
                    continue
                if not recursive:
                    rest = key[len(list_prefix):] if list_prefix else key
                    if "/" in rest.strip("/"):
                        continue
                out.append(
                    {
                        "path": key,
                        "size": int(obj.get("Size") or 0),
                        "modified_at": (
                            obj["LastModified"].isoformat()
                            if obj.get("LastModified")
                            else None
                        ),
                    }
                )
                if len(out) > limit:
                    truncated = True
                    return out[:limit], truncated
        return out, truncated

    def _mount_rows(self, path: str) -> list[dict[str, Any]]:
        collection = path if path in {"agents", "repos"} else None
        out: list[dict[str, Any]] = []
        for mount in self._known_gitea_repos():
            if collection and not mount.mount_path.startswith(f"{collection}/"):
                continue
            out.append({"path": mount.mount_path, "size": 0, "modified_at": None})
        return out

    def _list_gitea_files(
        self,
        *,
        path: str = "",
        recursive: bool = False,
        limit: int = LIST_FILES_DEFAULT_LIMIT,
    ) -> tuple[list[dict[str, Any]], bool]:
        if not (self._ctx.jwt or self._gitea_repo_owners):
            return [], False
        if not path or path in {"agents", "repos"}:
            rows = self._mount_rows(path)
            return rows[:limit], len(rows) > limit

        route = _workspace_route(path)
        if route is None:
            return [], False
        out: list[dict[str, Any]] = []
        truncated = False
        try:
            if recursive:
                items, truncated = self._with_gitea_backend(
                    route.repo,
                    scope="read",
                    owner=self._gitea_owner(route.repo, mount_path=route.mount_path),
                    fn=lambda backend: self._list_gitea_recursive_items(
                        backend,
                        route.inner_path,
                        limit=limit,
                    ),
                )
                result = None
            else:
                search_root = f"/{route.inner_path}" if route.inner_path else "/"
                result = self._with_gitea_backend(
                    route.repo,
                    scope="read",
                    owner=self._gitea_owner(route.repo, mount_path=route.mount_path),
                    fn=lambda backend: backend.ls(search_root),
                )
                items = result.entries if hasattr(result, "entries") else result
        except (GiteaAuthError, RuntimeError):
            return [], False
        if result is not None and getattr(result, "error", None):
            return [], False
        for item in items or []:
            rel = str(item.get("path") or "").lstrip("/")
            if not rel:
                continue
            is_dir = bool(item.get("is_dir")) or rel.endswith("/")
            rel = rel.rstrip("/")
            if rel == route.inner_path:
                continue
            path_suffix = rel.rstrip("/") + ("/" if is_dir else "")
            out.append(
                {
                    "path": f"{route.mount_path}{path_suffix}",
                    "size": int(item.get("size") or 0),
                    "modified_at": item.get("modified_at") or "",
                }
            )
            if len(out) > limit:
                truncated = True
                break
        if truncated:
            out = out[:limit]
        return out, truncated

    def list_files(
        self,
        path: str = "",
        *,
        recursive: bool = False,
        limit: int | None = None,
    ) -> str:
        try:
            norm = _normalize_workspace_listing_path(path)
        except ValueError as exc:
            return _json({"error": str(exc), "path": path})
        clean_limit = _clean_list_limit(limit)
        route = _workspace_route(norm) if norm else None
        if route is not None or norm in {"agents", "repos"}:
            minio_files: list[dict[str, Any]] = []
            minio_truncated = False
        else:
            minio_files, minio_truncated = self._list_minio_files(
                prefix=norm,
                recursive=recursive,
                limit=clean_limit,
            )
        remaining = max(0, clean_limit - len(minio_files))
        if remaining:
            gitea_files, gitea_truncated = self._list_gitea_files(
                path=norm,
                recursive=recursive,
                limit=remaining,
            )
        else:
            gitea_files, gitea_truncated = [], True
        files = minio_files + gitea_files
        files.sort(key=lambda item: item["path"])
        if len(files) > clean_limit:
            files = files[:clean_limit]
        return _json(
            {
                "path": norm,
                "recursive": recursive,
                "limit": clean_limit,
                "truncated": minio_truncated or gitea_truncated,
                "files": files,
            }
        )

    def read_file(self, path: str) -> str:
        try:
            norm = _normalize_workspace_path(path)
        except ValueError as exc:
            return _json({"error": str(exc), "path": path})
        route = _workspace_route(norm)
        if route is not None:
            if not route.inner_path:
                return _json({
                    "error": f"{route.collection}/ paths must point to a file",
                    "path": path,
                })
            try:
                result = self._with_gitea_backend(
                    route.repo,
                    scope="read",
                    owner=self._gitea_owner(route.repo, mount_path=route.mount_path),
                    fn=lambda backend: backend.read(route.inner_path),
                )
            except Exception as exc:  # noqa: BLE001
                return _json({"error": f"read failed: {exc}", "path": path})
            if getattr(result, "error", None):
                return _json({"error": f"read failed: {result.error}", "path": path})
            file_data = getattr(result, "file_data", None) or {}
            encoding = file_data.get("encoding")
            content = file_data.get("content", "")
            if encoding == "base64":
                raw = base64.b64decode(content)
                text = raw.decode("utf-8", errors="replace")
                size = len(raw)
            else:
                text = str(content)
                size = len(text.encode("utf-8"))
            return _json({
                "path": path,
                "content_type": "text/plain; charset=utf-8",
                "size": size,
                "content": text[:200_000],
            })

        _ensure_bucket(self._s3, self._bucket)
        try:
            resp = self._s3.get_object(Bucket=self._bucket, Key=norm)
        except ClientError as exc:
            return _json({"error": f"read failed: {exc}", "path": path})
        body = resp["Body"].read()
        text = body.decode("utf-8", errors="replace")
        return _json({
            "path": path,
            "content_type": resp.get("ContentType") or "application/octet-stream",
            "size": len(body),
            "content": text[:200_000],
        })

    def write_file(self, path: str, content: str) -> str:
        try:
            norm = _normalize_workspace_path(path)
        except ValueError as exc:
            return _json({"error": str(exc), "path": path})
        route = _workspace_route(norm)
        if route is not None:
            if not route.inner_path:
                return _json({
                    "error": f"{route.collection}/ paths must point to a file",
                    "path": path,
                })
            try:
                def _write(backend: GiteaBackend) -> Any:
                    existing = backend.read(route.inner_path)
                    if getattr(existing, "error", None):
                        return backend.write(route.inner_path, content)
                    file_data = getattr(existing, "file_data", None) or {}
                    current = str(file_data.get("content") or "")
                    return backend.edit(route.inner_path, current, content, replace_all=False)

                result = self._with_gitea_backend(
                    route.repo,
                    scope="write",
                    owner=self._gitea_owner(route.repo, mount_path=route.mount_path),
                    fn=_write,
                )
            except Exception as exc:  # noqa: BLE001
                return _json({"error": f"write failed: {exc}", "path": path})
            if getattr(result, "error", None):
                return _json({"error": result.error, "path": path})
            data = content.encode("utf-8")
            self._invalidate_gitea_backends(route.repo)
            return _json({"ok": True, "path": path, "size": len(data)})

        _ensure_bucket(self._s3, self._bucket)
        data = content.encode("utf-8")
        self._s3.put_object(
            Bucket=self._bucket,
            Key=norm,
            Body=data,
            ContentType="text/plain; charset=utf-8",
        )
        return _json({"ok": True, "path": path, "size": len(data)})

    def _delete_gitea_prefix(self, repo: str, prefix: str, *, owner: str | None = None) -> int:
        prefix = prefix.strip("/")
        search_root = f"/{prefix}" if prefix else "/"
        owner = owner or self._gitea_owner(repo)
        backend = self._gitea_backend(repo, scope="write", owner=owner)
        try:
            result = backend.glob("**", path=search_root)
        except GiteaAuthError:
            self._gitea_backends.pop((repo, owner, "write"), None)
            backend = self._gitea_backend(repo, scope="write", owner=owner)
            result = backend.glob("**", path=search_root)
        matches = result.matches if hasattr(result, "matches") else result
        keys: list[str] = []
        seen: set[str] = set()
        for item in matches or []:
            rel = str(item.get("path") or "").lstrip("/")
            if not rel:
                continue
            if prefix and not (
                rel == prefix or rel.startswith(prefix.rstrip("/") + "/")
            ):
                continue
            if rel not in seen:
                seen.add(rel)
                keys.append(rel)
        if not prefix:
            keys = [
                str(item.get("path") or "").lstrip("/")
                for item in matches or []
                if str(item.get("path") or "").lstrip("/")
            ]
        deleted = 0
        for key in keys:
            try:
                asyncio.run(backend._delete(key))  # type: ignore[attr-defined]
            except GiteaAuthError:
                self._gitea_backends.pop((repo, owner, "write"), None)
                backend = self._gitea_backend(repo, scope="write", owner=owner)
                asyncio.run(backend._delete(key))  # type: ignore[attr-defined]
            deleted += 1
        self._invalidate_gitea_backends(repo)
        return deleted

    def delete_file(self, path: str) -> str:
        try:
            norm = _normalize_workspace_path(path)
        except ValueError as exc:
            return _json({"error": str(exc), "path": path})
        if norm in {"agents", "repos"}:
            deleted = 0
            for mount in self._known_gitea_repos():
                if mount.mount_path.startswith(f"{norm}/"):
                    deleted += self._delete_gitea_prefix(mount.repo, "", owner=mount.owner)
            deleted += _delete_workspace_prefix(self._s3, self._bucket, norm)
            return _json({"ok": True, "path": norm, "deleted": deleted, "mode": "prefix"})

        route = _workspace_route(norm)
        if route is not None:
            try:
                deleted = self._delete_gitea_prefix(
                    route.repo,
                    route.inner_path,
                    owner=self._gitea_owner(route.repo, mount_path=route.mount_path),
                )
                self._invalidate_gitea_backends(route.repo)
            except Exception as exc:  # noqa: BLE001
                return _json({"error": f"delete failed: {exc}", "path": path})
            return _json({
                "ok": True,
                "path": norm,
                "deleted": deleted,
                "mode": "prefix",
            })

        _ensure_bucket(self._s3, self._bucket)
        try:
            result = _delete_workspace_path(self._s3, self._bucket, norm)
        except ValueError as exc:
            return _json({"error": str(exc), "path": path})
        return _json(result)


def build_file_tools(ctx: "OrchestratorContext") -> list[Any]:
    store = _WorkspaceFileStore(ctx)

    @tool
    def list_files(path: str = "", recursive: bool = False, limit: int = LIST_FILES_DEFAULT_LIMIT) -> str:
        """List a shallow, bounded workspace directory.

        Defaults to 80 entries. Prefer ``limit<=50`` and the narrowest known
        path. Directories end in ``/``. Use ``agents/<name>`` or
        ``repos/<name>`` for mounted source repos. Use ``recursive=true`` only
        for small trees or an explicit user request.

        Returns JSON with ``{path, recursive, limit, truncated, files}``, where
        each file is ``{path, size, modified_at}``.
        """
        return store.list_files(path, recursive=recursive, limit=limit)

    @tool
    def read_file(path: str) -> str:
        """Read a text file from the user's workspace.

        Paths under ``agents/<name>/`` and ``repos/<name>/`` are read from
        Gitea-backed repos. Other paths stay in MinIO.

        Returns JSON ``{path, content_type, size, content}``. Content is
        truncated at 200KB.
        """
        return store.read_file(path)

    @tool
    def write_file(path: str, content: str) -> str:
        """Create or overwrite a UTF-8 text file in the user's workspace.

        Files under ``agents/<name>/`` or ``repos/<name>/`` are written to
        the corresponding Gitea-backed repo so source changes are committed.

        content: The text to write. Do NOT pass binary placeholders.
        """
        return store.write_file(path, content)

    @tool
    def delete_file(path: str) -> str:
        """Delete a file or directory prefix from the user's workspace.

        Directory prefixes such as ``agents/<name>/`` or ``repos/<name>/``
        delete every object below them. Gitea-backed paths are deleted from
        the corresponding repo.
        """
        return store.delete_file(path)

    @tool
    async def deploy_agent_source(name: str) -> str:
        """Deploy the current managed source repo for ``name``.

        Use this after editing files under ``agents/<name>/``. Source file
        writes are committed to Gitea but intentionally do not deploy until
        this tool is called.

        Returns JSON from the control plane, including ``deploy_id`` when a
        new deployment was queued.
        """
        repo = str(name or "").strip()
        if not repo:
            return _json({"error": "name is required"})
        if not ctx.jwt:
            return _json({
                "error": "deploy_agent_source requires cp_jwt",
            })
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    f"{ctx.settings.cp_url.rstrip('/')}/v1/agents/{repo}/source/deploy",
                    headers={"authorization": f"bearer {ctx.jwt}"},
                )
        except httpx.HTTPError as exc:
            return _json({"error": f"cp unreachable: {exc}"})
        if resp.status_code >= 400:
            return _json({
                "error": f"cp {resp.status_code}",
                "detail": resp.text[:1000],
            })
        return _json(resp.json() if resp.content else {"ok": True})

    return [list_files, read_file, write_file, delete_file, deploy_agent_source]
