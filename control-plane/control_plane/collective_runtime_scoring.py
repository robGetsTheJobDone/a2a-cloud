"""Outcome scoring helpers for the collective runtime.

These helpers intentionally score existing ledger evidence instead of adding a
new execution path. The goal is to make routing and reputation depend on real
work outcomes that already exist in WorkJob/WorkEvent rows.
"""
from __future__ import annotations

from typing import Any

COMPLETE_STATUSES = {"complete", "completed", "passed", "success", "succeeded"}
FAILED_STATUSES = {"error", "failed", "errored", "blocked", "cancelled", "canceled"}
RUNNING_STATUSES = {"queued", "running", "pending", "leased"}


def score_work_job(job: Any, events: list[Any] | tuple[Any, ...] = ()) -> dict[str, Any]:
    """Return a normalized score payload for one work job.

    The score is deliberately simple and inspectable. It rewards terminal
    success, verification evidence, useful artifacts, and positive review
    signals; it penalizes failure, test failures, blocking reviews, and errors.
    """
    status = str(getattr(job, "status", "") or "").lower()
    output = _mapping(getattr(job, "output_payload", None))
    metadata = _mapping(getattr(job, "metadata_json", None))
    error = str(getattr(job, "error", "") or "")
    reasons: list[str] = []

    if status in COMPLETE_STATUSES:
        score = 0.62
        reasons.append("job completed")
    elif status in FAILED_STATUSES or error:
        score = 0.14
        reasons.append("job failed")
    elif status in RUNNING_STATUSES:
        score = 0.34
        reasons.append("job still in flight")
    else:
        score = 0.28
        reasons.append(f"unrecognized status {status or 'unknown'}")

    metrics = _merged_metrics(output, metadata, events)
    file_ops_count = _file_ops_count(output, events)
    if file_ops_count:
        score += min(0.08, file_ops_count * 0.01)
        reasons.append("file-operation evidence present")

    tests_passed = _int_metric(metrics, "tests_passed")
    tests_failed = _int_metric(metrics, "tests_failed")
    if tests_passed > 0 and tests_failed == 0:
        score += 0.14
        reasons.append("tests passed")
    if tests_failed > 0:
        score -= min(0.28, tests_failed * 0.08)
        reasons.append("tests failed")

    review_result = str(metrics.get("review_result") or "").lower()
    if review_result in {"approved", "passed", "pass", "ok"}:
        score += 0.08
        reasons.append("review passed")
    elif review_result in {"blocked", "failed", "fail", "rejected"}:
        score -= 0.22
        reasons.append("review blocked")

    task_outcome = str(metrics.get("task_outcome") or "").lower()
    if task_outcome in {"accepted", "merged", "resolved", "success"}:
        score += 0.08
        reasons.append("task outcome accepted")
    elif task_outcome in {"reopened", "rolled_back", "rejected"}:
        score -= 0.18
        reasons.append("task outcome negative")

    business_delta = _float_metric(metrics, "business_metric_delta")
    if business_delta > 0:
        score += min(0.08, business_delta)
        reasons.append("positive business metric delta")
    elif business_delta < 0:
        score += max(-0.12, business_delta)
        reasons.append("negative business metric delta")

    if error:
        score -= 0.08
        reasons.append("error recorded")

    score = round(max(0.0, min(1.0, score)), 4)
    return {
        "job_id": getattr(job, "job_id", None),
        "kind": getattr(job, "kind", None),
        "status": getattr(job, "status", None),
        "worker_name": getattr(job, "worker_name", None),
        "protocol_id": protocol_id_for_job(job),
        "score": score,
        "reasons": reasons,
        "metrics": {
            "file_ops_count": file_ops_count,
            "tests_passed": tests_passed,
            "tests_failed": tests_failed,
            "review_result": review_result or None,
            "task_outcome": task_outcome or None,
            "business_metric_delta": business_delta,
            "cost_cents": _float_metric(metrics, "cost_cents"),
            "token_usage": _float_metric(metrics, "token_usage"),
        },
    }


def aggregate_scores(scores: list[dict[str, Any]]) -> dict[str, Any]:
    if not scores:
        return {
            "count": 0,
            "average_score": 0.0,
            "completed_count": 0,
            "failed_count": 0,
            "in_flight_count": 0,
        }
    completed = sum(1 for item in scores if str(item.get("status") or "").lower() in COMPLETE_STATUSES)
    failed = sum(1 for item in scores if str(item.get("status") or "").lower() in FAILED_STATUSES)
    in_flight = sum(1 for item in scores if str(item.get("status") or "").lower() in RUNNING_STATUSES)
    return {
        "count": len(scores),
        "average_score": round(sum(float(item.get("score") or 0.0) for item in scores) / len(scores), 4),
        "completed_count": completed,
        "failed_count": failed,
        "in_flight_count": in_flight,
    }


def protocol_id_for_job(job: Any) -> str:
    for payload in (
        _mapping(getattr(job, "metadata_json", None)),
        _mapping(getattr(job, "input_payload", None)),
        _mapping(getattr(job, "output_payload", None)),
    ):
        value = payload.get("protocol_id") or payload.get("runtime_protocol_id")
        if isinstance(value, str) and value.strip():
            return value.strip()

    kind = str(getattr(job, "kind", "") or "").lower()
    title = str(getattr(job, "title", "") or "").lower()
    source_type = str(getattr(job, "source_type", "") or "").lower()
    text = " ".join((kind, title, source_type))
    if "review" in text or "security" in text:
        return "security_review"
    if "research" in text or "scout" in text:
        return "research_parallel_scouts"
    if "doc" in text:
        return "docs_update"
    if "agent_api" in text or "code" in text:
        return "code_change_with_review"
    return "single_agent_task"


def _merged_metrics(
    output: dict[str, Any],
    metadata: dict[str, Any],
    events: list[Any] | tuple[Any, ...],
) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for source in (metadata, output):
        merged.update(_mapping(source.get("metrics")))
        merged.update({key: value for key, value in source.items() if key.endswith("_count")})
        for key in (
            "tests_passed",
            "tests_failed",
            "review_result",
            "task_outcome",
            "business_metric_delta",
            "cost_cents",
            "token_usage",
        ):
            if key in source:
                merged[key] = source[key]
    for event in events:
        merged.update(_mapping(getattr(event, "metrics", None)))
        payload = _mapping(getattr(event, "payload", None))
        merged.update(_mapping(payload.get("metrics")))
        for key in (
            "tests_passed",
            "tests_failed",
            "review_result",
            "task_outcome",
            "business_metric_delta",
            "cost_cents",
            "token_usage",
        ):
            if key in payload:
                merged[key] = payload[key]
    return merged


def _file_ops_count(output: dict[str, Any], events: list[Any] | tuple[Any, ...]) -> int:
    count = 0
    file_ops = output.get("file_ops")
    if isinstance(file_ops, list):
        count += len(file_ops)
    for event in events:
        payload = _mapping(getattr(event, "payload", None))
        event_file_ops = payload.get("file_ops")
        if isinstance(event_file_ops, list):
            count += len(event_file_ops)
        metrics = _mapping(getattr(event, "metrics", None))
        count += _int_metric(metrics, "file_ops_count")
    return count


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _int_metric(metrics: dict[str, Any], key: str) -> int:
    value = metrics.get(key)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return 0
    return 0


def _float_metric(metrics: dict[str, Any], key: str) -> float:
    value = metrics.get(key)
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return 0.0
    return 0.0
