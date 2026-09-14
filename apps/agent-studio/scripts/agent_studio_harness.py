#!/usr/bin/env python3
"""Operator harness for Agent Studio acceptance runs.

The harness intentionally uses the public control-plane surfaces plus the
backend e2e user helper. It can mint a CP bearer token from the remote cluster,
mint a workspace/LLM grant from the same cluster, invoke Agent Studio directly,
and fail if the Agent Studio report or recorded subagent runs show a
failed/blocked/skipped required path. The connector MCP path is still available
for end-to-end orchestrator coverage.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

PLATFORM_DOMAIN = os.environ.get("A2A_PLATFORM_DOMAIN", "example.com")
DEFAULT_API_URL = os.environ.get("A2A_API_URL", f"https://api.{PLATFORM_DOMAIN}")
DEFAULT_AGENT_URL = os.environ.get("A2A_AGENT_STUDIO_URL", f"https://agent-studio.{PLATFORM_DOMAIN}")
DEFAULT_EMAIL = os.environ.get("A2A_HARNESS_EMAIL", f"agent-studio-harness@{PLATFORM_DOMAIN}")
DEFAULT_LLM_MODEL = "gpt-5.5"
DEFAULT_LLM_BASE_URL = "http://litellm.llm.svc.cluster.local:4000/v1"
DEFAULT_GOAL = (
    "Create a minimal deterministic A2A text utility agent with one skill "
    "`echo`. The skill must accept a required string field `text` and return "
    "a structured object containing ok=true and the echoed text. Keep the "
    "implementation small, avoid external network calls and secrets, and make "
    "the Agent Card input schema accurate so deterministic smoke calls pass."
)
DONE_STATUSES = {"ok", "failed", "approval_required", "input_required", "auth_required", "expired"}
BAD_RUN_STATUSES = {"error", "failed", "canceled", "cancelled", "denied", "auth_required", "input_required"}
GOOD_RUN_STATUSES = {"complete", "completed", "succeeded", "success", "ok", "passed"}
GOOD_HANDOFF_STATUSES = GOOD_RUN_STATUSES | {"built", "reviewed"}
REQUIRED_HANDOFF_AGENTS = {"agent-builder", "agent-reviewer", "code-editor-agent"}


def _ready_pod_target(
    *,
    namespace: str,
    kubeconfig: str | None,
    deployment: str,
    container: str,
) -> tuple[str, str]:
    """Resolve exec to a ready, non-terminating pod.

    ``kubectl exec deploy/...`` may choose a terminating pod during a Recreate
    rollout. The control-plane Knative API has the same token/grant helpers and
    signing environment, so it is a safe fallback while the worker pod drains.
    """
    selectors = [(f"app={deployment}", container)]
    if deployment == "control-plane-workers" and container == "connector-mcp-worker":
        selectors.append(("serving.knative.dev/service=control-plane", "api"))

    for selector, candidate_container in selectors:
        cmd = ["kubectl"]
        if kubeconfig:
            cmd.append(f"--kubeconfig={kubeconfig}")
        cmd.extend(["-n", namespace, "get", "pods", "-l", selector, "-o", "json"])
        try:
            document = json.loads(subprocess.check_output(cmd, text=True))
        except (subprocess.CalledProcessError, json.JSONDecodeError):
            continue
        for pod in document.get("items", []):
            metadata = pod.get("metadata", {})
            if metadata.get("deletionTimestamp"):
                continue
            conditions = pod.get("status", {}).get("conditions", [])
            ready = any(
                condition.get("type") == "Ready" and condition.get("status") == "True"
                for condition in conditions
            )
            names = {
                item.get("name") for item in pod.get("spec", {}).get("containers", [])
            }
            if ready and candidate_container in names and metadata.get("name"):
                return f"pod/{metadata['name']}", candidate_container

    # Preserve the caller's original target so kubectl returns the most useful
    # diagnostic when neither workload currently has a ready pod.
    return f"deploy/{deployment}", container


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    token = sub.add_parser("token", help="mint/reuse a remote CP e2e user token")
    token.add_argument("--email", default=os.environ.get("A2A_AGENT_STUDIO_EMAIL", DEFAULT_EMAIL))
    token.add_argument("--kubeconfig", default=os.environ.get("KUBECONFIG"))
    token.add_argument("--namespace", default="control-plane")
    token.add_argument("--deployment", default="control-plane-workers")
    token.add_argument("--container", default="connector-mcp-worker")
    token.add_argument("--json", action="store_true", dest="as_json")

    run = sub.add_parser("run", help="run Agent Studio and assert all subagents pass")
    run.add_argument("--mode", choices=("direct", "connector"), default="direct")
    run.add_argument("--api-url", default=os.environ.get("A2A_E2E_API_URL", DEFAULT_API_URL))
    run.add_argument("--agent-url", default=os.environ.get("A2A_AGENT_STUDIO_URL", DEFAULT_AGENT_URL))
    run.add_argument("--bearer-token", default=os.environ.get("A2A_AGENT_STUDIO_BEARER") or os.environ.get("A2A_SMOKE_BEARER"))
    run.add_argument("--mint-token", action="store_true")
    run.add_argument("--email", default=os.environ.get("A2A_AGENT_STUDIO_EMAIL", DEFAULT_EMAIL))
    run.add_argument("--kubeconfig", default=os.environ.get("KUBECONFIG"))
    run.add_argument("--namespace", default="control-plane")
    run.add_argument("--deployment", default="control-plane-workers")
    run.add_argument("--container", default="connector-mcp-worker")
    run.add_argument("--llm-model", default=os.environ.get("A2A_AGENT_STUDIO_LLM_MODEL", DEFAULT_LLM_MODEL))
    run.add_argument("--llm-base-url", default=os.environ.get("A2A_AGENT_STUDIO_LLM_BASE_URL", DEFAULT_LLM_BASE_URL))
    run.add_argument("--llm-budget-usd", type=float, default=float(os.environ.get("A2A_AGENT_STUDIO_LLM_BUDGET_USD", "5.0")))
    run.add_argument("--grant-ttl-seconds", type=int, default=3600)
    run.add_argument(
        "--name",
        default=(
            os.environ.get("A2A_AGENT_STUDIO_NAME")
            or os.environ.get("A2A_AGENT_STUDIO_TARGET_AGENT")
        ),
    )
    run.add_argument(
        "--expected-repo-url",
        default=os.environ.get("A2A_AGENT_STUDIO_EXPECTED_REPO_URL"),
        help=(
            "When set, verify the target agent resolves to this source repo "
            "before invoking Agent Studio."
        ),
    )
    run.add_argument(
        "--allow-generated-name",
        action="store_true",
        help="Allow the legacy studio-harness-<timestamp> target when --name is omitted.",
    )
    run.add_argument("--goal", default=DEFAULT_GOAL)
    run.add_argument(
        "--app-spec-json",
        default="{}",
        help="Structured AppSpec JSON forwarded unchanged to Agent Studio.",
    )
    run.add_argument("--max-iterations", type=int, default=2)
    run.add_argument("--quality-bar", choices=("standard", "high"), default="standard")
    run.add_argument("--exercise-code-editor", action=argparse.BooleanOptionalAction, default=True)
    run.add_argument("--timeout-seconds", type=float, default=3600.0)
    run.add_argument("--poll-seconds", type=float, default=8.0)
    run.add_argument("--auto-approve", action=argparse.BooleanOptionalAction, default=True)
    run.add_argument("--output", type=Path, default=Path("agent-studio-harness-report.json"))
    run.add_argument("--print-token", action="store_true")
    return parser


def mint_token(
    *,
    email: str,
    kubeconfig: str | None,
    namespace: str = "control-plane",
    deployment: str = "control-plane-workers",
    container: str = "connector-mcp-worker",
) -> dict[str, Any]:
    target, resolved_container = _ready_pod_target(
        namespace=namespace,
        kubeconfig=kubeconfig,
        deployment=deployment,
        container=container,
    )
    cmd = ["kubectl"]
    if kubeconfig:
        cmd.append(f"--kubeconfig={kubeconfig}")
    cmd.extend(
        [
            "-n",
            namespace,
            "exec",
            target,
            "-c",
            resolved_container,
            "--",
            "python",
            "-m",
            "control_plane.e2e_users",
            "--email",
            email,
            "--json",
        ]
    )
    out = subprocess.check_output(cmd, text=True)
    return json.loads(out)


def mint_grant(
    *,
    name: str,
    user_id: int,
    kubeconfig: str | None,
    namespace: str = "control-plane",
    deployment: str = "control-plane-workers",
    container: str = "connector-mcp-worker",
    llm_model: str = DEFAULT_LLM_MODEL,
    llm_budget_usd: float = 5.0,
    ttl_seconds: int = 3600,
) -> dict[str, Any]:
    code = r"""
import json
import sys
from control_plane.grants import mint_grant_token

cfg = json.loads(sys.argv[1])
name = cfg["name"]
token, payload = mint_grant_token(
    issuer="agent-studio-harness",
    audience="agent-studio",
    bucket=f"user-{int(cfg['user_id'])}-files",
    mode="read_write_overlay",
    allow_patterns=(f"agents/{name}/**",),
    outputs_prefix=f"agents/{name}/.agent-studio/",
    write_prefixes=(f"agents/{name}/",),
    source_grants=({"agent": name, "scope": "write"},),
    llm_models=(cfg["llm_model"],),
    llm_max_budget_usd=float(cfg["llm_budget_usd"]),
    ttl_seconds=int(cfg["ttl_seconds"]),
)
print(json.dumps({"grant": token, "payload": payload}, sort_keys=True))
"""
    target, resolved_container = _ready_pod_target(
        namespace=namespace,
        kubeconfig=kubeconfig,
        deployment=deployment,
        container=container,
    )
    cmd = ["kubectl"]
    if kubeconfig:
        cmd.append(f"--kubeconfig={kubeconfig}")
    cmd.extend(
        [
            "-n",
            namespace,
            "exec",
            target,
            "-c",
            resolved_container,
            "--",
            "python",
            "-c",
            code,
            json.dumps(
                {
                    "name": name,
                    "user_id": user_id,
                    "llm_model": llm_model,
                    "llm_budget_usd": llm_budget_usd,
                    "ttl_seconds": ttl_seconds,
                },
                sort_keys=True,
            ),
        ]
    )
    out = subprocess.check_output(cmd, text=True)
    return json.loads(out)


def _http_json(
    method: str,
    url: str,
    *,
    token: str | None = None,
    body: Any | None = None,
    timeout: float = 60.0,
) -> Any:
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url} failed: HTTP {exc.code}: {detail}") from exc
    return json.loads(raw) if raw else None


def _mcp_tool(
    *,
    api_url: str,
    token: str,
    tool: str,
    arguments: dict[str, Any],
    request_id: int,
) -> dict[str, Any]:
    body = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }
    env = _http_json(
        "POST",
        api_url.rstrip("/") + "/connector-mcp",
        token=token,
        body=body,
        timeout=90.0,
    )
    if isinstance(env, dict) and env.get("error"):
        raise RuntimeError(f"MCP {tool} failed: {env['error']}")
    return _extract_tool_result(env.get("result") if isinstance(env, dict) else env)


def _extract_tool_result(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            return structured
        content = result.get("content")
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict) or item.get("type") != "text":
                    continue
                text = item.get("text")
                if not isinstance(text, str):
                    continue
                parsed = _try_json(text)
                if isinstance(parsed, dict):
                    return parsed
                return {"status": "text", "content": text}
        if "status" in result:
            return result
    raise RuntimeError(f"unexpected MCP result shape: {result!r}")


def _try_json(text: str) -> Any | None:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _agent_name() -> str:
    return "studio-harness-" + time.strftime("%m%d%H%M")


def _repo_url_normalized(value: str | None) -> str:
    text = (value or "").strip()
    if text.endswith(".git"):
        text = text[:-4]
    return text.rstrip("/")


def _assert_expected_repo(
    *,
    api_url: str,
    token: str,
    name: str,
    expected_repo_url: str | None,
) -> None:
    expected = _repo_url_normalized(expected_repo_url)
    if not expected:
        return
    data = _http_json(
        "GET",
        api_url.rstrip("/") + "/v1/agents/" + urllib.parse.quote(name, safe=""),
        token=token,
        timeout=30.0,
    )
    actual_value = None
    if isinstance(data, dict):
        actual_value = data.get("repo_url") or data.get("source_repo_url")
    if not actual_value:
        editor = _http_json(
            "GET",
            api_url.rstrip("/")
            + "/v1/agents/"
            + urllib.parse.quote(name, safe="")
            + "/code-editor",
            token=token,
            timeout=30.0,
        )
        if isinstance(editor, dict):
            actual_value = editor.get("target_repo_url") or editor.get("repo_url")
    actual = _repo_url_normalized(actual_value if isinstance(actual_value, str) else None)
    if actual != expected:
        raise SystemExit(
            "target repo guard failed: "
            f"agent {name!r} resolved to {actual or '<none>'}, expected {expected}. "
            "Use the owner email/token for the target repo or override "
            "--expected-repo-url only when intentionally targeting another repo."
        )


def _agent_studio_prompt(
    *,
    name: str,
    goal: str,
    max_iterations: int,
    quality_bar: str,
    exercise_code_editor: bool,
    app_spec_json: str = "{}",
) -> str:
    args = {
        "name": name,
        "goal": goal,
        "public": False,
        "max_iterations": max_iterations,
        "quality_bar": quality_bar,
        "exercise_code_editor": exercise_code_editor,
        "app_spec_json": app_spec_json,
    }
    return (
        "Run Agent Studio acceptance exactly once. Invoke the deployed "
        "`agent-studio` agent skill `create_agent` with these JSON arguments:\n"
        f"{json.dumps(args, sort_keys=True)}\n\n"
        "Do not substitute a different agent or ask follow-up questions. "
        "When it completes, return a single JSON object with key "
        "`agent_studio_report` containing the full tool result."
    )


def _agent_studio_args(
    *,
    name: str,
    goal: str,
    max_iterations: int,
    quality_bar: str,
    exercise_code_editor: bool,
    app_spec_json: str = "{}",
    max_spend_cents: int = 500,
) -> dict[str, Any]:
    return {
        "name": name,
        "goal": goal,
        "public": False,
        "max_iterations": max_iterations,
        "quality_bar": quality_bar,
        "exercise_code_editor": exercise_code_editor,
        "app_spec_json": app_spec_json,
        "max_spend_cents": max_spend_cents,
    }


def _invoke_agent_studio_direct(
    *,
    agent_url: str,
    token: str,
    grant: str,
    name: str,
    goal: str,
    max_iterations: int,
    quality_bar: str,
    exercise_code_editor: bool,
    app_spec_json: str,
    max_spend_cents: int,
    api_url: str,
    llm_model: str,
    llm_base_url: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    body = {
        "arguments": _agent_studio_args(
            name=name,
            goal=goal,
            max_iterations=max_iterations,
            quality_bar=quality_bar,
            exercise_code_editor=exercise_code_editor,
            app_spec_json=app_spec_json,
            max_spend_cents=max_spend_cents,
        ),
        "grant": grant,
        "cp_jwt": token,
        "cp_url": api_url.rstrip("/"),
        "llm_creds": {
            "api_key": grant,
            "model": llm_model,
            "base_url": llm_base_url,
            "temperature_mode": "omit",
        },
    }
    return _http_json(
        "POST",
        agent_url.rstrip("/") + "/invoke/create_agent",
        body=body,
        timeout=timeout_seconds,
    )


def _poll_job(
    *,
    api_url: str,
    token: str,
    initial: dict[str, Any],
    timeout_seconds: float,
    poll_seconds: float,
    auto_approve: bool,
) -> dict[str, Any]:
    result = initial
    request_id = 100
    deadline = time.monotonic() + timeout_seconds
    while True:
        status = str(result.get("status") or "")
        print(f"connector job {result.get('job_id')}: {status}", flush=True)
        if status == "approval_required" and auto_approve:
            result = _mcp_tool(
                api_url=api_url,
                token=token,
                tool="resume_interaction",
                arguments={"job_id": str(result["job_id"]), "decision": "approve"},
                request_id=request_id,
            )
            request_id += 1
            continue
        if status in DONE_STATUSES:
            return result
        if time.monotonic() >= deadline:
            raise TimeoutError(f"connector job timed out: {json.dumps(result, sort_keys=True)}")
        time.sleep(poll_seconds)
        args: dict[str, Any] = {"job_id": str(result["job_id"])}
        if isinstance(result.get("thread_id"), str):
            args["thread_id"] = result["thread_id"]
        result = _mcp_tool(
            api_url=api_url,
            token=token,
            tool="chat_result",
            arguments=args,
            request_id=request_id,
        )
        request_id += 1


def _list_subagent_runs(
    *,
    api_url: str,
    token: str,
    thread_id: str | None,
) -> list[dict[str, Any]]:
    params = {"limit": "100"}
    if thread_id:
        params["thread_id"] = thread_id
    url = api_url.rstrip("/") + "/v1/me/subagent-runs?" + urllib.parse.urlencode(params)
    data = _http_json("GET", url, token=token, timeout=30.0)
    return data if isinstance(data, list) else []


def _get_subagent_run(
    *,
    api_url: str,
    token: str,
    grant_id: str,
) -> dict[str, Any] | None:
    try:
        data = _http_json(
            "GET",
            api_url.rstrip() + "/v1/me/subagent-runs/" + urllib.parse.quote(grant_id, safe=""),
            token=token,
            timeout=30.0,
        )
    except RuntimeError:
        return None
    return data if isinstance(data, dict) else None


def _direct_subagent_runs(
    *,
    api_url: str,
    token: str,
    final: dict[str, Any],
) -> list[dict[str, Any]]:
    grant_ids: list[str] = []
    root = final.get("grant_id")
    if isinstance(root, str) and root:
        grant_ids.append(root)
    for event in final.get("events") or []:
        if not isinstance(event, dict) or event.get("kind") != "composition_call_complete":
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        grant_id = payload.get("grant_id")
        if isinstance(grant_id, str) and grant_id and grant_id not in grant_ids:
            grant_ids.append(grant_id)
    runs = []
    for grant_id in grant_ids:
        run = _get_subagent_run(api_url=api_url, token=token, grant_id=grant_id)
        if run is not None:
            runs.append(run)
    return runs


def _extract_report(final: dict[str, Any]) -> dict[str, Any] | None:
    candidates: list[Any] = [final]
    content = final.get("content")
    if isinstance(content, str):
        candidates.extend(_json_objects_from_text(content))
    for candidate in candidates:
        report = _find_report(candidate)
        if report is not None:
            return report
    return None


def _json_objects_from_text(text: str) -> list[Any]:
    parsed = _try_json(text)
    if parsed is not None:
        return [parsed]
    out: list[Any] = []
    decoder = json.JSONDecoder()
    for match in re.finditer(r"{", text):
        try:
            value, _ = decoder.raw_decode(text[match.start() :])
        except json.JSONDecodeError:
            continue
        out.append(value)
    return out


def _find_report(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        nested = value.get("agent_studio_report")
        if isinstance(nested, dict):
            result = nested.get("result")
            if isinstance(result, dict) and {"ok", "status", "agent_name"}.issubset(result):
                return result
            return nested
        if {"ok", "status", "agent_name", "handoffs", "tests"}.issubset(value):
            return value
        for child in value.values():
            found = _find_report(child)
            if found is not None:
                return found
    if isinstance(value, list):
        for child in value:
            found = _find_report(child)
            if found is not None:
                return found
    return None


def _assert_acceptance(
    *,
    final: dict[str, Any],
    report: dict[str, Any] | None,
    subagent_runs: list[dict[str, Any]],
    exercise_code_editor: bool = True,
) -> None:
    problems: list[str] = []
    if final.get("status") is not None and final.get("status") != "ok":
        problems.append(f"connector status is {final.get('status')!r}")
    if report is None:
        problems.append("Agent Studio report was not found in final connector content")
    else:
        if report.get("ok") is not True:
            problems.append(f"Agent Studio report not ok: status={report.get('status')!r}")
        for item in report.get("tests") or []:
            if isinstance(item, dict) and item.get("status") == "fail":
                problems.append(f"failed smoke test {item.get('name')}: {item.get('summary')}")
        required_handoff_agents = set(REQUIRED_HANDOFF_AGENTS)
        if not exercise_code_editor:
            required_handoff_agents.discard("code-editor-agent")
        seen_required: set[str] = set()
        for item in report.get("handoffs") or []:
            if not isinstance(item, dict):
                continue
            agent = str(item.get("agent") or "")
            status = str(item.get("status") or "")
            if agent in required_handoff_agents:
                seen_required.add(agent)
                if status.lower() not in GOOD_HANDOFF_STATUSES:
                    problems.append(
                        f"required handoff {item.get('agent')}.{item.get('skill')} "
                        f"status={item.get('status')}: {item.get('result_summary')}"
                    )
            elif status in {"failed", "blocked"}:
                problems.append(
                    f"failed handoff {item.get('agent')}.{item.get('skill')}: {item.get('result_summary')}"
                )
        missing = sorted(required_handoff_agents - seen_required)
        if missing:
            problems.append("missing required handoff(s): " + ", ".join(missing))
    for run in subagent_runs:
        status = str(run.get("status") or "").lower()
        if status in BAD_RUN_STATUSES or (status and status not in GOOD_RUN_STATUSES):
            problems.append(
                f"subagent run {run.get('agent_name')}.{run.get('skill_name')} "
                f"status={run.get('status')} error={run.get('error')}"
            )
    if problems:
        raise SystemExit("Agent Studio harness failed:\n- " + "\n- ".join(problems))


def run_harness(args: argparse.Namespace) -> int:
    try:
        app_spec_json = json.dumps(
            json.loads(args.app_spec_json), separators=(",", ":"), sort_keys=True
        )
    except (TypeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"--app-spec-json must be a JSON object: {exc}") from exc
    if not isinstance(json.loads(app_spec_json), dict):
        raise SystemExit("--app-spec-json must contain a JSON object")
    if args.name:
        name = args.name
    elif args.allow_generated_name:
        name = _agent_name()
    else:
        raise SystemExit(
            "--name is required unless --allow-generated-name is set; refusing "
            "to create another studio-harness-* repo by accident."
        )

    token = args.bearer_token
    token_payload: dict[str, Any] | None = None
    if args.mint_token:
        token_payload = mint_token(email=args.email, kubeconfig=args.kubeconfig)
        token = str(token_payload["token"])
    if not token:
        raise SystemExit("--bearer-token/A2A_AGENT_STUDIO_BEARER or --mint-token is required")
    if args.print_token:
        print(token)

    me = _http_json("GET", args.api_url.rstrip("/") + "/v1/me", token=token, timeout=30.0)
    user_id = _user_id(me, token_payload)
    grant_payload: dict[str, Any] | None = None
    _assert_expected_repo(
        api_url=args.api_url,
        token=token,
        name=name,
        expected_repo_url=args.expected_repo_url,
    )

    if args.mode == "connector":
        prompt = _agent_studio_prompt(
            name=name,
            goal=args.goal,
            max_iterations=args.max_iterations,
            quality_bar=args.quality_bar,
            exercise_code_editor=args.exercise_code_editor,
            app_spec_json=app_spec_json,
        )
        initial = _mcp_tool(
            api_url=args.api_url,
            token=token,
            tool="chat",
            arguments={
                "prompt": prompt,
                "approval_mode": True,
                **(
                    {"organization_slug": token_payload["organization_slug"]}
                    if isinstance(token_payload, dict) and token_payload.get("organization_slug")
                    else {}
                ),
            },
            request_id=1,
        )
        final = _poll_job(
            api_url=args.api_url,
            token=token,
            initial=initial,
            timeout_seconds=args.timeout_seconds,
            poll_seconds=args.poll_seconds,
            auto_approve=args.auto_approve,
        )
        thread_id = final.get("thread_id") if isinstance(final.get("thread_id"), str) else None
        report = _extract_report(final)
    else:
        grant_payload = mint_grant(
            name=name,
            user_id=user_id,
            kubeconfig=args.kubeconfig,
            namespace=args.namespace,
            deployment=args.deployment,
            container=args.container,
            llm_model=args.llm_model,
            llm_budget_usd=args.llm_budget_usd,
            ttl_seconds=args.grant_ttl_seconds,
        )
        final = _invoke_agent_studio_direct(
            agent_url=args.agent_url,
            token=token,
            grant=str(grant_payload["grant"]),
            name=name,
            goal=args.goal,
            max_iterations=args.max_iterations,
            quality_bar=args.quality_bar,
            exercise_code_editor=args.exercise_code_editor,
            app_spec_json=app_spec_json,
            max_spend_cents=max(100, min(int(args.llm_budget_usd * 100), 3000)),
            api_url=args.api_url,
            llm_model=args.llm_model,
            llm_base_url=args.llm_base_url,
            timeout_seconds=args.timeout_seconds,
        )
        report = final.get("result") if isinstance(final.get("result"), dict) else _extract_report(final)
        runs = _direct_subagent_runs(api_url=args.api_url, token=token, final=final)
    if args.mode == "connector":
        runs = _list_subagent_runs(api_url=args.api_url, token=token, thread_id=thread_id)
    out = {
        "ok": bool(isinstance(report, dict) and report.get("ok") is True),
        "user": me,
        "token_user": {k: v for k, v in (token_payload or {}).items() if k != "token"},
        "target_agent": name,
        "mode": args.mode,
        "connector": final if args.mode == "connector" else None,
        "direct_invoke": final if args.mode == "direct" else None,
        "grant": (
            {"payload": grant_payload.get("payload")}
            if isinstance(grant_payload, dict)
            else None
        ),
        "agent_studio_report": report,
        "subagent_runs": runs,
    }
    args.output.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _assert_acceptance(
        final=final,
        report=report,
        subagent_runs=runs,
        exercise_code_editor=args.exercise_code_editor,
    )
    print(f"Agent Studio harness passed for {name}; wrote {args.output}")
    return 0


def _user_id(me: Any, token_payload: dict[str, Any] | None) -> int:
    candidates = []
    if isinstance(token_payload, dict):
        candidates.append(token_payload.get("user_id"))
    if isinstance(me, dict):
        candidates.extend((me.get("id"), me.get("user_id")))
        user = me.get("user")
        if isinstance(user, dict):
            candidates.extend((user.get("id"), user.get("user_id")))
    for value in candidates:
        try:
            user_id = int(value)
        except (TypeError, ValueError):
            continue
        if user_id > 0:
            return user_id
    raise SystemExit(f"could not determine user id from /v1/me: {me!r}")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "token":
        payload = mint_token(
            email=args.email,
            kubeconfig=args.kubeconfig,
            namespace=args.namespace,
            deployment=args.deployment,
            container=args.container,
        )
        print(json.dumps(payload, sort_keys=True) if args.as_json else payload["token"])
        return 0
    if args.command == "run":
        return run_harness(args)
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
