"""Gitea-backed DeepAgents backend.

Where ``WorkspaceBackend`` makes the invocation workspace the source of truth,
``GiteaBackend`` does the same for a managed source repository in Gitea. It is
the file substrate for meta-agents (reviewer, patcher, migrator, composer)
that read and modify other agents' source code — every write is a commit, so
the GitOps pipeline (ArgoCD) closes the loop automatically.

The backend is stateless after construction: pass a repo + ref + token and it
exposes the DeepAgents file API over the Gitea REST endpoints. Execution is
intentionally not implemented here — meta-agents should drive ``ctx.sandbox``
directly for tests.
"""
from __future__ import annotations

import asyncio
import base64
import fnmatch
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

try:
    from deepagents.backends.protocol import (
        EditResult,
        FileDownloadResponse,
        FileInfo,
        FileUploadResponse,
        GlobResult,
        GrepMatch,
        GrepResult,
        LsResult,
        ReadResult,
        WriteResult,
    )
except Exception:  # pragma: no cover - exercised only without deepagents
    @dataclass
    class WriteResult:
        error: str | None = None
        path: str | None = None

    @dataclass
    class EditResult:
        error: str | None = None
        path: str | None = None
        occurrences: int | None = None

    @dataclass
    class ReadResult:
        error: str | None = None
        file_data: dict[str, Any] | None = None

    @dataclass
    class LsResult:
        error: str | None = None
        entries: list[dict[str, Any]] | None = None

    @dataclass
    class GlobResult:
        error: str | None = None
        matches: list[dict[str, Any]] | None = None

    @dataclass
    class GrepResult:
        error: str | None = None
        matches: list[dict[str, Any]] | None = None

    @dataclass
    class FileDownloadResponse:
        path: str
        content: bytes | None = None
        error: str | None = None

    @dataclass
    class FileUploadResponse:
        path: str
        error: str | None = None

    FileInfo = dict[str, Any]  # type: ignore[assignment]
    GrepMatch = dict[str, Any]  # type: ignore[assignment]


class GiteaError(RuntimeError):
    """Raised when the Gitea API returns an unexpected error."""


class GiteaAuthError(GiteaError):
    """Raised when the Gitea API rejects the current token."""


@dataclass
class _TreeEntry:
    path: str
    sha: str
    size: int
    is_dir: bool


class GiteaBackend:
    """DeepAgents file backend backed by a Gitea repository.

    Each instance is bound to a single ``(owner, repo, ref)``. ``write`` and
    ``edit`` create commits on that ref. ``create_branch`` and ``create_pr``
    are exposed as extras for migrator/patcher workflows that want a PR-based
    review path instead of direct commits.
    """

    def __init__(
        self,
        gitea_url: str,
        owner: str,
        repo: str,
        *,
        ref: str = "main",
        token: str,
        author_name: str = "a2a-cloud",
        author_email: str = "noreply@a2acloud.io",
        commit_prefix: str = "",
    ) -> None:
        self._base = gitea_url.rstrip("/")
        self._owner = owner
        self._repo = repo
        self._ref = ref
        self._token = token
        self._author = {"name": author_name, "email": author_email}
        self._commit_prefix = commit_prefix
        self._tree_cache: dict[str, list[_TreeEntry]] | None = None
        self._id = f"gitea-{owner}-{repo}-{ref}".replace("/", "-")

    @property
    def id(self) -> str:
        return self._id

    @property
    def ref(self) -> str:
        return self._ref

    def switch_ref(self, ref: str) -> None:
        """Point the backend at a different branch/tag/commit."""
        self._ref = ref
        self._tree_cache = None

    def _api_path(self, *parts: str) -> str:
        suffix = "/".join(p.strip("/") for p in parts)
        return f"{self._base}/api/v1/{suffix}"

    def _norm(self, path: str | None) -> str:
        raw = (path or "").replace("\\", "/")
        return raw.strip("/")

    def _commit_message(self, action: str, path: str) -> str:
        prefix = f"{self._commit_prefix}: " if self._commit_prefix else ""
        return f"{prefix}{action} {path}"

    async def _client(self):  # type: ignore[no-untyped-def]
        import httpx  # late import: server-side has no client

        return httpx.AsyncClient(
            timeout=30.0,
            headers={
                "Authorization": f"token {self._token}",
                "Accept": "application/json",
            },
        )

    async def _request_json(self, method: str, url: str, **kwargs: Any) -> Any:
        async with await self._client() as client:
            resp = await client.request(method, url, **kwargs)
            if resp.status_code == 404:
                return None
            if resp.status_code in (401, 403):
                raise GiteaAuthError(
                    f"{method} {url} -> {resp.status_code}: {resp.text[:300]}"
                )
            if resp.status_code >= 400:
                raise GiteaError(f"{method} {url} -> {resp.status_code}: {resp.text[:300]}")
            if not resp.content:
                return {}
            return resp.json()

    async def _load_tree(self) -> list[_TreeEntry]:
        if self._tree_cache is not None and self._ref in self._tree_cache:
            return self._tree_cache[self._ref]
        url = self._api_path("repos", self._owner, self._repo, "git/trees", self._ref)
        data = await self._request_json("GET", url, params={"recursive": "true"})
        entries: list[_TreeEntry] = []
        if data:
            for item in data.get("tree", []) or []:
                entries.append(
                    _TreeEntry(
                        path=item["path"],
                        sha=item.get("sha", ""),
                        size=int(item.get("size", 0) or 0),
                        is_dir=item.get("type") == "tree",
                    )
                )
        cache = self._tree_cache or {}
        cache[self._ref] = entries
        self._tree_cache = cache
        return entries

    def _invalidate_tree(self) -> None:
        self._tree_cache = None

    async def _list_dir(self, path: str) -> list[FileInfo]:
        """List one repository directory without fetching the recursive tree."""
        norm = self._norm(path)
        url = self._api_path("repos", self._owner, self._repo, "contents", norm)
        data = await self._request_json("GET", url, params={"ref": self._ref})
        if data is None:
            return []
        items = data if isinstance(data, list) else [data]
        entries: list[FileInfo] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            raw_path = str(item.get("path") or item.get("name") or "").strip("/")
            if not raw_path:
                continue
            is_dir = item.get("type") in {"dir", "tree"}
            entries.append(
                {
                    "path": "/" + raw_path + ("/" if is_dir else ""),
                    "is_dir": is_dir,
                    "size": int(item.get("size") or 0),
                    "modified_at": item.get("modified_at") or "",
                }
            )
        return sorted(entries, key=lambda entry: str(entry["path"]))

    async def _get_file_meta(self, path: str) -> dict[str, Any] | None:
        url = self._api_path("repos", self._owner, self._repo, "contents", path)
        return await self._request_json("GET", url, params={"ref": self._ref})

    async def _read_bytes(self, path: str) -> bytes:
        meta = await self._get_file_meta(path)
        if not meta or meta.get("type") != "file":
            raise GiteaError(f"file not found: {path}")
        encoding = meta.get("encoding", "base64")
        content = meta.get("content", "")
        if encoding == "base64":
            return base64.b64decode(content)
        return content.encode("utf-8")

    async def _write_bytes(self, path: str, content: bytes, action: str) -> None:
        existing = await self._get_file_meta(path)
        body: dict[str, Any] = {
            "branch": self._ref,
            "content": base64.b64encode(content).decode("ascii"),
            "message": self._commit_message(action, path),
            "author": self._author,
            "committer": self._author,
        }
        if existing and existing.get("sha"):
            body["sha"] = existing["sha"]
        url = self._api_path("repos", self._owner, self._repo, "contents", path)
        method = "PUT" if existing else "POST"
        await self._request_json(method, url, json=body)
        self._invalidate_tree()

    async def _delete(self, path: str) -> None:
        meta = await self._get_file_meta(path)
        if not meta or not meta.get("sha"):
            raise GiteaError(f"cannot delete missing file: {path}")
        url = self._api_path("repos", self._owner, self._repo, "contents", path)
        body = {
            "branch": self._ref,
            "sha": meta["sha"],
            "message": self._commit_message("remove", path),
            "author": self._author,
            "committer": self._author,
        }
        await self._request_json("DELETE", url, json=body)
        self._invalidate_tree()

    def _file_data(self, data: bytes) -> dict[str, str]:
        now = datetime.now(timezone.utc).isoformat()
        try:
            return {
                "content": data.decode("utf-8"),
                "encoding": "utf-8",
                "created_at": now,
                "modified_at": now,
            }
        except UnicodeDecodeError:
            return {
                "content": base64.b64encode(data).decode("ascii"),
                "encoding": "base64",
                "created_at": now,
                "modified_at": now,
            }

    async def aread(self, file_path: str, offset: int = 0, limit: int = 2000) -> Any:
        path = self._norm(file_path)
        if not path:
            return ReadResult(error=f"File '{file_path}' not found")
        try:
            data = await self._read_bytes(path)
        except GiteaAuthError:
            raise
        except GiteaError:
            return ReadResult(error=f"File '{file_path}' not found")
        file_data = self._file_data(data)
        if file_data["encoding"] == "utf-8":
            lines = file_data["content"].splitlines()
            if offset or limit:
                file_data["content"] = "\n".join(lines[offset: offset + limit])
        return ReadResult(file_data=file_data)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> Any:
        return _run_sync(self.aread(file_path, offset=offset, limit=limit))

    async def awrite(self, file_path: str, content: str) -> Any:
        path = self._norm(file_path)
        if not path:
            return WriteResult(error="cannot write repo root")
        try:
            existing = await self._get_file_meta(path)
        except GiteaAuthError:
            raise
        if existing:
            return WriteResult(
                error=(
                    f"Cannot write to {file_path} because it already exists. "
                    "Read and then edit it, or write to a new path."
                )
            )
        try:
            await self._write_bytes(path, content.encode("utf-8"), action="add")
        except GiteaAuthError:
            raise
        except GiteaError as exc:
            return WriteResult(error=str(exc))
        return WriteResult(path="/" + path)

    def write(self, file_path: str, content: str) -> Any:
        return _run_sync(self.awrite(file_path, content))

    async def aedit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> Any:
        path = self._norm(file_path)
        if not path:
            return EditResult(error=f"Error: File '{file_path}' not found")
        try:
            data = await self._read_bytes(path)
        except GiteaAuthError:
            raise
        except GiteaError:
            return EditResult(error=f"Error: File '{file_path}' not found")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return EditResult(error=f"Error: File '{file_path}' is not UTF-8 text")
        occurrences = text.count(old_string)
        if occurrences == 0:
            return EditResult(error="Error: old_string not found")
        if occurrences > 1 and not replace_all:
            return EditResult(error="Error: old_string is not unique")
        new_text = text.replace(old_string, new_string, -1 if replace_all else 1)
        try:
            await self._write_bytes(path, new_text.encode("utf-8"), action="edit")
        except GiteaAuthError:
            raise
        except GiteaError as exc:
            return EditResult(error=str(exc))
        return EditResult(path="/" + path, occurrences=occurrences if replace_all else 1)

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> Any:
        return _run_sync(self.aedit(file_path, old_string, new_string, replace_all))

    async def als(self, path: str) -> Any:
        try:
            entries = await self._list_dir(path)
        except GiteaAuthError:
            raise
        except GiteaError as exc:
            return LsResult(error=str(exc))
        return LsResult(entries=entries)

    def ls(self, path: str) -> Any:
        return _run_sync(self.als(path))

    async def aglob(self, pattern: str, path: str = "/") -> Any:
        base = self._norm(path)
        try:
            tree = await self._load_tree()
        except GiteaAuthError:
            raise
        except GiteaError as exc:
            return GlobResult(error=str(exc))
        matches: list[FileInfo] = []
        for item in tree:
            if item.is_dir:
                continue
            if base and not item.path.startswith(base.rstrip("/") + "/"):
                continue
            rel = item.path[len(base.strip("/")):].lstrip("/") if base else item.path
            if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(item.path, pattern):
                matches.append({
                    "path": "/" + item.path,
                    "is_dir": False,
                    "size": item.size,
                    "modified_at": "",
                })
        return GlobResult(matches=matches)

    def glob(self, pattern: str, path: str = "/") -> Any:
        return _run_sync(self.aglob(pattern, path))

    async def agrep(self, pattern: str, path: str | None = None, glob: str | None = None) -> Any:
        base = self._norm(path)
        try:
            tree = await self._load_tree()
        except GiteaAuthError:
            raise
        except GiteaError as exc:
            return GrepResult(error=str(exc))
        matches: list[GrepMatch] = []
        base_is_file = bool(
            base and any(not item.is_dir and item.path == base for item in tree)
        )
        for item in tree:
            if item.is_dir:
                continue
            if base:
                if base_is_file:
                    if item.path != base:
                        continue
                elif not item.path.startswith(base.rstrip("/") + "/"):
                    continue
            if glob and not fnmatch.fnmatch(item.path, glob):
                continue
            try:
                data = await self._read_bytes(item.path)
                text = data.decode("utf-8")
            except (GiteaError, UnicodeDecodeError):
                continue
            for idx, line in enumerate(text.splitlines(), start=1):
                if pattern in line:
                    matches.append({"path": "/" + item.path, "line": idx, "text": line})
        return GrepResult(matches=matches)

    def grep(self, pattern: str, path: str | None = None, glob: str | None = None) -> Any:
        return _run_sync(self.agrep(pattern, path=path, glob=glob))

    async def aupload_files(self, files: list[tuple[str, bytes]]) -> list[Any]:
        responses: list[Any] = []
        for file_path, content in files:
            path = self._norm(file_path)
            if not path:
                responses.append(FileUploadResponse(path=file_path, error="invalid_path"))
                continue
            try:
                await self._write_bytes(path, content, action="upload")
                responses.append(FileUploadResponse(path="/" + path, error=None))
            except GiteaAuthError:
                raise
            except GiteaError as exc:
                responses.append(FileUploadResponse(path=file_path, error=str(exc)))
        return responses

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[Any]:
        return _run_sync(self.aupload_files(files))

    async def adownload_files(self, paths: list[str]) -> list[Any]:
        responses: list[Any] = []
        for file_path in paths:
            path = self._norm(file_path)
            if not path:
                responses.append(
                    FileDownloadResponse(path=file_path, content=None, error="invalid_path")
                )
                continue
            try:
                data = await self._read_bytes(path)
                responses.append(FileDownloadResponse(path="/" + path, content=data, error=None))
            except GiteaAuthError:
                raise
            except GiteaError:
                responses.append(
                    FileDownloadResponse(path=file_path, content=None, error="file_not_found")
                )
        return responses

    def download_files(self, paths: list[str]) -> list[Any]:
        return _run_sync(self.adownload_files(paths))

    async def acreate_branch(self, name: str, from_ref: str | None = None) -> dict[str, Any]:
        url = self._api_path("repos", self._owner, self._repo, "branches")
        body = {"new_branch_name": name, "old_branch_name": from_ref or self._ref}
        result = await self._request_json("POST", url, json=body)
        self._invalidate_tree()
        return result or {}

    async def acreate_pr(
        self,
        title: str,
        head: str,
        base: str = "main",
        body: str = "",
    ) -> dict[str, Any]:
        url = self._api_path("repos", self._owner, self._repo, "pulls")
        payload = {"title": title, "head": head, "base": base, "body": body}
        return await self._request_json("POST", url, json=payload) or {}

    async def aadd_commit_comment(
        self,
        sha: str,
        body: str,
        *,
        path: str | None = None,
        line: int | None = None,
    ) -> dict[str, Any]:
        url = self._api_path("repos", self._owner, self._repo, "commits", sha, "comments")
        payload: dict[str, Any] = {"body": body}
        if path:
            payload["path"] = path
        if line is not None:
            payload["line"] = line
        return await self._request_json("POST", url, json=payload) or {}


def _run_sync(coro: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    raise RuntimeError(
        "GiteaBackend sync methods unavailable inside a running event loop; use the async variants"
    )


__all__ = ["GiteaBackend", "GiteaAuthError", "GiteaError"]
