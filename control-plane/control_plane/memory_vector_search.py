from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import settings
from .memory_embeddings import embed_memory_texts, memory_embedding_text
from .models import AgentMemoryEntry


class MemoryVectorSearchUnavailable(RuntimeError):
    """Raised when Qdrant-backed memory search is not configured or unavailable."""


@dataclass(frozen=True)
class MemoryVectorMatch:
    memory_id: int
    score: float


class MemoryVectorSearch:
    def __init__(
        self,
        *,
        url: str | None,
        api_key: str | None,
        collection: str,
        embedding_dimension: int,
        timeout_seconds: float,
    ) -> None:
        self.url = (url or "").strip()
        self.api_key = (api_key or "").strip() or None
        self.collection = collection
        self.embedding_dimension = embedding_dimension
        self.timeout_seconds = timeout_seconds
        self._client: Any | None = None
        self._collection_ready = False

    @classmethod
    def from_settings(cls) -> "MemoryVectorSearch":
        return cls(
            url=settings.memory_qdrant_url or settings.agent_search_qdrant_url,
            api_key=settings.memory_qdrant_api_key or settings.agent_search_qdrant_api_key,
            collection=settings.memory_qdrant_collection,
            embedding_dimension=settings.memory_embedding_dimension,
            timeout_seconds=settings.memory_qdrant_timeout_seconds,
        )

    @property
    def configured(self) -> bool:
        return bool(self.url)

    async def close(self) -> None:
        if self._client is not None:
            close = getattr(self._client, "close", None)
            if close is not None:
                result = close()
                if hasattr(result, "__await__"):
                    await result
        self._client = None
        self._collection_ready = False

    async def upsert_memory(self, row: AgentMemoryEntry) -> None:
        if row.id is None or not self.configured:
            return
        await self._ensure_collection()
        text = memory_vector_document(row)
        vector = (await embed_memory_texts([text]))[0]
        models = _qdrant_models()
        await self._get_client().upsert(
            collection_name=self.collection,
            points=[
                models.PointStruct(
                    id=int(row.id),
                    vector=vector,
                    payload=memory_vector_payload(row),
                )
            ],
            wait=True,
        )

    async def delete_memory(self, memory_id: int) -> None:
        if not self.configured:
            return
        models = _qdrant_models()
        await self._get_client().delete(
            collection_name=self.collection,
            points_selector=models.PointIdsList(points=[int(memory_id)]),
            wait=True,
        )

    async def query_memory(
        self,
        query: str,
        *,
        agent_id: int,
        user_id: int,
        namespace: str,
        limit: int,
        score_threshold: float | None,
    ) -> list[MemoryVectorMatch]:
        if not self.configured:
            raise MemoryVectorSearchUnavailable("memory vector search is not configured")
        clean_query = query.strip()
        if not clean_query:
            return []
        await self._ensure_collection()
        vector = (await embed_memory_texts([clean_query]))[0]
        response = await self._get_client().query_points(
            collection_name=self.collection,
            query=vector,
            query_filter=_memory_filter(
                agent_id=agent_id,
                user_id=user_id,
                namespace=namespace,
            ),
            limit=max(1, limit),
            with_payload=False,
            with_vectors=False,
            score_threshold=score_threshold,
        )
        return [
            MemoryVectorMatch(memory_id=int(point.id), score=float(point.score))
            for point in getattr(response, "points", [])
        ]

    async def _ensure_collection(self) -> None:
        if self._collection_ready:
            return
        client = self._get_client()
        models = _qdrant_models()
        exists = await client.collection_exists(self.collection)
        if not exists:
            await client.create_collection(
                collection_name=self.collection,
                vectors_config=models.VectorParams(
                    size=self.embedding_dimension,
                    distance=models.Distance.COSINE,
                ),
            )
        self._collection_ready = True

    def _get_client(self) -> Any:
        if not self.configured:
            raise MemoryVectorSearchUnavailable("memory vector search is not configured")
        if self._client is None:
            qdrant_client = _qdrant_client()
            self._client = qdrant_client.AsyncQdrantClient(
                url=self.url,
                api_key=self.api_key,
                timeout=self.timeout_seconds,
            )
        return self._client


def memory_vector_document(row: AgentMemoryEntry) -> str:
    return memory_embedding_text(
        key=row.key,
        value=row.value,
        metadata=dict(row.metadata_json or {}),
    )


def memory_vector_payload(row: AgentMemoryEntry) -> dict[str, Any]:
    return {
        "memory_id": row.id,
        "agent_id": row.agent_id,
        "user_id": row.user_id,
        "agent_name": row.agent_name,
        "namespace": row.namespace,
        "key": row.key,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _memory_filter(*, agent_id: int, user_id: int, namespace: str) -> Any:
    models = _qdrant_models()
    return models.Filter(
        must=[
            models.FieldCondition(
                key="agent_id",
                match=models.MatchValue(value=agent_id),
            ),
            models.FieldCondition(
                key="user_id",
                match=models.MatchValue(value=user_id),
            ),
            models.FieldCondition(
                key="namespace",
                match=models.MatchValue(value=namespace),
            ),
        ]
    )


def _qdrant_client() -> Any:
    try:
        import qdrant_client
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise MemoryVectorSearchUnavailable("qdrant-client is not installed") from exc
    return qdrant_client


def _qdrant_models() -> Any:
    try:
        from qdrant_client import models
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise MemoryVectorSearchUnavailable("qdrant-client is not installed") from exc
    return models
