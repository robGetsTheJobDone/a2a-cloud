from __future__ import annotations

import hashlib
import json
from typing import Any


def stable_hash(value: Any) -> str:
    """Hash JSON-like data with stable key ordering for receipts."""
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def evaluate_trial_result(
    result: dict[str, Any],
    file_ops: list[dict[str, Any]],
    output_schema: dict[str, Any] | None,
    acceptance_criteria: str,
) -> tuple[str, int, str]:
    """Return ``(status, score, notes)`` for an agent trial run.

    This is intentionally deterministic. Rich LLM-as-judge grading can layer
    on later, but the first buyer-trust loop needs explainable scoring.
    """
    if result.get("error"):
        return "failed", 0, f"agent returned error: {str(result['error'])[:180]}"

    score = 50
    notes: list[str] = ["agent returned a non-error result"]
    schema = output_schema if isinstance(output_schema, dict) else {}
    required = schema.get("required") if isinstance(schema.get("required"), list) else []
    if required:
        missing = [key for key in required if key not in result]
        if missing:
            notes.append(f"missing required output keys: {', '.join(map(str, missing))}")
        else:
            score += 30
            notes.append("result includes all required output keys")
    else:
        score += 20
        notes.append("no strict output schema was configured")

    changed = [op for op in file_ops if op.get("op") in {"create", "update"}]
    if changed:
        score += 20
        notes.append(f"produced {len(changed)} file artifact(s)")
    elif _criteria_implies_artifact(acceptance_criteria):
        notes.append("acceptance criteria mentions artifacts, but no files changed")
    else:
        score += 10
        notes.append("no file artifacts required")

    score = min(score, 100)
    return ("passed" if score >= 70 else "failed"), score, "; ".join(notes)


def summarize_trial_receipt(
    *,
    result: dict[str, Any],
    input_files: list[dict[str, Any]],
    file_ops: list[dict[str, Any]],
    output_schema: dict[str, Any] | None,
    status: str,
    score: int,
    elapsed_ms: int | None,
) -> dict[str, Any]:
    """Small buyer-readable receipt summary for comparison UIs."""
    schema = output_schema if isinstance(output_schema, dict) else {}
    raw_required = schema.get("required")
    required = [str(key) for key in raw_required] if isinstance(raw_required, list) else []
    result_keys = sorted(str(key) for key in result.keys())
    missing = [key for key in required if key not in result]
    changed = [op for op in file_ops if op.get("op") in {"create", "update"}]
    input_errors = [item for item in input_files if item.get("error")]
    return {
        "status": status,
        "score": score,
        "elapsed_ms": elapsed_ms,
        "input_count": len(input_files),
        "input_error_count": len(input_errors),
        "artifact_count": len(changed),
        "required_keys": required,
        "result_keys": result_keys,
        "missing_required_keys": missing,
    }


def build_trial_args(
    *,
    skill: dict[str, Any],
    goal: str,
    input_paths: list[str],
    output_prefix: str,
    acceptance_criteria: str,
    output_schema: dict[str, Any],
) -> dict[str, Any]:
    """Construct useful default args from a skill schema.

    Agents are not standardized yet, so we fill common field names when present.
    If the schema is opaque, send a generic object every agent can inspect.
    """
    schema = skill.get("input_schema") if isinstance(skill, dict) else {}
    props = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(props, dict):
        return {
            "goal": goal,
            "input_paths": input_paths,
            "output_prefix": output_prefix,
            "acceptance_criteria": acceptance_criteria,
            "output_schema": output_schema,
        }

    out: dict[str, Any] = {}
    for key in props:
        lowered = key.lower()
        if lowered in {"goal", "task", "prompt", "instructions", "request"}:
            out[key] = goal
        elif lowered in {"input_paths", "file_paths", "files", "sources"}:
            out[key] = input_paths
        elif lowered in {"input_path", "file_path", "source"} and input_paths:
            out[key] = input_paths[0]
        elif lowered in {"output_prefix", "destination_prefix", "write_prefix"}:
            out[key] = output_prefix
        elif lowered in {"acceptance_criteria", "criteria", "rubric"}:
            out[key] = acceptance_criteria
        elif lowered in {"output_schema", "schema"}:
            out[key] = output_schema

    if out:
        return out
    return {
        "goal": goal,
        "input_paths": input_paths,
        "output_prefix": output_prefix,
        "acceptance_criteria": acceptance_criteria,
        "output_schema": output_schema,
    }


def receipt_id(receipt: dict[str, Any]) -> str:
    return stable_hash({k: v for k, v in receipt.items() if k != "receipt_id"})[:24]


def _criteria_implies_artifact(criteria: str) -> bool:
    lowered = criteria.lower()
    return any(
        token in lowered
        for token in ("file", "csv", "pdf", "report", "artifact", "spreadsheet", "json")
    )
