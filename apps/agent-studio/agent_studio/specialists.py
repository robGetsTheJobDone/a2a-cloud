from __future__ import annotations

import asyncio
import math
import re
from typing import Any

import httpx
from a2a_pack import WorkspaceMode
from a2a_pack.grants import verify_grant

from .browser_qa import BrowserProofRunner
from .models import (
    AcceptanceCall,
    BuildBrief,
    EvaluationResult,
    MutationRecord,
    OutputExpectation,
    ReviewSummary,
)
from .platform import PlatformHelperClient, _owner_from_repo_url

MAX_SMOKE_SKILLS = 3
CODE_EDITOR_AGENT_TARGET = "https://code-editor-agent.a2acloud.io"
BUILDER_HANDOFF_TIMEOUT_SECONDS = 3300.0
BUILDER_WORKSPACE_TTL_SECONDS = 3540
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")


class AgentStudioSpecialists:
    """Production specialist adapter backed by A2A handoff and CP helpers."""

    def __init__(
        self,
        ctx: Any,
        *,
        platform: PlatformHelperClient | None = None,
        builder_agent: str = "agent-builder",
        reviewer_agent: str = "agent-reviewer",
        code_editor_agent: str = CODE_EDITOR_AGENT_TARGET,
    ) -> None:
        self._ctx = ctx
        self._platform = platform or PlatformHelperClient.from_context(ctx)
        self._builder_agent = builder_agent
        self._reviewer_agent = reviewer_agent
        self._code_editor_agent = code_editor_agent

    async def build_agent(
        self,
        *,
        name: str,
        brief: BuildBrief,
        public: bool,
        version: str,
    ) -> dict[str, Any]:
        args = {
            "name": name,
            "prompt": brief.to_prompt(),
            "public": public,
            "version": version,
        }
        if brief.organization_slug:
            args["organization_slug"] = brief.organization_slug
        progress_stop = asyncio.Event()
        progress_task: asyncio.Task[None] | None = None
        try:
            grant = await _delegate_builder_workspace(
                self._ctx,
                name=name,
                audience=self._builder_agent,
            )
            progress_task = _start_builder_progress_mirror(
                self._ctx,
                grant=grant,
                stop=progress_stop,
            )
            call_result = await self._ctx.call(
                self._builder_agent,
                "build",
                args=args,
                grant=grant,
                timeout=BUILDER_HANDOFF_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            return {"ok": False, "name": name, "error": "agent-builder timed out"}
        except PermissionError as exc:
            if "no A2A client attached" in str(exc):
                return {
                    "ok": False,
                    "name": name,
                    "error": (
                        "agent-studio runtime is missing an outbound A2A client; "
                        "invoke through the platform orchestrator or attach a "
                        "client before calling agent-builder"
                    ),
                }
            return {
                "ok": False,
                "name": name,
                "error": _exception_summary("agent-builder handoff failed", exc),
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "name": name,
                "error": _exception_summary("agent-builder handoff failed", exc),
            }
        finally:
            progress_stop.set()
            if progress_task is not None:
                await progress_task

        result = call_result.result if hasattr(call_result, "result") else call_result
        if not isinstance(result, dict):
            return {
                "ok": False,
                "name": name,
                "error": "agent-builder returned non-object",
            }
        out = dict(result)
        if out.get("error"):
            out["ok"] = False
        if out.get("ok") and not isinstance(out.get("url"), str):
            out["ok"] = False
            out["warning"] = out.get("warning") or "agent-builder returned no live URL"
        out.setdefault("name", name)
        if hasattr(call_result, "events"):
            out["events_count"] = len(getattr(call_result, "events") or ())
        if hasattr(call_result, "artifacts"):
            out["artifacts_count"] = len(getattr(call_result, "artifacts") or ())
        return out

    async def load_live_state(self, *, name: str) -> dict[str, Any]:
        return await self._platform.refresh_agent(name)

    async def publish_agent(self, *, name: str) -> dict[str, Any]:
        return await self._platform.publish_agent(name)

    async def verify_distribution(
        self,
        *,
        name: str,
        brief: BuildBrief,
        live_state: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._platform.verify_distribution(
            name=name,
            url=str(live_state.get("url") or ""),
            repo_url=str(live_state.get("repo_url") or ""),
            spec=brief.app_spec.distribution,
        )

    async def evaluate(
        self,
        *,
        name: str,
        brief: BuildBrief,
        live_state: dict[str, Any],
    ) -> list[EvaluationResult]:
        if not live_state.get("ok"):
            return [
                EvaluationResult(
                    name="live-state",
                    status="fail",
                    summary=str(
                        live_state.get("error") or "could not load live agent state"
                    ),
                    details={"status_code": live_state.get("status_code")},
                )
            ]
        card = (
            live_state.get("card") if isinstance(live_state.get("card"), dict) else {}
        )
        skills = card.get("skills") if isinstance(card.get("skills"), list) else []
        if not skills:
            return [
                EvaluationResult(
                    name="agent-card",
                    status="fail",
                    summary="live Agent Card has no skills",
                    details={"agent": name},
                )
            ]
        app_spec = brief.app_spec
        results = [
            EvaluationResult(
                name="agent-card",
                status="pass",
                summary=f"live Agent Card exposes {len(skills)} skill(s)",
                details={
                    "agent": name,
                    "version": card.get("version"),
                    "skills": [
                        skill.get("name") for skill in skills if isinstance(skill, dict)
                    ],
                },
            )
        ]
        if app_spec.account_trial_calls:
            runtime = (
                card.get("runtime") if isinstance(card.get("runtime"), dict) else {}
            )
            access = (
                runtime.get("account_access")
                if isinstance(runtime.get("account_access"), dict)
                else {}
            )
            access_ok = (
                access.get("required") is True
                and access.get("platform_skill_calls") == app_spec.account_trial_calls
                and access.get("after_trial") == "byok"
            )
            results.append(
                EvaluationResult(
                    name="account-funded-trial",
                    status="pass" if access_ok else "fail",
                    summary=(
                        f"account receives {app_spec.account_trial_calls} platform-funded skill calls before BYOK"
                        if access_ok
                        else "live Agent Card does not match the required account trial/BYOK policy"
                    ),
                    details={
                        "expected_calls": app_spec.account_trial_calls,
                        "required": access.get("required"),
                        "observed_calls": access.get("platform_skill_calls"),
                        "after_trial": access.get("after_trial"),
                    },
                )
            )
        receipt_before: set[str] = set()
        if app_spec.requires_receipt:
            baseline = await self._platform.list_receipts(name)
            if baseline.get("ok"):
                receipt_before = {
                    str(item.get("receipt_id"))
                    for item in baseline.get("receipts") or []
                    if isinstance(item, dict) and item.get("receipt_id")
                }
            else:
                results.append(
                    EvaluationResult(
                        name="execution-receipt-baseline",
                        status="fail",
                        summary=str(
                            baseline.get("error") or "could not read receipt baseline"
                        ),
                        details={"status_code": baseline.get("status_code")},
                    )
                )

        if app_spec.persistence:
            databases = (
                ((card.get("runtime") or {}).get("platform_resources") or {}).get(
                    "databases"
                )
                if isinstance(card.get("runtime"), dict)
                else None
            )
            if isinstance(databases, list) and databases:
                results.append(
                    EvaluationResult(
                        name="managed-database",
                        status="pass",
                        summary=f"live Agent Card declares {len(databases)} managed database(s)",
                    )
                )
            else:
                results.append(
                    EvaluationResult(
                        name="managed-database",
                        status="fail",
                        summary="application contract requires persistence but the live Card declares no managed database",
                    )
                )

        smoke_target = _smoke_target(name=name, live_state=live_state, ctx=self._ctx)
        smoke_grant = await _delegate_target_workspace_if_needed(
            self._ctx,
            name=name,
            card=card,
        )
        acceptance_tools = {call.tool for call in app_spec.acceptance_calls}
        output_contract_checked = False
        for call in app_spec.acceptance_calls:
            acceptance, output_results = await self._acceptance_call(
                target=smoke_target,
                call=call,
                grant=smoke_grant,
                output_expectations=(
                    app_spec.output_expectations
                    if call.purpose == "success" and not output_contract_checked
                    else []
                ),
            )
            results.append(acceptance)
            results.extend(output_results)
            output_contract_checked = output_contract_checked or bool(output_results)
        if app_spec.output_expectations and not output_contract_checked:
            results.append(
                EvaluationResult(
                    name="output-contract",
                    status="warning",
                    summary="artifact/output contract is exercised by browser proof; add a deterministic success call for API-level artifact proof",
                    details={"actionable": False},
                )
            )

        generic_skills = [
            skill
            for skill in skills
            if isinstance(skill, dict)
            and str(skill.get("name") or skill.get("id") or "") not in acceptance_tools
        ]
        for skill in generic_skills[:MAX_SMOKE_SKILLS]:
            results.append(
                await self._smoke_skill(
                    name=name,
                    target=smoke_target,
                    skill=skill,
                    grant=smoke_grant,
                )
            )
        if len(generic_skills) > MAX_SMOKE_SKILLS:
            results.append(
                EvaluationResult(
                    name="smoke-skill-limit",
                    status="warning",
                    summary=(
                        f"smoked {MAX_SMOKE_SKILLS} additional skills plus "
                        f"{len(app_spec.acceptance_calls)} contract calls"
                    ),
                    details={"total_skills": len(skills), "smoked": MAX_SMOKE_SKILLS},
                )
            )

        if app_spec.product_ui or app_spec.profile == "full_stack":
            frontend = await self._platform.probe_frontend(
                str(live_state.get("url") or "")
            )
            results.append(
                EvaluationResult(
                    name="packed-frontend",
                    status="pass" if frontend.get("ok") else "fail",
                    summary=(
                        "packed frontend, runtime config, and normalized browser client loaded without leak markers"
                        if frontend.get("ok")
                        else str(
                            frontend.get("error") or "packed frontend probe failed"
                        )
                    ),
                    details={
                        "statuses": frontend.get("statuses"),
                        "auth_mode": frontend.get("auth_mode"),
                        "status_code": frontend.get("status_code"),
                    },
                )
            )
            for journey in app_spec.browser_journeys:
                browser = await BrowserProofRunner(self._ctx).run(
                    base_url=str(live_state.get("url") or ""),
                    journey=journey,
                    authorization=f"Bearer {getattr(self._ctx, 'cp_jwt', '')}"
                    if getattr(self._ctx, "cp_jwt", "")
                    else "",
                )
                results.append(
                    EvaluationResult(
                        name=f"browser:{journey.name}",
                        status="pass" if browser.get("ok") else "fail",
                        summary=(
                            f"real browser completed {len(browser.get('completed_steps') or [])} declared UI steps"
                            if browser.get("ok")
                            else str(browser.get("error") or "browser journey failed")
                        ),
                        details={
                            "completed_steps": browser.get("completed_steps") or [],
                            "screenshot": browser.get("screenshot"),
                            "console_errors": browser.get("console_errors") or [],
                            "page_errors": browser.get("page_errors") or [],
                        },
                    )
                )

        if app_spec.requires_mcp:
            mcp = await self._platform.list_mcp_tools(str(live_state.get("url") or ""))
            tool_names = {
                str(item.get("name"))
                for item in mcp.get("tools") or []
                if isinstance(item, dict) and item.get("name")
            }
            expected_names = {
                str(skill.get("name") or skill.get("id") or "")
                for skill in skills
                if isinstance(skill, dict)
            }
            missing = sorted(expected_names - tool_names)
            results.append(
                EvaluationResult(
                    name="mcp:tools-list",
                    status="pass" if mcp.get("ok") and not missing else "fail",
                    summary=(
                        f"MCP tools/list exposes {len(tool_names)} tools"
                        if mcp.get("ok") and not missing
                        else str(mcp.get("error") or f"MCP is missing tools: {missing}")
                    ),
                    details={"tool_names": sorted(tool_names), "missing": missing},
                )
            )
            mcp_call = next(
                (call for call in app_spec.acceptance_calls if call.purpose == "mcp"),
                None,
            )
            if mcp_call is None:
                for skill in skills:
                    if not isinstance(skill, dict):
                        continue
                    schema = skill.get("input_schema")
                    tool_name = str(skill.get("name") or skill.get("id") or "").strip()
                    if (
                        not tool_name
                        or not isinstance(schema, dict)
                        or _schema_has_file_upload(schema)
                    ):
                        continue
                    try:
                        mcp_call = AcceptanceCall(
                            purpose="mcp",
                            tool=tool_name,
                            arguments=_sample_args_from_schema(schema),
                        )
                    except ValueError:
                        continue
                    break
            if mcp_call is None:
                results.append(
                    EvaluationResult(
                        name="mcp:tools-call",
                        status="fail",
                        summary="application contract requires MCP but no schema-safe tools/call fixture is available",
                    )
                )
            elif mcp.get("ok") and mcp_call.tool in tool_names:
                called = await self._platform.call_mcp_tool(
                    str(live_state.get("url") or ""),
                    tool=mcp_call.tool,
                    arguments=mcp_call.arguments,
                )
                mcp_matches = not mcp_call.expected or _result_contains(
                    called.get("result"),
                    mcp_call.expected,
                )
                results.append(
                    EvaluationResult(
                        name="mcp:tools-call",
                        status="pass" if called.get("ok") and mcp_matches else "fail",
                        summary=(
                            f"MCP tools/call succeeded for {mcp_call.tool}"
                            if called.get("ok") and mcp_matches
                            else str(
                                called.get("error")
                                or (
                                    "MCP result did not contain expected deterministic fields"
                                    if called.get("ok")
                                    else "MCP tools/call failed"
                                )
                            )
                        ),
                        details={"tool": mcp_call.tool},
                    )
                )

        if app_spec.requires_receipt:
            receipt_result = await self._new_receipts(name, receipt_before)
            results.append(receipt_result)
        return results

    async def _acceptance_call(
        self,
        *,
        target: str,
        call: AcceptanceCall,
        grant: str | None,
        output_expectations: list[OutputExpectation],
    ) -> tuple[EvaluationResult, list[EvaluationResult]]:
        try:
            call_result = await self._ctx.call(
                target,
                call.tool,
                args=call.arguments,
                grant=grant,
                timeout=90,
            )
        except Exception as exc:  # noqa: BLE001
            if call.expect_error:
                return (
                    EvaluationResult(
                        name=f"acceptance:{call.purpose}:{call.tool}",
                        status="pass",
                        summary="expected failure fixture was rejected safely",
                        details={"error_type": type(exc).__name__},
                    ),
                    [],
                )
            return (
                EvaluationResult(
                    name=f"acceptance:{call.purpose}:{call.tool}",
                    status="fail",
                    summary=_exception_summary("acceptance call failed", exc),
                    details={"tool": call.tool},
                ),
                [],
            )
        result = call_result.result if hasattr(call_result, "result") else call_result
        artifacts = [
            item
            for item in getattr(call_result, "artifacts", ())
            if isinstance(item, dict)
        ]
        is_error = _result_is_error(result)
        expected_matches = not call.expected or _result_contains(result, call.expected)
        passed = (
            is_error and expected_matches
            if call.expect_error
            else not is_error and result is not None and expected_matches
        )
        acceptance = EvaluationResult(
            name=f"acceptance:{call.purpose}:{call.tool}",
            status="pass" if passed else "fail",
            summary=(
                "expected failure fixture returned safe structured validation"
                if passed and call.expect_error
                else "deterministic acceptance call succeeded"
                if passed
                else "failure fixture was unexpectedly accepted"
                if call.expect_error
                else "acceptance result did not contain the expected deterministic fields"
                if not expected_matches
                else "acceptance call returned an error or no result"
            ),
            details={
                "tool": call.tool,
                "purpose": call.purpose,
                "expected_fields": sorted(call.expected),
                "artifacts_count": len(artifacts),
            },
        )
        output_results = [
            _evaluate_output_expectation(
                expectation, result=result, artifacts=artifacts
            )
            for expectation in output_expectations
        ]
        return acceptance, output_results

    async def _new_receipts(
        self,
        name: str,
        before: set[str],
    ) -> EvaluationResult:
        latest: dict[str, Any] = {}
        for attempt in range(4):
            latest = await self._platform.list_receipts(name)
            if latest.get("ok"):
                fresh = [
                    item
                    for item in latest.get("receipts") or []
                    if isinstance(item, dict)
                    and item.get("receipt_id")
                    and str(item.get("receipt_id")) not in before
                ]
                if fresh:
                    return EvaluationResult(
                        name="execution-receipt",
                        status="pass",
                        summary=f"{len(fresh)} new production execution receipt(s) persisted",
                        details={
                            "skills": sorted(
                                {
                                    str(item.get("skill_name"))
                                    for item in fresh
                                    if item.get("skill_name")
                                }
                            )
                        },
                    )
            if attempt < 3:
                await asyncio.sleep(0.5)
        return EvaluationResult(
            name="execution-receipt",
            status="fail",
            summary=str(
                latest.get("error") or "no new execution receipt was persisted"
            ),
            details={"status_code": latest.get("status_code")},
        )

    async def _smoke_skill(
        self,
        *,
        name: str,
        target: str,
        skill: Any,
        grant: str | None = None,
    ) -> EvaluationResult:
        if not isinstance(skill, dict):
            return EvaluationResult(
                name="smoke-schema",
                status="warning",
                summary="Agent Card skill entry is not an object",
            )
        skill_name = str(skill.get("name") or skill.get("id") or "").strip()
        if not skill_name:
            return EvaluationResult(
                name="smoke-schema",
                status="warning",
                summary="Agent Card skill is missing a name",
            )
        schema = skill.get("input_schema")
        if not isinstance(schema, dict):
            return EvaluationResult(
                name=f"smoke:{skill_name}",
                status="warning",
                summary="skill has no input schema; smoke call skipped",
            )
        policy = skill.get("policy") if isinstance(skill.get("policy"), dict) else {}
        if policy.get("idempotent") is False:
            return EvaluationResult(
                name=f"smoke:{skill_name}",
                status="warning",
                summary=(
                    "generic smoke skipped a non-idempotent skill; the application "
                    "contract must provide an ordered deterministic fixture"
                ),
                details={"skill": skill_name, "actionable": False},
            )
        if _schema_has_file_upload(schema):
            return EvaluationResult(
                name=f"smoke:{skill_name}",
                status="warning",
                summary=(
                    "generic smoke skipped a FileUpload schema; the application contract must "
                    "exercise its browser upload bridge with a deterministic fixture"
                ),
                details={"skill": skill_name, "actionable": False},
            )
        if _schema_has_browser_base64_upload(schema):
            return EvaluationResult(
                name=f"smoke:{skill_name}",
                status="warning",
                summary=(
                    "generic smoke skipped a browser base64 upload schema; the application "
                    "contract or browser test must provide a valid product-specific fixture"
                ),
                details={"skill": skill_name, "actionable": False},
            )
        try:
            args = _sample_args_from_schema(schema)
        except ValueError as exc:
            return EvaluationResult(
                name=f"smoke:{skill_name}",
                status="fail",
                summary=f"schema mismatch: {exc}",
                details={"skill": skill_name},
            )
        try:
            call_result = await self._ctx.call(
                target,
                skill_name,
                args=args,
                grant=grant,
                timeout=45,
            )
        except TimeoutError:
            return EvaluationResult(
                name=f"smoke:{skill_name}",
                status="fail",
                summary="smoke call timed out",
                details={"skill": skill_name, "args": args},
            )
        except Exception as exc:  # noqa: BLE001
            return EvaluationResult(
                name=f"smoke:{skill_name}",
                status="fail",
                summary=_exception_summary("smoke call failed", exc),
                details={"skill": skill_name, "args": args},
            )
        result = call_result.result if hasattr(call_result, "result") else call_result
        if isinstance(result, dict) and result.get("error") and not result.get("ok"):
            return EvaluationResult(
                name=f"smoke:{skill_name}",
                status="fail",
                summary=f"smoke call returned error: {str(result.get('error'))[:160]}",
                details={"skill": skill_name, "args": args},
            )
        if result is None:
            return EvaluationResult(
                name=f"smoke:{skill_name}",
                status="warning",
                summary="smoke call returned no result",
                details={"skill": skill_name, "args": args},
            )
        return EvaluationResult(
            name=f"smoke:{skill_name}",
            status="pass",
            summary="smoke call succeeded",
            details={
                "skill": skill_name,
                "args": args,
                "events_count": len(getattr(call_result, "events", ()) or ()),
            },
        )

    async def review_agent(
        self,
        *,
        name: str,
        ref: str,
        owner: str | None,
    ) -> ReviewSummary:
        owner = owner or await self._deployment_owner(name=name)
        args = {"agent_name": name, "ref": ref}
        if owner:
            args["owner"] = owner
        try:
            call_result = await self._ctx.call(
                self._reviewer_agent,
                "review",
                args=args,
                timeout=900,
            )
        except TimeoutError:
            return ReviewSummary(status="skipped", summary="agent-reviewer timed out")
        except Exception as exc:  # noqa: BLE001
            return ReviewSummary(
                status="skipped",
                summary=_exception_summary("agent-reviewer handoff failed", exc),
            )
        result = call_result.result if hasattr(call_result, "result") else call_result
        if not isinstance(result, dict):
            return ReviewSummary(
                status="skipped", summary="agent-reviewer returned non-object"
            )
        if result.get("error"):
            return ReviewSummary(
                status="skipped",
                summary=f"agent-reviewer error: {str(result.get('error'))[:240]}",
            )
        findings = _safe_findings(result.get("findings"))
        critical = sum(
            1 for finding in findings if finding.get("severity") == "critical"
        )
        warnings = sum(
            1 for finding in findings if finding.get("severity") == "warning"
        )
        status: str = "passed"
        if critical:
            status = "critical"
        elif warnings:
            status = "warnings"
        elif result.get("ok") is False:
            status = "warnings"
            warnings = max(warnings, 1)
        return ReviewSummary(
            status=status,  # type: ignore[arg-type]
            summary=str(result.get("summary") or ""),
            review_id=_string_or_none(result.get("review_id")),
            critical_count=critical,
            warning_count=warnings,
            findings=findings,
        )

    async def _deployment_owner(self, *, name: str) -> str | None:
        status = await self._platform.deployment_status(name)
        deployments = status.get("deployments") if status.get("ok") else None
        if not isinstance(deployments, list):
            deployment = status.get("deployment") if status.get("ok") else None
            deployments = [deployment] if isinstance(deployment, dict) else []
        for deployment in deployments:
            if not isinstance(deployment, dict):
                continue
            owner = _owner_from_repo_url(
                _string_or_none(deployment.get("source_repo_url"))
            )
            if owner:
                return owner
        return None

    async def exercise_code_editor(
        self,
        *,
        name: str,
        ref: str,
        owner: str | None,
        session_name: str,
    ) -> MutationRecord:
        opt_in = await self._platform.enable_code_editor(name)
        if not opt_in.get("ok"):
            return MutationRecord(
                status="failed",
                summary=f"code-editor opt-in failed: {opt_in.get('error') or 'unknown error'}",
            )
        args: dict[str, Any] = {
            "agent_name": name,
            "prompt": (
                "Harness dry-run only. Inspect the target agent source, confirm "
                "the repository is reachable, and do not edit files or push changes."
            ),
            "ref": _editable_ref(ref),
            "session_name": session_name,
            "dry_run": True,
            "push_on_failure": False,
            "max_turns": 1,
            "output_format": "json",
            "timeout_seconds": 900,
        }
        if owner:
            args["owner"] = owner
        try:
            call_result = await self._ctx.call(
                self._code_editor_agent,
                "turn",
                args=args,
                timeout=1200,
            )
        except TimeoutError:
            return MutationRecord(
                status="failed", summary="code-editor dry-run timed out", timed_out=True
            )
        except Exception as exc:  # noqa: BLE001
            return MutationRecord(
                status="failed",
                summary=_exception_summary("code-editor dry-run handoff failed", exc),
            )
        result = call_result.result if hasattr(call_result, "result") else call_result
        if not isinstance(result, dict):
            return MutationRecord(
                status="failed", summary="code-editor dry-run returned non-object"
            )
        if result.get("error"):
            return MutationRecord(
                status="failed",
                summary=f"code-editor dry-run error: {str(result.get('error'))[:240]}",
            )
        sync = result.get("sync") if isinstance(result.get("sync"), dict) else {}
        push = result.get("push") if isinstance(result.get("push"), dict) else {}
        exit_code = _int_or_none(result.get("exit_code"))
        timed_out = bool(result.get("timed_out"))
        if result.get("ok") and push.get("reason") == "dry_run":
            return MutationRecord(
                status="dry_run",
                summary="code-editor dry-run completed",
                base_sha=_string_or_none(sync.get("head_sha")),
                exit_code=exit_code,
                timed_out=timed_out,
            )
        return MutationRecord(
            status="failed",
            summary=str(push.get("reason") or "code-editor dry-run failed"),
            base_sha=_string_or_none(sync.get("head_sha")),
            exit_code=exit_code,
            timed_out=timed_out,
        )

    async def patch_agent(
        self,
        *,
        name: str,
        prompt: str,
        ref: str,
        owner: str | None,
        session_name: str,
        target_finding_hash: str | None = None,
        target_review_id: str | None = None,
    ) -> MutationRecord:
        opt_in = await self._platform.enable_code_editor(name)
        if not opt_in.get("ok"):
            return MutationRecord(
                status="failed",
                summary=f"code-editor opt-in failed: {opt_in.get('error') or 'unknown error'}",
                target_finding_hash=target_finding_hash,
                target_review_id=target_review_id,
            )
        args: dict[str, Any] = {
            "agent_name": name,
            "prompt": prompt,
            "ref": _editable_ref(ref),
            "session_name": session_name,
            "dry_run": False,
            "push_on_failure": False,
            "max_turns": 12,
            "output_format": "json",
            "timeout_seconds": 1800,
        }
        if owner:
            args["owner"] = owner
        try:
            call_result = await self._ctx.call(
                self._code_editor_agent,
                "turn",
                args=args,
                timeout=3600,
            )
        except TimeoutError:
            return MutationRecord(
                status="failed",
                summary="code-editor timed out",
                timed_out=True,
                target_finding_hash=target_finding_hash,
                target_review_id=target_review_id,
            )
        except Exception as exc:  # noqa: BLE001
            return MutationRecord(
                status="failed",
                summary=_exception_summary("code-editor handoff failed", exc),
                target_finding_hash=target_finding_hash,
                target_review_id=target_review_id,
            )
        result = call_result.result if hasattr(call_result, "result") else call_result
        if not isinstance(result, dict):
            return MutationRecord(
                status="failed",
                summary="code-editor returned non-object",
                target_finding_hash=target_finding_hash,
                target_review_id=target_review_id,
            )
        if result.get("error"):
            return MutationRecord(
                status="failed",
                summary=f"code-editor error: {str(result.get('error'))[:240]}",
                target_finding_hash=target_finding_hash,
                target_review_id=target_review_id,
            )
        sync = result.get("sync") if isinstance(result.get("sync"), dict) else {}
        changes = (
            result.get("changes") if isinstance(result.get("changes"), dict) else {}
        )
        push = result.get("push") if isinstance(result.get("push"), dict) else {}
        changed_files = [
            str(item)
            for item in (
                changes.get("files") if isinstance(changes.get("files"), list) else []
            )
            if str(item)
        ][:100]
        exit_code = _int_or_none(result.get("exit_code"))
        timed_out = bool(result.get("timed_out"))
        if not result.get("ok"):
            return MutationRecord(
                status="failed",
                summary="code-editor turn failed",
                base_sha=_string_or_none(sync.get("head_sha")),
                head_sha=_string_or_none(push.get("head_sha")),
                target_finding_hash=target_finding_hash,
                target_review_id=target_review_id,
                changed_files=changed_files,
                exit_code=exit_code,
                timed_out=timed_out,
            )
        head_sha = _string_or_none(push.get("head_sha"))
        deployment_id = _string_or_none(push.get("deployment_id")) or _string_or_none(
            result.get("deployment_id")
        )
        review_id = _string_or_none(push.get("review_id")) or _string_or_none(
            result.get("review_id")
        )
        proof_id = _string_or_none(push.get("proof_id")) or _string_or_none(
            result.get("proof_id")
        )
        if push.get("attempted") and push.get("ok") and head_sha:
            deploy = await self._platform.deploy_source(name)
            if not deploy.get("ok"):
                return MutationRecord(
                    status="failed",
                    summary=(
                        "code-editor pushed source changes but source deploy failed: "
                        f"{deploy.get('error') or 'unknown error'}"
                    ),
                    base_sha=_string_or_none(sync.get("head_sha")),
                    head_sha=head_sha,
                    target_finding_hash=target_finding_hash,
                    target_review_id=target_review_id,
                    review_id=review_id,
                    proof_id=proof_id,
                    changed_files=changed_files,
                    exit_code=exit_code,
                    timed_out=timed_out,
                )
            deployment_id = (
                _string_or_none(deploy.get("deployment_id")) or deployment_id
            )
            return MutationRecord(
                status="pushed",
                summary="code-editor pushed and deployed source changes",
                base_sha=_string_or_none(sync.get("head_sha")),
                head_sha=head_sha,
                target_finding_hash=target_finding_hash,
                target_review_id=target_review_id,
                deployment_id=deployment_id,
                review_id=review_id,
                proof_id=proof_id,
                changed_files=changed_files,
                exit_code=exit_code,
                timed_out=timed_out,
            )
        return MutationRecord(
            status="skipped",
            summary=str(push.get("reason") or "code-editor produced no pushed changes"),
            base_sha=_string_or_none(sync.get("head_sha")),
            target_finding_hash=target_finding_hash,
            target_review_id=target_review_id,
            changed_files=changed_files,
            exit_code=exit_code,
            timed_out=timed_out,
        )

    async def refresh_agent(self, *, name: str) -> dict[str, Any]:
        return await self._platform.refresh_agent(name)


def _exception_summary(label: str, exc: Exception) -> str:
    return f"{label}: {type(exc).__name__}: {_safe_error_text(exc)}"


def _safe_error_text(value: Any, *, max_chars: int = 1000) -> str:
    """Keep actionable error context without retaining signed A2A evidence."""

    text = str(value)
    evidence_positions = [
        position
        for marker in ('"a2a_evidence"', '\\"a2a_evidence\\"', "'a2a_evidence'")
        if (position := text.find(marker)) >= 0
    ]
    if evidence_positions:
        text = (
            text[: min(evidence_positions)].rstrip(" ,{\\") + " [A2A evidence redacted]"
        )
    text = _JWT_RE.sub("[redacted token]", text)
    if len(text) > max_chars:
        return f"{text[:max_chars]}...[truncated {len(text) - max_chars} chars]"
    return text


def _start_builder_progress_mirror(
    ctx: Any,
    *,
    grant: str,
    stop: asyncio.Event,
) -> asyncio.Task[None] | None:
    cp_url = str(getattr(ctx, "cp_url", "") or "").rstrip("/")
    cp_jwt = str(getattr(ctx, "cp_jwt", "") or "")
    if not cp_url or not cp_jwt:
        return None
    try:
        grant_id = verify_grant(grant).grant_id
    except Exception:  # noqa: BLE001
        return None
    return asyncio.create_task(
        _mirror_builder_progress(
            ctx,
            cp_url=cp_url,
            cp_jwt=cp_jwt,
            grant_id=grant_id,
            stop=stop,
        ),
        name=f"agent-builder-progress-{grant_id}",
    )


async def _mirror_builder_progress(
    ctx: Any,
    *,
    cp_url: str,
    cp_jwt: str,
    grant_id: str,
    stop: asyncio.Event,
) -> None:
    seen: set[int] = set()
    async with httpx.AsyncClient(timeout=10.0) as client:
        while True:
            try:
                response = await client.get(
                    f"{cp_url}/v1/me/subagent-runs/{grant_id}",
                    headers={"Authorization": f"Bearer {cp_jwt}"},
                )
                if response.status_code == 200:
                    payload = response.json()
                    for event in payload.get("events") or ():
                        event_id = event.get("id")
                        if not isinstance(event_id, int) or event_id in seen:
                            continue
                        seen.add(event_id)
                        message = _child_progress_message(event)
                        if message:
                            await ctx.emit_progress(f"agent-builder: {message}")
            except Exception:  # noqa: BLE001
                # Progress mirroring is observational and must never fail the build.
                pass
            if stop.is_set():
                return
            try:
                await asyncio.wait_for(stop.wait(), timeout=2.5)
            except TimeoutError:
                continue


def _child_progress_message(event: dict[str, Any]) -> str | None:
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return None
    summary = payload.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return None
    clean = " ".join(summary.split())
    if "still working" in clean or clean.startswith(("building agent", "deployed:")):
        return clean[:240]
    tool_start = re.match(r"^→\s*([a-zA-Z0-9_-]+)", clean)
    if tool_start:
        return f"running {tool_start.group(1)}"
    tool_done = re.match(r"^\s*([a-zA-Z0-9_-]+)\s*←", clean)
    if tool_done:
        return f"{tool_done.group(1)} completed"
    return None


async def _delegate_builder_workspace(ctx: Any, *, name: str, audience: str) -> str:
    try:
        workspace = ctx.workspace
    except Exception as exc:  # noqa: BLE001
        raise PermissionError(
            "agent-studio did not receive a workspace grant; invoke through "
            "the a2acloud platform so it can delegate project write access "
            "to agent-builder"
        ) from exc
    parent_grant = getattr(workspace, "current_grant", None)
    llm_scope: dict[str, Any] = {}
    if parent_grant is not None:
        # agent-builder is platform-LLM-only. Delegated grants do not inherit
        # LLM authority implicitly, so pass through the parent's already
        # bounded allowance. delegate_grant mechanically rejects any widening.
        llm_scope = {
            "llm_models": tuple(getattr(parent_grant, "llm_models", ()) or ()),
            "llm_max_budget_usd": getattr(parent_grant, "llm_max_budget_usd", None),
            "llm_rpm_limit": getattr(parent_grant, "llm_rpm_limit", None),
            "llm_tpm_limit": getattr(parent_grant, "llm_tpm_limit", None),
        }
    cp_url = str(getattr(ctx, "cp_url", "") or "").rstrip("/")
    try:
        parent_token = str(getattr(ctx.llm, "api_key", "") or "")
    except Exception:  # noqa: BLE001
        parent_token = ""
    if cp_url and parent_token:
        payload = {
            "audience": audience,
            "allow_patterns": [f"agents/{name}/**"],
            "mode": WorkspaceMode.READ_WRITE_OVERLAY.value,
            "outputs_prefix": f"agents/{name}/",
            "write_prefixes": [f"agents/{name}/"],
            "source_grants": [{"agent": name, "scope": "write"}],
            "ttl_seconds": BUILDER_WORKSPACE_TTL_SECONDS,
            **llm_scope,
        }
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                f"{cp_url}/v1/workspace-grants/delegate",
                json=payload,
                headers={"X-A2A-Grant": parent_token},
            )
        if response.status_code >= 400:
            raise PermissionError(
                f"control-plane child grant failed: {response.status_code}: "
                f"{response.text[:300]}"
            )
        result = response.json()
        token = result.get("grant") if isinstance(result, dict) else None
        if not isinstance(token, str) or not token:
            raise PermissionError("control-plane child grant returned no token")
        return token
    return await workspace.delegate(
        audience=audience,
        allow_patterns=(f"agents/{name}/**",),
        mode=WorkspaceMode.READ_WRITE_OVERLAY,
        outputs_prefix=f"agents/{name}/",
        write_prefixes=(f"agents/{name}/",),
        source_grants=({"agent": name, "scope": "write"},),
        ttl_seconds=BUILDER_WORKSPACE_TTL_SECONDS,
        **llm_scope,
    )


async def _delegate_target_workspace_if_needed(
    ctx: Any,
    *,
    name: str,
    card: dict[str, Any],
) -> str | None:
    workspace_access = card.get("workspace_access")
    if not isinstance(workspace_access, dict) or not workspace_access.get("enabled"):
        return None
    try:
        workspace = ctx.workspace
    except Exception:
        return None
    mode = WorkspaceMode.READ_ONLY
    allowed_modes = workspace_access.get("allowed_modes")
    if isinstance(allowed_modes, list) and "read_write_overlay" in allowed_modes:
        mode = WorkspaceMode.READ_WRITE_OVERLAY
    cp_url = str(getattr(ctx, "cp_url", "") or "").rstrip("/")
    try:
        parent_token = str(getattr(ctx.llm, "api_key", "") or "")
    except Exception:  # noqa: BLE001
        parent_token = ""
    smoke_prefix = f"agents/{name}/.agent-studio/smoke/"
    if cp_url and parent_token:
        payload = {
            "audience": name,
            "allow_patterns": [f"agents/{name}/**"],
            "mode": mode.value,
            "outputs_prefix": smoke_prefix,
            "write_prefixes": [smoke_prefix],
            "source_grants": [{"agent": name, "scope": "write"}],
            "ttl_seconds": 900,
        }
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                f"{cp_url}/v1/workspace-grants/delegate",
                json=payload,
                headers={"X-A2A-Grant": parent_token},
            )
        if response.status_code >= 400:
            raise PermissionError(
                f"control-plane smoke-test grant failed: {response.status_code}: "
                f"{response.text[:300]}"
            )
        result = response.json()
        token = result.get("grant") if isinstance(result, dict) else None
        if not isinstance(token, str) or not token:
            raise PermissionError("control-plane smoke-test grant returned no token")
        return token
    return await workspace.delegate(
        audience=name,
        allow_patterns=(f"agents/{name}/**",),
        mode=mode,
        outputs_prefix=smoke_prefix,
        write_prefixes=(smoke_prefix,),
        source_grants=({"agent": name, "scope": "write"},),
        ttl_seconds=900,
    )


def _sample_args_from_schema(
    schema: dict[str, Any],
    *,
    root_schema: dict[str, Any] | None = None,
    depth: int = 0,
) -> dict[str, Any]:
    if depth > 12:
        raise ValueError("input schema nesting exceeds smoke-test limit")
    root = schema if isinstance(schema.get("$defs"), dict) else (root_schema or schema)
    schema = _resolve_local_schema_ref(schema, root_schema=root)
    if schema.get("type") not in (None, "object"):
        raise ValueError("top-level input schema must be an object")
    properties = schema.get("properties")
    if properties is None:
        return {}
    if not isinstance(properties, dict):
        raise ValueError("input_schema.properties must be an object")
    required = schema.get("required") or []
    if not isinstance(required, list):
        raise ValueError("input_schema.required must be a list")
    args: dict[str, Any] = {}
    for field in required:
        name = str(field or "").strip()
        if not name:
            continue
        field_schema = properties.get(name)
        if not isinstance(field_schema, dict):
            raise ValueError(f"required field {name!r} has no schema")
        args[name] = _sample_value(
            field_schema,
            name=name,
            root_schema=root,
            depth=depth + 1,
        )
    return args


def _smoke_target(*, name: str, live_state: dict[str, Any], ctx: Any) -> str:
    url = live_state.get("url")
    if isinstance(url, str) and url.startswith(("http://", "https://")):
        return url.rstrip("/")
    cp_url = str(getattr(ctx, "cp_url", "") or "")
    if "a2acloud.io" in cp_url or "control-plane.control-plane.svc" in cp_url:
        return f"https://{name}.a2acloud.io"
    return name


def _sample_value(
    schema: dict[str, Any],
    *,
    name: str,
    root_schema: dict[str, Any] | None = None,
    depth: int = 0,
) -> Any:
    if depth > 12:
        raise ValueError(f"schema nesting for {name!r} exceeds smoke-test limit")
    root = schema if isinstance(schema.get("$defs"), dict) else (root_schema or schema)
    schema = _resolve_local_schema_ref(schema, root_schema=root)
    if "default" in schema:
        return schema["default"]
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return enum[0]
    for key in ("const", "example"):
        if key in schema:
            return schema[key]
    for union_key in ("oneOf", "anyOf"):
        options = schema.get(union_key)
        if isinstance(options, list):
            option = next(
                (
                    item
                    for item in options
                    if isinstance(item, dict) and item.get("type") != "null"
                ),
                None,
            )
            if option is not None:
                return _sample_value(
                    option,
                    name=name,
                    root_schema=root,
                    depth=depth + 1,
                )
    all_of = schema.get("allOf")
    if isinstance(all_of, list) and all_of and isinstance(all_of[0], dict):
        return _sample_value(
            all_of[0],
            name=name,
            root_schema=root,
            depth=depth + 1,
        )
    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        schema_type = next((item for item in schema_type if item != "null"), None)
    if schema_type == "string" or schema_type is None:
        return _sample_string(schema, name=name)
    if schema_type == "integer":
        return _sample_integer(schema)
    if schema_type == "number":
        return _sample_number(schema)
    if schema_type == "boolean":
        return False
    if schema_type == "array":
        items = schema.get("items") if isinstance(schema.get("items"), dict) else {}
        max_items = schema.get("maxItems")
        if isinstance(max_items, int) and max_items <= 0:
            return []
        min_items = int(schema.get("minItems") or 0)
        count = max(1, min_items)
        return [
            _sample_value(
                items,
                name=name if count == 1 else f"{name}-{index + 1}",
                root_schema=root,
                depth=depth + 1,
            )
            for index in range(count)
        ]
    if schema_type == "object":
        return _sample_args_from_schema(
            schema,
            root_schema=root,
            depth=depth + 1,
        )
    return f"smoke-{name}"


def _sample_integer(schema: dict[str, Any]) -> int:
    lower: int | None = None
    minimum = schema.get("minimum")
    if isinstance(minimum, (int, float)) and not isinstance(minimum, bool):
        lower = math.ceil(minimum)
    exclusive_minimum = schema.get("exclusiveMinimum")
    if isinstance(exclusive_minimum, (int, float)) and not isinstance(
        exclusive_minimum, bool
    ):
        lower = max(
            lower if lower is not None else -math.inf,
            math.floor(exclusive_minimum) + 1,
        )

    upper: int | None = None
    maximum = schema.get("maximum")
    if isinstance(maximum, (int, float)) and not isinstance(maximum, bool):
        upper = math.floor(maximum)
    exclusive_maximum = schema.get("exclusiveMaximum")
    if isinstance(exclusive_maximum, (int, float)) and not isinstance(
        exclusive_maximum, bool
    ):
        exclusive_upper = math.ceil(exclusive_maximum) - 1
        upper = min(upper if upper is not None else math.inf, exclusive_upper)

    candidate = max(1, lower if lower is not None else 1)
    if upper is not None and candidate > upper:
        candidate = upper
    if lower is not None and candidate < lower:
        candidate = lower
    return int(candidate)


def _sample_number(schema: dict[str, Any]) -> float:
    minimum = schema.get("minimum")
    exclusive_minimum = schema.get("exclusiveMinimum")
    maximum = schema.get("maximum")
    exclusive_maximum = schema.get("exclusiveMaximum")
    lower = (
        float(exclusive_minimum)
        if isinstance(exclusive_minimum, (int, float))
        and not isinstance(exclusive_minimum, bool)
        else float(minimum)
        if isinstance(minimum, (int, float)) and not isinstance(minimum, bool)
        else None
    )
    upper = (
        float(exclusive_maximum)
        if isinstance(exclusive_maximum, (int, float))
        and not isinstance(exclusive_maximum, bool)
        else float(maximum)
        if isinstance(maximum, (int, float)) and not isinstance(maximum, bool)
        else None
    )
    lower_exclusive = isinstance(exclusive_minimum, (int, float)) and not isinstance(
        exclusive_minimum, bool
    )
    upper_exclusive = isinstance(exclusive_maximum, (int, float)) and not isinstance(
        exclusive_maximum, bool
    )

    candidate = 1.0
    if lower is not None and (
        candidate < lower or (lower_exclusive and candidate <= lower)
    ):
        candidate = math.nextafter(lower, math.inf) if lower_exclusive else lower
    if upper is not None and (
        candidate > upper or (upper_exclusive and candidate >= upper)
    ):
        if lower is None:
            candidate = min(upper / 2.0, math.nextafter(upper, -math.inf))
        else:
            candidate = lower + ((upper - lower) / 2.0)
        if lower_exclusive and candidate <= lower:
            candidate = math.nextafter(lower, math.inf)
        if upper_exclusive and candidate >= upper:
            candidate = math.nextafter(upper, -math.inf)
    return float(candidate)


def _resolve_local_schema_ref(
    schema: dict[str, Any],
    *,
    root_schema: dict[str, Any],
) -> dict[str, Any]:
    ref = schema.get("$ref")
    if not isinstance(ref, str):
        return schema
    if not ref.startswith("#/"):
        raise ValueError(f"unsupported non-local schema reference: {ref}")
    value: Any = root_schema
    for raw_part in ref[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(value, dict) or part not in value:
            raise ValueError(f"unresolved schema reference: {ref}")
        value = value[part]
    if not isinstance(value, dict):
        raise ValueError(f"schema reference does not resolve to an object: {ref}")
    return value


def _sample_string(schema: dict[str, Any], *, name: str) -> str:
    string_format = schema.get("format")
    if string_format in {"uri", "url", "uri-reference"}:
        candidate = "https://example.com/smoke"
    elif string_format == "email":
        candidate = "smoke@example.com"
    elif string_format == "date-time":
        candidate = "2026-01-01T00:00:00Z"
    elif string_format == "date":
        candidate = "2026-01-01"
    elif string_format == "uuid":
        candidate = "00000000-0000-4000-8000-000000000000"
    else:
        slug = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-") or "value"
        candidate = f"smoke-{slug}"

    pattern = schema.get("pattern")
    if isinstance(pattern, str):
        if pattern == r"^sha256:[a-f0-9]{64}$":
            candidate = "sha256:" + ("0" * 64)
        elif not re.fullmatch(pattern, candidate):
            alternatives = ["smoke-value", "abc", "value", "00000000"]
            candidate = next(
                (item for item in alternatives if re.fullmatch(pattern, item)),
                candidate,
            )

    min_length = schema.get("minLength")
    if isinstance(min_length, int) and len(candidate) < min_length:
        candidate += "x" * (min_length - len(candidate))
    max_length = schema.get("maxLength")
    if isinstance(max_length, int) and len(candidate) > max_length:
        candidate = candidate[:max_length]
    if isinstance(pattern, str) and not re.fullmatch(pattern, candidate):
        raise ValueError(
            f"unable to generate smoke value matching pattern for {name!r}"
        )
    return candidate


def _schema_has_file_upload(value: Any, *, depth: int = 0) -> bool:
    if depth > 12:
        return False
    if isinstance(value, dict):
        if "x-a2a-file-upload" in value:
            return True
        return any(
            _schema_has_file_upload(item, depth=depth + 1) for item in value.values()
        )
    if isinstance(value, list):
        return any(_schema_has_file_upload(item, depth=depth + 1) for item in value)
    return False


def _schema_has_browser_base64_upload(value: Any, *, depth: int = 0) -> bool:
    if depth > 12:
        return False
    if isinstance(value, dict):
        properties = value.get("properties")
        if isinstance(properties, dict) and "data_base64" in properties:
            return True
        return any(
            _schema_has_browser_base64_upload(item, depth=depth + 1)
            for item in value.values()
        )
    if isinstance(value, list):
        return any(
            _schema_has_browser_base64_upload(item, depth=depth + 1) for item in value
        )
    return False


def _editable_ref(ref: str) -> str:
    return "main" if re.fullmatch(r"[0-9a-fA-F]{40}", ref.strip()) else ref


def _result_is_error(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    if result.get("ok") is False or result.get("valid") is False:
        return True
    if result.get("error") not in (None, "", False):
        return True
    for key in ("errors", "validation_errors"):
        value = result.get(key)
        if isinstance(value, list) and value:
            return True
    return False


def _result_contains(actual: Any, expected: Any) -> bool:
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return False
        return all(
            key in actual and _result_contains(actual[key], value)
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) < len(expected):
            return False
        return all(
            _result_contains(actual[index], value)
            for index, value in enumerate(expected)
        )
    return actual == expected


def _evaluate_output_expectation(
    expectation: OutputExpectation,
    *,
    result: Any,
    artifacts: list[dict[str, Any]],
) -> EvaluationResult:
    candidates = artifacts + _result_output_descriptors(result)
    matching = [
        item
        for item in candidates
        if (
            not expectation.filename
            or str(item.get("name") or item.get("filename") or "")
            == expectation.filename
        )
        and (
            not expectation.media_type
            or str(item.get("mime_type") or item.get("media_type") or "")
            == expectation.media_type
        )
    ]
    if expectation.kind == "json":
        passed = isinstance(result, (dict, list))
    elif expectation.kind == "text":
        passed = isinstance(result, str) or _contains_value_type(result, str)
    elif expectation.kind == "table":
        passed = isinstance(result, list) or _contains_value_type(result, list)
    elif expectation.kind == "artifact":
        passed = len(matching) >= expectation.minimum_count and bool(artifacts)
    elif expectation.kind == "download":
        passed = len(matching) >= expectation.minimum_count and any(
            item.get("uri")
            or item.get("url")
            or item.get("data")
            or item.get("content")
            for item in matching
        )
    else:  # preview
        passed = len(matching) >= expectation.minimum_count and any(
            item.get("uri")
            or item.get("url")
            or item.get("data")
            or item.get("content")
            or item.get("text")
            for item in matching
        )
    label = expectation.name or expectation.filename or expectation.kind
    return EvaluationResult(
        name=f"output:{expectation.kind}:{label}",
        status="pass" if passed else "fail",
        summary=(
            f"production result satisfied the {expectation.kind} output contract"
            if passed
            else f"production result did not expose the required {expectation.kind} output"
        ),
        details={
            "filename": expectation.filename,
            "media_type": expectation.media_type,
            "minimum_count": expectation.minimum_count,
            "artifact_count": len(artifacts),
            "candidate_count": len(candidates),
        },
    )


def _result_output_descriptors(value: Any, *, depth: int = 0) -> list[dict[str, Any]]:
    if depth > 6:
        return []
    if isinstance(value, dict):
        descriptors: list[dict[str, Any]] = []
        descriptor_keys = {
            "name",
            "filename",
            "mime_type",
            "media_type",
            "uri",
            "url",
            "data",
            "content",
            "text",
        }
        if descriptor_keys.intersection(value):
            descriptors.append(value)
        for nested in value.values():
            descriptors.extend(_result_output_descriptors(nested, depth=depth + 1))
        return descriptors[:100]
    if isinstance(value, list):
        descriptors: list[dict[str, Any]] = []
        for nested in value[:100]:
            descriptors.extend(_result_output_descriptors(nested, depth=depth + 1))
        return descriptors[:100]
    return []


def _contains_value_type(
    value: Any, expected_type: type[Any], *, depth: int = 0
) -> bool:
    if depth > 6:
        return False
    if isinstance(value, expected_type):
        return True
    if isinstance(value, dict):
        return any(
            _contains_value_type(item, expected_type, depth=depth + 1)
            for item in value.values()
        )
    if isinstance(value, list):
        return any(
            _contains_value_type(item, expected_type, depth=depth + 1)
            for item in value[:100]
        )
    return False


def _safe_findings(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    for item in value[:100]:
        if not isinstance(item, dict):
            continue
        out.append(
            {
                "severity": str(item.get("severity") or "info"),
                "category": str(item.get("category") or ""),
                "message": str(item.get("message") or "")[:500],
                "file": _string_or_none(item.get("file")),
                "line": _int_or_none(item.get("line")),
                "suggestion": (
                    str(item.get("suggestion"))[:500]
                    if item.get("suggestion") is not None
                    else None
                ),
            }
        )
    return out


def _string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
