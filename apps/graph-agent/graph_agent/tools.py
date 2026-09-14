"""LangChain tools the chart deepagent uses inside the sandbox."""
from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
from langchain_core.tools import tool

if TYPE_CHECKING:
    from .config import Settings


@dataclass(frozen=True)
class ToolContext:
    bucket: str
    settings: "Settings"
    grant_token: str | None = None


async def _post(ctx: ToolContext, path: str, body: dict[str, Any]) -> dict[str, Any]:
    base = ctx.settings.sandbox_url
    timeout = ctx.settings.sandbox_timeout_s + 30
    headers = (
        {"authorization": f"Bearer {ctx.settings.sandbox_token}"}
        if ctx.settings.sandbox_token
        else {"X-A2A-Grant": ctx.grant_token}
        if ctx.grant_token
        else None
    )
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(f"{base}{path}", json=body, headers=headers)
    except httpx.HTTPError as exc:
        return {"error": f"sandbox unreachable: {exc}"}
    if r.status_code >= 400:
        return {"error": f"sandbox {r.status_code}", "detail": r.text[:1000]}
    return r.json()


def build_tools(ctx: ToolContext) -> list[Any]:
    image = ctx.settings.image
    timeout = ctx.settings.sandbox_timeout_s

    @tool
    async def list_workspace_files() -> str:
        """List every file in the user's workspace.

        Returns JSON ``{files: [path, ...]}`` — workspace-relative paths.
        Use BEFORE assuming what's available.
        """
        script = (
            "set -e\n"
            "python - <<'PY'\n"
            "import json, os\n"
            "out = []\n"
            "for dp, dn, fns in os.walk('/workspace'):\n"
            "    dn[:] = [d for d in dn if not d.startswith('.')]\n"
            "    for fn in fns:\n"
            "        out.append(os.path.relpath(os.path.join(dp, fn), '/workspace'))\n"
            "        if len(out) >= 200: break\n"
            "    if len(out) >= 200: break\n"
            "out.sort()\n"
            "print(json.dumps({'files': out}))\n"
            "PY\n"
        )
        result = await _post(
            ctx, "/v1/run_shell",
            {
                "bucket": ctx.bucket, "script": script, "image": image,
                "memory_mib": 256, "timeout_seconds": 30,
            },
        )
        if "error" in result:
            return json.dumps(result)
        return result.get("stdout", "").strip() or json.dumps({"files": []})

    @tool
    async def read_csv_sample(path: str, rows: int = 20) -> str:
        """Read the first ``rows`` lines of a CSV file under /workspace.

        Args:
            path: workspace-relative path, e.g. ``data/sales.csv``.
            rows: how many lines to return including the header.

        Returns JSON ``{path, sample}`` or ``{error}``.
        """
        safe = shlex.quote(path.lstrip("/"))
        n = max(1, min(rows, 200))
        script = f"set -e\nhead -n {n} /workspace/{safe}\n"
        result = await _post(
            ctx, "/v1/run_shell",
            {
                "bucket": ctx.bucket, "script": script, "image": image,
                "memory_mib": 256, "timeout_seconds": 30,
            },
        )
        if "error" in result:
            return json.dumps(result)
        if (result.get("exit_code") or 0) != 0:
            return json.dumps({
                "error": "read failed",
                "stderr": (result.get("stderr") or "")[:500],
            })
        return json.dumps({"path": path, "sample": (result.get("stdout") or "")[:8000]})

    @tool
    async def run_python_in_sandbox(code: str) -> str:
        """Run a Python snippet inside the workspace-mounted microVM.

        pandas + matplotlib + numpy are pre-installed by the wrapper.
        Always set ``matplotlib.use('Agg')`` before importing ``pyplot``.
        Use ``os.makedirs('/workspace/outputs', exist_ok=True)`` before
        writing the PNG.

        Returns JSON ``{exit_code, stdout, stderr}``. Read errors carefully.
        """
        # Encode the source so we don't have to worry about heredoc / quote
        # escaping inside the shell script — the sandbox-runtime's run_shell
        # endpoint doesn't accept arbitrary env vars, so we can't go through
        # os.environ.
        import base64
        b64 = base64.b64encode(code.encode("utf-8")).decode("ascii")
        wrap = (
            "set -e\n"
            "pip install -q --no-cache-dir pandas matplotlib numpy >/dev/null\n"
            f"echo '{b64}' | base64 -d > /tmp/_script.py\n"
            "python /tmp/_script.py\n"
        )
        result = await _post(
            ctx, "/v1/run_shell",
            {
                "bucket": ctx.bucket,
                "script": wrap,
                "image": image,
                "memory_mib": 1024,
                "timeout_seconds": timeout,
            },
        )
        return json.dumps(result if "error" in result else {
            "exit_code": result.get("exit_code"),
            "stdout": (result.get("stdout") or "")[:4000],
            "stderr": (result.get("stderr") or "")[:4000],
        })

    return [list_workspace_files, read_csv_sample, run_python_in_sandbox]
