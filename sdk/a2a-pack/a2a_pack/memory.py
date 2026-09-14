"""Workspace-file backed long-term memory for meta-agents."""
from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Sequence

from pydantic import BaseModel, ConfigDict, Field

from .workspace import WorkspaceDenied, WorkspaceMode

if TYPE_CHECKING:
    from .context import RunContext
    from .runtime import AgentMemory
    from .workspace import WorkspaceClient


_SEGMENT_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_LOCAL_VECTOR_SIZE = 64


class MemoryRecord(BaseModel):
    """A JSON note persisted under the caller's granted workspace."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    namespace: str = "notes"
    value: Any
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str


class MemoryLogEntry(BaseModel):
    """One append-only memory event."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    log: str
    value: Any
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class MemoryClient:
    """Durable memory stored as workspace files below a reserved prefix."""

    def __init__(
        self,
        ctx: "RunContext[Any]",
        *,
        root_prefix: str = "memory",
        agent_namespace: str | None = None,
        user_namespace: str | None = None,
    ) -> None:
        self._ctx = ctx
        self._root = _safe_segment(root_prefix or "memory")
        try:
            workspace = ctx.workspace
        except PermissionError:
            workspace = None
        self._agent = _safe_segment(
            agent_namespace or getattr(workspace, "issuer", None) or "agent"
        )
        self._user = _safe_segment(
            user_namespace or getattr(ctx, "caller", None) or "anonymous"
        )

    def for_scope(
        self,
        *,
        agent_namespace: str | None = None,
        user_namespace: str | None = None,
    ) -> "MemoryClient":
        """Return a client bound to an explicit agent/user namespace."""

        return MemoryClient(
            self._ctx,
            root_prefix=self._root,
            agent_namespace=agent_namespace or self._agent,
            user_namespace=user_namespace or self._user,
        )

    def for_tier(self, tier: str, *, agent_name: str | None = None) -> Any:
        """Return the memory backend for ``tier``.

        ``workspace``/``files`` returns this client. ``kv`` returns the
        control-plane transactional memory client and requires forwarded
        ``ctx.cp_url`` + ``ctx.cp_jwt``.
        """

        normalized = tier.strip().lower()
        if normalized in {"workspace", "files", "file"}:
            return self
        if normalized == "kv":
            return self.control_plane(agent_name=agent_name)
        if normalized in {"vector", "semantic"}:
            local = self.local_vector(agent_name=agent_name)
            if local is not None:
                return local
            return self.control_plane(agent_name=agent_name, semantic=True)
        raise ValueError(f"unsupported memory tier: {tier!r}")

    def for_manifest(
        self,
        memory: "AgentMemory | dict[str, Any] | None",
        *,
        agent_name: str | None = None,
    ) -> "UnifiedMemoryClient":
        tiers, namespace = _manifest_memory_policy(memory)
        return UnifiedMemoryClient(
            self,
            tiers=tiers,
            default_namespace=namespace or "notes",
            agent_name=agent_name,
        )

    def control_plane(
        self,
        *,
        agent_name: str | None = None,
        semantic: bool = False,
    ) -> "ControlPlaneMemoryClient":
        cp_url = getattr(self._ctx, "cp_url", None)
        cp_jwt = getattr(self._ctx, "cp_jwt", None)
        name = agent_name or self._agent
        if not cp_url or not cp_jwt:
            raise RuntimeError(
                "control-plane memory requires cp_url/cp_jwt; "
                "declare wants_cp_jwt=True on the agent"
            )
        return ControlPlaneMemoryClient(
            cp_url=cp_url,
            cp_jwt=cp_jwt,
            agent_name=name,
            semantic=semantic,
        )

    def local_vector(
        self,
        *,
        agent_name: str | None = None,
    ) -> "LocalQdrantMemoryClient | None":
        import os

        qdrant_url = (
            os.environ.get("A2A_MEMORY_VECTOR_URL")
            or os.environ.get("A2A_QDRANT_URL")
            or os.environ.get("QDRANT_URL")
        )
        if not qdrant_url:
            return None
        return LocalQdrantMemoryClient(
            qdrant_url=qdrant_url,
            agent_name=agent_name or self._agent,
        )

    @property
    def root_prefix(self) -> str:
        return self._root

    @property
    def agent_namespace(self) -> str:
        return self._agent

    @property
    def user_namespace(self) -> str:
        return self._user

    async def put_note(
        self,
        key: str,
        value: Any,
        *,
        namespace: str = "notes",
        metadata: dict[str, Any] | None = None,
    ) -> MemoryRecord:
        path = self._note_path(namespace, key)
        self._require_write(path)
        now = _now()
        previous = await self.get_note(key, namespace=namespace)
        record = MemoryRecord(
            key=key,
            namespace=namespace,
            value=value,
            metadata=dict(metadata or {}),
            created_at=previous.created_at if previous is not None else now,
            updated_at=now,
        )
        self._workspace.write_bytes(
            path,
            json.dumps(record.model_dump(mode="json"), sort_keys=True).encode("utf-8"),
        )
        return record

    async def get_note(
        self,
        key: str,
        *,
        namespace: str = "notes",
    ) -> MemoryRecord | None:
        path = self._note_path(namespace, key)
        if not self._exists(path):
            return None
        self._require_read(path)
        data = self._workspace.read_bytes(path)
        return MemoryRecord.model_validate(json.loads(data.decode("utf-8")))

    async def list_notes(
        self,
        *,
        namespace: str = "notes",
        limit: int = 100,
    ) -> list[MemoryRecord]:
        prefix = self._namespace_prefix(namespace)
        records: list[MemoryRecord] = []
        for path in self._iter_paths(prefix):
            if len(records) >= limit:
                break
            if not path.endswith(".json"):
                continue
            try:
                data = self._workspace.read_bytes(path)
                records.append(
                    MemoryRecord.model_validate(json.loads(data.decode("utf-8")))
                )
            except Exception:  # noqa: BLE001
                continue
        records.sort(key=lambda item: item.updated_at, reverse=True)
        return records[:limit]

    async def search_notes(
        self,
        query: str,
        *,
        namespace: str = "notes",
        limit: int = 10,
    ) -> list[MemoryRecord]:
        needle = query.casefold().strip()
        if not needle:
            return await self.list_notes(namespace=namespace, limit=limit)
        hits: list[MemoryRecord] = []
        for record in await self.list_notes(namespace=namespace, limit=500):
            haystack = json.dumps(record.model_dump(mode="json"), sort_keys=True).casefold()
            if needle in haystack:
                hits.append(record)
                if len(hits) >= limit:
                    break
        return hits

    async def search(
        self,
        query: str,
        *,
        namespace: str = "notes",
        limit: int = 10,
    ) -> list[MemoryRecord]:
        return await self.search_notes(query, namespace=namespace, limit=limit)

    async def append_log(
        self,
        name: str,
        value: Any,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryLogEntry:
        path = self._log_path(name)
        self._require_write(path)
        entry = MemoryLogEntry(
            log=name,
            value=value,
            metadata=dict(metadata or {}),
            created_at=_now(),
        )
        existing = b""
        if self._exists(path):
            self._require_read(path)
            existing = self._workspace.read_bytes(path)
            if existing and not existing.endswith(b"\n"):
                existing += b"\n"
        line = json.dumps(entry.model_dump(mode="json"), sort_keys=True).encode("utf-8")
        self._workspace.write_bytes(path, existing + line + b"\n")
        return entry

    async def read_log(self, name: str, *, limit: int = 100) -> list[MemoryLogEntry]:
        path = self._log_path(name)
        if not self._exists(path):
            return []
        self._require_read(path)
        entries: list[MemoryLogEntry] = []
        for line in self._workspace.read_bytes(path).decode("utf-8").splitlines():
            if not line.strip():
                continue
            entries.append(MemoryLogEntry.model_validate(json.loads(line)))
        return entries[-limit:]

    async def remember(
        self,
        key: str,
        value: Any,
        *,
        namespace: str = "notes",
        metadata: dict[str, Any] | None = None,
    ) -> MemoryRecord:
        return await self.put_note(
            key,
            value,
            namespace=namespace,
            metadata=metadata,
        )

    async def recall(
        self,
        key: str,
        *,
        namespace: str = "notes",
    ) -> MemoryRecord | None:
        return await self.get_note(key, namespace=namespace)

    def as_tools(self) -> list[Any]:
        """Return LangChain-compatible tools when LangChain is installed."""

        try:
            from langchain_core.tools import tool
        except Exception:  # noqa: BLE001
            return [
                _plain_tool(self.remember),
                _plain_tool(self.recall),
                _plain_tool(self.list_notes),
                _plain_tool(self.search_notes),
                _plain_tool(self.append_log),
            ]

        @tool
        async def remember(
            key: str,
            value: Any,
            namespace: str = "notes",
            metadata: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            """Persist a durable memory note."""

            record = await self.remember(
                key,
                value,
                namespace=namespace,
                metadata=metadata,
            )
            return record.model_dump(mode="json")

        @tool
        async def recall(key: str, namespace: str = "notes") -> dict[str, Any] | None:
            """Recall a durable memory note by key."""

            record = await self.recall(key, namespace=namespace)
            return None if record is None else record.model_dump(mode="json")

        @tool
        async def list_notes(
            namespace: str = "notes",
            limit: int = 20,
        ) -> list[dict[str, Any]]:
            """List recent durable memory notes."""

            return [
                record.model_dump(mode="json")
                for record in await self.list_notes(namespace=namespace, limit=limit)
            ]

        @tool
        async def search_notes(
            query: str,
            namespace: str = "notes",
            limit: int = 10,
        ) -> list[dict[str, Any]]:
            """Search durable memory notes by text."""

            return [
                record.model_dump(mode="json")
                for record in await self.search_notes(
                    query,
                    namespace=namespace,
                    limit=limit,
                )
            ]

        @tool
        async def append_log(
            name: str,
            value: Any,
            metadata: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            """Append one durable memory log entry."""

            entry = await self.append_log(name, value, metadata=metadata)
            return entry.model_dump(mode="json")

        return [remember, recall, list_notes, search_notes, append_log]

    @property
    def _workspace(self) -> "WorkspaceClient":
        return self._ctx.workspace

    def _namespace_prefix(self, namespace: str) -> str:
        return f"{self._root}/{self._agent}/{self._user}/{_safe_segment(namespace)}/"

    def _note_path(self, namespace: str, key: str) -> str:
        clean = _safe_key(key)
        if not clean.endswith(".json"):
            clean += ".json"
        return self._namespace_prefix(namespace) + clean

    def _log_path(self, name: str) -> str:
        clean = _safe_key(name)
        if not clean.endswith(".jsonl"):
            clean += ".jsonl"
        return f"{self._root}/{self._agent}/{self._user}/logs/{clean}"

    def _iter_paths(self, prefix: str) -> list[str]:
        iterator = getattr(self._workspace, "iter_paths", None)
        if iterator is None:
            return []
        return [
            path
            for path in sorted(iterator())
            if path.startswith(prefix) and self._can_read(path)
        ]

    def _exists(self, path: str) -> bool:
        exists = getattr(self._workspace, "exists", None)
        if exists is not None:
            try:
                return bool(exists(path))
            except WorkspaceDenied:
                return False
        return path in self._iter_paths("")

    def _can_read(self, path: str) -> bool:
        access = getattr(self._workspace, "_access", None)
        denied = tuple(getattr(access, "deny_patterns", ()) or ())
        if any(fnmatch.fnmatch(path, pattern) for pattern in denied):
            return False
        allowed = tuple(getattr(self._workspace, "allow_patterns", ("**",)) or ("**",))
        return any(fnmatch.fnmatch(path, pattern) for pattern in allowed)

    def _can_write(self, path: str) -> bool:
        if getattr(self._workspace, "current_mode", None) == WorkspaceMode.READ_ONLY:
            return False
        is_writable_output = getattr(self._workspace, "is_writable_output", None)
        if is_writable_output is not None and is_writable_output(path):
            return True
        if (
            getattr(self._workspace, "write_prefixes", ())
            or getattr(self._workspace, "outputs_prefix", None) is not None
        ):
            return False
        return self._can_read(path)

    def _require_read(self, path: str) -> None:
        if not self._can_read(path):
            raise WorkspaceDenied(f"memory read denied by workspace grant: {path}")

    def _require_write(self, path: str) -> None:
        if not self._can_write(path):
            raise WorkspaceDenied(f"memory write denied by workspace grant: {path}")


def memory_tools(ctx: "RunContext[Any]") -> list[Any]:
    return MemoryClient(ctx).as_tools()


class ControlPlaneMemoryClient:
    """Client for the control-plane transactional KV memory endpoints."""

    def __init__(
        self,
        *,
        cp_url: str,
        cp_jwt: str,
        agent_name: str,
        semantic: bool = False,
    ) -> None:
        self._cp_url = cp_url.rstrip("/")
        self._cp_jwt = cp_jwt
        self._agent_name = _safe_segment(agent_name)
        self._semantic = semantic

    async def put_note(
        self,
        key: str,
        value: Any,
        *,
        namespace: str = "notes",
        metadata: dict[str, Any] | None = None,
    ) -> MemoryRecord:
        payload = {
            "namespace": _safe_segment(namespace),
            "key": _safe_key(key),
            "value": value,
            "metadata": dict(metadata or {}),
        }
        data = await self._request("PUT", "", json=payload)
        return _record_from_control_plane(data)

    async def get_note(
        self,
        key: str,
        *,
        namespace: str = "notes",
    ) -> MemoryRecord | None:
        try:
            data = await self._request(
                "GET",
                f"/{_safe_segment(namespace)}/{_safe_key(key)}",
            )
        except KeyError:
            return None
        return _record_from_control_plane(data)

    async def list_notes(
        self,
        *,
        namespace: str = "notes",
        limit: int = 100,
    ) -> list[MemoryRecord]:
        data = await self._request(
            "GET",
            "",
            params={"namespace": _safe_segment(namespace), "limit": limit},
        )
        return [_record_from_control_plane(item) for item in data]

    async def search_notes(
        self,
        query: str,
        *,
        namespace: str = "notes",
        limit: int = 10,
    ) -> list[MemoryRecord]:
        if self._semantic:
            data = await self._request(
                "GET",
                "/search",
                params={
                    "namespace": _safe_segment(namespace),
                    "limit": limit,
                    "q": query,
                },
            )
            return [_record_from_control_plane(item) for item in data]
        data = await self._request(
            "GET",
            "",
            params={
                "namespace": _safe_segment(namespace),
                "limit": limit,
                "q": query,
            },
        )
        return [_record_from_control_plane(item) for item in data]

    async def search(
        self,
        query: str,
        *,
        namespace: str = "notes",
        limit: int = 10,
    ) -> list[MemoryRecord]:
        return await self.search_notes(query, namespace=namespace, limit=limit)


    async def remember(
        self,
        key: str,
        value: Any,
        *,
        namespace: str = "notes",
        metadata: dict[str, Any] | None = None,
    ) -> MemoryRecord:
        return await self.put_note(
            key,
            value,
            namespace=namespace,
            metadata=metadata,
        )

    async def recall(
        self,
        key: str,
        *,
        namespace: str = "notes",
    ) -> MemoryRecord | None:
        return await self.get_note(key, namespace=namespace)

    async def _request(
        self,
        method: str,
        suffix: str,
        **kwargs: Any,
    ) -> Any:
        import httpx

        url = f"{self._cp_url}/v1/agents/{self._agent_name}/memory{suffix}"
        headers = {"authorization": f"Bearer {self._cp_jwt}"}
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.request(method, url, headers=headers, **kwargs)
        if response.status_code == 404:
            raise KeyError("memory entry not found")
        if response.status_code == 204:
            return None
        if response.status_code >= 400:
            raise RuntimeError(f"control-plane memory request failed: {response.text}")
        return response.json()


class LocalQdrantMemoryClient:
    """Local Qdrant-backed vector memory used by Docker chat/dev harnesses."""

    def __init__(
        self,
        *,
        qdrant_url: str,
        agent_name: str,
    ) -> None:
        self._qdrant_url = qdrant_url.rstrip("/")
        self._collection = f"a2a_{_safe_segment(agent_name).replace('-', '_')}"

    async def put_note(
        self,
        key: str,
        value: Any,
        *,
        namespace: str = "notes",
        metadata: dict[str, Any] | None = None,
    ) -> MemoryRecord:
        await self._ensure_collection()
        record = MemoryRecord(
            key=_safe_key(key),
            namespace=_safe_segment(namespace),
            value=value,
            metadata=dict(metadata or {}),
            created_at=_now(),
            updated_at=_now(),
        )
        payload = record.model_dump(mode="json")
        await self._request(
            "PUT",
            f"/collections/{self._collection}/points",
            params={"wait": "true"},
            json={
                "points": [
                    {
                        "id": _qdrant_point_id(record.namespace, record.key),
                        "vector": _local_vector_embedding(payload),
                        "payload": payload,
                    }
                ]
            },
        )
        return record

    async def get_note(
        self,
        key: str,
        *,
        namespace: str = "notes",
    ) -> MemoryRecord | None:
        await self._ensure_collection()
        data = await self._request(
            "POST",
            f"/collections/{self._collection}/points",
            json={
                "ids": [_qdrant_point_id(_safe_segment(namespace), _safe_key(key))],
                "with_payload": True,
                "with_vector": False,
            },
        )
        points = data.get("result") if isinstance(data, dict) else None
        if not isinstance(points, list) or not points:
            return None
        payload = points[0].get("payload") if isinstance(points[0], dict) else None
        return _record_from_qdrant_payload(payload)

    async def list_notes(
        self,
        *,
        namespace: str = "notes",
        limit: int = 100,
    ) -> list[MemoryRecord]:
        await self._ensure_collection()
        data = await self._request(
            "POST",
            f"/collections/{self._collection}/points/scroll",
            json={
                "limit": limit,
                "with_payload": True,
                "with_vector": False,
                "filter": _qdrant_namespace_filter(namespace),
            },
        )
        result = data.get("result") if isinstance(data, dict) else None
        points = result.get("points") if isinstance(result, dict) else None
        if not isinstance(points, list):
            return []
        records = [
            record
            for point in points
            if isinstance(point, dict)
            if (record := _record_from_qdrant_payload(point.get("payload"))) is not None
        ]
        records.sort(key=lambda item: item.updated_at, reverse=True)
        return records[:limit]

    async def search_notes(
        self,
        query: str,
        *,
        namespace: str = "notes",
        limit: int = 10,
    ) -> list[MemoryRecord]:
        await self._ensure_collection()
        data = await self._request(
            "POST",
            f"/collections/{self._collection}/points/search",
            json={
                "vector": _local_vector_embedding(query),
                "limit": limit,
                "with_payload": True,
                "with_vector": False,
                "filter": _qdrant_namespace_filter(namespace),
            },
        )
        points = data.get("result") if isinstance(data, dict) else None
        if not isinstance(points, list):
            return []
        return [
            record
            for point in points
            if isinstance(point, dict)
            if (record := _record_from_qdrant_payload(point.get("payload"))) is not None
        ]

    async def search(
        self,
        query: str,
        *,
        namespace: str = "notes",
        limit: int = 10,
    ) -> list[MemoryRecord]:
        return await self.search_notes(query, namespace=namespace, limit=limit)


    async def remember(
        self,
        key: str,
        value: Any,
        *,
        namespace: str = "notes",
        metadata: dict[str, Any] | None = None,
    ) -> MemoryRecord:
        return await self.put_note(
            key,
            value,
            namespace=namespace,
            metadata=metadata,
        )

    async def recall(
        self,
        key: str,
        *,
        namespace: str = "notes",
    ) -> MemoryRecord | None:
        return await self.get_note(key, namespace=namespace)

    async def _ensure_collection(self) -> None:
        try:
            await self._request(
                "PUT",
                f"/collections/{self._collection}",
                json={"vectors": {"size": _LOCAL_VECTOR_SIZE, "distance": "Cosine"}},
            )
        except RuntimeError as exc:
            if "already exists" not in str(exc):
                raise

    async def _request(
        self,
        method: str,
        suffix: str,
        **kwargs: Any,
    ) -> Any:
        import httpx

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.request(
                method,
                f"{self._qdrant_url}{suffix}",
                **kwargs,
            )
        if response.status_code == 404:
            return {}
        if response.status_code >= 400:
            raise RuntimeError(f"local Qdrant memory request failed: {response.text}")
        return response.json() if response.content else {}


class UnifiedMemoryClient:
    """Manifest-aware facade over files, KV, and vector memory tiers."""

    def __init__(
        self,
        base: MemoryClient,
        *,
        tiers: Sequence[str],
        default_namespace: str = "notes",
        agent_name: str | None = None,
    ) -> None:
        self._base = base
        self._tiers = tuple(dict.fromkeys(tier.strip().lower() for tier in tiers if tier))
        self._namespace = _safe_segment(default_namespace or "notes")
        self._agent_name = agent_name

    @property
    def tiers(self) -> tuple[str, ...]:
        return self._tiers

    async def put_note(
        self,
        key: str,
        value: Any,
        *,
        namespace: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryRecord:
        client = self._client_for_write()
        return await client.put_note(
            key,
            value,
            namespace=self._resolve_namespace(namespace),
            metadata=metadata,
        )

    async def get_note(
        self,
        key: str,
        *,
        namespace: str | None = None,
    ) -> MemoryRecord | None:
        for client in self._clients_for_read():
            try:
                record = await client.get_note(key, namespace=self._resolve_namespace(namespace))
            except Exception:  # noqa: BLE001
                continue
            if record is not None:
                return record
        return None

    async def list_notes(
        self,
        *,
        namespace: str | None = None,
        limit: int = 100,
    ) -> list[MemoryRecord]:
        for client in self._clients_for_read():
            try:
                return await client.list_notes(
                    namespace=self._resolve_namespace(namespace),
                    limit=limit,
                )
            except Exception:  # noqa: BLE001
                continue
        return []

    async def search_notes(
        self,
        query: str,
        *,
        namespace: str | None = None,
        limit: int = 10,
    ) -> list[MemoryRecord]:
        for client in self._clients_for_search():
            try:
                hits = await client.search_notes(
                    query,
                    namespace=self._resolve_namespace(namespace),
                    limit=limit,
                )
            except Exception:  # noqa: BLE001
                continue
            if hits:
                return hits
        return []

    async def search(
        self,
        query: str,
        *,
        namespace: str | None = None,
        limit: int = 10,
    ) -> list[MemoryRecord]:
        return await self.search_notes(query, namespace=namespace, limit=limit)

    async def remember(
        self,
        key: str,
        value: Any,
        *,
        namespace: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryRecord:
        return await self.put_note(
            key,
            value,
            namespace=namespace,
            metadata=metadata,
        )

    async def recall(
        self,
        key: str,
        *,
        namespace: str | None = None,
    ) -> MemoryRecord | None:
        return await self.get_note(key, namespace=namespace)

    async def append_log(
        self,
        name: str,
        value: Any,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryLogEntry:
        return await self._base.append_log(name, value, metadata=metadata)

    async def read_log(self, name: str, *, limit: int = 100) -> list[MemoryLogEntry]:
        return await self._base.read_log(name, limit=limit)

    def _resolve_namespace(self, namespace: str | None) -> str:
        return _safe_segment(namespace or self._namespace)

    def _client_for_write(self) -> Any:
        for tier in ("kv", "files"):
            if tier in self._tiers:
                try:
                    return self._base.for_tier(tier, agent_name=self._agent_name)
                except Exception:  # noqa: BLE001
                    continue
        return self._base

    def _clients_for_read(self) -> list[Any]:
        clients: list[Any] = []
        for tier in ("kv", "files", "vector"):
            if tier not in self._tiers:
                continue
            try:
                clients.append(self._base.for_tier(tier, agent_name=self._agent_name))
            except Exception:  # noqa: BLE001
                continue
        return clients or [self._base]

    def _clients_for_search(self) -> list[Any]:
        clients: list[Any] = []
        for tier in ("vector", "kv", "files"):
            if tier not in self._tiers:
                continue
            try:
                clients.append(self._base.for_tier(tier, agent_name=self._agent_name))
            except Exception:  # noqa: BLE001
                continue
        return clients or [self._base]


def _record_from_control_plane(data: dict[str, Any]) -> MemoryRecord:
    return MemoryRecord(
        key=str(data["key"]),
        namespace=str(data.get("namespace") or "notes"),
        value=data.get("value"),
        metadata=dict(data.get("metadata") or {}),
        created_at=str(data["created_at"]),
        updated_at=str(data["updated_at"]),
    )


def _record_from_qdrant_payload(payload: Any) -> MemoryRecord | None:
    if not isinstance(payload, dict):
        return None
    try:
        return MemoryRecord.model_validate(payload)
    except Exception:  # noqa: BLE001
        return None


def _qdrant_point_id(namespace: str, key: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"a2a-memory:{namespace}:{key}"))


def _qdrant_namespace_filter(namespace: str) -> dict[str, Any]:
    return {
        "must": [
            {
                "key": "namespace",
                "match": {"value": _safe_segment(namespace)},
            }
        ]
    }


def _local_vector_embedding(value: Any) -> list[float]:
    text = (
        json.dumps(value, sort_keys=True, default=str)
        if not isinstance(value, str)
        else value
    )
    vector = [0.0] * _LOCAL_VECTOR_SIZE
    tokens = re.findall(r"[A-Za-z0-9_]+", text.casefold()) or [text.casefold()]
    for token in tokens:
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=16).digest()
        idx = int.from_bytes(digest[:4], "big") % _LOCAL_VECTOR_SIZE
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vector[idx] += sign
    norm = sum(item * item for item in vector) ** 0.5 or 1.0
    return [item / norm for item in vector]


def _safe_segment(value: str) -> str:
    clean = _SEGMENT_RE.sub("-", str(value).replace("\\", "/").strip("/")).strip(".-")
    if not clean or clean in {".", ".."}:
        raise ValueError("memory namespace segment must not be empty")
    return clean[:120]


def _safe_key(key: str) -> str:
    raw = str(key).replace("\\", "/").strip("/")
    parts = raw.split("/")
    if not parts or any(not part or part in {".", ".."} for part in parts):
        raise ValueError("memory key must be a safe relative path")
    return "/".join(_safe_segment(part) for part in parts)


def _manifest_memory_policy(
    memory: "AgentMemory | dict[str, Any] | None",
) -> tuple[tuple[str, ...], str | None]:
    if memory is None:
        return (("files",), None)
    raw_tiers = getattr(memory, "tiers", None)
    namespace = getattr(memory, "namespace", None)
    if isinstance(memory, dict):
        raw_tiers = memory.get("tiers", raw_tiers)
        namespace = memory.get("namespace", namespace)
    if isinstance(raw_tiers, str):
        tiers = (raw_tiers,)
    elif isinstance(raw_tiers, Sequence):
        tiers = tuple(str(item) for item in raw_tiers)
    else:
        tiers = ()
    clean = tuple(
        tier.strip().lower()
        for tier in tiers
        if tier and tier.strip().lower() in {"files", "kv", "vector"}
    )
    return (clean or ("files",), str(namespace) if namespace else None)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _plain_tool(fn: Any) -> Any:
    return fn


__all__ = [
    "ControlPlaneMemoryClient",
    "LocalQdrantMemoryClient",
    "MemoryClient",
    "MemoryLogEntry",
    "MemoryRecord",
    "UnifiedMemoryClient",
    "memory_tools",
]
