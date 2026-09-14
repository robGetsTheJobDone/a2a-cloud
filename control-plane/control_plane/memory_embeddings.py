from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any

import httpx

from .config import settings

TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


class MemoryEmbeddingError(RuntimeError):
    """Raised when a memory embedding provider cannot return usable vectors."""


def memory_embedding_text(*, key: str, value: Any, metadata: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"Key: {key}",
            "Value: " + json.dumps(value, sort_keys=True, default=str),
            "Metadata: " + json.dumps(metadata, sort_keys=True, default=str),
        ]
    )


async def embed_memory_texts(texts: list[str]) -> list[list[float]]:
    provider = settings.memory_embedding_provider.strip().lower()
    if provider == "litellm":
        return await _litellm_embeddings(texts)
    if provider in {"", "local", "hash"}:
        return [_local_embedding(text, settings.memory_embedding_dimension) for text in texts]
    raise MemoryEmbeddingError(f"unsupported memory embedding provider: {provider}")


async def _litellm_embeddings(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    url = settings.litellm_url.rstrip("/") + "/v1/embeddings"
    headers = {"authorization": f"Bearer {settings.litellm_key}"}
    try:
        async with httpx.AsyncClient(timeout=settings.memory_embedding_timeout_seconds) as client:
            response = await client.post(
                url,
                headers=headers,
                json={
                    "model": settings.memory_embedding_model,
                    "input": texts,
                },
            )
    except httpx.HTTPError as exc:
        raise MemoryEmbeddingError(f"LiteLLM embeddings request failed: {exc}") from exc
    if response.status_code >= 400:
        raise MemoryEmbeddingError(
            f"LiteLLM embeddings failed with HTTP {response.status_code}: {response.text[:500]}"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise MemoryEmbeddingError("LiteLLM embeddings response was not JSON") from exc
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise MemoryEmbeddingError("LiteLLM embeddings response missing data")
    vectors_by_index: dict[int, list[float]] = {}
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            continue
        raw_vector = item.get("embedding")
        if not isinstance(raw_vector, list):
            continue
        try:
            parsed = [float(value) for value in raw_vector]
        except (TypeError, ValueError) as exc:
            raise MemoryEmbeddingError("LiteLLM embeddings response contains non-numeric values") from exc
        vectors_by_index[int(item.get("index", index))] = _normalize(parsed)
    vectors = [vectors_by_index.get(index) for index in range(len(texts))]
    if any(vector is None for vector in vectors):
        raise MemoryEmbeddingError("LiteLLM embeddings response did not include every input")
    return [vector for vector in vectors if vector is not None]


def _local_embedding(text: str, dimensions: int) -> list[float]:
    size = max(32, min(int(dimensions or 256), 2048))
    vector = [0.0] * size
    for token in TOKEN_RE.findall(text.casefold()):
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % size
        vector[index] += 1.0
    return _normalize(vector)


def _normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm <= 0:
        return vector
    return [value / norm for value in vector]
