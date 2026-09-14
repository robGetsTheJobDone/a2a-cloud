from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from threading import Lock
from typing import TYPE_CHECKING, Any

from .config import settings

if TYPE_CHECKING:
    from .models import Agent


class SemanticAgentSearchUnavailable(RuntimeError):
    """Raised when semantic agent search is not configured or unavailable."""


@dataclass(frozen=True)
class SemanticAgentMatch:
    agent_id: int
    score: float


class SemanticAgentSearch:
    def __init__(
        self,
        *,
        url: str | None,
        api_key: str | None,
        collection: str,
        embedding_model: str,
        embedding_dimension: int,
        timeout_seconds: float,
    ) -> None:
        self.url = (url or "").strip()
        self.api_key = (api_key or "").strip() or None
        self.collection = collection
        self.embedding_model = embedding_model
        self.embedding_dimension = embedding_dimension
        self.timeout_seconds = timeout_seconds
        self._client: Any | None = None
        self._collection_ready = False
        self._embedding: Any | None = None
        self._embedding_lock = Lock()

    @classmethod
    def from_settings(cls) -> "SemanticAgentSearch":
        return cls(
            url=settings.agent_search_qdrant_url,
            api_key=settings.agent_search_qdrant_api_key,
            collection=settings.agent_search_qdrant_collection,
            embedding_model=settings.agent_search_embedding_model,
            embedding_dimension=settings.agent_search_embedding_dimension,
            timeout_seconds=settings.agent_search_qdrant_timeout_seconds,
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

    async def index_agents(self, agents: list["Agent"]) -> None:
        if not agents or not self.configured:
            return
        await self._ensure_collection()
        documents = [agent_search_document(agent) for agent in agents]
        vectors = await self._embed(documents)
        models = _qdrant_models()
        points = [
            models.PointStruct(
                id=agent.id,
                vector=vector,
                payload=agent_search_payload(agent),
            )
            for agent, vector in zip(agents, vectors, strict=True)
            if agent.id is not None
        ]
        if not points:
            return
        await self._get_client().upsert(
            collection_name=self.collection,
            points=points,
            wait=True,
        )

    async def delete_agent(self, agent_id: int) -> None:
        if not self.configured:
            return
        models = _qdrant_models()
        await self._get_client().delete(
            collection_name=self.collection,
            points_selector=models.PointIdsList(points=[agent_id]),
            wait=True,
        )

    async def query_agents(
        self,
        query: str,
        *,
        user_id: int,
        organization_ids: tuple[int, ...] = (),
        limit: int,
        score_threshold: float | None,
    ) -> list[SemanticAgentMatch]:
        if not self.configured:
            raise SemanticAgentSearchUnavailable("semantic agent search is not configured")
        clean_query = query.strip()
        if not clean_query:
            return []
        await self._ensure_collection()
        vector = (await self._embed([clean_query]))[0]
        response = await self._get_client().query_points(
            collection_name=self.collection,
            query=vector,
            query_filter=_visibility_filter(user_id, organization_ids),
            limit=max(1, limit),
            with_payload=False,
            with_vectors=False,
            score_threshold=score_threshold,
        )
        return [
            SemanticAgentMatch(agent_id=int(point.id), score=float(point.score))
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
            raise SemanticAgentSearchUnavailable("semantic agent search is not configured")
        if self._client is None:
            qdrant_client = _qdrant_client()
            self._client = qdrant_client.AsyncQdrantClient(
                url=self.url,
                api_key=self.api_key,
                timeout=self.timeout_seconds,
            )
        return self._client

    async def _embed(self, texts: list[str]) -> list[list[float]]:
        return await asyncio.to_thread(self._embed_sync, texts)

    def _embed_sync(self, texts: list[str]) -> list[list[float]]:
        with self._embedding_lock:
            if self._embedding is None:
                fastembed = _fastembed()
                self._embedding = fastembed.TextEmbedding(
                    model_name=self.embedding_model,
                )
            vectors = list(self._embedding.embed(texts))
        return [
            vector.tolist() if hasattr(vector, "tolist") else list(vector)
            for vector in vectors
        ]


def agent_search_document(agent: "Agent") -> str:
    card = agent.card if isinstance(agent.card, dict) else {}
    skills = card.get("skills") if isinstance(card.get("skills"), list) else []
    runtime = card.get("runtime") if isinstance(card.get("runtime"), dict) else {}
    fields = [
        f"Name: {agent.name}",
        f"Description: {agent.description or card.get('description') or ''}",
        f"Version: {agent.version}",
        f"Status: {agent.status}",
        f"Visibility: {'public' if agent.public else 'private'}",
    ]
    llm_provisioning = runtime.get("llm_provisioning")
    if llm_provisioning:
        fields.append(f"LLM provisioning: {llm_provisioning}")
    for skill in skills:
        if not isinstance(skill, dict):
            continue
        tags = ", ".join(str(tag) for tag in skill.get("tags") or [])
        schema_terms = ", ".join(_schema_property_names(skill.get("input_schema")))
        fields.append(
            "Skill: "
            f"{skill.get('name') or ''}. "
            f"{skill.get('description') or ''}. "
            f"Tags: {tags}. "
            f"Input fields: {schema_terms}."
        )
    return "\n".join(fields)


def agent_search_payload(agent: "Agent") -> dict[str, Any]:
    card = agent.card if isinstance(agent.card, dict) else {}
    return {
        "agent_id": agent.id,
        "name": agent.name,
        "owner_id": agent.owner_id,
        "organization_id": agent.organization_id,
        "public": bool(agent.public),
        "status": agent.status,
        "skills": _skill_names(card),
        "tags": _skill_tags(card),
        "updated_at": agent.updated_at.isoformat() if agent.updated_at else None,
    }


def lexical_agent_score(agent: "Agent", query: str, *, tags: list[str], skill: str | None) -> float:
    card = agent.card if isinstance(agent.card, dict) else {}
    haystack = _agent_text(agent).lower()
    terms = _terms(" ".join([query, " ".join(tags), skill or ""]))
    if not terms:
        return 0.0
    score = 0.0
    name = agent.name.lower()
    skills = {item.lower() for item in _skill_names(card)}
    card_tags = {item.lower() for item in _skill_tags(card)}
    for term in terms:
        if term == name:
            score += 8.0
        elif term in name:
            score += 4.0
        if term in skills:
            score += 6.0
        if term in card_tags:
            score += 3.0
        if term in haystack:
            score += 1.0
    if skill and skill in _skill_names(card):
        score += 10.0
    if tags:
        score += len({tag.lower() for tag in tags} & card_tags) * 4.0
    return score


def _agent_text(agent: "Agent") -> str:
    card = agent.card if isinstance(agent.card, dict) else {}
    return "\n".join(
        [
            agent.name,
            agent.description or "",
            str(card.get("description") or ""),
            " ".join(_skill_names(card)),
            " ".join(_skill_tags(card)),
        ]
    )


def _schema_property_names(schema: Any) -> list[str]:
    if not isinstance(schema, dict):
        return []
    props = schema.get("properties")
    if not isinstance(props, dict):
        return []
    return [str(name) for name in props]


def _skill_names(card: dict[str, Any]) -> list[str]:
    skills = card.get("skills") if isinstance(card.get("skills"), list) else []
    out: list[str] = []
    for skill in skills:
        if isinstance(skill, dict) and skill.get("name"):
            out.append(str(skill["name"]))
    return out


def _skill_tags(card: dict[str, Any]) -> list[str]:
    skills = card.get("skills") if isinstance(card.get("skills"), list) else []
    out: list[str] = []
    for skill in skills:
        if not isinstance(skill, dict):
            continue
        out.extend(str(tag) for tag in skill.get("tags") or [])
    return sorted(set(out))


def _terms(value: str) -> list[str]:
    return [term for term in re.split(r"\W+", value.lower()) if term]


def _visibility_filter(user_id: int, organization_ids: tuple[int, ...] = ()) -> Any:
    models = _qdrant_models()
    should = [
            models.FieldCondition(
                key="public",
                match=models.MatchValue(value=True),
            ),
            models.FieldCondition(
                key="owner_id",
                match=models.MatchValue(value=user_id),
            ),
        ]
    if organization_ids:
        should.append(
            models.FieldCondition(
                key="organization_id",
                match=models.MatchAny(any=list(organization_ids)),
            )
        )
    return models.Filter(should=should)


def _qdrant_client() -> Any:
    try:
        import qdrant_client
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise SemanticAgentSearchUnavailable("qdrant-client is not installed") from exc
    return qdrant_client


def _qdrant_models() -> Any:
    try:
        from qdrant_client import models
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise SemanticAgentSearchUnavailable("qdrant-client is not installed") from exc
    return models


def _fastembed() -> Any:
    try:
        import fastembed
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise SemanticAgentSearchUnavailable("fastembed is not installed") from exc
    return fastembed
