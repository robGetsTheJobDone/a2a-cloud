"""``a2a receipt`` — read and verify the signed receipt behind a call.

Every governed call answers with ``X-A2A-Receipt-ID`` / ``X-A2A-Receipt-URL``,
and hands back the *signed token* in the response body: the terminal
``a2a.evidence`` SSE frame, or ``result._meta.a2aCloudEvidence`` on buffered
JSON. ``a2a call`` (and the typed ``a2a <agent> <tool>`` commands) remember
both under ``~/.a2a/receipts/<id>.json`` — override with ``A2A_RECEIPTS_DIR``
— so the receipt for the call you just made is inspectable *by id*, offline.

Verification is Ed25519 over ``<b64url(payload)>.<b64url(signature)>`` and is
performed by :func:`a2a_pack.receipts.verify_receipt`; this module never
re-implements the crypto. It only sources the public key, in order:

1. ``--key <base64>`` — an explicit flag always wins
2. ``A2A_RECEIPT_VERIFYING_KEY`` (or ``A2A_RECEIPT_SIGNING_KEY``) in the env
3. ``GET {api}/v1/public/receipt-keys`` — *every* published key is tried, so a
   receipt signed before a key rotation still verifies against the old key
   while the platform still publishes it

The first two need no network, so ``a2a receipt verify --key ...`` works fully
offline.

Receipt payloads are attacker-controlled until the signature checks out, so
everything rendered out of one is escaped and printed after the verdict.
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, NamedTuple, Optional

import typer
from rich.console import Console
from rich.markup import escape as escape_markup

from . import credentials
from .api_client import ApiError, ControlPlaneClient
from .oauth_login import refresh_credentials_if_needed
from ..receipts import ExecutionReceipt, ReceiptInvalid, verify_receipt

console = Console()
err_console = Console(stderr=True)

RECEIPTS_DIR = Path(
    os.environ.get("A2A_RECEIPTS_DIR", str(Path.home() / ".a2a" / "receipts"))
).expanduser()
CACHE_SCHEMA_VERSION = 1
# Enough to cover a working session; the cache is a convenience, not storage.
CACHE_KEEP = 200
RECEIPT_KEYS_PATH = "/v1/public/receipt-keys"

receipt_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Inspect and verify signed execution receipts.",
)


def _fail(msg: str, code: int = 1) -> None:
    console.print(f"[red]error:[/] {msg}")
    raise typer.Exit(code)


_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _clean(value: Any, limit: int = 400) -> str:
    """Render untrusted receipt text as one harmless line.

    A receipt payload is just base64 until its signature checks out, so a
    forger controls every string in it. Control characters (newlines above
    all) would let a payload draw extra rows — including a fake verdict — and
    rich markup would let it paint them green. Neither survives this."""
    text = _CONTROL_CHARS.sub(" ", str(value))
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return escape_markup(text)


# ---------------------------------------------------------------------------
# response headers -> receipt pointer
# ---------------------------------------------------------------------------


RECEIPT_HEADERS = {
    "id": "X-A2A-Receipt-ID",
    "url": "X-A2A-Receipt-URL",
    "token": "X-A2A-Receipt-Token",
}


def receipt_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Pull the receipt pointer out of a governed response's headers.

    Returns ``{}`` for agents that emit no receipt (local/dev runs) so callers
    can stay quiet instead of warning about a non-problem."""
    observed: dict[str, str] = {}
    for key, name in RECEIPT_HEADERS.items():
        try:
            raw = headers.get(name) or headers.get(name.lower()) or ""
        except (AttributeError, TypeError):  # pragma: no cover - defensive
            return {}
        value = str(raw).strip()
        if value:
            observed[key] = value
    return observed


def receipt_from_evidence(evidence: Any) -> dict[str, str]:
    """Pull the receipt pointer out of a governed response's *body*.

    The gateway can only put a reservation (id + url) in the headers of a
    streamed response — the receipt is not sealed until the stream ends. The
    signed token arrives instead in the terminal ``a2a.evidence`` SSE frame,
    or in ``result._meta.a2aCloudEvidence`` when the response was buffered.
    Both carry the same ``{"receipt": {receipt_id, signed_token, url}}``."""
    if not isinstance(evidence, Mapping):
        return {}
    receipt = evidence.get("receipt")
    if not isinstance(receipt, Mapping):
        return {}
    observed: dict[str, str] = {}
    for key, field in (("id", "receipt_id"), ("url", "url"), ("token", "signed_token")):
        value = str(receipt.get(field) or "").strip()
        if value:
            observed[key] = value
    return observed


def decode_receipt_payload(token: str) -> dict[str, Any] | None:
    """Best-effort decode of a receipt token's payload, for *display only*.

    This is not verification — nothing here checks the signature. Use
    :func:`a2a_pack.receipts.verify_receipt` (via ``a2a receipt verify``) for
    that."""
    if not token or "." not in token:
        return None
    payload_b64 = token.rsplit(".", 1)[0]
    try:
        pad = "=" * (-len(payload_b64) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload_b64 + pad))
    except Exception:  # noqa: BLE001 — a malformed token is just "no preview"
        return None
    return data if isinstance(data, dict) else None


def format_receipt_line(observed: Mapping[str, str]) -> str | None:
    """One quiet line pointing at the receipt a call just produced."""
    payload = decode_receipt_payload(observed.get("token", "")) or {}
    receipt_id = observed.get("id") or str(payload.get("receipt_id") or "")
    if not receipt_id:
        return None
    facts: list[str] = []
    status = str(payload.get("status") or "")
    if status:
        facts.append(status)
    elapsed = payload.get("elapsed_ms")
    if isinstance(elapsed, int) and not isinstance(elapsed, bool):
        facts.append(f"{elapsed}ms")
    detail = f"  {' · '.join(facts)}" if facts else ""
    return f"receipt  {receipt_id}{detail}  ->  a2a receipt verify {receipt_id}"


# ---------------------------------------------------------------------------
# local receipt cache (written by `a2a call`, read by show/verify/list)
# ---------------------------------------------------------------------------


def _safe_id(receipt_id: str) -> str:
    return receipt_id if re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", receipt_id) else ""


def _cache_path(receipt_id: str) -> Path | None:
    safe = _safe_id(receipt_id)
    return (RECEIPTS_DIR / f"{safe}.json") if safe else None


def _prune_cache() -> None:
    try:
        paths = sorted(RECEIPTS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    for path in paths[:-CACHE_KEEP]:
        try:
            path.unlink()
        except OSError:
            continue


def remember_receipt(observed: Mapping[str, str], *, agent: str = "") -> Path | None:
    """Cache the receipt pointer a call observed. Never raises: remembering is
    a convenience and must not turn a successful call into a failure."""
    receipt_id = observed.get("id") or ""
    path = _cache_path(receipt_id)
    if path is None:
        return None
    entry = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "receipt_id": receipt_id,
        "agent": agent,
        "url": observed.get("url", ""),
        "signed_token": observed.get("token", ""),
        "seen_at": int(time.time()),
    }
    try:
        RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)
        # A token embeds the input/result previews of the call, so it is as
        # private as ~/.a2a/credentials.json and gets the same 0600/0700.
        os.chmod(RECEIPTS_DIR, 0o700)
        path.write_text(json.dumps(entry, indent=2))
        os.chmod(path, 0o600)
    except OSError:
        return None
    _prune_cache()
    return path


def _cached(receipt_id: str) -> dict[str, Any] | None:
    path = _cache_path(receipt_id)
    if path is None or not path.exists():
        return None
    try:
        entry = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(entry, dict) or entry.get("schema_version") != CACHE_SCHEMA_VERSION:
        return None
    return entry


def _cached_entries() -> list[dict[str, Any]]:
    try:
        paths = sorted(RECEIPTS_DIR.glob("*.json"))
    except OSError:
        return []
    entries = []
    for path in paths:
        try:
            entry = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(entry, dict) and entry.get("schema_version") == CACHE_SCHEMA_VERSION:
            entries.append(entry)
    entries.sort(key=lambda e: int(e.get("seen_at") or 0), reverse=True)
    return entries


# ---------------------------------------------------------------------------
# control-plane lookups
# ---------------------------------------------------------------------------


def _client(api: str | None) -> ControlPlaneClient:
    creds = credentials.load()
    if creds is None:
        _fail("not logged in (run `a2a login`) — fetching a receipt needs your account")
    try:
        creds = refresh_credentials_if_needed(creds) or creds
    except Exception:  # noqa: BLE001 — stale token still better than none
        pass
    return ControlPlaneClient(api or creds.api_url, creds.token)


def _unreachable(api: str | None, exc: Exception, what: str) -> None:
    _fail(
        f"could not reach {credentials.resolve_api_url(api).rstrip('/')} to {what} "
        f"({exc.__class__.__name__}) — retry, or verify a token you already hold "
        "with --token/--key"
    )


def _fetch_receipt(agent: str, receipt_id: str, api: str | None) -> dict[str, Any]:
    import httpx

    try:
        return _client(api).get_agent_receipt(agent, receipt_id)
    except ApiError as exc:
        if exc.status == 404:
            _fail(
                f"no receipt {receipt_id!r} on agent {agent!r}; "
                f"run 'a2a receipt list {agent}' to see recent ones"
            )
        _fail(f"could not fetch receipt {receipt_id}: {exc}")
    except httpx.HTTPError as exc:
        _unreachable(api, exc, f"fetch receipt {receipt_id}")
    raise AssertionError("unreachable")


class KeyCandidate(NamedTuple):
    """A key to try, and what to call it when reporting the verdict.

    ``key is None`` means "let :func:`verify_receipt` read the environment"."""

    key: str | None
    source: str


def fetch_receipt_public_keys(api: str | None = None) -> tuple[list[KeyCandidate], str]:
    """Fetch *every* published verifying key, active one first.

    Returns ``(candidates, reason)``; the reason explains an empty list. All
    published keys are returned because receipts carry no key id: after a
    rotation the only way to tell a genuine old receipt from a forgery is to
    try each key the platform still publishes."""
    import httpx

    url = f"{credentials.resolve_api_url(api).rstrip('/')}{RECEIPT_KEYS_PATH}"
    try:
        resp = httpx.get(url, timeout=15, follow_redirects=True)
    except httpx.HTTPError as exc:
        return [], f"{url} unreachable ({exc.__class__.__name__})"
    if resp.status_code != 200:
        return [], f"{url} returned HTTP {resp.status_code}"
    try:
        data = resp.json()
    except ValueError:
        return [], f"{url} returned an unreadable body"
    if not isinstance(data, dict):
        return [], f"{url} returned an unreadable body"
    keys = [k for k in (data.get("keys") or []) if isinstance(k, dict) and k.get("public_key")]
    if not keys:
        return [], f"{url} published no keys"
    active = str(data.get("active_kid") or "")
    keys.sort(key=lambda k: str(k.get("kid") or "") != active)
    return (
        [
            KeyCandidate(str(k["public_key"]), f"{url} (kid {str(k.get('kid') or '?')})")
            for k in keys
        ],
        "",
    )


# ---------------------------------------------------------------------------
# resolve: id | --token | stdin  ->  a signed token
# ---------------------------------------------------------------------------


class ResolvedReceipt(NamedTuple):
    receipt_id: str
    token: str
    payload: dict[str, Any]
    agent: str
    source: str


def _read_token_arg(value: str) -> str:
    raw = value.strip()
    if raw in {"-", "@-"}:
        return sys.stdin.read().strip()
    if raw.startswith("@"):
        try:
            return Path(raw[1:]).expanduser().read_text().strip()
        except OSError as exc:
            _fail(f"could not read {raw[1:]}: {exc}")
    return raw


def _from_token(raw: str, *, source: str) -> ResolvedReceipt:
    payload = decode_receipt_payload(raw)
    if payload is None:
        _fail(
            "that is not a receipt token — expected "
            "'<base64url payload>.<base64url signature>' "
            "(the X-A2A-Receipt-Token header of a governed call)"
        )
        raise AssertionError("unreachable")
    return ResolvedReceipt(
        receipt_id=str(payload.get("receipt_id") or ""),
        token=raw,
        payload=payload,
        agent=str(payload.get("agent_name") or ""),
        source=source,
    )


def resolve_receipt(
    receipt_id: str | None,
    *,
    token: str | None = None,
    agent: str | None = None,
    api: str | None = None,
) -> ResolvedReceipt:
    """Turn what the user typed into a signed token plus its decoded payload."""
    if token:
        return _from_token(_read_token_arg(token), source="--token")
    if receipt_id in {"-", "@-"}:
        return _from_token(sys.stdin.read().strip(), source="stdin")
    if not receipt_id:
        _fail("pass a receipt id, --token TOKEN, or - to read a token on stdin")
        raise AssertionError("unreachable")

    entry = _cached(receipt_id) or {}
    cached_token = str(entry.get("signed_token") or "")
    if cached_token:
        resolved = _from_token(cached_token, source=f"local cache ({RECEIPTS_DIR})")
        return resolved._replace(
            receipt_id=resolved.receipt_id or receipt_id,
            agent=resolved.agent or str(entry.get("agent") or ""),
        )

    agent_name = (agent or str(entry.get("agent") or "")).strip()
    if not agent_name:
        _fail(
            f"no receipt found for {receipt_id}; it is not in the local cache "
            f"({RECEIPTS_DIR}). Run 'a2a receipt list <agent>' to find it, "
            "then retry with --agent <agent>"
        )
    row = _fetch_receipt(agent_name, receipt_id, api)
    signed = str(row.get("signed_token") or "")
    if not signed:
        _fail(f"receipt {receipt_id} on {agent_name} has no signed token to verify")
    resolved = _from_token(signed, source=f"{agent_name} (control plane)")
    return resolved._replace(
        receipt_id=resolved.receipt_id or receipt_id,
        agent=resolved.agent or agent_name,
    )


# ---------------------------------------------------------------------------
# verification
# ---------------------------------------------------------------------------


class Verification(NamedTuple):
    ok: bool
    detail: str
    key_source: str
    receipt: ExecutionReceipt | None


def resolve_public_keys(key: str | None, api: str | None) -> tuple[list[KeyCandidate], str]:
    """Keys to try, in order, plus the reason when there are none.

    An explicit ``--key`` beats the ambient environment — env vars leak in
    from dev shells and CI, and silently ignoring a key the user typed turns a
    genuine receipt into a forgery verdict. Env beats the network. Only when
    nothing local is set do we ask the platform, and then we try every key it
    publishes."""
    env_key = os.environ.get("A2A_RECEIPT_VERIFYING_KEY", "").strip()
    if key:
        flag = key.strip()
        if env_key and env_key != flag:
            console.print(
                "[dim]note: --key overrides A2A_RECEIPT_VERIFYING_KEY "
                "(they are different keys)[/]",
                highlight=False,
            )
        return [KeyCandidate(flag, "--key")], ""
    if env_key:
        return [KeyCandidate(None, "A2A_RECEIPT_VERIFYING_KEY")], ""
    if os.environ.get("A2A_RECEIPT_SIGNING_KEY", "").strip():
        return [KeyCandidate(None, "A2A_RECEIPT_SIGNING_KEY")], ""
    return fetch_receipt_public_keys(api)


def verify_token(token: str, *, public_key: str | None) -> ExecutionReceipt:
    """Verify ``token`` with ``public_key`` (or the env key when ``None``).

    The crypto is :func:`a2a_pack.receipts.verify_receipt`; we only decide
    which key it should read."""
    if public_key is None:
        return verify_receipt(token)
    previous = os.environ.get("A2A_RECEIPT_VERIFYING_KEY")
    os.environ["A2A_RECEIPT_VERIFYING_KEY"] = public_key
    try:
        return verify_receipt(token)
    finally:
        if previous is None:
            os.environ.pop("A2A_RECEIPT_VERIFYING_KEY", None)
        else:
            os.environ["A2A_RECEIPT_VERIFYING_KEY"] = previous


def check_receipt(token: str, *, key: str | None, api: str | None) -> Verification:
    """Try each candidate key; PASS on the first that verifies.

    FAIL means *no* key we could reach signed this receipt — never merely that
    the newest one didn't."""
    candidates, reason = resolve_public_keys(key, api)
    if not candidates:
        return Verification(
            ok=False,
            detail=f"no verifying key available: {reason}",
            key_source=reason,
            receipt=None,
        )
    failures: list[str] = []
    for candidate in candidates:
        try:
            receipt = verify_token(token, public_key=candidate.key)
        except ReceiptInvalid as exc:
            failures.append(str(exc))
            continue
        except Exception as exc:  # noqa: BLE001 — unusable key material, etc.
            failures.append(f"could not verify with the key from {candidate.source}: {exc}")
            continue
        return Verification(
            ok=True,
            detail="Ed25519 signature valid",
            key_source=candidate.source,
            receipt=receipt,
        )
    tried = "; ".join(dict.fromkeys(c.source for c in candidates))
    if len(candidates) > 1:
        tried = f"{len(candidates)} published keys ({tried})"
    return Verification(ok=False, detail=failures[0], key_source=tried, receipt=None)


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def _stamp(seconds: Any) -> str:
    try:
        value = int(seconds)
    except (TypeError, ValueError):
        return "-"
    if not value:
        return "-"
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _row(label: str, value: Any) -> None:
    text = "-" if value in (None, "", (), []) else _clean(value)
    console.print(f"  [dim]{label:<14}[/]{text}", highlight=False)


def _group(title: str) -> None:
    console.print(f"\n[bold]{title}[/]")


def _dicts(value: Any) -> list[dict[str, Any]]:
    """The dict entries of what should be a list of objects. A forged payload
    can put anything here; anything else is dropped rather than crashed on."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _render_receipt(payload: dict[str, Any]) -> None:
    raw_file_ops = payload.get("file_ops")
    file_ops = raw_file_ops if isinstance(raw_file_ops, dict) else {}
    tool_calls = _dicts(payload.get("tool_calls"))
    artifacts = _dicts(payload.get("artifacts"))
    handoffs = _dicts(payload.get("handoffs"))
    grant_ids = payload.get("grant_ids")
    if not isinstance(grant_ids, (list, tuple)):
        grant_ids = ()

    _group("identity")
    _row("receipt", payload.get("receipt_id"))
    version = str(payload.get("agent_version") or "")
    _row("agent", f"{payload.get('agent_name') or '-'}{f' v{version}' if version else ''}")
    _row("caller", payload.get("caller"))
    _row("task", payload.get("task_id"))

    _group("call")
    _row("skill", payload.get("skill_name"))
    _row("input hash", payload.get("input_hash"))
    _row("input", payload.get("input_preview"))

    _group("authority")
    _row("grants", ", ".join(str(g) for g in grant_ids))

    _group("effects")
    _row(
        "files",
        f"{file_ops.get('reads', 0)} read / {file_ops.get('writes', 0)} written "
        f"({file_ops.get('bytes_read', 0)} B in, {file_ops.get('bytes_written', 0)} B out)",
    )
    _row(
        "tools",
        ", ".join(
            f"{t.get('name')} [{t.get('status', 'ok')}, {t.get('elapsed_ms', 0)}ms]"
            for t in tool_calls
        ),
    )
    _row("artifacts", ", ".join(f"{a.get('path')} ({a.get('bytes', 0)} B)" for a in artifacts))
    _row(
        "handoffs",
        ", ".join(
            f"{h.get('callee')}.{h.get('skill')} [{h.get('status', 'ok')}]" for h in handoffs
        ),
    )

    _group("outcome")
    _row("status", payload.get("status"))
    _row("error", payload.get("error_type"))
    _row("result", payload.get("result_preview"))
    _row("eval score", payload.get("eval_score"))
    _row("reviewer", payload.get("reviewer"))

    _group("timing")
    _row("started", _stamp(payload.get("started_at")))
    _row("ended", _stamp(payload.get("ended_at")))
    _row("elapsed", f"{payload.get('elapsed_ms', 0)}ms")


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


_ID_ARGUMENT = typer.Argument(
    None,
    help="Receipt id from a call, or - to read a signed token on stdin",
)
_TOKEN_OPTION = typer.Option(
    None,
    "--token",
    help="Signed receipt token (inline, @file, or - for stdin)",
)
_AGENT_OPTION = typer.Option(
    None,
    "--agent",
    help="Agent that produced the receipt (needed to fetch it from the platform)",
)
_KEY_OPTION = typer.Option(
    None,
    "--key",
    help="Base64 Ed25519 public key to verify with, offline (overrides A2A_RECEIPT_VERIFYING_KEY)",
)
_API_OPTION = typer.Option(None, "--api", hidden=True)


@receipt_app.command("show")
def show_receipt(
    receipt_id: Optional[str] = _ID_ARGUMENT,
    token: Optional[str] = _TOKEN_OPTION,
    agent: Optional[str] = _AGENT_OPTION,
    key: Optional[str] = _KEY_OPTION,
    api: Optional[str] = _API_OPTION,
) -> None:
    """Print what a receipt says: identity, call, authority, effects, outcome, timing.

    Exits 1 when the signature is bad, so `a2a receipt show` is safe in a
    script; a receipt nothing could verify (no key reachable) still exits 0."""
    resolved = resolve_receipt(receipt_id, token=token, agent=agent, api=api)

    # Verdict first, always. The body below is decoded from a token that
    # anyone can mint; only this line is a statement about who signed it.
    result = check_receipt(resolved.token, key=key, api=api)
    if result.ok:
        console.print(
            f"\n  [bold green]signature PASS[/] "
            f"[dim]Ed25519 · key from {_clean(result.key_source)}[/]",
            highlight=False,
        )
    elif result.detail.startswith("no verifying key"):
        console.print(
            f"\n  [yellow]signature UNVERIFIED[/] [dim]{_clean(result.detail)}[/]\n"
            "  [dim]pass --key <base64> or set A2A_RECEIPT_VERIFYING_KEY to check it offline[/]\n"
            "  [dim]the fields below are unverified claims until it does[/]",
            highlight=False,
        )
    else:
        console.print(
            f"\n  [bold red]signature FAIL[/] [dim]{_clean(result.detail)} "
            f"(key from {_clean(result.key_source)})[/]\n"
            "  [dim]nobody with a key we trust signed this — the fields below are "
            "not evidence of anything[/]",
            highlight=False,
        )
    console.print(f"  [dim]source: {_clean(resolved.source)}[/]", highlight=False)

    _render_receipt(resolved.payload)
    if not result.ok and not result.detail.startswith("no verifying key"):
        raise typer.Exit(1)


@receipt_app.command("verify")
def verify_receipt_command(
    receipt_id: Optional[str] = _ID_ARGUMENT,
    token: Optional[str] = _TOKEN_OPTION,
    agent: Optional[str] = _AGENT_OPTION,
    key: Optional[str] = _KEY_OPTION,
    api: Optional[str] = _API_OPTION,
) -> None:
    """Check a receipt's Ed25519 signature. Exit 0 on PASS, 1 on FAIL."""
    resolved = resolve_receipt(receipt_id, token=token, agent=agent, api=api)
    result = check_receipt(resolved.token, key=key, api=api)
    if result.ok and result.receipt is not None:
        receipt = result.receipt
        console.print(
            f"[bold green]PASS[/] {receipt.receipt_id}  "
            f"{receipt.agent_name}.{receipt.skill_name} · {receipt.status} · "
            f"{receipt.elapsed_ms}ms",
            highlight=False,
        )
        console.print(
            f"  [dim]{result.detail} · key from {_clean(result.key_source)}[/]", highlight=False
        )
        return
    console.print(
        f"[bold red]FAIL[/] {_clean(resolved.receipt_id) or '(no id)'}  "
        f"{_clean(result.detail)}",
        highlight=False,
    )
    if result.detail.startswith("no verifying key"):
        console.print(
            "  [dim]pass --key <base64 public key> or set A2A_RECEIPT_VERIFYING_KEY "
            "to verify offline[/]",
            highlight=False,
        )
    else:
        console.print(
            f"  [dim]not signed by the key from {_clean(result.key_source)} — "
            "do not trust it[/]",
            highlight=False,
        )
    raise typer.Exit(1)


@receipt_app.command("list")
def list_receipts(
    agent: Optional[str] = typer.Argument(
        None, help="Agent whose receipts to list (omit to list ones this CLI has seen)"
    ),
    limit: int = typer.Option(20, "--limit", "-n", help="How many receipts to show"),
    api: Optional[str] = _API_OPTION,
) -> None:
    """List recent receipts for AGENT (or the ones cached by recent calls)."""
    # The platform route rejects anything outside 1..200 with a raw 422, and a
    # 0/negative slice would quietly print "nothing here" over a full cache.
    limit = max(1, min(int(limit), 200))
    if not agent:
        entries = _cached_entries()[:limit]
        if not entries:
            console.print(
                "(no receipts seen yet — run `a2a call <agent> <tool>`, "
                "or pass an agent name to list the platform's)"
            )
            return
        for entry in entries:
            payload = decode_receipt_payload(str(entry.get("signed_token") or "")) or {}
            status = _clean(payload.get("status") or "?", 12)
            what = _clean(payload.get("skill_name") or entry.get("agent") or "-", 40)
            console.print(
                f"  [cyan]{_clean(entry.get('receipt_id'), 80)}[/]  "
                f"{status:<9} {what:<24} "
                f"[dim]{_stamp(entry.get('seen_at'))}[/]",
                highlight=False,
            )
        console.print(f"\n[dim]cached locally in {RECEIPTS_DIR}[/]")
        return

    import httpx

    try:
        rows = _client(api).list_agent_receipts(agent, limit=limit)
    except ApiError as exc:
        if exc.status == 404:
            _fail(f"agent {agent!r} not found (see `a2a agents`)")
        _fail(f"could not list receipts for {agent}: {exc}")
        raise AssertionError("unreachable")
    except httpx.HTTPError as exc:
        _unreachable(api, exc, f"list receipts for {agent}")
        raise AssertionError("unreachable")
    if not rows:
        console.print(f"(no receipts for {agent} yet)")
        return
    for row in rows:
        console.print(
            f"  [cyan]{_clean(row.get('receipt_id'), 80)}[/]  "
            f"{_clean(row.get('status') or '?', 12):<9} "
            f"{_clean(row.get('skill_name') or '-', 40):<24} "
            f"{_clean(row.get('elapsed_ms') or 0, 12)}ms  "
            f"[dim]{_clean(row.get('started_at') or row.get('created_at') or '', 40)}[/]",
            highlight=False,
        )
    console.print(f"\n[dim]a2a receipt show <id> --agent {agent}[/]")


__all__ = [
    "RECEIPTS_DIR",
    "decode_receipt_payload",
    "fetch_receipt_public_keys",
    "format_receipt_line",
    "receipt_app",
    "receipt_from_evidence",
    "receipt_headers",
    "remember_receipt",
    "resolve_receipt",
    "verify_token",
]
