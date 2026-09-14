"""Tools exposed to the inner DeepAgents graph.

The reviewer relies heavily on the LLM reading source through the
GiteaBackend (built-in DeepAgents `read_file`, `grep`, `glob`) plus the
skill bundles for judgment. Tools here cover what the model cannot do
on its own: round-trip the project through a sandbox to verify ``a2a
card`` succeeds, run ``ruff`` for objective static issues, and submit a
typed final report.
"""
from __future__ import annotations

import base64
import io
import json
import tarfile
import textwrap
from dataclasses import dataclass
from typing import Any, Literal

import httpx
from langchain_core.tools import tool
from pydantic import BaseModel, Field

from .config import Settings

_FINDING_LITERAL = Literal["critical", "warning", "info"]
_CATEGORY_LITERAL = Literal["security", "correctness", "ergonomics", "policy", "scope"]


class Finding(BaseModel):
    severity: _FINDING_LITERAL
    category: _CATEGORY_LITERAL
    message: str
    file: str | None = None
    line: int | None = None
    suggestion: str | None = None


class ReviewReport(BaseModel):
    ok: bool = Field(
        ..., description="True iff there are zero critical findings."
    )
    agent_name: str
    ref: str
    summary: str
    findings: list[Finding] = Field(default_factory=list)


@dataclass(frozen=True)
class ToolContext:
    settings: Settings
    agent_name: str
    ref: str
    fetch_source: Any  # async callable: () -> dict[str, bytes]
    completion_box: dict[str, ReviewReport]


def build_tools(ctx: ToolContext) -> list[Any]:
    """Construct LangChain tools bound to ``ctx``."""

    sandbox_url = ctx.settings.sandbox_url.rstrip("/")
    sandbox_timeout = ctx.settings.sandbox_timeout_s
    sandbox_headers: dict[str, str] = {}
    if ctx.settings.sandbox_token:
        sandbox_headers["Authorization"] = f"Bearer {ctx.settings.sandbox_token}"

    async def _tarball() -> bytes:
        files: dict[str, bytes] = await ctx.fetch_source()
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for path, data in files.items():
                info = tarfile.TarInfo(name=path)
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
        return buf.getvalue()

    async def _run_in_sandbox(script: str) -> dict[str, Any]:
        tar_b64 = base64.b64encode(await _tarball()).decode()
        payload = {
            "command": script,
            "image": ctx.settings.image,
            "stdin_b64": tar_b64,
            "timeout_seconds": sandbox_timeout,
        }
        async with httpx.AsyncClient(timeout=sandbox_timeout + 30) as client:
            resp = await client.post(
                f"{sandbox_url}/v1/run_shell",
                json=payload,
                headers=sandbox_headers,
            )
        if resp.status_code >= 400:
            return {"error": f"sandbox {resp.status_code}: {resp.text[:300]}"}
        return resp.json()

    @tool
    async def sandbox_a2a_card() -> dict[str, Any]:
        """Round-trip the agent project through a sandbox.

        Extracts the Gitea source into ``/workspace``, installs requirements,
        and runs ``a2a card`` to verify the manifest + class load cleanly.

        Returns ``{"ok": True, "card": {...}}`` on success, or
        ``{"ok": False, "exit_code": int, "stdout": str, "stderr": str}`` on
        any failure. Treat exit_code != 0 as a CRITICAL finding.
        """
        script = textwrap.dedent(
            """
            set -e
            mkdir -p /workspace/project
            cd /workspace/project
            tar -xzf - 2>/dev/null
            pip install --quiet -r requirements.txt 2>&1 | tail -5
            a2a card
            """
        ).strip()
        out = await _run_in_sandbox(script)
        if out.get("exit_code", 0) != 0:
            return {
                "ok": False,
                "exit_code": out.get("exit_code"),
                "stdout": (out.get("stdout") or "")[-2000:],
                "stderr": (out.get("stderr") or "")[-2000:],
            }
        try:
            card = json.loads(out.get("stdout") or "{}")
        except json.JSONDecodeError:
            return {
                "ok": False,
                "exit_code": 0,
                "error": "a2a card stdout was not valid JSON",
                "stdout": (out.get("stdout") or "")[-2000:],
            }
        return {"ok": True, "card": card}

    @tool
    async def sandbox_ruff_check() -> dict[str, Any]:
        """Run ``ruff check`` on the agent source in a sandbox.

        Returns ``{"exit_code": int, "findings": [...]}`` where each entry
        carries ``code``, ``message``, ``file``, ``line``. Treat any finding
        as at least an INFO; security-related codes (S-prefix) should be
        WARNING or CRITICAL depending on the rule.
        """
        script = textwrap.dedent(
            """
            set -e
            mkdir -p /workspace/project
            cd /workspace/project
            tar -xzf - 2>/dev/null
            pip install --quiet ruff 2>&1 | tail -3
            ruff check --output-format=json . || true
            """
        ).strip()
        out = await _run_in_sandbox(script)
        stdout = out.get("stdout") or ""
        try:
            findings = json.loads(stdout) if stdout.strip() else []
        except json.JSONDecodeError:
            findings = []
        return {
            "exit_code": out.get("exit_code", 0),
            "stderr": (out.get("stderr") or "")[-1000:],
            "findings": findings[:200],
        }

    @tool
    async def submit_review_report(report_json: str) -> dict[str, Any]:
        """Submit the final structured review report. Call exactly once.

        ``report_json`` must parse to ``{ok, agent_name, ref, summary,
        findings: [{severity, category, message, file?, line?, suggestion?}]}``.
        Use lowercase severities (critical|warning|info) and categories
        (security|correctness|ergonomics|policy|scope).

        Returns ``{"accepted": True}`` on success or ``{"accepted": False,
        "error": "..."}`` on schema failure — fix the JSON and call again.
        """
        try:
            raw = json.loads(report_json)
            report = ReviewReport.model_validate(raw)
        except Exception as exc:  # noqa: BLE001
            return {"accepted": False, "error": f"{type(exc).__name__}: {exc}"}
        report = report.model_copy(update={
            "agent_name": ctx.agent_name,
            "ref": ctx.ref,
        })
        ctx.completion_box["report"] = report
        return {"accepted": True}

    return [sandbox_a2a_card, sandbox_ruff_check, submit_review_report]
