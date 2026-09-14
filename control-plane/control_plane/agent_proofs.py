from __future__ import annotations

import json
from hashlib import sha256
from typing import Any


def card_hash(card: dict[str, Any]) -> str:
    raw = json.dumps(card or {}, sort_keys=True, separators=(",", ":"))
    return sha256(raw.encode("utf-8")).hexdigest()


def proof_badge(status: str | None) -> str:
    if status == "passed":
        return "verified"
    if status == "failed":
        return "degraded"
    return "unverified"


def preview_args(args: dict[str, Any], limit: int = 240) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in args.items():
        if isinstance(value, str) and len(value) > limit:
            out[key] = value[:limit] + f"... (+{len(value) - limit} chars)"
        else:
            out[key] = value
    return out


def sample_args_from_schema(schema: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(schema, dict):
        return {}
    value = _sample_value(schema)
    return value if isinstance(value, dict) else {}


def _sample_value(schema: dict[str, Any]) -> Any:
    if "default" in schema:
        return schema["default"]
    if "const" in schema:
        return schema["const"]
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return enum[0]

    typ = schema.get("type")
    if isinstance(typ, list):
        typ = next((t for t in typ if t != "null"), typ[0] if typ else None)

    if typ == "object" or isinstance(schema.get("properties"), dict):
        props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        required = schema.get("required") if isinstance(schema.get("required"), list) else []
        keys = required or list(props.keys())
        out: dict[str, Any] = {}
        for key in keys:
            child = props.get(key)
            if isinstance(child, dict):
                out[key] = _sample_value(child)
        return out
    if typ == "array":
        return []
    if typ == "integer":
        return 1
    if typ == "number":
        return 1.0
    if typ == "boolean":
        return True
    if typ == "string":
        fmt = schema.get("format")
        if fmt == "date":
            return "2026-01-01"
        if fmt == "date-time":
            return "2026-01-01T00:00:00Z"
        if fmt == "email":
            return "user@example.com"
        if fmt == "uri":
            return "https://example.com"
        return "sample"
    return None


def summarize_result(result: Any, file_ops: list[dict[str, Any]]) -> str:
    if isinstance(result, dict):
        if result.get("error"):
            return f"error: {str(result['error'])[:160]}"
        for key in ("summary", "message", "chart_path", "output_path", "path", "url"):
            value = result.get(key)
            if isinstance(value, str) and value:
                return f"{key}={value[:180]}"
    if file_ops:
        return f"ok, {len(file_ops)} file change{'s' if len(file_ops) != 1 else ''}"
    if isinstance(result, dict) and result:
        return "returned structured output"
    return "ok"


def public_proof_summary(
    status: str | None,
    *,
    events_count: int = 0,
    file_ops_count: int = 0,
) -> str:
    """One safe sentence about a proof run, for unauthenticated readers.

    Deliberately *derived*, never :attr:`AgentProofRun.summary`. The stored
    column comes out of :func:`summarize_result`, which reads the agent's own
    return value: in every case that carries information it is a result excerpt
    (``message=...``, ``summary=...``) or a workspace path
    (``output_path=...``, ``chart_path=...``) — exactly the material the public
    projection withholds. Strip those and what is left is a statement about the
    run's outcome and size, which is what this rebuilds from metadata the
    projection already publishes. So the public sentence carries no payload by
    construction, not by filtering.

    Failures say only that the run did not pass: ``AgentProofRun.error`` is the
    agent's own exception text and is withheld for the same reason.
    """
    if status == "passed":
        if file_ops_count > 0:
            noun = "file operation" if file_ops_count == 1 else "file operations"
            return f"completed successfully with {file_ops_count} recorded {noun}"
        if events_count > 0:
            noun = "event" if events_count == 1 else "events"
            return f"completed successfully with {events_count} recorded {noun}"
        return "completed successfully in a platform-verified run"
    if status == "failed":
        return "did not complete successfully"
    return "verification has not completed"


def result_is_error(result: Any) -> bool:
    return isinstance(result, dict) and bool(result.get("error"))
