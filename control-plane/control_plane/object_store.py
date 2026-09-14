"""Thin object-store wrapper for replay session event tuples.

Sessions land in a dedicated bucket so the operator can size + retain them
independently of user file storage. Connection settings reuse the same
S3-compatible boto3 client style as :mod:`control_plane.minio_client` —
MinIO in prod, any S3 endpoint in dev — driven by env vars:

    * ``A2A_CP_MINIO_ENDPOINT`` — S3 endpoint (shared with minio_client).
    * ``A2A_CP_MINIO_ACCESS_KEY`` / ``A2A_CP_MINIO_SECRET_KEY`` — credentials.
    * ``REPLAY_SESSIONS_BUCKET`` — bucket name. Defaults to
      ``a2a-replay-sessions``.

The wrapper is intentionally narrow: write a JSONL blob from a list of
event dicts, iterate the blob back as parsed dicts with optional skip.
Tests inject :class:`InMemoryReplayObjectStore` via :func:`set_default_store`.
"""
from __future__ import annotations

import json
import os
from typing import Iterable, Iterator, Protocol


REPLAY_SESSIONS_BUCKET_ENV = "REPLAY_SESSIONS_BUCKET"
_DEFAULT_BUCKET = "a2a-replay-sessions"


def replay_sessions_bucket() -> str:
    return os.environ.get(REPLAY_SESSIONS_BUCKET_ENV, _DEFAULT_BUCKET)


def session_events_key(agent_id: int | None, session_id: str) -> str:
    """Stable object key for a session's events blob."""
    return f"sessions/{agent_id or 'unbound'}/{session_id}.jsonl"


class ReplayObjectStore(Protocol):
    def put_jsonl(self, key: str, records: Iterable[dict]) -> int: ...

    def iter_jsonl(self, key: str, *, since: int = 0) -> Iterator[dict]: ...


class S3ReplayObjectStore:
    """Production object store backed by the shared MinIO/S3 client.

    Lazy-imports :mod:`control_plane.minio_client` so unit tests that swap
    in :class:`InMemoryReplayObjectStore` don't pay the boto3 import cost
    or require live S3 credentials.
    """

    def __init__(self, bucket: str | None = None) -> None:
        self._bucket = bucket or replay_sessions_bucket()

    def put_jsonl(self, key: str, records: Iterable[dict]) -> int:
        from .minio_client import _S3, ensure_bucket

        ensure_bucket(self._bucket)
        lines = [json.dumps(r, separators=(",", ":")) for r in records]
        body = ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")
        _S3.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=body,
            ContentType="application/x-ndjson",
        )
        return len(lines)

    def iter_jsonl(self, key: str, *, since: int = 0) -> Iterator[dict]:
        from botocore.exceptions import ClientError

        from .minio_client import _S3, ensure_bucket

        ensure_bucket(self._bucket)
        try:
            resp = _S3.get_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in {"404", "NoSuchKey", "NotFound"}:
                raise FileNotFoundError(key) from exc
            raise
        stream = resp["Body"]
        try:
            buffer = b""
            idx = 0
            for chunk in stream.iter_chunks(chunk_size=64 * 1024):
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if not line.strip():
                        continue
                    if idx >= since:
                        yield json.loads(line.decode("utf-8"))
                    idx += 1
            if buffer.strip():
                if idx >= since:
                    yield json.loads(buffer.decode("utf-8"))
        finally:
            stream.close()


class InMemoryReplayObjectStore:
    """Dict-backed store for tests. Records are kept as parsed dicts.

    Identical wire semantics to :class:`S3ReplayObjectStore` for the two
    methods the router actually uses.
    """

    def __init__(self) -> None:
        self._blobs: dict[str, list[dict]] = {}

    def put_jsonl(self, key: str, records: Iterable[dict]) -> int:
        materialized = [dict(r) for r in records]
        self._blobs[key] = materialized
        return len(materialized)

    def iter_jsonl(self, key: str, *, since: int = 0) -> Iterator[dict]:
        if key not in self._blobs:
            raise FileNotFoundError(key)
        for idx, record in enumerate(self._blobs[key]):
            if idx >= since:
                yield record

    # Test-only conveniences. The protocol does not require these.
    def has(self, key: str) -> bool:
        return key in self._blobs

    def get_all(self, key: str) -> list[dict]:
        return list(self._blobs.get(key, ()))


_default_store: ReplayObjectStore | None = None


def get_default_store() -> ReplayObjectStore:
    """Module-level singleton. Lazy-instantiated so import is side-effect free."""
    global _default_store
    if _default_store is None:
        _default_store = S3ReplayObjectStore()
    return _default_store


def set_default_store(store: ReplayObjectStore | None) -> None:
    """Override the default store (test hook). Pass ``None`` to reset."""
    global _default_store
    _default_store = store


__all__ = [
    "InMemoryReplayObjectStore",
    "REPLAY_SESSIONS_BUCKET_ENV",
    "ReplayObjectStore",
    "S3ReplayObjectStore",
    "get_default_store",
    "replay_sessions_bucket",
    "session_events_key",
    "set_default_store",
]
