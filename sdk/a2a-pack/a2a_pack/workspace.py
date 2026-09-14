"""Workspace capability negotiation.

Agents never receive a filesystem path. They negotiate a *view* by intent::

    view = await ctx.workspace.open_view(
        purpose="Fix failing payment test",
        hints=["payment", "checkout"],
        file_types=["python"],
        max_files=10,
        mode=WorkspaceMode.READ_WRITE_OVERLAY,
    )
    for path in view.files:
        content = await view.read(path)

The runtime resolves the request (semantic search + dependency graph + git
metadata + policy + optional human approval) and returns a bounded grant.
Writes are staged as :class:`WorkspacePatch` objects, never applied directly
to the host filesystem from inside the sandbox.
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from datetime import datetime
from enum import Enum
from fnmatch import fnmatch
from mimetypes import guess_type
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Literal, Sequence
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, NonNegativeInt, PositiveInt

if TYPE_CHECKING:
    from .grants import Grant


class WorkspaceMode(str, Enum):
    READ_ONLY = "read_only"
    READ_WRITE_OVERLAY = "read_write_overlay"  # writes staged as patches
    READ_WRITE_DIRECT = "read_write_direct"  # discouraged; needs explicit policy


class FileType(str, Enum):
    PYTHON = "python"
    TYPESCRIPT = "typescript"
    JAVASCRIPT = "javascript"
    YAML = "yaml"
    JSON = "json"
    TOML = "toml"
    MARKDOWN = "markdown"
    SQL = "sql"
    SHELL = "shell"
    OTHER = "other"


class FileMatch(BaseModel):
    """Result row from :meth:`WorkspaceClient.search`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    file_type: FileType
    score: float = 0.0
    summary: str | None = None
    size_bytes: NonNegativeInt = 0


class UploadedFile(BaseModel):
    """Workspace-backed file staged for a skill invocation.

    Agent API multipart uploads are persisted into the caller workspace before
    the skill runs. The handler receives this metadata object and reads bytes
    through ``ctx.workspace`` using ``path``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    filename: str
    media_type: str
    size_bytes: NonNegativeInt = 0


class FileUpload:
    """Schema marker for a required or optional file upload parameter.

    Use with ``typing.Annotated``::

        document: Annotated[UploadedFile, FileUpload(accept=["application/pdf"])]

    The marker is carried as ``x-a2a-file-upload`` in the skill input schema so
    OpenAPI exporters can render the field as ``multipart/form-data`` while the
    runtime still invokes the skill with JSON-safe ``UploadedFile`` metadata.
    """

    def __init__(
        self,
        *,
        accept: Sequence[str] = (),
        max_bytes: int | None = None,
        multiple: bool = False,
        description: str | None = None,
    ) -> None:
        self.accept = tuple(str(item).strip() for item in accept if str(item).strip())
        self.max_bytes = max_bytes
        self.multiple = bool(multiple)
        self.description = description

    def schema_extra(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"required_upload": True}
        if self.accept:
            payload["accept"] = list(self.accept)
        if self.max_bytes is not None:
            payload["max_bytes"] = int(self.max_bytes)
        if self.multiple:
            payload["multiple"] = True
        if self.description:
            payload["description"] = self.description
        return {"x-a2a-file-upload": payload}


class WorkspaceGrant(BaseModel):
    """An approved access grant for a bounded set of files."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    grant_id: str
    purpose: str
    files: tuple[FileMatch, ...]
    mode: WorkspaceMode
    reason: str
    expires_at: datetime | None = None
    requires_human_approval: bool = False


class WorkspacePatch(BaseModel):
    """A staged write. Not applied until the runtime/approver commits it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    grant_id: str
    path: str
    operation: Literal["create", "update", "delete"]
    content: bytes | None = None  # bytes for create/update, None for delete


class WorkspaceAccess(BaseModel):
    """Class-level workspace policy.

    Use :meth:`none` for agents that do not touch any workspace, or
    :meth:`dynamic` to allow capability negotiation under bounds.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    max_files: NonNegativeInt = 0
    allowed_modes: tuple[WorkspaceMode, ...] = ()
    require_reason: bool = True
    deny_patterns: tuple[str, ...] = ()
    require_human_approval: bool = False
    max_total_size_bytes: PositiveInt = 100 * 1024 * 1024

    @classmethod
    def none(cls) -> "WorkspaceAccess":
        return cls(enabled=False)

    @classmethod
    def dynamic(
        cls,
        *,
        max_files: int = 25,
        allowed_modes: Sequence[WorkspaceMode] = (WorkspaceMode.READ_ONLY,),
        require_reason: bool = True,
        deny_patterns: Sequence[str] = (),
        require_human_approval: bool = False,
        max_total_size_bytes: int = 100 * 1024 * 1024,
    ) -> "WorkspaceAccess":
        return cls(
            enabled=True,
            max_files=max_files,
            allowed_modes=tuple(allowed_modes),
            require_reason=require_reason,
            deny_patterns=tuple(deny_patterns),
            require_human_approval=require_human_approval,
            max_total_size_bytes=max_total_size_bytes,
        )


class WorkspaceDenied(PermissionError):
    """Raised when a workspace request violates the agent's policy."""


def _normalize_write_prefixes(
    outputs_prefix: str | None = None,
    write_prefixes: Sequence[str] = (),
) -> tuple[str, ...]:
    prefixes: list[str] = []
    values: list[str] = []
    if outputs_prefix:
        values.append(outputs_prefix)
    values.extend(write_prefixes)
    for value in values:
        clean = str(value).replace("\\", "/").strip("/")
        if not clean:
            continue
        normalized = clean + "/"
        if normalized not in prefixes:
            prefixes.append(normalized)
    return tuple(prefixes)


def _merge_patterns(
    inherited: Sequence[str],
    requested: Sequence[str],
) -> tuple[str, ...]:
    out: list[str] = []
    for pattern in (*tuple(inherited), *tuple(requested)):
        if pattern and pattern not in out:
            out.append(pattern)
    return tuple(out)


def _matches_write_prefix(path: str, write_prefixes: Sequence[str]) -> bool:
    clean = path.replace("\\", "/").strip("/")
    for prefix in write_prefixes:
        normalized = prefix.replace("\\", "/").strip("/")
        if normalized and (clean == normalized or clean.startswith(normalized + "/")):
            return True
    return False


class WorkspaceView(ABC):
    """A bounded view over a granted set of files.

    Returned by :meth:`WorkspaceClient.open_view`. Reads always go to the
    granted view; writes return :class:`WorkspacePatch` objects that the
    runtime will commit (or reject) outside the sandbox.
    """

    grant: WorkspaceGrant

    @property
    def files(self) -> tuple[FileMatch, ...]:
        return self.grant.files

    @abstractmethod
    async def read(self, path: str) -> bytes: ...

    @abstractmethod
    async def write(self, path: str, content: bytes) -> WorkspacePatch: ...

    @abstractmethod
    async def delete(self, path: str) -> WorkspacePatch: ...

    @abstractmethod
    async def patches(self) -> tuple[WorkspacePatch, ...]: ...


class WorkspaceClient(ABC):
    """Negotiation surface handed to the agent via ``ctx.workspace``.

    The concrete implementation is provided by the runtime; agents must
    program against this interface only.
    """

    @abstractmethod
    async def search(
        self,
        *,
        query: str,
        types: Sequence[FileType] = (),
        limit: int = 20,
    ) -> list[FileMatch]: ...

    @abstractmethod
    async def request_access(
        self,
        *,
        files: Sequence[FileMatch | str],
        mode: WorkspaceMode,
        reason: str,
        purpose: str = "",
    ) -> WorkspaceGrant: ...

    @abstractmethod
    async def open_view(
        self,
        *,
        purpose: str,
        hints: Sequence[str] = (),
        file_types: Sequence[FileType] = (),
        max_files: int = 10,
        mode: WorkspaceMode = WorkspaceMode.READ_ONLY,
        reason: str | None = None,
    ) -> WorkspaceView: ...

    @abstractmethod
    async def list_grants(self) -> list[WorkspaceGrant]: ...

    def install_grant(self, grant: "Grant") -> None:  # noqa: F821
        """Replace this client's access policy with a superseding grant.

        Called by :meth:`RunContext.request_scope` after the platform mints a
        new grant covering additional read patterns / longer TTL / new write
        prefix. Subsequent reads/writes use the new policy.

        Default impl raises — concrete clients (LocalWorkspaceClient or the
        runtime's MinIO-backed variant) override.
        """
        raise NotImplementedError(
            "this WorkspaceClient does not support runtime grant install"
        )

    async def delegate(
        self,
        *,
        audience: str,
        allow_patterns: Sequence[str] = ("**",),
        deny_patterns: Sequence[str] = (),
        mode: WorkspaceMode = WorkspaceMode.READ_ONLY,
        outputs_prefix: str | None = None,
        write_prefixes: Sequence[str] = (),
        source_grants: Sequence[Any] = (),
        ttl_seconds: int = 300,
    ) -> str:
        """Mint a signed grant token the caller can hand to ``ctx.call``.

        The default implementation requires the workspace to expose
        ``self.bucket`` and ``self.issuer`` — override in concrete clients
        that don't fit that shape.
        """
        from .grants import Grant, delegate_grant, mint_grant

        bucket = getattr(self, "bucket", None) or getattr(self, "_bucket", None)
        if bucket is None:
            raise NotImplementedError(
                "this WorkspaceClient does not expose a bucket; override delegate()"
            )
        issuer = getattr(self, "issuer", "self")
        parent = getattr(self, "current_grant", None)
        if not isinstance(parent, Grant):
            parent = getattr(self, "_current_grant", None)
        access = getattr(self, "_access", None)
        inherited_denies = tuple(getattr(access, "deny_patterns", ()) or ())
        child_denies = _merge_patterns(inherited_denies, tuple(deny_patterns))
        if isinstance(parent, Grant):
            _, token = delegate_grant(
                parent,
                issuer=issuer,
                audience=audience,
                bucket=bucket,
                mode=mode,
                allow_patterns=tuple(allow_patterns),
                deny_patterns=child_denies,
                outputs_prefix=outputs_prefix,
                write_prefixes=tuple(write_prefixes),
                source_grants=tuple(source_grants),
                ttl_seconds=ttl_seconds,
            )
            return token
        if getattr(access, "enabled", False):
            allowed_modes = tuple(getattr(access, "allowed_modes", ()) or ())
            if allowed_modes and mode not in allowed_modes:
                raise WorkspaceDenied(f"mode {mode.value!r} not in allowed_modes")
        _, token = mint_grant(
            issuer=issuer,
            audience=audience,
            bucket=bucket,
            mode=mode,
            allow_patterns=tuple(allow_patterns),
            deny_patterns=child_denies,
            outputs_prefix=outputs_prefix,
            write_prefixes=tuple(write_prefixes),
            source_grants=tuple(source_grants),
            ttl_seconds=ttl_seconds,
        )
        return token


# ---------------------------------------------------------------------------
# Local in-memory implementation, for dev/tests.
# ---------------------------------------------------------------------------


class LocalWorkspaceView(WorkspaceView):
    def __init__(
        self,
        grant: WorkspaceGrant,
        client: "LocalWorkspaceClient",
    ) -> None:
        self.grant = grant
        self._client = client
        self._patches: list[WorkspacePatch] = []
        self._granted_paths = {f.path for f in grant.files}

    def _check(self, path: str) -> None:
        if path not in self._granted_paths:
            raise WorkspaceDenied(
                f"path {path!r} not in grant {self.grant.grant_id}"
            )

    async def read(self, path: str) -> bytes:
        self._check(path)
        return self._client._files[path]

    async def write(self, path: str, content: bytes) -> WorkspacePatch:
        if self.grant.mode is WorkspaceMode.READ_ONLY:
            raise WorkspaceDenied(f"grant is read-only: {self.grant.grant_id}")
        self._check(path)
        op: Literal["create", "update"] = (
            "create" if path not in self._client._files else "update"
        )
        if getattr(self._client, "_apply_patches_directly", False):
            self._client.write_bytes(path, content)
        patch = WorkspacePatch(
            grant_id=self.grant.grant_id,
            path=path,
            operation=op,
            content=content,
        )
        self._patches.append(patch)
        return patch

    async def delete(self, path: str) -> WorkspacePatch:
        if self.grant.mode is WorkspaceMode.READ_ONLY:
            raise WorkspaceDenied(f"grant is read-only: {self.grant.grant_id}")
        self._check(path)
        if getattr(self._client, "_apply_patches_directly", False):
            self._client.delete_path(path)
        patch = WorkspacePatch(
            grant_id=self.grant.grant_id,
            path=path,
            operation="delete",
            content=None,
        )
        self._patches.append(patch)
        return patch

    async def patches(self) -> tuple[WorkspacePatch, ...]:
        return tuple(self._patches)


class LocalWorkspaceClient(WorkspaceClient):
    """In-memory workspace for local dev and tests.

    Search is naive substring match; ranking is keyword-overlap. Real
    runtime implementations replace this with embeddings/dep-graph search.
    Policy enforcement (:class:`WorkspaceAccess`) IS applied here so tests
    cover the rejection paths.
    """

    _EXT_TO_TYPE: dict[str, FileType] = {
        ".py": FileType.PYTHON,
        ".ts": FileType.TYPESCRIPT,
        ".tsx": FileType.TYPESCRIPT,
        ".js": FileType.JAVASCRIPT,
        ".jsx": FileType.JAVASCRIPT,
        ".yaml": FileType.YAML,
        ".yml": FileType.YAML,
        ".json": FileType.JSON,
        ".toml": FileType.TOML,
        ".md": FileType.MARKDOWN,
        ".sql": FileType.SQL,
        ".sh": FileType.SHELL,
    }

    def __init__(
        self,
        files: dict[str, bytes],
        *,
        access: WorkspaceAccess,
        bucket: str = "local",
        issuer: str = "local",
    ) -> None:
        self._files: dict[str, bytes] = dict(files)
        self._access = access
        self._grants: dict[str, WorkspaceGrant] = {}
        self._counter = 0
        # Expose bucket+issuer so the default WorkspaceClient.delegate() works.
        self.bucket = bucket
        self.issuer = issuer
        # Capability state derived from the current platform grant. Updated
        # in install_grant when the platform supersedes the original grant.
        self.allow_patterns: tuple[str, ...] = ("**",)
        self.outputs_prefix: str | None = None
        self.write_prefixes: tuple[str, ...] = ()
        self.current_mode: WorkspaceMode | None = None
        self.current_grant_id: str | None = None
        self.current_expires_at: int = 0
        self.current_grant: Grant | None = None
        self._apply_patches_directly = False

    def install_grant(self, grant: "Grant") -> None:  # noqa: F821
        # WorkspaceAccess is frozen; rebuild rather than mutate.
        self._access = WorkspaceAccess(
            enabled=True,
            max_files=self._access.max_files,
            allowed_modes=self._access.allowed_modes,
            require_reason=self._access.require_reason,
            deny_patterns=tuple(grant.deny_patterns),
            require_human_approval=self._access.require_human_approval,
            max_total_size_bytes=self._access.max_total_size_bytes,
        )
        self.allow_patterns = tuple(grant.allow_patterns)
        self.outputs_prefix = grant.outputs_prefix
        self.write_prefixes = _normalize_write_prefixes(
            grant.outputs_prefix,
            grant.write_prefixes,
        )
        self.current_mode = grant.mode
        self.current_grant_id = grant.grant_id
        self.current_expires_at = grant.expires_at
        self.current_grant = grant

    def is_writable_output(self, path: str) -> bool:
        if self.write_prefixes:
            return _matches_write_prefix(path, self.write_prefixes)
        if self.outputs_prefix is None:
            return not self._denied(path)
        return _matches_write_prefix(path, (self.outputs_prefix,))

    def exists(self, path: str) -> bool:
        return path in self._files

    def read_bytes(self, path: str) -> bytes:
        return self._files[path]

    def write_bytes(self, path: str, content: bytes) -> None:
        self._files[path] = content

    def delete_path(self, path: str) -> None:
        self._files.pop(path, None)

    def iter_paths(self) -> Iterable[str]:
        return iter(sorted(self._files))

    def _detect(self, path: str) -> FileType:
        for ext, ft in self._EXT_TO_TYPE.items():
            if path.endswith(ext):
                return ft
        return FileType.OTHER

    def _denied(self, path: str) -> bool:
        for pat in self._access.deny_patterns:
            regex = re.compile(_glob_to_regex(pat))
            if regex.fullmatch(path):
                return True
        return False

    async def search(
        self,
        *,
        query: str,
        types: Sequence[FileType] = (),
        limit: int = 20,
    ) -> list[FileMatch]:
        if not self._access.enabled:
            raise WorkspaceDenied("workspace disabled by policy")
        type_set = set(types)
        terms = [t.lower() for t in re.split(r"\W+", query) if t]
        out: list[FileMatch] = []
        for path, data in self._files.items():
            if self._denied(path):
                continue
            ft = self._detect(path)
            if type_set and ft not in type_set:
                continue
            haystack = (path + "\n" + data.decode("utf-8", errors="ignore")).lower()
            score = sum(haystack.count(t) for t in terms)
            if score == 0:
                continue
            out.append(
                FileMatch(
                    path=path,
                    file_type=ft,
                    score=float(score),
                    size_bytes=len(data),
                )
            )
        out.sort(key=lambda m: -m.score)
        return out[:limit]

    async def request_access(
        self,
        *,
        files: Sequence[FileMatch | str],
        mode: WorkspaceMode,
        reason: str,
        purpose: str = "",
    ) -> WorkspaceGrant:
        if not self._access.enabled:
            raise WorkspaceDenied("workspace disabled by policy")
        if mode not in self._access.allowed_modes:
            raise WorkspaceDenied(f"mode {mode.value!r} not in allowed_modes")
        if self._access.require_reason and not reason.strip():
            raise WorkspaceDenied("reason required by policy")
        normalized: list[FileMatch] = []
        for f in files:
            if isinstance(f, str):
                if f not in self._files:
                    raise WorkspaceDenied(f"unknown path: {f!r}")
                normalized.append(
                    FileMatch(
                        path=f,
                        file_type=self._detect(f),
                        size_bytes=len(self._files[f]),
                    )
                )
            else:
                if f.path not in self._files:
                    raise WorkspaceDenied(f"unknown path: {f.path!r}")
                normalized.append(f)
        if len(normalized) > self._access.max_files:
            raise WorkspaceDenied(
                f"requested {len(normalized)} files, max_files={self._access.max_files}"
            )
        for m in normalized:
            if self._denied(m.path):
                raise WorkspaceDenied(f"path denied by policy: {m.path}")
        total = sum(m.size_bytes for m in normalized)
        if total > self._access.max_total_size_bytes:
            raise WorkspaceDenied(
                f"total size {total} exceeds max_total_size_bytes"
            )
        self._counter += 1
        grant = WorkspaceGrant(
            grant_id=f"grant-{self._counter}",
            purpose=purpose,
            files=tuple(normalized),
            mode=mode,
            reason=reason,
            requires_human_approval=self._access.require_human_approval,
        )
        self._grants[grant.grant_id] = grant
        return grant

    async def open_view(
        self,
        *,
        purpose: str,
        hints: Sequence[str] = (),
        file_types: Sequence[FileType] = (),
        max_files: int = 10,
        mode: WorkspaceMode = WorkspaceMode.READ_ONLY,
        reason: str | None = None,
    ) -> WorkspaceView:
        query = " ".join([purpose, *hints])
        matches = await self.search(query=query, types=file_types, limit=max_files * 3)
        chosen = matches[:max_files]
        grant = await self.request_access(
            files=chosen,
            mode=mode,
            reason=reason or purpose,
            purpose=purpose,
        )
        return LocalWorkspaceView(grant, self)

    async def list_grants(self) -> list[WorkspaceGrant]:
        return list(self._grants.values())


class FileSystemWorkspaceClient(LocalWorkspaceClient):
    """Local workspace backed by a directory on disk.

    Used by ``a2a dev`` / ``a2a test`` so framework file tools and direct
    ``ctx.workspace`` writes are visible on the developer's machine before
    the agent is uploaded.
    """

    def __init__(
        self,
        root: Path | str,
        *,
        access: WorkspaceAccess,
        bucket: str = "local",
        issuer: str = "local-dev",
        allow_patterns: Sequence[str] = ("**",),
        outputs_prefix: str | None = "outputs",
        write_prefixes: Sequence[str] = (),
        mode: WorkspaceMode = WorkspaceMode.READ_WRITE_OVERLAY,
    ) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        super().__init__(
            _read_workspace_files(self.root),
            access=access,
            bucket=bucket,
            issuer=issuer,
        )
        self.allow_patterns = tuple(allow_patterns)
        self.outputs_prefix = outputs_prefix
        self.write_prefixes = _normalize_write_prefixes(outputs_prefix, write_prefixes)
        self.current_mode = mode
        self.current_grant_id = "local-dev"
        self._apply_patches_directly = True
        for prefix in self.write_prefixes:
            (self.root / prefix.strip("/")).mkdir(parents=True, exist_ok=True)

    def write_bytes(self, path: str, content: bytes) -> None:
        clean = _safe_relative_path(path)
        super().write_bytes(clean, content)
        target = self.root / clean
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    def delete_path(self, path: str) -> None:
        clean = _safe_relative_path(path)
        super().delete_path(clean)
        target = self.root / clean
        try:
            target.unlink()
        except FileNotFoundError:
            return


# ---------------------------------------------------------------------------
# MinIO/S3-backed implementation for deployed handoffs.
# ---------------------------------------------------------------------------


def _read_workspace_files(root: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    if not root.exists():
        return files
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel.startswith(".git/") or "/.git/" in rel:
            continue
        files[rel] = path.read_bytes()
    return files


def _safe_relative_path(path: str) -> str:
    clean = path.replace("\\", "/").strip("/")
    if not clean or clean.startswith("../") or "/../" in clean or clean == "..":
        raise WorkspaceDenied(f"unsafe workspace path: {path!r}")
    return clean


class MinIOWorkspaceView(WorkspaceView):
    def __init__(
        self,
        grant: WorkspaceGrant,
        client: "MinIOWorkspaceClient",
    ) -> None:
        self.grant = grant
        self._client = client
        self._patches: list[WorkspacePatch] = []
        self._granted_paths = {f.path for f in grant.files}

    def _check_read(self, path: str) -> None:
        if path not in self._granted_paths:
            raise WorkspaceDenied(
                f"path {path!r} not in grant {self.grant.grant_id}"
            )

    def _check_write(self, path: str) -> None:
        if self.grant.mode is WorkspaceMode.READ_ONLY:
            raise WorkspaceDenied(f"grant is read-only: {self.grant.grant_id}")
        if path not in self._granted_paths and not self._client.is_writable_output(path):
            raise WorkspaceDenied(
                f"path {path!r} not in grant {self.grant.grant_id} or write_prefixes"
            )

    async def read(self, path: str) -> bytes:
        self._check_read(path)
        return self._client.read_bytes(path)

    async def write(self, path: str, content: bytes) -> WorkspacePatch:
        self._check_write(path)
        operation: Literal["create", "update"] = (
            "update" if self._client.exists(path) else "create"
        )
        self._client.write_bytes(path, content)
        patch = WorkspacePatch(
            grant_id=self.grant.grant_id,
            path=path,
            operation=operation,
            content=content,
        )
        self._patches.append(patch)
        return patch

    async def delete(self, path: str) -> WorkspacePatch:
        self._check_write(path)
        self._client.delete_path(path)
        patch = WorkspacePatch(
            grant_id=self.grant.grant_id,
            path=path,
            operation="delete",
            content=None,
        )
        self._patches.append(patch)
        return patch

    async def patches(self) -> tuple[WorkspacePatch, ...]:
        return tuple(self._patches)


class ControlPlaneWorkspaceClient(WorkspaceClient):
    """Workspace client backed by grant-scoped control-plane file routes."""

    _EXT_TO_TYPE = LocalWorkspaceClient._EXT_TO_TYPE

    def __init__(
        self,
        *,
        bucket: str,
        control_plane_url: str,
        grant_token: str,
        access: WorkspaceAccess,
        issuer: str = "control-plane-runtime",
    ) -> None:
        self.bucket = bucket
        self.issuer = issuer
        self._base_url = control_plane_url.rstrip("/")
        self._grant_token = grant_token
        self._access = access
        self._grants: dict[str, WorkspaceGrant] = {}
        self._counter = 0
        self.allow_patterns: tuple[str, ...] = ("**",)
        self.outputs_prefix: str | None = None
        self.write_prefixes: tuple[str, ...] = ()
        self.current_mode: WorkspaceMode | None = None
        self.current_grant_id: str | None = None
        self.current_expires_at: int = 0
        self.current_grant: Grant | None = None

    def install_grant(self, grant: "Grant") -> None:  # noqa: F821
        self._install_grant(grant)

    def install_grant_token(self, grant: "Grant", token: str) -> None:  # noqa: F821
        self._grant_token = token
        self._install_grant(grant)

    def _install_grant(self, grant: "Grant") -> None:  # noqa: F821
        self._access = WorkspaceAccess(
            enabled=True,
            max_files=self._access.max_files,
            allowed_modes=self._access.allowed_modes,
            require_reason=self._access.require_reason,
            deny_patterns=tuple(grant.deny_patterns),
            require_human_approval=self._access.require_human_approval,
            max_total_size_bytes=self._access.max_total_size_bytes,
        )
        self.allow_patterns = tuple(grant.allow_patterns or ("**",))
        self.outputs_prefix = grant.outputs_prefix
        self.write_prefixes = _normalize_write_prefixes(
            grant.outputs_prefix,
            grant.write_prefixes,
        )
        self.current_mode = grant.mode
        self.current_grant_id = grant.grant_id
        self.current_expires_at = grant.expires_at
        self.current_grant = grant

    def _headers(self) -> dict[str, str]:
        return {"X-A2A-Grant": self._grant_token}

    def _url(self, path: str = "") -> str:
        suffix = quote(path.lstrip("/"), safe="/")
        return f"{self._base_url}/v1/workspace-grants/{suffix}"

    def _detect(self, path: str) -> FileType:
        for ext, ft in self._EXT_TO_TYPE.items():
            if path.endswith(ext):
                return ft
        return FileType.OTHER

    def _allowed(self, path: str) -> bool:
        allowed = any(fnmatch(path, pat) for pat in self.allow_patterns)
        denied = any(fnmatch(path, pat) for pat in self._access.deny_patterns)
        return allowed and not denied

    def is_writable_output(self, path: str) -> bool:
        if self.write_prefixes:
            return _matches_write_prefix(path, self.write_prefixes)
        if self.outputs_prefix is None:
            return self._allowed(path)
        return _matches_write_prefix(path, (self.outputs_prefix,))

    def exists(self, path: str) -> bool:
        import httpx

        with httpx.Client(timeout=30.0) as client:
            response = client.head(self._url(f"files/{path}"), headers=self._headers())
        if response.status_code == 404:
            return False
        if response.status_code >= 400:
            raise WorkspaceDenied(f"workspace stat failed: {response.status_code}")
        return True

    def read_bytes(self, path: str) -> bytes:
        import httpx

        with httpx.Client(timeout=60.0) as client:
            response = client.get(self._url(f"files/{path}"), headers=self._headers())
        if response.status_code == 404:
            raise FileNotFoundError(path)
        if response.status_code >= 400:
            raise WorkspaceDenied(f"workspace read failed: {response.status_code}")
        return response.content

    def write_bytes(self, path: str, content: bytes) -> None:
        import httpx

        with httpx.Client(timeout=60.0) as client:
            response = client.put(
                self._url(f"files/{path}"),
                headers={**self._headers(), "Content-Type": "application/octet-stream"},
                content=content,
            )
        if response.status_code >= 400:
            raise WorkspaceDenied(f"workspace write failed: {response.status_code}")

    def delete_path(self, path: str) -> None:
        import httpx

        with httpx.Client(timeout=60.0) as client:
            response = client.delete(self._url(f"files/{path}"), headers=self._headers())
        if response.status_code not in {204, 404} and response.status_code >= 400:
            raise WorkspaceDenied(f"workspace delete failed: {response.status_code}")

    def iter_paths(self) -> Iterable[str]:
        import httpx

        with httpx.Client(timeout=60.0) as client:
            response = client.get(self._url("files"), headers=self._headers())
        if response.status_code >= 400:
            raise WorkspaceDenied(f"workspace list failed: {response.status_code}")
        for item in response.json():
            path = item.get("path") if isinstance(item, dict) else None
            if isinstance(path, str):
                yield path

    def _head_match(self, path: str) -> FileMatch:
        import httpx

        with httpx.Client(timeout=30.0) as client:
            response = client.head(self._url(f"files/{path}"), headers=self._headers())
        if response.status_code == 404:
            raise FileNotFoundError(path)
        if response.status_code >= 400:
            raise WorkspaceDenied(f"workspace stat failed: {response.status_code}")
        return FileMatch(
            path=path,
            file_type=self._detect(path),
            size_bytes=int(response.headers.get("X-A2A-File-Size") or 0),
        )

    async def search(
        self,
        *,
        query: str,
        types: Sequence[FileType] = (),
        limit: int = 20,
    ) -> list[FileMatch]:
        if not self._access.enabled:
            raise WorkspaceDenied("workspace disabled by policy")
        type_set = set(types)
        terms = [t.lower() for t in re.split(r"\W+", query) if t]
        out: list[FileMatch] = []
        for path in self.iter_paths():
            if not self._allowed(path):
                continue
            ft = self._detect(path)
            if type_set and ft not in type_set:
                continue
            haystack = path.lower()
            score = sum(haystack.count(t) for t in terms) if terms else 1
            if score == 0:
                continue
            try:
                match = self._head_match(path)
            except FileNotFoundError:
                continue
            out.append(match.model_copy(update={"score": float(score)}))
        out.sort(key=lambda m: -m.score)
        return out[:limit]

    async def request_access(
        self,
        *,
        files: Sequence[FileMatch | str],
        mode: WorkspaceMode,
        reason: str,
        purpose: str = "",
    ) -> WorkspaceGrant:
        if not self._access.enabled:
            raise WorkspaceDenied("workspace disabled by policy")
        if mode not in self._access.allowed_modes:
            raise WorkspaceDenied(f"mode {mode.value!r} not in allowed_modes")
        if self._access.require_reason and not reason.strip():
            raise WorkspaceDenied("reason required by policy")
        normalized: list[FileMatch] = []
        for f in files:
            path = f if isinstance(f, str) else f.path
            if not self._allowed(path) and not (
                mode is not WorkspaceMode.READ_ONLY and self.is_writable_output(path)
            ):
                raise WorkspaceDenied(f"path denied by grant: {path}")
            if isinstance(f, FileMatch):
                normalized.append(f)
            elif self.exists(path):
                normalized.append(self._head_match(path))
            elif mode is not WorkspaceMode.READ_ONLY and self.is_writable_output(path):
                normalized.append(
                    FileMatch(path=path, file_type=self._detect(path), size_bytes=0)
                )
            else:
                raise WorkspaceDenied(f"unknown path: {path!r}")
        if len(normalized) > self._access.max_files:
            raise WorkspaceDenied(
                f"requested {len(normalized)} files, max_files={self._access.max_files}"
            )
        total = sum(m.size_bytes for m in normalized)
        if total > self._access.max_total_size_bytes:
            raise WorkspaceDenied(
                f"total size {total} exceeds max_total_size_bytes"
            )
        self._counter += 1
        grant = WorkspaceGrant(
            grant_id=f"grant-{self._counter}",
            purpose=purpose,
            files=tuple(normalized),
            mode=mode,
            reason=reason,
            requires_human_approval=self._access.require_human_approval,
        )
        self._grants[grant.grant_id] = grant
        return grant

    async def open_view(
        self,
        *,
        purpose: str,
        hints: Sequence[str] = (),
        file_types: Sequence[FileType] = (),
        max_files: int = 10,
        mode: WorkspaceMode = WorkspaceMode.READ_ONLY,
        reason: str | None = None,
    ) -> WorkspaceView:
        query = " ".join([purpose, *hints])
        matches = await self.search(query=query, types=file_types, limit=max_files * 3)
        chosen = matches[:max_files]
        grant = await self.request_access(
            files=chosen,
            mode=mode,
            reason=reason or purpose,
            purpose=purpose,
        )
        return MinIOWorkspaceView(grant, self)  # type: ignore[arg-type]

    async def list_grants(self) -> list[WorkspaceGrant]:
        return list(self._grants.values())


class MinIOWorkspaceClient(WorkspaceClient):
    """Workspace client backed by the user's MinIO/S3 bucket.

    The control plane mints a grant with bucket + path policy during handoff.
    Deployed agents use this client so ``ctx.workspace`` can read the caller's
    actual files and persist writes back to the same bucket.
    """

    _EXT_TO_TYPE = LocalWorkspaceClient._EXT_TO_TYPE

    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str,
        access_key_id: str,
        secret_access_key: str,
        access: WorkspaceAccess,
        issuer: str = "minio-runtime",
    ) -> None:
        import boto3
        from botocore.config import Config as _BotoConfig

        self.bucket = bucket
        self.issuer = issuer
        self._access = access
        self._grants: dict[str, WorkspaceGrant] = {}
        self._counter = 0
        self.allow_patterns: tuple[str, ...] = ("**",)
        self.outputs_prefix: str | None = None
        self.write_prefixes: tuple[str, ...] = ()
        self.current_mode: WorkspaceMode | None = None
        self.current_grant_id: str | None = None
        self.current_expires_at: int = 0
        self.current_grant: Grant | None = None
        self._s3 = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name="us-east-1",
            config=_BotoConfig(s3={"addressing_style": "path"}),
        )
        self._ensure_bucket()

    def _ensure_bucket(self) -> None:
        try:
            self._s3.head_bucket(Bucket=self.bucket)
        except Exception as exc:  # noqa: BLE001
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if code in {"404", "NoSuchBucket", "NotFound"}:
                self._s3.create_bucket(Bucket=self.bucket)
            elif code != "403":
                raise

    def install_grant(self, grant: "Grant") -> None:  # noqa: F821
        self._access = WorkspaceAccess(
            enabled=True,
            max_files=self._access.max_files,
            allowed_modes=self._access.allowed_modes,
            require_reason=self._access.require_reason,
            deny_patterns=tuple(grant.deny_patterns),
            require_human_approval=self._access.require_human_approval,
            max_total_size_bytes=self._access.max_total_size_bytes,
        )
        self.allow_patterns = tuple(grant.allow_patterns or ("**",))
        self.outputs_prefix = grant.outputs_prefix
        self.write_prefixes = _normalize_write_prefixes(
            grant.outputs_prefix,
            grant.write_prefixes,
        )
        self.current_mode = grant.mode
        self.current_grant_id = grant.grant_id
        self.current_expires_at = grant.expires_at
        self.current_grant = grant

    def _detect(self, path: str) -> FileType:
        for ext, ft in self._EXT_TO_TYPE.items():
            if path.endswith(ext):
                return ft
        return FileType.OTHER

    def _allowed(self, path: str) -> bool:
        allowed = any(fnmatch(path, pat) for pat in self.allow_patterns)
        denied = any(fnmatch(path, pat) for pat in self._access.deny_patterns)
        return allowed and not denied

    def is_writable_output(self, path: str) -> bool:
        if self.write_prefixes:
            return _matches_write_prefix(path, self.write_prefixes)
        if self.outputs_prefix is None:
            return self._allowed(path)
        return _matches_write_prefix(path, (self.outputs_prefix,))

    def exists(self, path: str) -> bool:
        try:
            self._s3.head_object(Bucket=self.bucket, Key=path)
            return True
        except Exception as exc:  # noqa: BLE001
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if code in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise

    def read_bytes(self, path: str) -> bytes:
        resp = self._s3.get_object(Bucket=self.bucket, Key=path)
        return resp["Body"].read()

    def write_bytes(self, path: str, content: bytes) -> None:
        self._s3.put_object(
            Bucket=self.bucket,
            Key=path,
            Body=content,
            ContentType=guess_type(path)[0] or "application/octet-stream",
        )

    def delete_path(self, path: str) -> None:
        self._s3.delete_object(Bucket=self.bucket, Key=path)

    def iter_paths(self) -> Iterable[str]:
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket):
            for obj in page.get("Contents") or ():
                key = obj.get("Key")
                if key:
                    yield key

    def _head_match(self, path: str) -> FileMatch:
        resp = self._s3.head_object(Bucket=self.bucket, Key=path)
        return FileMatch(
            path=path,
            file_type=self._detect(path),
            size_bytes=int(resp.get("ContentLength") or 0),
        )

    async def search(
        self,
        *,
        query: str,
        types: Sequence[FileType] = (),
        limit: int = 20,
    ) -> list[FileMatch]:
        if not self._access.enabled:
            raise WorkspaceDenied("workspace disabled by policy")
        type_set = set(types)
        terms = [t.lower() for t in re.split(r"\W+", query) if t]
        paginator = self._s3.get_paginator("list_objects_v2")
        out: list[FileMatch] = []
        for page in paginator.paginate(Bucket=self.bucket):
            for obj in page.get("Contents") or ():
                path = obj["Key"]
                if not self._allowed(path):
                    continue
                ft = self._detect(path)
                if type_set and ft not in type_set:
                    continue
                haystack = path.lower()
                score = sum(haystack.count(t) for t in terms) if terms else 1
                if score == 0:
                    continue
                out.append(
                    FileMatch(
                        path=path,
                        file_type=ft,
                        score=float(score),
                        size_bytes=int(obj.get("Size") or 0),
                    )
                )
        out.sort(key=lambda m: -m.score)
        return out[:limit]

    async def request_access(
        self,
        *,
        files: Sequence[FileMatch | str],
        mode: WorkspaceMode,
        reason: str,
        purpose: str = "",
    ) -> WorkspaceGrant:
        if not self._access.enabled:
            raise WorkspaceDenied("workspace disabled by policy")
        if mode not in self._access.allowed_modes:
            raise WorkspaceDenied(f"mode {mode.value!r} not in allowed_modes")
        if self._access.require_reason and not reason.strip():
            raise WorkspaceDenied("reason required by policy")
        normalized: list[FileMatch] = []
        for f in files:
            path = f if isinstance(f, str) else f.path
            if not self._allowed(path) and not (
                mode is not WorkspaceMode.READ_ONLY and self.is_writable_output(path)
            ):
                raise WorkspaceDenied(f"path denied by grant: {path}")
            if isinstance(f, FileMatch):
                normalized.append(f)
            elif self.exists(path):
                normalized.append(self._head_match(path))
            elif mode is not WorkspaceMode.READ_ONLY and self.is_writable_output(path):
                normalized.append(
                    FileMatch(path=path, file_type=self._detect(path), size_bytes=0)
                )
            else:
                raise WorkspaceDenied(f"unknown path: {path!r}")
        if len(normalized) > self._access.max_files:
            raise WorkspaceDenied(
                f"requested {len(normalized)} files, max_files={self._access.max_files}"
            )
        total = sum(m.size_bytes for m in normalized)
        if total > self._access.max_total_size_bytes:
            raise WorkspaceDenied(
                f"total size {total} exceeds max_total_size_bytes"
            )
        self._counter += 1
        grant = WorkspaceGrant(
            grant_id=f"grant-{self._counter}",
            purpose=purpose,
            files=tuple(normalized),
            mode=mode,
            reason=reason,
            requires_human_approval=self._access.require_human_approval,
        )
        self._grants[grant.grant_id] = grant
        return grant

    async def open_view(
        self,
        *,
        purpose: str,
        hints: Sequence[str] = (),
        file_types: Sequence[FileType] = (),
        max_files: int = 10,
        mode: WorkspaceMode = WorkspaceMode.READ_ONLY,
        reason: str | None = None,
    ) -> WorkspaceView:
        query = " ".join([purpose, *hints])
        matches = await self.search(query=query, types=file_types, limit=max_files * 3)
        chosen = matches[:max_files]
        grant = await self.request_access(
            files=chosen,
            mode=mode,
            reason=reason or purpose,
            purpose=purpose,
        )
        return MinIOWorkspaceView(grant, self)

    async def list_grants(self) -> list[WorkspaceGrant]:
        return list(self._grants.values())


def _glob_to_regex(pattern: str) -> str:
    """Translate a simple ``foo/**/*.py`` style glob to a regex."""
    out: list[str] = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "*":
            if i + 1 < len(pattern) and pattern[i + 1] == "*":
                out.append(".*")
                i += 2
                if i < len(pattern) and pattern[i] == "/":
                    i += 1
            else:
                out.append("[^/]*")
                i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == ".":
            out.append(r"\.")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return "".join(out)
