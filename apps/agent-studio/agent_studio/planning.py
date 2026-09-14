from __future__ import annotations

import re

from .models import (
    AppSpec,
    CapabilityDecision,
    DistributionSpec,
    LaunchCapabilityName,
    LaunchPlan,
    StartupRecipe,
)


_RECIPE_PATTERNS: tuple[tuple[StartupRecipe, re.Pattern[str]], ...] = (
    ("csv_tool", re.compile(r"\b(csv|spreadsheet|rows?|columns?)\b", re.I)),
    (
        "document_generator",
        re.compile(r"\b(document|report|proposal|contract|pdf|docx)\b", re.I),
    ),
    (
        "email_assistant",
        re.compile(r"\b(emails?|inbox|mailbox|reply|support ticket)\b", re.I),
    ),
    (
        "scheduled_monitor",
        re.compile(r"\b(monitor|watch|alert|schedule|recurring|daily|weekly)\b", re.I),
    ),
    (
        "calculator",
        re.compile(r"\b(calculate|calculator|estimate|quote|score|convert)\b", re.I),
    ),
    (
        "approval_workflow",
        re.compile(
            r"\b(approve|approval|review gate|sign[ -]?off|refund|payout|charge)\b",
            re.I,
        ),
    ),
    (
        "dashboard",
        re.compile(
            r"\b(dashboard|tracker|portal|analytics|visuali[sz]e|chart)\b", re.I
        ),
    ),
)


def infer_recipe(goal: str, requested: StartupRecipe = "auto") -> StartupRecipe:
    if requested != "auto":
        return requested
    for recipe, pattern in _RECIPE_PATTERNS:
        if pattern.search(goal):
            return recipe
    return "custom"


def build_launch_plan(goal: str, app_spec: AppSpec) -> LaunchPlan:
    """Turn the brief into an explicit, reviewable platform-capability plan."""

    text = " ".join([goal, app_spec.primary_workflow, *app_spec.integrations])
    explicit = set(app_spec.capabilities)
    decisions: dict[LaunchCapabilityName, tuple[bool, str]] = {
        "frontend": (
            app_spec.product_ui
            or app_spec.profile == "full_stack"
            or "frontend" in explicit,
            "product UI/full-stack contract"
            if app_spec.product_ui or app_spec.profile == "full_stack"
            else "not requested",
        ),
        "database": (
            app_spec.persistence or "database" in explicit,
            "durable persistence/reload contract"
            if app_spec.persistence
            else "not requested",
        ),
        "files": (
            app_spec.uploads
            or bool(
                re.search(r"\b(file|upload|csv|pdf|document|artifact)\b", text, re.I)
            )
            or "files" in explicit,
            "file/upload workflow"
            if app_spec.uploads
            or re.search(r"\b(file|upload|csv|pdf|document|artifact)\b", text, re.I)
            else "not requested",
        ),
        "email": (
            bool(re.search(r"\b(emails?|inbox|mailbox|gmail)\b", text, re.I))
            or "email" in explicit,
            "email integration detected"
            if re.search(r"\b(emails?|inbox|mailbox|gmail)\b", text, re.I)
            else "not requested",
        ),
        "schedules": (
            bool(
                re.search(
                    r"\b(schedule|recurring|daily|weekly|monitor|watch|alert)\b",
                    text,
                    re.I,
                )
            )
            or "schedules" in explicit,
            "scheduled/monitoring workflow detected"
            if re.search(
                r"\b(schedule|recurring|daily|weekly|monitor|watch|alert)\b", text, re.I
            )
            else "not requested",
        ),
        "payments": (
            bool(
                re.search(
                    r"\b(stripe|payment|charge|refund|payout|invoice)\b", text, re.I
                )
            )
            or "payments" in explicit,
            "payment or money movement detected"
            if re.search(
                r"\b(stripe|payment|charge|refund|payout|invoice)\b", text, re.I
            )
            else "not requested",
        ),
        "browser": (
            bool(app_spec.browser_journeys)
            or app_spec.product_ui
            or "browser" in explicit,
            "production UI needs browser proof"
            if app_spec.product_ui or app_spec.browser_journeys
            else "not requested",
        ),
        "mcp": (
            app_spec.requires_mcp or "mcp" in explicit,
            "MCP distribution contract" if app_spec.requires_mcp else "not requested",
        ),
        "auth": (
            app_spec.auth == "platform"
            or app_spec.account_trial_calls > 0
            or "auth" in explicit,
            "platform account/trial contract"
            if app_spec.auth == "platform" or app_spec.account_trial_calls > 0
            else "not requested",
        ),
        "artifacts": (
            bool(app_spec.output_expectations) or "artifacts" in explicit,
            "user-visible output contract"
            if app_spec.output_expectations
            else "not requested",
        ),
    }
    return LaunchPlan(
        recipe=infer_recipe(text, app_spec.recipe),
        capabilities=[
            CapabilityDecision(capability=name, required=required, reason=reason)
            for name, (required, reason) in decisions.items()
        ],
        account_trial_calls=app_spec.account_trial_calls,
        distribution=DistributionSpec.model_validate(
            app_spec.distribution.model_dump()
        ),
    )


__all__ = ["build_launch_plan", "infer_recipe"]
