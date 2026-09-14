"""Consume published agents as typed CLI commands.

``a2a use <agent>`` fetches the agent's card — skills with real JSON Schema
inputs plus its consumer-setup manifest — checks/prompts for the setup values,
and caches a stub under ``~/.a2a/agents/<name>.json``. Every cached stub is
mounted as a typer sub-app at CLI start, so each skill becomes a *typed*
subcommand:

    a2a use blog-openapi-agent
    a2a blog-openapi-agent create-blog-post --title "Hi" --content @post.md

``a2a call <agent> <skill>`` is the schema-free escape hatch — it works with or
without a cached stub.

Values support ``@path`` (read file contents) and ``@-`` (read stdin) for any
string field; object/array fields take inline JSON or ``@file.json``.

Invocation rides the agent's MCP endpoint (``<url>/mcp``, streamable HTTP) with
the caller's platform bearer token, the same channel IDE clients use.

Governed calls answer with receipt headers; every invocation path here prints a
one-line pointer at that receipt on **stderr** (so ``a2a call ... | jq`` keeps
working) and caches it for ``a2a receipt show/verify <id>``.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Optional

import typer
from rich.console import Console

from . import credentials
from .api_client import ApiError, ControlPlaneClient
from .oauth_login import refresh_credentials_if_needed
from .receipts_cli import (
    format_receipt_line,
    receipt_from_evidence,
    receipt_headers,
    remember_receipt,
)

console = Console()
# Receipt lines are notes about the call, not its output: stdout stays clean
# for pipelines (`a2a call ... | jq`).
err_console = Console(stderr=True, soft_wrap=True)

AGENTS_DIR = Path(
    os.environ.get("A2A_AGENTS_DIR", str(Path.home() / ".a2a" / "agents"))
).expanduser()
STUB_SCHEMA_VERSION = 1
_ENVELOPE_KEYS = {"parameters", "body"}
# Skill ids the OpenAPI auto-agent adds that aren't useful as typed commands.
_SKIP_SKILLS = {"auto"}


def _fail(msg: str, code: int = 1) -> None:
    console.print(f"[red]error:[/] {msg}")
    raise typer.Exit(code)


# ---------------------------------------------------------------------------
# card fetch + stub cache
# ---------------------------------------------------------------------------


def _bearer_token() -> str | None:
    creds = credentials.load()
    if creds is None:
        return None
    try:
        creds = refresh_credentials_if_needed(creds)
    except Exception:  # noqa: BLE001 — stale token still better than none
        pass
    return creds.token if creds else None


def _call_meta() -> dict[str, str]:
    """Per-call platform context, matching the node MCP gateway (a2amcp):
    `params._meta = {cp_jwt, cp_url}` on every tools/call so the agent can
    resolve the caller's consumer setup / LLM creds platform-side."""
    creds = credentials.load()
    if creds is None:
        return {}
    try:
        creds = refresh_credentials_if_needed(creds) or creds
    except Exception:  # noqa: BLE001 — stale token still better than none
        pass
    meta: dict[str, str] = {}
    if creds.token:
        meta["cp_jwt"] = creds.token
    if creds.api_url:
        meta["cp_url"] = creds.api_url
    return meta


def _resolve_agent_url(name: str, api: str | None) -> str:
    creds = credentials.load()
    if creds is None:
        _fail("not logged in (run `a2a login`) — the registry lookup needs your account")
    client = ControlPlaneClient(api or creds.api_url, creds.token)
    try:
        rows = client.list_agents()
    except ApiError as exc:
        _fail(f"could not list agents: {exc}")
    row = next((r for r in rows if r.get("name") == name), None)
    if row is None:
        _fail(f"agent {name!r} not found in the registry (see `a2a agents`)")
    url = (row.get("url") or "").rstrip("/")
    if not url:
        # The registry only records a URL once a deploy reaches "live", so an
        # empty URL is normally a build that has not finished (or failed).
        _fail(
            f"agent {name!r} has no URL recorded yet — the registry fills it in "
            f"when a deploy goes live. Check it with `a2a logs {name} --follow`"
        )
    return url


def _fetch_card(url: str) -> dict[str, Any]:
    import httpx

    for path in ("/.well-known/agent-card", "/.well-known/agent-card.json"):
        try:
            resp = httpx.get(f"{url}{path}", timeout=30, follow_redirects=True)
            if resp.status_code == 200:
                return resp.json()
        except httpx.HTTPError:
            continue
    _fail(f"could not fetch the agent card from {url}")
    raise AssertionError("unreachable")


def _stub_path(agent: str) -> Path:
    return AGENTS_DIR / f"{agent}.json"


def _load_stub(agent: str) -> dict[str, Any] | None:
    path = _stub_path(agent)
    if not path.exists():
        return None
    try:
        stub = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return stub if stub.get("schema_version") == STUB_SCHEMA_VERSION else None


def _write_stub(agent: str, url: str, card: dict[str, Any]) -> dict[str, Any]:
    skills = []
    for sk in card.get("skills") or []:
        sid = sk.get("id") or sk.get("name")
        if not sid or sid in _SKIP_SKILLS:
            continue
        skills.append(
            {
                "id": sid,
                "description": sk.get("description") or "",
                "input_schema": sk.get("input_schema") or {},
            }
        )
    stub = {
        "schema_version": STUB_SCHEMA_VERSION,
        "agent": agent,
        "url": url,
        "description": card.get("description") or "",
        "version": card.get("version") or "",
        "fetched_at": int(time.time()),
        "skills": skills,
        "consumer_setup": (card.get("consumer_setup") or {}).get("fields") or [],
    }
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    _stub_path(agent).write_text(json.dumps(stub, indent=2))
    return stub


# ---------------------------------------------------------------------------
# consumer-setup handshake
# ---------------------------------------------------------------------------


def _app_url() -> str:
    """The web app origin, derived from the API origin (api.x -> app.x)."""
    creds = credentials.load()
    api = (creds.api_url if creds else "https://api.a2acloud.io").rstrip("/")
    return api.replace("://api.", "://app.", 1)


def _agent_settings_url(agent: str) -> str:
    return f"{_app_url()}/a/{agent}"


def _setup_handshake(agent: str, client: ControlPlaneClient) -> None:
    """Report the PLATFORM-side consumer-setup status for AGENT. Read-only by
    design: setup values are secrets managed in the app UI and resolved by the
    control plane at invocation time — the CLI never collects, stores, or
    transmits them. If something required is missing, print the settings link
    and get out of the way."""
    try:
        status = client.get_consumer_setup(agent)
    except ApiError as exc:
        console.print(f"[dim]setup status unavailable ({exc}); calls will report what's missing[/]")
        return
    declaration = status.get("declaration") or {}
    fields = [f for group in declaration.values() for f in group]
    if not fields:
        return
    missing = set(status.get("missing_required") or [])
    stored = {v.get("name") for v in status.get("values") or []}
    for field in fields:
        fname = field.get("name") or ""
        label = field.get("label") or fname
        if fname in missing:
            console.print(f"  [yellow]![/] {label} [dim]({fname})[/] — required, not configured")
        elif fname in stored or field.get("required"):
            console.print(f"  [green]✓[/] {label} [dim]({fname})[/]")
        else:
            console.print(f"  [dim]○ {label} ({fname}) — optional[/]")
    if missing:
        console.print(
            f"\n  configure once in the app: [bold]{_agent_settings_url(agent)}[/]\n"
            "  [dim]values are stored platform-side and injected at call time — nothing to paste here[/]"
        )
    elif status.get("complete"):
        console.print("  [dim]setup complete (platform-side)[/]")


# ---------------------------------------------------------------------------
# MCP invocation
# ---------------------------------------------------------------------------


def _collect_body_evidence(payload: dict[str, Any], receipt: dict[str, str] | None) -> None:
    """Merge the evidence the gateway inlined into a JSON-RPC response.

    Buffered responses carry it at ``result._meta.a2aCloudEvidence`` (MCP) or
    top-level ``a2a_evidence``. This is where the *signed token* lives — the
    headers of a streamed response can only carry the reservation."""
    if receipt is None or not isinstance(payload, dict):
        return
    result = payload.get("result")
    meta = result.get("_meta") if isinstance(result, dict) else None
    for evidence in (
        meta.get("a2aCloudEvidence") if isinstance(meta, dict) else None,
        payload.get("a2a_evidence"),
    ):
        receipt.update(receipt_from_evidence(evidence))


def _parse_mcp_response(resp: Any, *, receipt: dict[str, str] | None = None) -> dict[str, Any]:
    """Return the JSON-RPC response (the event carrying result/error).

    Streaming agents interleave notification events (progress, logs) on the
    SSE stream before the response; those are surfaced as dim status lines,
    never mistaken for the payload.

    A governed stream ends with an ``a2a.evidence`` frame holding the sealed
    receipt; when ``receipt`` is passed, that pointer (id, url and the signed
    token) is merged into it — the receipt can only be sealed once the stream
    is over, so it cannot have been in the response headers."""
    ctype = resp.headers.get("content-type", "")
    if "text/event-stream" in ctype:
        final: dict[str, Any] = {}
        for line in resp.text.splitlines():
            if not line.startswith("data:"):
                continue
            try:
                event = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "a2a.evidence":
                if receipt is not None:
                    receipt.update(receipt_from_evidence(event.get("evidence")))
            elif "result" in event or "error" in event:
                final = event
            elif event.get("method") == "notifications/progress":
                message = (event.get("params") or {}).get("message")
                if message:
                    console.print(f"[dim]· {message}[/]", highlight=False)
        _collect_body_evidence(final, receipt)
        return final
    try:
        payload = resp.json()
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    _collect_body_evidence(payload, receipt)
    return payload


def invoke_skill(
    url: str,
    skill_id: str,
    arguments: dict[str, Any],
    *,
    receipt: dict[str, str] | None = None,
) -> Any:
    """Call one skill over the agent's MCP endpoint and return its result
    payload. Raises RuntimeError with a readable message on failure.

    Pass ``receipt`` to collect the governed-call receipt pointer: the observed
    ``X-A2A-Receipt-ID`` / ``-URL`` / ``-Token`` headers are written into that
    dict (an out-parameter, so the return value stays the result payload and
    existing callers are unaffected), then the evidence the gateway put in the
    response body — the ``a2a.evidence`` SSE frame or ``result._meta``. The
    body wins: a streamed call reserves its receipt id in the headers but can
    only seal (and sign) it once the stream is done. It is filled in even when
    the call fails, because the failure has a receipt too. Ungoverned agents
    leave it empty."""
    import httpx

    mcp = f"{url.rstrip('/')}/mcp"
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    token = _bearer_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    # Generous ceiling: a scale-to-zero agent pays a cold start before it can
    # even accept the call, and long-running skills stream slowly after that.
    with httpx.Client(timeout=180, follow_redirects=True) as client:
        init = client.post(
            mcp,
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "a2a-cli", "version": "1"},
                },
            },
        )
        if init.status_code >= 400:
            raise RuntimeError(f"agent MCP endpoint refused initialize ({init.status_code})")
        session = init.headers.get("mcp-session-id")
        if session:
            headers["mcp-session-id"] = session
        client.post(
            mcp, headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
        try:
            resp = client.post(
                mcp,
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": skill_id,
                        "arguments": arguments,
                        **({"_meta": meta} if (meta := _call_meta()) else {}),
                    },
                },
            )
            if receipt is not None:
                receipt.update(receipt_headers(resp.headers))
            payload = _parse_mcp_response(resp, receipt=receipt)
        except httpx.HTTPError as exc:
            # Scale-to-zero agents can drop a streamed response mid-body while
            # waking or recycling — surface it as a retryable error, not a trace.
            raise RuntimeError(
                f"transport error talking to {skill_id!r} ({exc.__class__.__name__}: {exc}); "
                "the agent may be cold-starting — retry in a few seconds"
            ) from exc

    if not payload:
        raise RuntimeError(
            f"{skill_id!r} stream ended without a result — the agent may have "
            "died mid-call or recycled while waking; retry in a few seconds"
        )
    if "error" in payload:
        message = str(payload["error"].get("message") or payload["error"])
        if "LLM key required" in message or "LLM credential" in message:
            message += (
                "\nhint: this agent runs on your LLM credential — add one in "
                "Settings > LLM credentials (app.a2acloud.io), then retry."
            )
        if "ConsumerSetupLookupError" in message or "consumer setup required" in message:
            agent_name = url.split("://", 1)[-1].split(".", 1)[0]
            message += (
                f"\nhint: configure this agent once at {_agent_settings_url(agent_name)} — "
                "values are stored platform-side and injected at call time."
            )
        raise RuntimeError(message)
    result = payload.get("result", payload)
    # MCP tool results wrap content blocks; unwrap a single text block to JSON
    # when possible so shell pipelines get clean data.
    content = result.get("content") if isinstance(result, dict) else None
    if isinstance(content, list) and len(content) == 1 and content[0].get("type") == "text":
        text = content[0].get("text", "")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    return result


def _report_receipt(observed: dict[str, str], *, agent: str = "") -> None:
    """Cache the call's receipt and point at it in one quiet stderr line.

    Agents that return no receipt headers (local/dev runs, ungoverned imports)
    print nothing — a missing receipt is not an error worth shouting about."""
    if not observed:
        return
    remember_receipt(observed, agent=agent)
    line = format_receipt_line(observed)
    if line:
        err_console.print(line, style="dim", highlight=False)


# ---------------------------------------------------------------------------
# schema -> typed CLI fields
# ---------------------------------------------------------------------------


_PY_TYPES = {"string": "str", "integer": "int", "number": "float", "boolean": "bool"}


def _schema_type(schema: dict[str, Any]) -> str:
    t = schema.get("type")
    if isinstance(t, str):
        return t
    for alt in schema.get("anyOf") or schema.get("oneOf") or []:
        at = alt.get("type")
        if isinstance(at, str) and at != "null":
            return at
    return "object" if "properties" in schema else "any"


def cli_fields(input_schema: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten a skill's input schema into CLI field specs.

    OpenAPI auto-agent skills use a {parameters, body} envelope; we lift the
    body's properties and the operation parameters to top-level flags (body
    wins name collisions; colliding parameters get a `param-` prefix) and
    remember each field's route so invocation reassembles the envelope.
    Non-envelope schemas map properties straight to flags.
    """
    props = input_schema.get("properties") or {}
    required = set(input_schema.get("required") or [])
    fields: list[dict[str, Any]] = []

    def add(name: str, schema: dict[str, Any], *, req: bool, route: str, flag: str | None = None) -> None:
        jtype = _schema_type(schema)
        fields.append(
            {
                "name": name,
                "flag": (flag or name).replace("_", "-"),
                "py": re.sub(r"\W", "_", (flag or name)),
                "jtype": jtype,
                "pytype": _PY_TYPES.get(jtype, "str"),
                "required": req,
                "help": (schema.get("description") or "").split("\n")[0][:120],
                "route": route,
            }
        )

    if set(props) <= _ENVELOPE_KEYS and props:
        body = props.get("body") or {}
        body_props = body.get("properties") or {}
        body_required = set(body.get("required") or [])
        params = (props.get("parameters") or {}).get("properties") or {}
        params_required = set((props.get("parameters") or {}).get("required") or [])
        if body_props:
            for name, schema in body_props.items():
                add(name, schema, req=name in body_required and "body" in required, route="body")
        elif "body" in props:
            add("body", body, req="body" in required, route="top")
        if params:
            for name, schema in params.items():
                flag = f"param-{name}" if any(f["name"] == name for f in fields) else name
                add(name, schema, req=name in params_required, route="parameters", flag=flag)
        elif "parameters" in props:
            # Untyped `parameters` (a pre-typed-schema card): still expose it as
            # a generic JSON flag so path/query params stay reachable.
            add("parameters", props["parameters"], req="parameters" in required, route="top")
        return fields

    for name, schema in props.items():
        add(name, schema, req=name in required, route="top")
    return fields


def _coerce_value(raw: Any, jtype: str) -> Any:
    """Apply @file / @- indirection and JSON parsing for structured types."""
    if not isinstance(raw, str):
        return raw
    if raw.startswith("@"):
        source = raw[1:]
        raw = sys.stdin.read() if source == "-" else Path(source).expanduser().read_text()
        raw = raw.rstrip("\n")
    if jtype in ("object", "array", "any"):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    return raw


def assemble_arguments(fields: list[dict[str, Any]], values: dict[str, Any]) -> dict[str, Any]:
    """Route coerced CLI values back into the skill's argument shape."""
    top: dict[str, Any] = {}
    body: dict[str, Any] = {}
    params: dict[str, Any] = {}
    for field in fields:
        val = values.get(field["py"])
        if val is None:
            continue
        val = _coerce_value(val, field["jtype"])
        {"top": top, "body": body, "parameters": params}[field["route"]][field["name"]] = val
    out = dict(top)
    if body:
        out["body"] = body
    if params:
        out["parameters"] = params
    return out


# ---------------------------------------------------------------------------
# dynamic command synthesis
# ---------------------------------------------------------------------------


def _make_skill_command(stub: dict[str, Any], skill: dict[str, Any]) -> Any:
    """Synthesize a typed function for one skill so typer renders real flags,
    types, and required-ness in --help. exec is contained: the source is built
    only from sanitized identifiers; user data never reaches it."""
    fields = cli_fields(skill.get("input_schema") or {})
    lines = ["def _cmd("]
    for f in fields:
        opt = f"typer.Option(..., '--{f['flag']}', help={f['help']!r})" if f["required"] else (
            f"typer.Option(None, '--{f['flag']}', help={f['help']!r})"
        )
        anno = f["pytype"] if f["required"] else f"Optional[{f['pytype']}]"
        lines.append(f"    {f['py']}: {anno} = {opt},")
    lines.append("):")
    lines.append("    _values = dict(locals())")
    lines.append("    _run(_values)")
    src = "\n".join(lines)

    def _run(values: dict[str, Any]) -> None:
        args = assemble_arguments(fields, values)
        observed: dict[str, str] = {}
        try:
            result = invoke_skill(stub["url"], skill["id"], args, receipt=observed)
        except (RuntimeError, OSError) as exc:
            _report_receipt(observed, agent=str(stub.get("agent") or ""))
            _fail(str(exc))
        if isinstance(result, (dict, list)):
            console.print_json(json.dumps(result))
        else:
            console.print(result)
        _report_receipt(observed, agent=str(stub.get("agent") or ""))

    ns: dict[str, Any] = {"typer": typer, "Optional": Optional, "_run": _run}
    exec(src, ns)  # noqa: S102 — source assembled from sanitized identifiers only
    cmd = ns["_cmd"]
    cmd.__doc__ = skill.get("description") or skill["id"]
    return cmd


def register_agent_stubs(app: typer.Typer) -> None:
    """Mount every cached agent stub as `a2a <agent> <skill> --flags`. Never
    let a corrupt stub break the CLI."""
    try:
        paths = sorted(AGENTS_DIR.glob("*.json"))
    except OSError:
        return
    for path in paths:
        try:
            stub = json.loads(path.read_text())
            if stub.get("schema_version") != STUB_SCHEMA_VERSION:
                continue
            sub = typer.Typer(
                no_args_is_help=True,
                help=stub.get("description") or f"skills of {stub['agent']} (via `a2a use`)",
            )
            for skill in stub.get("skills") or []:
                sub.command(name=skill["id"].replace("_", "-"))(
                    _make_skill_command(stub, skill)
                )
            app.add_typer(sub, name=stub["agent"])
        except Exception:  # noqa: BLE001 — a bad stub must not kill the CLI
            continue


# ---------------------------------------------------------------------------
# `a2a use` + `a2a call`
# ---------------------------------------------------------------------------


def register_commands(app: typer.Typer) -> None:
    @app.command(name="use")
    def use(
        agent: str = typer.Argument(help="Agent name from the registry (`a2a agents`)"),
        api: Optional[str] = typer.Option(None, "--api", hidden=True),
        no_prompt: bool = typer.Option(
            False, "--no-prompt", hidden=True, help="Deprecated: the CLI never prompts for setup."
        ),
    ) -> None:
        """Make AGENT's tools available as typed `a2a <agent> <tool>` commands.

        Fetches the agent card (tools + input schemas + consumer setup), checks
        the platform-side setup status, then caches the typed stub. Re-run any
        time to refresh."""
        url = _resolve_agent_url(agent, api)
        card = _fetch_card(url)
        stub = _write_stub(agent, url, card)
        console.print(f"[bold green]{agent}[/] [dim]{url}[/]")
        creds = credentials.load()
        _setup_handshake(agent, ControlPlaneClient(api or creds.api_url, creds.token))
        if not stub["skills"]:
            console.print("[yellow]card exposes no typed tools[/]")
            return
        console.print("\ncommands:")
        for skill in stub["skills"]:
            flags = " ".join(
                f"--{f['flag']}" for f in cli_fields(skill["input_schema"]) if f["required"]
            )
            console.print(f"  a2a {agent} {skill['id'].replace('_', '-')} {flags}".rstrip())
        console.print(
            "\n[dim]string flags accept @file / @- for stdin; object flags take JSON or @file.json[/]"
        )

    @app.command(name="unuse")
    def unuse(agent: str = typer.Argument(help="Agent stub to remove")) -> None:
        """Remove AGENT's cached CLI stub (stored setup values are kept)."""
        path = _stub_path(agent)
        if not path.exists():
            _fail(f"no cached stub for {agent!r}")
        path.unlink()
        console.print(f"removed {agent} commands")

    @app.command(name="call")
    def call(
        agent: str = typer.Argument(help="Agent name"),
        tool: str = typer.Argument(help="Tool id (see the agent card or `a2a use`)"),
        args: list[str] = typer.Argument(
            None, help="key=value pairs; values support @file and @- for stdin"
        ),
        json_body: Optional[str] = typer.Option(
            None, "--json", help="Full arguments as JSON (inline or @file.json)"
        ),
        api: Optional[str] = typer.Option(None, "--api", hidden=True),
    ) -> None:
        """Invoke one tool without a typed stub — the schema-free escape hatch."""
        stub = _load_stub(agent)
        url = stub["url"] if stub else _resolve_agent_url(agent, api)
        arguments: dict[str, Any] = {}
        if json_body:
            parsed = _coerce_value(json_body, "object")
            if not isinstance(parsed, dict):
                _fail("--json must be a JSON object")
            arguments.update(parsed)
        for pair in args or []:
            if "=" not in pair:
                _fail(f"expected key=value, got {pair!r}")
            key, _, raw = pair.partition("=")
            arguments[key] = _coerce_value(raw, "any")
        observed: dict[str, str] = {}
        try:
            result = invoke_skill(url, tool.replace("-", "_"), arguments, receipt=observed)
        except (RuntimeError, OSError) as exc:
            _report_receipt(observed, agent=agent)
            _fail(str(exc))
        if isinstance(result, (dict, list)):
            console.print_json(json.dumps(result))
        else:
            console.print(result)
        _report_receipt(observed, agent=agent)
