"""Typed kernel contract helpers.

These models own internal construction for kernel evidence and still emit the
plain JSON payloads expected by WorkEvent rows, thread events, and dashboards.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

JsonObject = dict[str, Any]
EvidencePayloadT = TypeVar("EvidencePayloadT", bound=JsonObject)


@dataclass(frozen=True)
class KernelEvidenceEvent(Generic[EvidencePayloadT]):
    evidence_kind: str
    event_type: str
    payload: EvidencePayloadT
    evidence_key: str | None = None
    title: str | None = None
    status: str | None = None
    severity: str | None = None
    message: str | None = None
    source: JsonObject = field(default_factory=dict)

    def to_payload(self) -> JsonObject:
        out: JsonObject = {
            "type": "evidence_event",
            "evidence_kind": self.evidence_kind,
            "event_type": self.event_type,
            "title": self.title,
            "status": self.status,
            "severity": self.severity,
            "message": self.message,
            "source": self.source,
            "payload": self.payload,
        }
        if self.evidence_key is not None:
            out["evidence_key"] = self.evidence_key
        return out


def evidence_key(
    event_type: str,
    payload: JsonObject,
    *,
    source: JsonObject | None = None,
    source_event_id: Any = None,
) -> str:
    parts: list[str] = [event_type]
    source_payload = source or {}
    for key in ("kind", "dag_run_id", "node_id", "job_id", "agent", "skill"):
        value = source_payload.get(key)
        if value is not None:
            parts.append(f"{key}:{value}")
    if source_event_id is not None:
        parts.append(f"event:{source_event_id}")
    for key in (
        "suite_id",
        "scenario_id",
        "invariant_id",
        "attempt_id",
        "decision_id",
        "episode_id",
        "arena_id",
        "event_id",
        "id",
    ):
        value = payload.get(key)
        if value is not None:
            parts.append(f"{key}:{value}")
    if len(parts) == 1:
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode()
        ).hexdigest()[:16]
        parts.append(f"payload:{digest}")
    return "|".join(str(part) for part in parts)
