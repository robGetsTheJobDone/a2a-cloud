"""LangChain tools the inner deepagents loop uses.

- workspace file CRUD through the caller's scoped workspace grant
- sandbox python (for `a2a card` / `a2a validate` round-trips)
- cp_deploy_tarball (call /v1/agents/from-tarball on the user's behalf)
- cp_deploy_source_repo (call /v1/agents/{name}/source/deploy)
- cp_compose_meta_agent (call /v1/agents/compose with a manifest)
"""

from __future__ import annotations

import asyncio
import ast
import base64
import hashlib
import io
import json
import logging
import os
import re
import tarfile
import tempfile
import time
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any

import boto3
import httpx
from botocore.config import Config as _BotoConfig
from botocore.exceptions import ClientError
from langchain_core.tools import tool

from . import workspace_grant_check

if TYPE_CHECKING:
    from .config import Settings


A2A_PACK_MIN_VERSION = "0.1.104"  # first release with the @tool decorator name
A2A_PACK_SANDBOX_FALLBACK_VERSION = "0.1.104"
logger = logging.getLogger(__name__)

_SANDBOX_FAILURE_RE = re.compile(
    r"^A2A_SANDBOX_FAILURE stage=(?P<stage>[a-z0-9_-]+) exit_code=(?P<code>\d+)$",
    re.MULTILINE,
)
_SANDBOX_STDOUT_LIMIT = 6000
_SANDBOX_STDERR_LIMIT = 4000


@dataclass(frozen=True)
class ToolContext:
    bucket: str
    settings: "Settings"
    cp_jwt: str | None = None
    organization_slug: str | None = None
    workspace: Any | None = None
    sandbox: Any | None = None
    grant_token: str | None = None


def _canonical_agent_url(name: str | None) -> str | None:
    if not isinstance(name, str) or not name.strip():
        return None
    return f"https://{name.strip()}.a2acloud.io"


def _deploy_poll_url(body: dict[str, Any], name: str | None) -> str | None:
    for key in ("expected_url", "url"):
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return _canonical_agent_url(name)


async def _wait_for_live_card(
    url: str | None,
    expected_version: str | None,
    timeout_s: float = 900.0,
) -> tuple[bool, dict[str, Any]]:
    """Poll ``{url}/.well-known/agent-card`` until ``version`` matches
    ``expected_version`` or ``timeout_s`` elapses. Returns
    ``(matched, last_seen_card)``. ``last_seen_card`` is ``{}`` if the
    endpoint never responded with a parseable card.

    Backoff: 2s → 1.5x per try, capped at 10s. Tolerates pod-not-ready
    (any HTTPError or 4xx/5xx) — just keeps polling.
    """
    if not url or not expected_version:
        return False, {}
    deadline = time.monotonic() + timeout_s
    last_card: dict[str, Any] = {}
    interval = 2.0
    while time.monotonic() < deadline:
        try:
            async with httpx.AsyncClient(timeout=5.0) as c:
                r = await c.get(f"{url.rstrip('/')}/.well-known/agent-card")
            if r.status_code == 200:
                card = r.json() or {}
                if isinstance(card, dict):
                    last_card = card
                    if card.get("version") == expected_version:
                        return True, card
        except (httpx.HTTPError, ValueError):
            pass
        await asyncio.sleep(interval)
        interval = min(interval * 1.5, 10.0)
    return False, last_card


async def _wait_for_live_deployment(
    *,
    cp_url: str,
    cp_jwt: str,
    name: str,
    deployment_id: str | None,
    expected_head_sha: str | None,
    timeout_s: float = 900.0,
) -> tuple[bool, dict[str, Any]]:
    """Wait for the exact control-plane deployment to reach ``live``.

    An Agent Card alone is not sufficient evidence: a same-version previous
    revision can still answer while the new deployment is provisioning a
    database or failing another platform readiness check.
    """

    if not deployment_id:
        return False, {}
    deadline = time.monotonic() + timeout_s
    last_deployment: dict[str, Any] = {}
    interval = 2.0
    terminal_failures = {"failed", "rolled_back", "canceled", "cancelled"}
    while time.monotonic() < deadline:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    f"{cp_url}/v1/agents/{name}/deployments/{deployment_id}",
                    headers={"authorization": f"bearer {cp_jwt}"},
                )
            if response.status_code == 200:
                payload = response.json() or {}
                if isinstance(payload, dict):
                    deployment = payload.get("deployment", payload)
                    if isinstance(deployment, dict):
                        last_deployment = deployment
                        status = str(deployment.get("status") or "").lower()
                        head_sha = deployment.get("head_sha")
                        head_matches = (
                            not expected_head_sha or head_sha == expected_head_sha
                        )
                        if status == "live" and head_matches:
                            return True, deployment
                        if status in terminal_failures:
                            return False, deployment
        except (httpx.HTTPError, ValueError):
            pass
        await asyncio.sleep(interval)
        interval = min(interval * 1.5, 10.0)
    return False, last_deployment


def _s3(ctx: ToolContext) -> Any:
    return boto3.client(
        "s3",
        endpoint_url=ctx.settings.minio_endpoint,
        aws_access_key_id=ctx.settings.minio_access_key,
        aws_secret_access_key=ctx.settings.minio_secret_key,
        region_name="us-east-1",
        config=_BotoConfig(s3={"addressing_style": "path"}),
    )


def _ensure_bucket(s3: Any, bucket: str) -> None:
    try:
        s3.head_bucket(Bucket=bucket)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code in {"404", "NoSuchBucket", "NotFound"}:
            s3.create_bucket(Bucket=bucket)
        elif code != "403":
            raise


def _sandbox_result_payload(
    *,
    exit_code: Any,
    stdout: Any,
    stderr: Any,
) -> dict[str, Any]:
    """Keep the actionable end of failed sandbox output.

    Agent Card JSON can be much larger than the tool result budget. A failure
    therefore returns output tails and a compact stage parsed from the shell
    trap, while a successful run retains the traditional stdout head.
    """
    stdout_text = str(stdout or "")
    stderr_text = str(stderr or "")
    failed = exit_code not in (0, None)
    matches = list(_SANDBOX_FAILURE_RE.finditer(stderr_text))
    result: dict[str, Any] = {"exit_code": exit_code}
    if failed:
        result["failed_stage"] = matches[-1].group("stage") if matches else "unknown"
    result["stdout"] = (
        stdout_text[-_SANDBOX_STDOUT_LIMIT:]
        if failed
        else stdout_text[:_SANDBOX_STDOUT_LIMIT]
    )
    result["stderr"] = stderr_text[-_SANDBOX_STDERR_LIMIT:]
    if len(stdout_text) > _SANDBOX_STDOUT_LIMIT:
        result["stdout_truncated"] = True
        result["stdout_total_chars"] = len(stdout_text)
    if len(stderr_text) > _SANDBOX_STDERR_LIMIT:
        result["stderr_truncated"] = True
        result["stderr_total_chars"] = len(stderr_text)
    return result


class _ObjectStore:
    def iter_keys(self, prefix: str) -> list[str]:
        raise NotImplementedError

    def get(self, key: str) -> bytes:
        raise NotImplementedError

    def put(self, key: str, body: bytes, content_type: str) -> None:
        raise NotImplementedError

    def delete(self, key: str) -> None:
        raise NotImplementedError


class _S3ObjectStore(_ObjectStore):
    def __init__(self, s3: Any, bucket: str) -> None:
        self._s3 = s3
        self._bucket = bucket

    def iter_keys(self, prefix: str) -> list[str]:
        keys: list[str] = []
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
            for obj in page.get("Contents") or ():
                key = obj.get("Key")
                if isinstance(key, str):
                    keys.append(key)
        return sorted(keys)

    def get(self, key: str) -> bytes:
        obj = self._s3.get_object(Bucket=self._bucket, Key=key)
        return obj["Body"].read()

    def put(self, key: str, body: bytes, content_type: str) -> None:
        self._s3.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=body,
            ContentType=content_type,
        )

    def delete(self, key: str) -> None:
        self._s3.delete_object(Bucket=self._bucket, Key=key)


class _WorkspaceObjectStore(_ObjectStore):
    def __init__(self, workspace: Any) -> None:
        self._workspace = workspace

    def iter_keys(self, prefix: str) -> list[str]:
        return sorted(
            key
            for key in self._workspace.iter_paths()
            if isinstance(key, str) and key.startswith(prefix)
        )

    def get(self, key: str) -> bytes:
        return self._workspace.read_bytes(key)

    def put(self, key: str, body: bytes, content_type: str) -> None:
        del content_type
        self._workspace.write_bytes(key, body)

    def delete(self, key: str) -> None:
        self._workspace.delete_path(key)


def _store_from_args(
    store_or_s3: Any,
    bucket_or_prefix: str,
    prefix: str | None,
) -> tuple[_ObjectStore, str]:
    """Support new helper calls with ObjectStore and old tests with fake S3."""
    if isinstance(store_or_s3, _ObjectStore):
        return store_or_s3, bucket_or_prefix
    if hasattr(store_or_s3, "iter_keys") and hasattr(store_or_s3, "get"):
        return store_or_s3, bucket_or_prefix
    if prefix is None:
        raise TypeError("prefix is required when passing an S3 client")
    return _S3ObjectStore(store_or_s3, bucket_or_prefix), prefix


def _sandbox_headers(
    bearer_token: str | None,
    grant_token: str | None,
) -> dict[str, str] | None:
    if bearer_token:
        return {"authorization": f"Bearer {bearer_token}"}
    if grant_token:
        return {"X-A2A-Grant": grant_token}
    return None


_AGENT_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,62}$")
_SKILL_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_BUILDER_STATE_FILE = ".a2a-builder-state.json"
_BUILDER_INTERNAL_PREFIX = ".agent-builder/"
_EXCLUDED_SOURCE_PARTS = frozenset(
    {
        "__pycache__",
        ".git",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".venv",
        "node_modules",
    }
)
_EXCLUDED_SOURCE_SUFFIXES = (".pyc", ".pyo", ".pyd")
_EXCLUDED_SOURCE_PART_SUFFIXES = (".egg-info",)


def _validate_name(name: str) -> None:
    if not _AGENT_NAME_RE.match(name):
        raise ValueError(
            f"invalid agent name {name!r}: must match {_AGENT_NAME_RE.pattern}"
        )


def _validate_skill_name(name: str) -> None:
    if not _SKILL_NAME_RE.match(name) or "--" in name:
        raise ValueError(
            "invalid skill name "
            f"{name!r}: use lowercase alphanumeric kebab-case, max 64 chars"
        )


def _agent_prefix(name: str) -> str:
    _validate_name(name)
    return f"agents/{name}/"


def _builder_state_key(name: str) -> str:
    return _agent_prefix(name) + _BUILDER_STATE_FILE


def _should_include_source_path(path: str) -> bool:
    normalized = path.replace("\\", "/").strip("/")
    if not normalized:
        return False
    parts = normalized.split("/")
    if any(part in _EXCLUDED_SOURCE_PARTS for part in parts):
        return False
    if any(part.endswith(_EXCLUDED_SOURCE_PART_SUFFIXES) for part in parts):
        return False
    return not normalized.endswith(_EXCLUDED_SOURCE_SUFFIXES)


def _should_include_agent_rel_path(rel: str) -> bool:
    if rel == _BUILDER_STATE_FILE or rel.startswith(_BUILDER_INTERNAL_PREFIX):
        return False
    return _should_include_source_path(rel)


def build_tools(ctx: ToolContext) -> list[Any]:
    bucket = ctx.bucket
    settings = ctx.settings
    if ctx.workspace is not None:
        store: _ObjectStore = _WorkspaceObjectStore(ctx.workspace)
    else:
        s3 = _s3(ctx)
        _ensure_bucket(s3, bucket)
        store = _S3ObjectStore(s3, bucket)
    source_backed_workspace = bool(
        ctx.workspace is not None
        and (
            getattr(ctx.workspace, "source_backed_agent_paths", False)
            or type(ctx.workspace).__name__ == "ControlPlaneWorkspaceClient"
        )
    )

    async def _deploy_managed_source(
        name: str,
        *,
        expected_version: str | None = None,
    ) -> str:
        if not ctx.cp_jwt:
            return json.dumps(
                {
                    "error": "no CP JWT forwarded — agent declaration missing wants_cp_jwt=True",
                }
            )
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    f"{settings.cp_url}/v1/agents/{name}/source/deploy",
                    headers={"authorization": f"bearer {ctx.cp_jwt}"},
                )
        except httpx.HTTPError as exc:
            return json.dumps({"error": f"cp unreachable: {exc}"})
        if response.status_code >= 400:
            return json.dumps(
                {
                    "error": f"cp {response.status_code}",
                    "detail": response.text[:1000],
                }
            )
        payload = response.json()
        body = payload if isinstance(payload, dict) else {}
        deployment = body.get("deployment") if isinstance(body, dict) else None
        deployment = deployment if isinstance(deployment, dict) else {}
        url = deployment.get("agent_url") or _canonical_agent_url(name)
        source_sha = body.get("source_sha")
        out: dict[str, Any] = {
            "ok": True,
            "name": body.get("agent_name") or name,
            "status": body.get("status") or deployment.get("status"),
            "url": url,
            "head_sha": source_sha,
            "deployment_id": body.get("deploy_id"),
            "deployment": deployment,
            "already_deployed": bool(body.get("already_deployed")),
            "skipped": bool(body.get("skipped")),
            "source_deploy": True,
        }
        if body.get("summary"):
            out["summary"] = body.get("summary")
        if body.get("reason"):
            out["reason"] = body.get("reason")
        if expected_version:
            live, live_card = await _wait_for_live_card(
                str(url) if url else None,
                expected_version,
                settings.deploy_wait_timeout_s,
            )
            out.update(
                {
                    "ok": live,
                    "live": live,
                    "version": expected_version,
                    "live_version": live_card.get("version"),
                    "live_skills": [
                        {
                            "name": skill.get("name"),
                            "input_schema": skill.get("input_schema") or {},
                        }
                        for skill in live_card.get("skills") or []
                        if isinstance(skill, dict)
                    ],
                }
            )
            if not live:
                out["error"] = "source deploy did not reach the expected live version"
        if isinstance(source_sha, str) and source_sha:
            state = _read_builder_state(store, name)
            state.update(
                {
                    "agent": name,
                    "repo_head_sha": source_sha,
                    "deployment_id": body.get("deploy_id"),
                    "version": expected_version,
                    "updated_at": int(time.time()),
                    "source": "agent-builder-source",
                }
            )
            _write_builder_state(store, name, state)
        return json.dumps(out)

    @tool
    def init_agent_template(
        name: str,
        description: str = "A new A2A agent",
        frontend: str = "none",
        profile: str = "standard",
    ) -> str:
        """Initialize ``agents/<name>/`` from the installed a2a-pack template.

        Use this FIRST for a new project. It writes the same baseline files
        as ``a2a init`` for the SDK version installed in this runtime:
        ``agent.py``, ``a2a.yaml``, and ``requirements.txt``. Set
        ``frontend`` to ``"react"`` for a Vite packed app, ``"static"`` for a
        minimal bundled HTML app, or ``"none"`` for a headless agent. After
        this, edit those files with ``read_agent_file`` + ``write_agent_file``
        to implement the user's requested behavior. Set ``profile`` to
        ``"full_stack"`` for a product app; that forces React and adds a
        managed Postgres declaration, migration seed, and regression contract
        that must be customized and pass before deployment.
        """
        try:
            profile_kind = _normalize_profile(profile)
            if profile_kind == "full_stack":
                frontend = "react"
            frontend_kind = _normalize_frontend_kind(frontend)
            files = _render_a2a_init_template(
                name,
                description=description,
                frontend=frontend_kind,
                profile=profile_kind,
            )
            prefix = _agent_prefix(name)
        except (OSError, RuntimeError, ValueError) as exc:
            return json.dumps({"error": str(exc)})
        written: list[dict[str, Any]] = []
        for path, content in files.items():
            key = prefix + path
            encoded = content.encode("utf-8")
            store.put(key, encoded, "text/plain; charset=utf-8")
            written.append({"path": key, "size": len(encoded)})
        return json.dumps(
            {
                "ok": True,
                "agent": name,
                "frontend": frontend_kind,
                "profile": profile_kind,
                "files": written,
                "docs": (
                    "https://docs.a2acloud.io/concepts/packed-frontends"
                    if frontend_kind != "none"
                    else "https://docs.a2acloud.io/quickstart"
                ),
            }
        )

    @tool
    def list_agent_files(name: str) -> str:
        """List every file under ``agents/<name>/`` in the user's workspace.

        Use this before edits to see what you already wrote / what's left.
        """
        try:
            prefix = _agent_prefix(name)
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        out: list[dict[str, Any]] = []
        for key in store.iter_keys(prefix):
            rel = key[len(prefix) :]
            if not _should_include_agent_rel_path(rel):
                continue
            try:
                size = len(store.get(key))
            except FileNotFoundError:
                logger.warning("Skipping disappeared source file during list: %s", key)
                continue
            out.append({"path": rel, "size": size})
        return json.dumps({"agent": name, "files": out})

    @tool
    def write_agent_file(name: str, path: str, content: str) -> str:
        """Write a file under ``agents/<name>/<path>`` in the user's workspace.

        Use this for ``agent.py``, ``a2a.yaml``, ``requirements.txt``, and
        any auxiliary modules or packed frontend files the agent needs. Empty
        writes are rejected so an incomplete tool call cannot truncate a file.
        """
        try:
            prefix = _agent_prefix(name)
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        rel = path.lstrip("/")
        if not _should_include_agent_rel_path(rel):
            return json.dumps({"error": f"{rel} is managed by agent-builder"})
        if not content:
            return json.dumps(
                {
                    "error": "content must not be empty; retry with the complete file",
                    "path": prefix + rel,
                    "retryable": True,
                }
            )
        key = prefix + rel
        encoded = content.encode("utf-8")
        out: dict[str, Any] = {"ok": True, "path": key, "size": len(encoded)}
        try:
            store.put(key, encoded, "text/plain; charset=utf-8")
        except Exception as exc:  # noqa: BLE001 - tool failures must remain recoverable
            logger.warning("Workspace write failed for %s: %s", key, exc)
            return json.dumps(
                {
                    "error": f"workspace write failed: {exc}",
                    "path": key,
                    "retryable": True,
                }
            )
        return json.dumps(out)

    @tool
    def read_agent_file(name: str, path: str) -> str:
        """Read a single file from the scaffolded agent project."""
        try:
            prefix = _agent_prefix(name)
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        rel = path.lstrip("/")
        if not _should_include_agent_rel_path(rel):
            return json.dumps({"error": f"{rel} is managed by agent-builder"})
        key = prefix + rel
        try:
            data = store.get(key)
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"error": f"read failed: {exc}"})
        body = data.decode("utf-8", errors="replace")
        return json.dumps({"path": key, "content": body[:200_000]})

    @tool
    def write_agent_skill(
        name: str,
        skill_name: str,
        description: str,
        instructions: str,
        supporting_files_json: str = "{}",
    ) -> str:
        """Create a DeepAgents skill bundle in ``agents/<name>/skills/``.

        Use this for generated agents that should rely on DeepAgents'
        progressive-disclosure skills instead of fake one-off tools.
        ``supporting_files_json`` is a JSON object mapping relative paths
        inside the skill directory to text content, for example
        ``{"references/schema.md": "..."}``.

        After writing skills, update ``agent.py`` so it seeds packaged
        ``skills/`` files into ``ctx.workspace_backend()`` and passes
        ``skills=[...]`` to ``create_deep_agent``.
        """
        try:
            prefix = _agent_prefix(name)
            _validate_skill_name(skill_name)
            files = _parse_supporting_skill_files(supporting_files_json)
            skill_md = _render_skill_md(skill_name, description, instructions)
        except ValueError as exc:
            return json.dumps({"error": str(exc)})

        base = prefix + f"skills/{skill_name}/"
        written: list[dict[str, Any]] = []
        payloads = {"SKILL.md": skill_md, **files}
        for rel, content in payloads.items():
            key = base + rel
            encoded = content.encode("utf-8")
            store.put(key, encoded, "text/plain; charset=utf-8")
            written.append({"path": key, "size": len(encoded)})
        return json.dumps(
            {
                "ok": True,
                "agent": name,
                "skill": skill_name,
                "source_path": f"skills/{skill_name}/",
                "files": written,
                "wire_agent_py": (
                    "Seed project skills into ctx.workspace_backend() and pass "
                    "skills=[RUNTIME_SKILLS_ROOT] to create_deep_agent."
                ),
            }
        )

    @tool
    async def test_agent_in_sandbox(name: str) -> str:
        """Spin a microVM, ``pip install a2a-pack`` + the agent's own
        requirements.txt, then run ``a2a card`` to verify the scaffold
        produces a valid Card. When a packed frontend is declared, it also
        prints ``a2a frontend info`` metadata. Returns stdout/stderr.
        """
        try:
            prefix = _agent_prefix(name)
        except ValueError as exc:
            return json.dumps({"error": str(exc)})

        # Bundle the workspace files into the sandbox via base64 tar so
        # the sandbox doesn't need MinIO read access for this tool.
        bundle_bytes = _tarball_workspace_dir(store, prefix)
        if not bundle_bytes:
            return json.dumps(
                {
                    "error": "no files in agents/" + name + "/ — write some first",
                }
            )
        source_hash = _source_bundle_hash(bundle_bytes)
        b64 = base64.b64encode(bundle_bytes).decode("ascii")
        grant_check_b64 = base64.b64encode(
            Path(workspace_grant_check.__file__).read_bytes()
        ).decode("ascii")
        script = (
            "set -e\n"
            "A2A_SANDBOX_STAGE=bootstrap\n"
            'trap \'status=$?; if [ "$status" -ne 0 ]; then '
            'printf "A2A_SANDBOX_FAILURE stage=%s exit_code=%s\\n" '
            '"$A2A_SANDBOX_STAGE" "$status" >&2; fi\' EXIT\n'
            "export PYTHONDONTWRITEBYTECODE=1\n"
            "A2A_SANDBOX_STAGE=a2a-pack-install\n"
            "python -m pip install --quiet "
            f"'a2a-pack>={A2A_PACK_MIN_VERSION}' >/dev/null || "
            "(echo 'a2a-pack>="
            f"{A2A_PACK_MIN_VERSION} unavailable; falling back to "
            f"a2a-pack=={A2A_PACK_SANDBOX_FALLBACK_VERSION} for sandbox card validation' >&2; "
            "python -m pip install --quiet --index-url https://pypi.org/simple "
            f"'a2a-pack=={A2A_PACK_SANDBOX_FALLBACK_VERSION}' >/dev/null)\n"
            "A2A_SANDBOX_STAGE=unpack\n"
            "mkdir -p /tmp/agent\n"
            f"echo '{b64}' | base64 -d | tar -xzf - -C /tmp/agent\n"
            "cd /tmp/agent\n"
            "ls -la\n"
            "if [ -f requirements.txt ]; then\n"
            "  A2A_SANDBOX_STAGE=requirements-install\n"
            "  pip install --quiet -r requirements.txt >/dev/null\n"
            "fi\n"
            "A2A_SANDBOX_STAGE=card\n"
            "echo '--- card ---'\n"
            "a2a card --project . > /tmp/a2a-agent-card.json\n"
            "cat /tmp/a2a-agent-card.json\n"
            "A2A_SANDBOX_STAGE=workspace-grant-policy-check\n"
            "echo '--- workspace grant policy ---'\n"
            f"echo '{grant_check_b64}' | base64 -d > /tmp/a2a-workspace-grant-check.py\n"
            "python /tmp/a2a-workspace-grant-check.py /tmp/a2a-agent-card.json\n"
            "if [ -d tests ] || find . -maxdepth 1 -name 'test_*.py' -print -quit | grep -q .; then\n"
            "  echo '--- tests ---'\n"
            "  A2A_SANDBOX_STAGE=tests-install\n"
            "  python -m pip install --quiet pytest pytest-asyncio >/dev/null\n"
            "  A2A_SANDBOX_STAGE=tests\n"
            "  python -m pytest -q\n"
            "fi\n"
            "A2A_SANDBOX_STAGE=undefined-name-check\n"
            "echo '--- undefined-name check ---'\n"
            "python - <<'PY'\n"
            "import ast, builtins, pathlib, sys\n"
            "path = pathlib.Path('agent.py')\n"
            "if not path.exists():\n"
            "    sys.exit(0)\n"
            "tree = ast.parse(path.read_text(), filename=str(path))\n"
            "builtins_scope = set(dir(builtins)) | {'__name__', '__file__', '__package__'}\n"
            "globals_scope = set(builtins_scope)\n"
            "def bind_target(target, scope):\n"
            "    if isinstance(target, ast.Name):\n"
            "        scope.add(target.id)\n"
            "    elif isinstance(target, (ast.Tuple, ast.List)):\n"
            "        for item in target.elts:\n"
            "            bind_target(item, scope)\n"
            "for node in tree.body:\n"
            "    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):\n"
            "        globals_scope.add(node.name)\n"
            "    elif isinstance(node, ast.Import):\n"
            "        for alias in node.names:\n"
            "            globals_scope.add((alias.asname or alias.name.split('.')[0]))\n"
            "    elif isinstance(node, ast.ImportFrom):\n"
            "        for alias in node.names:\n"
            "            if alias.name != '*':\n"
            "                globals_scope.add(alias.asname or alias.name)\n"
            "    elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):\n"
            "        targets = getattr(node, 'targets', None) or [getattr(node, 'target', None)]\n"
            "        for target in targets:\n"
            "            bind_target(target, globals_scope)\n"
            "class UndefinedNameVisitor(ast.NodeVisitor):\n"
            "    def __init__(self):\n"
            "        self.scopes = [set(globals_scope)]\n"
            "        self.errors = []\n"
            "    def _defined(self, name):\n"
            "        return any(name in scope for scope in reversed(self.scopes))\n"
            "    def _bind_args(self, args, scope):\n"
            "        for arg in list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs):\n"
            "            scope.add(arg.arg)\n"
            "        if args.vararg:\n"
            "            scope.add(args.vararg.arg)\n"
            "        if args.kwarg:\n"
            "            scope.add(args.kwarg.arg)\n"
            "    def visit_FunctionDef(self, node):\n"
            "        self.visit_function(node)\n"
            "    def visit_AsyncFunctionDef(self, node):\n"
            "        self.visit_function(node)\n"
            "    def visit_function(self, node):\n"
            "        for dec in node.decorator_list:\n"
            "            self.visit(dec)\n"
            "        for default in list(node.args.defaults) + list(node.args.kw_defaults):\n"
            "            if default is not None:\n"
            "                self.visit(default)\n"
            "        scope = set()\n"
            "        self._bind_args(node.args, scope)\n"
            "        self.scopes.append(scope)\n"
            "        for stmt in node.body:\n"
            "            self.visit(stmt)\n"
            "        self.scopes.pop()\n"
            "    def visit_ClassDef(self, node):\n"
            "        for dec in node.decorator_list:\n"
            "            self.visit(dec)\n"
            "        for base in node.bases:\n"
            "            self.visit(base)\n"
            "        self.scopes.append(set())\n"
            "        for stmt in node.body:\n"
            "            self.visit(stmt)\n"
            "        self.scopes.pop()\n"
            "    def visit_Import(self, node):\n"
            "        for alias in node.names:\n"
            "            self.scopes[-1].add(alias.asname or alias.name.split('.')[0])\n"
            "    def visit_ImportFrom(self, node):\n"
            "        for alias in node.names:\n"
            "            if alias.name != '*':\n"
            "                self.scopes[-1].add(alias.asname or alias.name)\n"
            "    def visit_Assign(self, node):\n"
            "        self.visit(node.value)\n"
            "        for target in node.targets:\n"
            "            bind_target(target, self.scopes[-1])\n"
            "    def visit_AnnAssign(self, node):\n"
            "        if node.annotation:\n"
            "            self.visit(node.annotation)\n"
            "        if node.value:\n"
            "            self.visit(node.value)\n"
            "        bind_target(node.target, self.scopes[-1])\n"
            "    def visit_For(self, node):\n"
            "        self.visit(node.iter)\n"
            "        bind_target(node.target, self.scopes[-1])\n"
            "        for stmt in node.body + node.orelse:\n"
            "            self.visit(stmt)\n"
            "    def visit_AsyncFor(self, node):\n"
            "        self.visit_For(node)\n"
            "    def visit_With(self, node):\n"
            "        for item in node.items:\n"
            "            self.visit(item.context_expr)\n"
            "            if item.optional_vars:\n"
            "                bind_target(item.optional_vars, self.scopes[-1])\n"
            "        for stmt in node.body:\n"
            "            self.visit(stmt)\n"
            "    def visit_AsyncWith(self, node):\n"
            "        self.visit_With(node)\n"
            "    def visit_ExceptHandler(self, node):\n"
            "        if node.type:\n"
            "            self.visit(node.type)\n"
            "        if node.name:\n"
            "            self.scopes[-1].add(node.name)\n"
            "        for stmt in node.body:\n"
            "            self.visit(stmt)\n"
            "    def _visit_comp(self, node):\n"
            "        self.scopes.append(set())\n"
            "        for gen in node.generators:\n"
            "            self.visit(gen.iter)\n"
            "            bind_target(gen.target, self.scopes[-1])\n"
            "            for cond in gen.ifs:\n"
            "                self.visit(cond)\n"
            "        for field in ('elt', 'key', 'value'):\n"
            "            value = getattr(node, field, None)\n"
            "            if value is not None:\n"
            "                self.visit(value)\n"
            "        self.scopes.pop()\n"
            "    def visit_ListComp(self, node):\n"
            "        self._visit_comp(node)\n"
            "    def visit_SetComp(self, node):\n"
            "        self._visit_comp(node)\n"
            "    def visit_DictComp(self, node):\n"
            "        self._visit_comp(node)\n"
            "    def visit_GeneratorExp(self, node):\n"
            "        self._visit_comp(node)\n"
            "    def visit_Name(self, node):\n"
            "        if isinstance(node.ctx, ast.Load) and not self._defined(node.id):\n"
            "            self.errors.append(f'{path}:{node.lineno}:{node.col_offset}: undefined name {node.id!r}')\n"
            "        elif isinstance(node.ctx, (ast.Store, ast.Del)):\n"
            "            self.scopes[-1].add(node.id)\n"
            "visitor = UndefinedNameVisitor()\n"
            "visitor.visit(tree)\n"
            "if visitor.errors:\n"
            "    print('\\n'.join(visitor.errors))\n"
            "    sys.exit(1)\n"
            "print('ok')\n"
            "PY\n"
            "if grep -q '^frontend:' a2a.yaml 2>/dev/null; then\n"
            "  echo '--- frontend ---'\n"
            "  A2A_SANDBOX_STAGE=frontend-contract\n"
            "  a2a frontend info --project .\n"
            "  if grep -q '^[[:space:]]*build:' a2a.yaml; then\n"
            "    A2A_SANDBOX_STAGE=frontend-runtime-install\n"
            "    if ! command -v npm >/dev/null 2>&1; then\n"
            "      NODE_VERSION=20.20.2\n"
            "      NODE_SHA256=df770b2a6f130ed8627c9782c988fda9669fa23898329a61a871e32f965e007d\n"
            "      NODE_RUNTIME=/tmp/node-v${NODE_VERSION}-linux-x64\n"
            '      python - "$NODE_VERSION" "$NODE_SHA256" <<\'PY\'\n'
            "import hashlib, pathlib, sys, tarfile, urllib.request\n"
            "version, expected = sys.argv[1:]\n"
            "archive = pathlib.Path(f'/tmp/node-v{version}-linux-x64.tar.xz')\n"
            "url = f'https://nodejs.org/dist/v{version}/{archive.name}'\n"
            "digest = hashlib.sha256()\n"
            "with urllib.request.urlopen(url, timeout=60) as response, archive.open('wb') as output:\n"
            "    while chunk := response.read(1024 * 1024):\n"
            "        digest.update(chunk)\n"
            "        output.write(chunk)\n"
            "if digest.hexdigest() != expected:\n"
            "    archive.unlink(missing_ok=True)\n"
            "    raise SystemExit('pinned Node runtime checksum mismatch')\n"
            "root = pathlib.Path('/tmp').resolve()\n"
            "with tarfile.open(archive, mode='r:xz') as bundle:\n"
            "    for member in bundle.getmembers():\n"
            "        target = (root / member.name).resolve()\n"
            "        if target != root and root not in target.parents:\n"
            "            raise SystemExit('unsafe path in pinned Node runtime archive')\n"
            "    bundle.extractall(root, filter='data')\n"
            "archive.unlink()\n"
            "PY\n"
            '      export PATH="$NODE_RUNTIME/bin:$PATH"\n'
            "      node --version\n"
            "      npm --version\n"
            "    fi\n"
            "    A2A_SANDBOX_STAGE=frontend-dependencies\n"
            "    (cd frontend && if [ -f package-lock.json ]; then npm ci --no-audit --no-fund; else npm install --no-audit --no-fund; fi) >/dev/null\n"
            "    A2A_SANDBOX_STAGE=frontend-build\n"
            "    (cd frontend && npm run build)\n"
            "    test -f frontend/dist/index.html\n"
            "  fi\n"
            "  A2A_SANDBOX_STAGE=frontend-secret-scan\n"
            "  if grep -RIE '(svc\\.cluster\\.local|A2A_LITELLM_KEY|OPENAI_API_KEY|DATABASE_URL|MINIO_SECRET|Bearer[[:space:]]+eyJ)' frontend --exclude-dir=node_modules --exclude-dir=dist; then\n"
            "    echo 'frontend contains a forbidden platform secret/internal endpoint marker' >&2\n"
            "    exit 1\n"
            "  fi\n"
            "fi\n"
            "A2A_SANDBOX_STAGE=database-contract\n"
            "python - <<'PY'\n"
            "import json, pathlib, sys, yaml\n"
            "root = pathlib.Path('.')\n"
            "manifest = yaml.safe_load((root / 'a2a.yaml').read_text()) or {}\n"
            "declared = ((manifest.get('resources') or {}).get('databases') or [])\n"
            "card = json.loads(pathlib.Path('/tmp/a2a-agent-card.json').read_text())\n"
            "live = ((((card.get('runtime') or {}).get('platform_resources') or {}).get('databases')) or [])\n"
            "if bool(declared) != bool(live):\n"
            "    print('managed database must be declared in both a2a.yaml and the Agent Card', file=sys.stderr)\n"
            "    sys.exit(1)\n"
            "for database in declared:\n"
            "    migration = ((database or {}).get('migrations') or {}).get('path')\n"
            "    if not migration:\n"
            "        print('managed database is missing migrations.path', file=sys.stderr)\n"
            "        sys.exit(1)\n"
            "    directory = root / str(migration)\n"
            "    if not directory.is_dir() or not any(directory.glob('*.sql')):\n"
            "        print(f'migration directory {migration!r} has no SQL files', file=sys.stderr)\n"
            "        sys.exit(1)\n"
            "print('ok')\n"
            "PY\n"
            "A2A_SANDBOX_STAGE=complete\n"
        )
        if ctx.sandbox is not None:
            try:
                out = await ctx.sandbox.run_shell(
                    script,
                    image=settings.image,
                    workspace=bucket,
                    memory_mib=512,
                    timeout_seconds=settings.sandbox_timeout_s,
                )
            except Exception as exc:  # noqa: BLE001
                return json.dumps(
                    {
                        "error": f"sandbox {type(exc).__name__}",
                        "detail": str(exc)[:1000],
                    }
                )
            result = _sandbox_result_payload(
                exit_code=getattr(out, "exit_code", None),
                stdout=getattr(out, "stdout", ""),
                stderr=getattr(out, "stderr", ""),
            )
            _record_sandbox_result(store, name, source_hash=source_hash, result=result)
            return json.dumps(result)

        headers = _sandbox_headers(settings.sandbox_token, ctx.grant_token)
        async with httpx.AsyncClient(timeout=settings.sandbox_timeout_s + 30) as c:
            r = await c.post(
                f"{settings.sandbox_url}/v1/run_shell",
                headers=headers,
                json={
                    "bucket": bucket,
                    "script": script,
                    "image": settings.image,
                    "memory_mib": 512,
                    "timeout_seconds": settings.sandbox_timeout_s,
                },
            )
        if r.status_code >= 400:
            return json.dumps(
                {"error": f"sandbox {r.status_code}", "detail": r.text[:1000]}
            )
        out = r.json()
        result = _sandbox_result_payload(
            exit_code=out.get("exit_code"),
            stdout=out.get("stdout"),
            stderr=out.get("stderr"),
        )
        _record_sandbox_result(store, name, source_hash=source_hash, result=result)
        return json.dumps(result)

    @tool
    async def cp_deploy_tarball(
        name: str,
        version: str = "0.1.0",
        public: bool = True,
        force: bool = False,
    ) -> str:
        """Tar the agent at ``agents/<name>/`` and POST it to the control
        plane's ``/v1/agents/from-tarball`` endpoint on the user's behalf,
        then BLOCK until the live ``.well-known/agent-card`` reports the
        new version (or 180s timeout).

        Returns JSON with ``ok``, ``live``, ``url``, ``version``,
        ``live_version``, and ``live_skills[{name, input_schema}]``. When
        ``live=true`` the agent is callable; when ``live=false`` the
        deploy POST succeeded but the pod never came up with the new
        version — caller should treat this as a failure, not success.

        Compare ``live_skills[].input_schema`` against the code you wrote
        to confirm the advertised schema matches the params the function
        actually accepts. Mismatches cause downstream ``agent 400`` from
        ``call_agent``.

        ``force`` defaults to false so a stale MinIO workspace cannot
        silently overwrite source that was deployed from a user's computer
        or another tool. Only set ``force=True`` after the user explicitly
        accepts replacing the current managed repo contents.

        Requires the orchestrator to have forwarded the user's CP JWT
        (the platform does this automatically when this agent's Card
        declared ``wants_cp_jwt=True``).
        """
        try:
            prefix = _agent_prefix(name)
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        if not ctx.cp_jwt:
            return json.dumps(
                {
                    "error": "no CP JWT forwarded — agent declaration missing wants_cp_jwt=True",
                }
            )

        builder_state = _read_builder_state(store, name)
        bundle_bytes = _tarball_workspace_dir(store, prefix)
        if not bundle_bytes:
            return json.dumps({"error": f"no files at agents/{name}/"})
        source_hash = _source_bundle_hash(bundle_bytes)
        sandbox_error = _sandbox_gate_error(builder_state, source_hash=source_hash)
        if sandbox_error is not None:
            return json.dumps(sandbox_error)

        try:
            latest_deploy = await _latest_cp_deployment(
                settings.cp_url, ctx.cp_jwt, name
            )
        except RuntimeError as exc:
            return json.dumps({"error": str(exc)})
        if latest_deploy is not None and source_backed_workspace:
            source_version = _read_manifest_version(bundle_bytes) or version
            return await _deploy_managed_source(name, expected_version=source_version)
        drift = _deployment_drift_error(
            name,
            latest_deploy,
            builder_state,
            force=force,
        )
        if drift is not None:
            return json.dumps(drift)
        base_head = builder_state.get("repo_head_sha")
        if force:
            base_head = None

        # Read the entrypoint from a2a.yaml so we can pass it verbatim.
        entrypoint = _read_entrypoint(bundle_bytes) or f"agent:{_class_name(name)}"
        try:
            agent_dsl_json = _compile_agent_dsl_json(bundle_bytes)
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"error": f"agent_dsl compile failed: {exc}"})
        compiled_description = str(
            (json.loads(agent_dsl_json) or {}).get("description") or ""
        ).strip()

        # NOTE: the CP from-tarball endpoint names the file field
        # ``source`` (see apps/control-plane/control_plane/routes/agents.py).
        files = {"source": (f"{name}.tar.gz", bundle_bytes, "application/gzip")}
        data = {
            "name": name,
            "version": version,
            "entrypoint": entrypoint,
            "public": "true" if public else "false",
            "description": compiled_description or "Built by agent-builder.",
            "agent_dsl": agent_dsl_json,
        }
        if ctx.organization_slug:
            data["organization_slug"] = ctx.organization_slug
        if isinstance(base_head, str) and base_head:
            data["base_head_sha"] = base_head
        try:
            async with httpx.AsyncClient(timeout=60.0) as c:
                r = await c.post(
                    f"{settings.cp_url}/v1/agents/from-tarball",
                    headers={"authorization": f"bearer {ctx.cp_jwt}"},
                    files=files,
                    data=data,
                )
        except httpx.HTTPError as exc:
            return json.dumps({"error": f"cp unreachable: {exc}"})
        if r.status_code >= 400:
            return json.dumps({"error": f"cp {r.status_code}", "detail": r.text[:1000]})
        body = r.json()
        name_ = body.get("name")
        version_ = body.get("version")
        url_ = _deploy_poll_url(body, str(name_) if name_ is not None else name)
        head_sha = body.get("head_sha")
        deployment_id = body.get("deployment_id") or body.get("deploy_id")
        if isinstance(head_sha, str) and head_sha:
            _write_builder_state(
                store,
                name,
                {
                    "agent": name,
                    "repo_head_sha": head_sha,
                    "deployment_id": deployment_id,
                    "version": version_,
                    "updated_at": int(time.time()),
                    "source": "agent-builder",
                },
            )

        # CP returns immediately with status="building". The agent isn't
        # actually callable until the pod rolls + serves the new card.
        # Poll /.well-known/agent-card until the live card reports the
        # version we just deployed (or timeout). Surface the live skills
        # so the LLM can verify the input_schema actually matches the
        # code it wrote — catches the schema/code skew that causes
        # downstream 400s on call_agent.
        live, live_card = await _wait_for_live_card(
            url_,
            version_,
            timeout_s=settings.deploy_wait_timeout_s,
        )
        deployment_live = False
        deployment_state: dict[str, Any] = {}
        if live:
            deployment_live, deployment_state = await _wait_for_live_deployment(
                cp_url=settings.cp_url,
                cp_jwt=ctx.cp_jwt,
                name=name,
                deployment_id=(
                    str(deployment_id) if deployment_id is not None else None
                ),
                expected_head_sha=(str(head_sha) if head_sha is not None else None),
                timeout_s=settings.deploy_wait_timeout_s,
            )
            live = deployment_live
        out: dict[str, Any] = {
            "ok": live,
            "name": name_,
            "version": version_,
            "status": body.get("status"),
            "url": url_,
            "head_sha": head_sha,
            "deployment_id": deployment_id,
            "workspace_base_head_sha": base_head,
            "live": live,
        }
        if live:
            out["live_version"] = live_card.get("version")
            out["live_skills"] = [
                {
                    "name": s.get("name"),
                    "input_schema": s.get("input_schema") or {},
                }
                for s in (live_card.get("skills") or [])
            ]
        else:
            deployment_status = str(deployment_state.get("status") or "unknown")
            out["error"] = (
                "exact deployment did not become live within timeout "
                f"(status={deployment_status}); inspect deployment_id before "
                "declaring the build done."
            )
            if live_card:
                out["last_seen_version"] = live_card.get("version")
        return json.dumps(out)

    @tool
    async def cp_deploy_source_repo(name: str) -> str:
        """Deploy the current managed source repo for ``name``.

        Use this when source changes already live in the platform-managed
        Gitea repo. Do not use it for normal MinIO builder workspaces; for
        those, run ``test_agent_in_sandbox`` and then ``cp_deploy_tarball``.

        Returns JSON from the control plane, including ``deploy_id`` and a
        serialized ``deployment`` when one is queued.
        """
        try:
            _validate_name(name)
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        return await _deploy_managed_source(name)

    @tool
    async def cp_compose_meta_agent(
        name: str,
        manifest_json: str,
        description: str = "",
        version: str = "0.1.0",
        public: bool = True,
        refresh_existing: bool = False,
    ) -> str:
        """Deploy a declarative meta-agent composition through the control plane.

        Use this when the user asks to compose existing agents toward a goal.
        ``manifest_json`` must be a JSON object with ``composition`` and
        optional ``goal`` / ``memory`` blocks. The control plane validates
        referenced sub-agents and skills, generates editable MetaAgent source,
        commits it to the user's managed source repo, and stamps the runtime
        repo. This is the preferred path for meta-agents; do not hand-write
        orchestration source when a manifest is enough.
        """
        try:
            prefix = _agent_prefix(name)
            manifest = _parse_manifest_json(manifest_json)
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        if not ctx.cp_jwt:
            return json.dumps(
                {
                    "error": "no CP JWT forwarded — agent declaration missing wants_cp_jwt=True",
                }
            )
        body = {
            "name": name,
            "description": description,
            "version": version,
            "public": public,
            "manifest": manifest,
            "refresh_existing": refresh_existing,
        }
        if ctx.organization_slug:
            body["organization_slug"] = ctx.organization_slug
        try:
            async with httpx.AsyncClient(timeout=60.0) as c:
                r = await c.post(
                    f"{settings.cp_url}/v1/agents/compose",
                    headers={"authorization": f"bearer {ctx.cp_jwt}"},
                    json=body,
                )
        except httpx.HTTPError as exc:
            return json.dumps({"error": f"cp unreachable: {exc}"})
        if r.status_code >= 400:
            return json.dumps({"error": f"cp {r.status_code}", "detail": r.text[:1000]})
        payload = r.json() or {}
        head_sha = payload.get("head_sha")
        store.put(
            prefix + "meta_agent_manifest.json",
            (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            "application/json",
        )
        if isinstance(head_sha, str) and head_sha:
            _write_builder_state(
                store,
                name,
                {
                    "agent": name,
                    "repo_head_sha": head_sha,
                    "deployment_id": payload.get("deployment_id"),
                    "version": payload.get("version"),
                    "updated_at": int(time.time()),
                    "source": "agent-builder-compose",
                },
            )
        url = payload.get("expected_url") or payload.get("url")
        live, live_card = await _wait_for_live_card(
            url,
            payload.get("version"),
            timeout_s=settings.deploy_wait_timeout_s,
        )
        out: dict[str, Any] = {
            "ok": live,
            "name": payload.get("name"),
            "version": payload.get("version"),
            "status": payload.get("status"),
            "url": url,
            "head_sha": head_sha,
            "deployment_id": payload.get("deployment_id"),
            "preview": payload.get("preview") or {},
            "live": live,
        }
        if live:
            out["live_version"] = live_card.get("version")
            out["live_skills"] = [
                {
                    "name": s.get("name"),
                    "input_schema": s.get("input_schema") or {},
                }
                for s in (live_card.get("skills") or [])
            ]
        else:
            out["error"] = (
                "compose deploy did not become live within timeout — build may "
                "still be rolling. Inspect deployment_id or call cp_refresh_agent later."
            )
            if live_card:
                out["last_seen_version"] = live_card.get("version")
        return json.dumps(out)

    @tool
    async def cp_refresh_agent(name: str) -> str:
        """Force the control plane to re-fetch the agent's live
        ``/.well-known/agent-card`` and update its stored copy.

        Use this when the pod was redeployed out-of-band (CI bump, manual
        rollout) and the CP's cached card is stale — symptoms are stale
        ``input_schema`` in ``list_my_agents`` / ``discover_agent`` and
        4xx from ``call_agent`` on what should be a valid tool call.

        ``cp_deploy_tarball`` already does this automatically by polling
        the live card, so you only need ``cp_refresh_agent`` when you
        did NOT deploy through this tool.

        Returns JSON: the fresh ``AgentDetailOut`` (with ``card`` +
        ``skills`` + ``input_schema``), or ``{error, ...}``.
        """
        if not ctx.cp_jwt:
            return json.dumps(
                {
                    "error": "no CP JWT forwarded — agent declaration missing wants_cp_jwt=True",
                }
            )
        try:
            async with httpx.AsyncClient(timeout=15.0) as c:
                r = await c.get(
                    f"{settings.cp_url}/v1/agents/{name}",
                    headers={"authorization": f"bearer {ctx.cp_jwt}"},
                )
        except httpx.HTTPError as exc:
            return json.dumps({"error": f"cp unreachable: {exc}"})
        if r.status_code >= 400:
            return json.dumps({"error": f"cp {r.status_code}", "detail": r.text[:1000]})
        body = r.json() or {}
        return json.dumps(
            {
                "ok": True,
                "name": body.get("name"),
                "version": body.get("version"),
                "status": body.get("status"),
                "url": body.get("url"),
                "card": body.get("card") or {},
            }
        )

    @tool
    async def sync_agent_workspace_from_repo(name: str, force: bool = False) -> str:
        """Replace ``agents/<name>/`` in MinIO with the current managed repo source.

        Use this before editing an existing deployed agent, or after
        ``cp_deploy_tarball`` returns ``workspace_untracked`` /
        ``workspace_drift``. It downloads the owner's current managed repo
        through the control plane and records the repo head as the deploy base
        marker. When a tracked workspace differs from the repo, the sync is
        rejected so validated local edits cannot be discarded after a build.
        ``force=True`` is destructive and is only allowed after the user
        explicitly chooses to replace those local changes.
        """
        try:
            prefix = _agent_prefix(name)
        except ValueError as exc:
            return json.dumps({"error": str(exc)})
        if not ctx.cp_jwt:
            return json.dumps(
                {
                    "error": "no CP JWT forwarded — agent declaration missing wants_cp_jwt=True",
                }
            )
        try:
            async with httpx.AsyncClient(timeout=60.0) as c:
                r = await c.get(
                    f"{settings.cp_url}/v1/agents/{name}/source",
                    headers={"authorization": f"bearer {ctx.cp_jwt}"},
                )
        except httpx.HTTPError as exc:
            return json.dumps({"error": f"cp unreachable: {exc}"})
        if r.status_code >= 400:
            return json.dumps({"error": f"cp {r.status_code}", "detail": r.text[:1000]})
        head_sha = r.headers.get("x-a2a-repo-head-sha")
        try:
            files = _files_from_tarball(r.content)
        except ValueError as exc:
            return json.dumps({"error": f"invalid source tarball: {exc}"})
        if not files:
            return json.dumps({"error": "managed repo source export was empty"})
        builder_state = _read_builder_state(store, name)
        current_bundle = _tarball_workspace_dir(store, prefix)
        current_files = _files_from_tarball(current_bundle) if current_bundle else {}
        tracked_head = builder_state.get("repo_head_sha")
        if current_files == files:
            _write_builder_state(
                store,
                name,
                {
                    "agent": name,
                    "repo_head_sha": head_sha,
                    "file_count": len(files),
                    "updated_at": int(time.time()),
                    "source": "repo-sync",
                },
            )
            return json.dumps(
                {
                    "ok": True,
                    "agent": name,
                    "repo_head_sha": head_sha,
                    "files": [],
                    "unchanged": True,
                }
            )
        if (
            not force
            and isinstance(tracked_head, str)
            and tracked_head
            and current_files
            and current_files != files
        ):
            return json.dumps(
                {
                    "ok": False,
                    "error": "workspace_has_local_changes",
                    "agent": name,
                    "message": (
                        "Refusing to replace tracked workspace changes with the "
                        "managed repo. Deploy the validated workspace, or use "
                        "force=True only after the user explicitly chooses to "
                        "discard those changes."
                    ),
                    "tracked_head_sha": tracked_head,
                    "current_head_sha": head_sha,
                }
            )
        written = _replace_workspace_files(store, prefix, files)
        _write_builder_state(
            store,
            name,
            {
                "agent": name,
                "repo_head_sha": head_sha,
                "file_count": len(written),
                "updated_at": int(time.time()),
                "source": "repo-sync",
            },
        )
        return json.dumps(
            {
                "ok": True,
                "agent": name,
                "repo_head_sha": head_sha,
                "files": written,
            }
        )

    @tool
    def list_a2a_pack(subdir: str = "") -> str:
        """List ``.py`` files under the installed ``a2a_pack`` package.

        Use this when you need to recall what the SDK actually exposes
        — what decorators exist, what ``RunContext`` methods are
        available, how ``WorkspaceClient`` is shaped, what the
        ``AgentRuntime`` fields are, etc. The agent code you scaffold
        runs on this exact package, so reading it is the authoritative
        reference (the system-prompt examples are intentionally
        abridged).

        ``subdir`` is an optional path segment relative to the package
        root (``""``, ``"serve"``, ``"mcp"`` …). Returns JSON:
        ``{root, files: [{path, size}]}``.
        """
        try:
            import a2a_pack
            import os as _os

            base = _os.path.dirname(_os.path.abspath(a2a_pack.__file__))
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"error": f"a2a_pack not importable: {exc}"})
        if subdir.startswith("/") or ".." in subdir.split("/"):
            return json.dumps({"error": "subdir must be relative; '..' disallowed"})
        root = _os.path.join(base, subdir) if subdir else base
        if not _os.path.commonpath([base, _os.path.abspath(root)]) == base:
            return json.dumps({"error": "subdir escapes package root"})
        if not _os.path.isdir(root):
            return json.dumps({"error": f"not a directory: {subdir}"})
        out: list[dict[str, Any]] = []
        for dirpath, _, filenames in _os.walk(root):
            for fname in filenames:
                if not fname.endswith(".py"):
                    continue
                full = _os.path.join(dirpath, fname)
                rel = _os.path.relpath(full, base)
                try:
                    size = _os.path.getsize(full)
                except OSError:
                    continue
                out.append({"path": rel, "size": size})
        out.sort(key=lambda r: r["path"])
        return json.dumps({"root": base, "files": out})

    @tool
    def read_a2a_pack(path: str) -> str:
        """Read a file from the installed ``a2a_pack`` package.

        Path is relative to the package root, e.g. ``agent.py``,
        ``context.py``, ``runtime.py``, ``workspace.py``, ``card.py``,
        ``mcp/server.py``. Returns JSON: ``{path, content}``. Capped
        at 64 KiB; reach for ``list_a2a_pack`` first if you need to
        chase a deeper file.
        """
        try:
            import a2a_pack
            import os as _os

            base = _os.path.dirname(_os.path.abspath(a2a_pack.__file__))
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"error": f"a2a_pack not importable: {exc}"})
        if path.startswith("/") or ".." in path.split("/"):
            return json.dumps({"error": "path must be relative; '..' disallowed"})
        full = _os.path.abspath(_os.path.join(base, path))
        if _os.path.commonpath([base, full]) != base:
            return json.dumps({"error": "path escapes package root"})
        if not _os.path.isfile(full):
            return json.dumps({"error": f"not a file: {path}"})
        try:
            with open(full, "r", encoding="utf-8") as fh:
                content = fh.read(64 * 1024 + 1)
        except OSError as exc:
            return json.dumps({"error": str(exc)})
        truncated = len(content) > 64 * 1024
        if truncated:
            content = content[: 64 * 1024]
        return json.dumps(
            {
                "path": path,
                "content": content,
                "truncated": truncated,
            }
        )

    return [
        init_agent_template,
        list_agent_files,
        write_agent_file,
        read_agent_file,
        write_agent_skill,
        test_agent_in_sandbox,
        cp_deploy_tarball,
        cp_deploy_source_repo,
        cp_compose_meta_agent,
        cp_refresh_agent,
        sync_agent_workspace_from_repo,
        list_a2a_pack,
        read_a2a_pack,
    ]


# --- helpers (not tools) ---


def _tarball_workspace_dir(
    store_or_s3: Any,
    bucket_or_prefix: str,
    prefix: str | None = None,
) -> bytes:
    """Pull every object under ``prefix`` and produce a relative tarball."""
    store, prefix = _store_from_args(store_or_s3, bucket_or_prefix, prefix)
    buf = io.BytesIO()
    added = False
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for key in store.iter_keys(prefix):
            rel = key[len(prefix) :]
            if not _should_include_agent_rel_path(rel):
                continue
            try:
                body = store.get(key)
            except FileNotFoundError:
                logger.warning(
                    "Skipping disappeared source file during tarball: %s", key
                )
                continue
            info = tarfile.TarInfo(name=rel)
            info.size = len(body)
            info.mode = 0o644
            tf.addfile(info, io.BytesIO(body))
            added = True
    return buf.getvalue() if added else b""


def _read_builder_state(store: _ObjectStore, name: str) -> dict[str, Any]:
    try:
        data = json.loads(store.get(_builder_state_key(name)).decode("utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def _write_builder_state(
    store: _ObjectStore,
    name: str,
    state: dict[str, Any],
) -> None:
    body = json.dumps(state, sort_keys=True, indent=2).encode("utf-8")
    store.put(_builder_state_key(name), body, "application/json")


def _source_bundle_hash(bundle: bytes) -> str:
    digest = hashlib.sha256()
    with tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz") as tf:
        members = sorted(
            (member for member in tf.getmembers() if member.isfile()),
            key=lambda member: member.name,
        )
        for member in members:
            extracted = tf.extractfile(member)
            body = extracted.read() if extracted is not None else b""
            digest.update(member.name.encode("utf-8"))
            digest.update(b"\0")
            digest.update(str(len(body)).encode("ascii"))
            digest.update(b"\0")
            digest.update(body)
            digest.update(b"\0")
    return digest.hexdigest()


def _record_sandbox_result(
    store: _ObjectStore,
    name: str,
    *,
    source_hash: str,
    result: dict[str, Any],
) -> None:
    state = _read_builder_state(store, name)
    state["last_sandbox"] = {
        "source_hash": source_hash,
        "exit_code": result.get("exit_code"),
        "updated_at": int(time.time()),
    }
    _write_builder_state(store, name, state)


def _sandbox_gate_error(
    builder_state: dict[str, Any],
    *,
    source_hash: str,
) -> dict[str, Any] | None:
    sandbox = builder_state.get("last_sandbox")
    if not isinstance(sandbox, dict):
        return {
            "ok": False,
            "error": "sandbox_not_passed",
            "message": "Run test_agent_in_sandbox for the current source before deploying.",
        }
    if sandbox.get("source_hash") != source_hash:
        return {
            "ok": False,
            "error": "sandbox_not_passed",
            "message": "Source changed after the last sandbox pass; rerun test_agent_in_sandbox.",
            "last_exit_code": sandbox.get("exit_code"),
        }
    if sandbox.get("exit_code") != 0:
        return {
            "ok": False,
            "error": "sandbox_not_passed",
            "message": "Last sandbox run failed; fix the source and rerun test_agent_in_sandbox.",
            "last_exit_code": sandbox.get("exit_code"),
        }
    return None


def _parse_manifest_json(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError) as exc:
        raise ValueError("manifest_json must be a JSON object") from exc
    if not isinstance(parsed, dict):
        raise ValueError("manifest_json must be a JSON object")
    composition = parsed.get("composition")
    if not isinstance(composition, (dict, list)):
        raise ValueError("manifest_json requires a composition object or list")
    return parsed


def _render_skill_md(skill_name: str, description: str, instructions: str) -> str:
    desc = " ".join(description.strip().split())
    body = instructions.strip()
    if not desc:
        raise ValueError("skill description is required")
    if len(desc) > 1024:
        raise ValueError("skill description must be 1024 characters or less")
    if not body:
        raise ValueError("skill instructions are required")
    if "\n---" in body or body.startswith("---"):
        raise ValueError("skill instructions must not contain frontmatter delimiters")
    return (
        "---\n"
        f"name: {skill_name}\n"
        "description: " + json.dumps(desc) + "\n"
        "---\n"
        f"# {skill_name}\n\n" + body + "\n"
    )


def _parse_supporting_skill_files(raw: str) -> dict[str, str]:
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError) as exc:
        raise ValueError("supporting_files_json must be a JSON object") from exc
    if not isinstance(parsed, dict):
        raise ValueError("supporting_files_json must be a JSON object")
    out: dict[str, str] = {}
    for path, content in parsed.items():
        if not isinstance(path, str) or not isinstance(content, str):
            raise ValueError("supporting skill file paths and contents must be strings")
        rel = _safe_supporting_skill_path(path)
        if rel == "SKILL.md":
            raise ValueError("supporting_files_json cannot override SKILL.md")
        out[rel] = content
    return out


def _safe_supporting_skill_path(path: str) -> str:
    rel = path.replace("\\", "/").lstrip("/")
    while rel.startswith("./"):
        rel = rel[2:]
    if not rel or ".." in rel.split("/") or rel.startswith("."):
        raise ValueError(f"unsafe supporting skill file path: {path!r}")
    if rel.endswith("/") or "//" in rel:
        raise ValueError(f"unsafe supporting skill file path: {path!r}")
    return rel


def _delete_workspace_objects(store: _ObjectStore, prefix: str) -> None:
    for key in store.iter_keys(prefix):
        store.delete(key)


def _files_from_tarball(bundle: bytes) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz") as tf:
            for member in tf.getmembers():
                if not member.isfile():
                    continue
                rel = member.name
                while rel.startswith("./"):
                    rel = rel[2:]
                if not rel or rel.startswith("/") or ".." in rel.split("/"):
                    raise ValueError(f"unsafe member path: {member.name}")
                if not _should_include_agent_rel_path(rel):
                    continue
                extracted = tf.extractfile(member)
                if extracted is None:
                    continue
                out[rel] = extracted.read()
    except tarfile.TarError as exc:
        raise ValueError(str(exc)) from exc
    return out


def _compile_agent_dsl_json(bundle: bytes) -> str:
    import sys
    import yaml
    from a2a_pack import apply_project_manifest, compile_agent_to_dsl
    from a2a_pack.cli.loader import load_agent_class

    files = _files_from_tarball(bundle)
    with tempfile.TemporaryDirectory(prefix="agent-builder-dsl-") as tmp:
        root = Path(tmp)
        for rel, body in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        cfg = yaml.safe_load((root / "a2a.yaml").read_text(encoding="utf-8")) or {}
        if not isinstance(cfg, dict):
            raise ValueError("a2a.yaml must be a mapping")
        language = str(cfg.get("language") or "python").strip().lower()
        if language != "python":
            raise ValueError(
                f"agent-builder deploy only supports python Agent DSL, got {language!r}"
            )
        entrypoint = str(cfg.get("entrypoint") or "").strip()
        if not entrypoint:
            raise ValueError("a2a.yaml entrypoint is required")
        entrypoint_module = entrypoint.split(":", 1)[0].strip()
        old_entrypoint_module = sys.modules.get(entrypoint_module)
        old_dont_write_bytecode = os.environ.get("PYTHONDONTWRITEBYTECODE")
        old_sys_dont_write_bytecode = sys.dont_write_bytecode
        os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
        sys.dont_write_bytecode = True
        try:
            cls = load_agent_class(entrypoint, project_dir=root)
            apply_project_manifest(cls, cfg)
            dsl = compile_agent_to_dsl(
                cls,
                language="python",
                entrypoint=entrypoint,
                metadata={
                    "source": "agent-builder",
                    "project_manifest": {
                        key: value
                        for key, value in cfg.items()
                        if key in {"name", "version", "entrypoint", "frontend"}
                    },
                },
            )
        finally:
            if old_dont_write_bytecode is None:
                os.environ.pop("PYTHONDONTWRITEBYTECODE", None)
            else:
                os.environ["PYTHONDONTWRITEBYTECODE"] = old_dont_write_bytecode
            sys.dont_write_bytecode = old_sys_dont_write_bytecode
            if old_entrypoint_module is None:
                sys.modules.pop(entrypoint_module, None)
            else:
                sys.modules[entrypoint_module] = old_entrypoint_module
        return dsl.model_dump_json()


def _replace_workspace_files(
    store_or_s3: Any,
    bucket_or_prefix: str,
    prefix_or_files: str | dict[str, bytes],
    files: dict[str, bytes] | None = None,
) -> list[dict[str, Any]]:
    if files is None:
        if not isinstance(prefix_or_files, dict):
            raise TypeError("files are required")
        store, prefix = _store_from_args(store_or_s3, bucket_or_prefix, None)
        files = prefix_or_files
    else:
        if not isinstance(prefix_or_files, str):
            raise TypeError("prefix must be a string")
        store, prefix = _store_from_args(store_or_s3, bucket_or_prefix, prefix_or_files)
    written: list[dict[str, Any]] = []
    _delete_workspace_objects(store, prefix)
    for rel, body in sorted(files.items()):
        key = prefix + rel
        store.put(key, body, "application/octet-stream")
        written.append({"path": rel, "size": len(body)})
    return written


async def _latest_cp_deployment(
    cp_url: str,
    cp_jwt: str,
    name: str,
) -> dict[str, Any] | None:
    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.get(
                f"{cp_url}/v1/agents/{name}/deployments",
                headers={"authorization": f"bearer {cp_jwt}"},
            )
    except httpx.HTTPError as exc:
        raise RuntimeError(f"cp unreachable: {exc}") from exc
    if r.status_code == 404:
        return None
    if r.status_code >= 400:
        raise RuntimeError(f"cp {r.status_code}: {r.text[:1000]}")
    body = r.json() or []
    if not isinstance(body, list) or not body:
        return None
    first = body[0]
    return first if isinstance(first, dict) else None


def _deployment_drift_error(
    name: str,
    latest_deployment: dict[str, Any] | None,
    builder_state: dict[str, Any],
    *,
    force: bool = False,
) -> dict[str, Any] | None:
    if force or latest_deployment is None:
        return None
    latest_head = latest_deployment.get("head_sha")
    base_head = builder_state.get("repo_head_sha")
    if not isinstance(base_head, str) or not base_head:
        return {
            "ok": False,
            "error": "workspace_untracked",
            "agent": name,
            "message": (
                "This agent already has managed source, but the MinIO "
                "workspace has no deploy base marker. Refresh it before "
                "deploying, or use force=True only if the user wants to "
                "replace the repo."
            ),
            "current_head_sha": latest_head,
            "current_deployment_id": latest_deployment.get("deploy_id"),
        }
    return None


def _read_entrypoint(bundle: bytes) -> str | None:
    try:
        with tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz") as tf:
            for member in tf.getmembers():
                if member.name.endswith("a2a.yaml") and tf.extractfile(member):
                    text = tf.extractfile(member).read().decode("utf-8", "replace")  # type: ignore[union-attr]
                    for line in text.splitlines():
                        if line.lstrip().startswith("entrypoint:"):
                            return line.split(":", 1)[1].strip().strip("\"'")
    except Exception:  # noqa: BLE001
        pass
    return None


def _read_manifest_version(bundle: bytes) -> str | None:
    try:
        manifest = _read_bundle_text(bundle, "a2a.yaml")
        if not manifest:
            return None
        for line in manifest.splitlines():
            if line.lstrip().startswith("version:"):
                return line.split(":", 1)[1].strip().strip("\"'") or None
    except Exception:  # noqa: BLE001
        pass
    return None


def _read_bundle_text(bundle: bytes, path: str) -> str | None:
    try:
        with tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz") as tf:
            for member in tf.getmembers():
                if member.name == path and member.isfile():
                    extracted = tf.extractfile(member)
                    if extracted is None:
                        return None
                    return extracted.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return None
    return None


def _class_name(slug: str) -> str:
    return "".join(p.capitalize() for p in re.split(r"[-_]+", slug) if p) or "MyAgent"


_FRONTEND_TEMPLATE_FILES: dict[str, dict[str, str]] = {
    "static": {
        "frontend/dist/index.html": "frontend/static-index.html.tmpl",
    },
    "react": {
        "frontend/package.json": "frontend/react-package.json.tmpl",
        "frontend/index.html": "frontend/react-index.html.tmpl",
        "frontend/vite.config.js": "frontend/react-vite.config.js.tmpl",
        "frontend/src/main.jsx": "frontend/react-main.jsx.tmpl",
        "frontend/src/App.jsx": "frontend/react-app.jsx.tmpl",
        "frontend/src/a2a.js": "frontend/react-a2a.js.tmpl",
        "frontend/src/style.css": "frontend/react-style.css.tmpl",
    },
}


def _normalize_frontend_kind(frontend: str | None) -> str:
    kind = (frontend or "none").strip().lower()
    if kind in {"", "no", "off", "false"}:
        kind = "none"
    if kind not in {"none", "static", "react"}:
        raise ValueError("frontend must be one of: none, static, react")
    return kind


def _normalize_profile(profile: str | None) -> str:
    kind = (profile or "standard").strip().lower().replace("-", "_")
    if kind in {"", "default", "basic"}:
        kind = "standard"
    if kind not in {"standard", "full_stack"}:
        raise ValueError("profile must be one of: standard, full_stack")
    return kind


def _frontend_block(frontend: str) -> str:
    kind = _normalize_frontend_kind(frontend)
    if kind == "none":
        return ""
    build = "\n  build: npm run build" if kind == "react" else ""
    return (
        "frontend:\n"
        "  path: frontend"
        f"{build}\n"
        "  dist: dist\n"
        "  mount: /app\n"
        "  auth: inherit"
    )


def _render_frontend_files(frontend: str, *, name: str) -> dict[str, str]:
    kind = _normalize_frontend_kind(frontend)
    if kind == "none":
        return {}
    return {
        path: _render_sdk_template(template, name=name)
        for path, template in _FRONTEND_TEMPLATE_FILES[kind].items()
    }


def _render_a2a_init_template(
    name: str,
    *,
    description: str = "A new A2A agent",
    frontend: str = "none",
    profile: str = "standard",
) -> dict[str, str]:
    _validate_name(name)
    profile_kind = _normalize_profile(profile)
    if profile_kind == "full_stack":
        frontend = "react"
    frontend_kind = _normalize_frontend_kind(frontend)
    class_name = _class_name(name)
    files = {
        "agent.py": _render_sdk_template(
            "agent.py.tmpl",
            name=name,
            class_name=class_name,
            description=description,
        ),
        "a2a.yaml": _render_sdk_template(
            "a2a.yaml.tmpl",
            name=name,
            class_name=class_name,
            frontend_block=_frontend_block(frontend_kind),
        ),
        "requirements.txt": _render_sdk_template("requirements.txt.tmpl"),
    }
    files.update(_render_frontend_files(frontend_kind, name=name))
    if profile_kind == "full_stack":
        files["a2a.yaml"] = (
            files["a2a.yaml"].rstrip() + "\n" + _full_stack_database_block(name) + "\n"
        )
        files["requirements.txt"] = (
            files["requirements.txt"].rstrip() + "\npsycopg[binary]>=3.2\n"
        )
        files["db/migrations/001_app_records.sql"] = _full_stack_migration()
        files["tests/test_full_stack_contract.py"] = _full_stack_contract_test()
    return files


def _full_stack_database_block(name: str) -> str:
    database_name = (name[:48].rstrip("-") or "app") + "-data"
    return (
        "resources:\n"
        "  databases:\n"
        f"    - name: {database_name}\n"
        "      provider: neon\n"
        "      engine: postgres\n"
        "      scope: user\n"
        "      branch: main\n"
        "      access_mode: read_write\n"
        "      env:\n"
        "        url: DATABASE_URL\n"
        "      migrations:\n"
        "        path: db/migrations\n"
        "      scale_to_zero: true"
    )


def _full_stack_migration() -> str:
    return """\
CREATE TABLE IF NOT EXISTS app_records (
    id BIGSERIAL PRIMARY KEY,
    tenant_key TEXT NOT NULL,
    record_kind TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS app_records_tenant_kind_idx
    ON app_records (tenant_key, record_kind, created_at DESC);
"""


def _full_stack_contract_test() -> str:
    return """\
from pathlib import Path

from a2a_pack.cli.local import load_local_project


ROOT = Path(__file__).resolve().parents[1]


def test_full_stack_product_contract():
    manifest = (ROOT / "a2a.yaml").read_text(encoding="utf-8")
    source = (ROOT / "agent.py").read_text(encoding="utf-8")
    frontend = (ROOT / "frontend" / "src" / "App.jsx").read_text(encoding="utf-8")
    frontend_client = (ROOT / "frontend" / "src" / "a2a.js").read_text(encoding="utf-8")

    assert "frontend:" in manifest and "mount: /app" in manifest
    assert "resources:" in manifest and "databases:" in manifest
    assert "migrations:" in manifest and "db/migrations" in manifest
    assert "PlatformUserAuth" in source
    assert "AgentPlatformResources" in source and "AgentDatabase(" in source
    assert "Skill runner" not in frontend, "replace the generic scaffold with the product workflow"
    assert "unwrapInvokeResponse" in frontend_client, "unwrap the /invoke result envelope before rendering"

    migrations = list((ROOT / "db" / "migrations").glob("*.sql"))
    assert migrations and all(path.read_text(encoding="utf-8").strip() for path in migrations)

    card = load_local_project(ROOT).agent_cls().card()
    databases = card.runtime.platform_resources.databases
    assert databases, "live Agent Card must declare its managed database"
    assert databases[0].migrations is not None
    assert databases[0].migrations.path == "db/migrations"
"""


def _render_sdk_template(template: str, /, **vars: str) -> str:
    parts = template.split("/")
    local_templates = next(
        (
            candidate
            for parent in Path(__file__).resolve().parents
            if (
                candidate := parent
                / "sdk"
                / "a2a-pack"
                / "a2a_pack"
                / "cli"
                / "templates"
            ).exists()
        ),
        None,
    )
    if local_templates is not None:
        text = local_templates.joinpath(*parts).read_text(encoding="utf-8")
    else:
        resource = resources.files("a2a_pack.cli.templates")
        for part in parts:
            resource = resource.joinpath(part)
        text = resource.read_text(encoding="utf-8")
    for key, value in vars.items():
        text = text.replace("{{ " + key + " }}", value)
    return text
