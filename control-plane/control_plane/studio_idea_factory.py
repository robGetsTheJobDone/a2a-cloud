from __future__ import annotations

import hashlib
import re
from typing import Any


_AUDIENCES = (
    "independent consultants",
    "small ecommerce teams",
    "local service businesses",
    "B2B sales teams",
    "finance operators",
    "people teams",
    "content studios",
    "property managers",
    "customer success teams",
    "software agencies",
)

_WORKFLOWS: tuple[dict[str, Any], ...] = (
    {
        "recipe": "csv_tool",
        "job": "clean and validate messy CSV exports",
        "output": "a corrected table and CSV download",
        "tools": ("inspect_csv", "clean_rows", "download_clean_csv"),
    },
    {
        "recipe": "document_generator",
        "job": "turn a short intake into a polished client document",
        "output": "a preview and named document artifact",
        "tools": ("collect_brief", "generate_document", "download_document"),
    },
    {
        "recipe": "email_assistant",
        "job": "triage a shared inbox and draft policy-grounded replies",
        "output": "a prioritized queue and approval-ready drafts",
        "tools": ("triage_message", "draft_reply", "request_approval"),
    },
    {
        "recipe": "scheduled_monitor",
        "job": "watch one important signal and explain meaningful changes",
        "output": "a live status, history, and bounded alert",
        "tools": ("check_signal", "list_changes", "configure_alert"),
    },
    {
        "recipe": "calculator",
        "job": "calculate a repeatable quote or decision score",
        "output": "an exact result with assumptions and rationale",
        "tools": ("validate_inputs", "calculate_result", "explain_result"),
    },
    {
        "recipe": "approval_workflow",
        "job": "route a consequential request through a clear approval gate",
        "output": "a review packet and auditable decision record",
        "tools": ("prepare_request", "review_policy", "record_decision"),
    },
    {
        "recipe": "dashboard",
        "job": "turn recurring operational updates into a tiny useful dashboard",
        "output": "filters, trends, exceptions, and durable history",
        "tools": ("record_update", "list_metrics", "explain_exception"),
    },
    {
        "recipe": "document_generator",
        "job": "research a company or topic and produce a cited one-page brief",
        "output": "a source-backed preview and downloadable brief",
        "tools": ("research_sources", "build_brief", "download_brief"),
    },
    {
        "recipe": "custom",
        "job": "normalize webhook payloads into one reliable downstream format",
        "output": "a validated payload, failure explanation, and receipt",
        "tools": ("inspect_payload", "transform_payload", "validate_output"),
    },
    {
        "recipe": "dashboard",
        "job": "track a recurring checklist and surface overdue work",
        "output": "an owner-focused board and follow-up list",
        "tools": ("create_checklist", "update_item", "list_overdue"),
    },
)


def generate_startup_ideas(theme: str, *, count: int = 100) -> list[dict[str, Any]]:
    """Generate a deterministic, diverse and scored mini-startup portfolio."""

    clean_theme = (
        " ".join(str(theme or "").split())[:120] or "useful one-page businesses"
    )
    seed = int(hashlib.sha256(clean_theme.encode("utf-8")).hexdigest()[:8], 16)
    ideas: list[dict[str, Any]] = []
    for index in range(max(1, min(int(count), 100))):
        audience_index = index % len(_AUDIENCES)
        workflow_index = (index // len(_AUDIENCES)) % len(_WORKFLOWS)
        audience = _AUDIENCES[audience_index]
        workflow = _WORKFLOWS[workflow_index]
        usefulness = 6 + ((seed + audience_index + workflow_index * 3) % 5)
        recurrence = 5 + ((seed // 7 + audience_index * 2 + workflow_index) % 6)
        viability = 5 + ((seed // 11 + audience_index + workflow_index * 2) % 6)
        platform_fit = 7 + ((seed // 13 + workflow_index) % 4)
        buildability = 7 + ((seed // 17 + 9 - workflow_index) % 4)
        total = round(
            usefulness * 0.28
            + recurrence * 0.20
            + viability * 0.18
            + platform_fit * 0.20
            + buildability * 0.14,
            2,
        )
        title = _title(str(workflow["job"]), audience)
        suffix = f"-{index + 1:03d}"
        slug_prefix = _slug(f"{clean_theme}-{workflow['recipe']}-{audience_index + 1}")
        slug = f"{slug_prefix[: 40 - len(suffix)].rstrip('-')}{suffix}"
        goal = (
            f"Build a one-page {str(workflow['recipe']).replace('_', ' ')} for {audience} that "
            f"helps them {workflow['job']}. The product must return {workflow['output']}. "
            f"Keep the first workflow useful in under two minutes. Theme: {clean_theme}."
        )
        ideas.append(
            {
                "rank": 0,
                "idea_id": f"idea-{index + 1:03d}",
                "name": slug,
                "title": title,
                "goal": goal,
                "audience": audience,
                "recipe": workflow["recipe"],
                "mcp_tools": list(workflow["tools"]),
                "scores": {
                    "usefulness": usefulness,
                    "recurrence": recurrence,
                    "viability": viability,
                    "platform_fit": platform_fit,
                    "buildability": buildability,
                    "total": total,
                },
            }
        )
    ideas.sort(key=lambda item: (-item["scores"]["total"], item["idea_id"]))
    for rank, idea in enumerate(ideas, start=1):
        idea["rank"] = rank
    return ideas


def _slug(value: str) -> str:
    return (
        re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", value.lower())).strip("-")
        or "mini-startup"
    )


def _title(job: str, audience: str) -> str:
    return f"{' '.join(job.split()[:5]).capitalize()} for {' '.join(audience.split()[:3])}"[
        :100
    ]


__all__ = ["generate_startup_ideas"]
