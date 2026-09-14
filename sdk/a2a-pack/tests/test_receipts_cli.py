"""Tests for `a2a receipt` and the receipt line `a2a call` prints.

The crypto lives in :mod:`a2a_pack.receipts`; these cover the CLI around it —
key sourcing (env / --key / published endpoint), PASS/FAIL exit codes, the
local receipt cache, and the quiet one-liner every governed call leaves behind.
"""
from __future__ import annotations

import base64
import json
import time

import pytest
import typer
from typer.testing import CliRunner

from a2a_pack.cli import receipts_cli
from a2a_pack.receipts import seal_receipt


@pytest.fixture
def runner():
    """A fresh runner per test — with click 8.4 a reused CliRunner stopped
    surfacing ``result.stderr`` on later invocations."""
    return CliRunner()


@pytest.fixture
def keypair(monkeypatch):
    """A fresh Ed25519 keypair wired into the receipt signing env."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    private_b64 = base64.b64encode(key.private_bytes_raw()).decode()
    public_b64 = base64.b64encode(key.public_key().public_bytes_raw()).decode()
    monkeypatch.setenv("A2A_RECEIPT_SIGNING_KEY", private_b64)
    monkeypatch.setenv("A2A_RECEIPT_VERIFYING_KEY", public_b64)
    return private_b64, public_b64


@pytest.fixture
def signed(keypair):
    started = int(time.time()) - 1
    receipt, token = seal_receipt(
        agent_name="finance-recon",
        agent_version="1.4.2",
        skill_name="reconcile",
        caller="user:42",
        task_id="mcp-2",
        started_at=started,
        ended_at=started + 1,
        inputs={"period": "2026-Q1"},
        result={"exceptions": 7},
        grant_ids=("grant-1",),
    )
    return receipt, token


@pytest.fixture
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(receipts_cli, "RECEIPTS_DIR", tmp_path / "receipts")
    return tmp_path / "receipts"


def _no_network(monkeypatch):
    """Fail loudly if anything reaches for the published key endpoint."""

    def _boom(api=None):  # pragma: no cover - only runs when the test fails
        raise AssertionError("verification tried to hit the network")

    monkeypatch.setattr(receipts_cli, "fetch_receipt_public_keys", _boom)


def _published(*keys: tuple[str, str]):
    """Stand in for the published key set: (public_key_b64, kid) pairs."""
    candidates = [
        receipts_cli.KeyCandidate(pk, f"https://api.example/v1/public/receipt-keys (kid {kid})")
        for pk, kid in keys
    ]
    return lambda api=None: (candidates, "")


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


def test_verify_passes_on_freshly_sealed_receipt(runner, signed, monkeypatch):
    receipt, token = signed
    _no_network(monkeypatch)
    result = runner.invoke(receipts_cli.receipt_app, ["verify", "--token", token])
    assert result.exit_code == 0, result.output
    assert "PASS" in result.output
    assert receipt.receipt_id in result.output
    assert "finance-recon.reconcile" in result.output
    assert "A2A_RECEIPT_VERIFYING_KEY" in result.output


def _retoken(payload: dict, signature_b64: str) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return f"{base64.urlsafe_b64encode(raw).rstrip(b'=').decode()}.{signature_b64}"


def test_verify_fails_on_tampered_payload(runner, signed, monkeypatch):
    """Rewrite one byte of the payload: the signature no longer covers it."""
    _, token = signed
    _no_network(monkeypatch)
    payload_b64, sig = token.rsplit(".", 1)
    payload = receipts_cli.decode_receipt_payload(token)
    assert payload["status"] == "ok"
    payload["status"] = "Ok"  # one byte, and the receipt now lies

    result = runner.invoke(
        receipts_cli.receipt_app, ["verify", "--token", _retoken(payload, sig)]
    )
    assert result.exit_code == 1, result.output
    assert "FAIL" in result.output
    assert "signature mismatch" in result.output
    assert "do not trust it" in result.output


def test_verify_rejects_a_mangled_payload_encoding(runner, signed, monkeypatch):
    _, token = signed
    _no_network(monkeypatch)
    payload_b64, sig = token.rsplit(".", 1)
    flipped = payload_b64[:-1] + ("A" if payload_b64[-1] != "A" else "B")
    result = runner.invoke(receipts_cli.receipt_app, ["verify", "--token", f"{flipped}.{sig}"])
    assert result.exit_code == 1, result.output
    assert "not a receipt token" in result.output


def test_verify_offline_with_key_option(runner, keypair, signed, monkeypatch):
    """--key alone verifies: no env key, no network."""
    _, token = signed
    _, public_b64 = keypair
    monkeypatch.delenv("A2A_RECEIPT_VERIFYING_KEY", raising=False)
    monkeypatch.delenv("A2A_RECEIPT_SIGNING_KEY", raising=False)
    _no_network(monkeypatch)

    result = runner.invoke(
        receipts_cli.receipt_app, ["verify", "--token", token, "--key", public_b64]
    )
    assert result.exit_code == 0, result.output
    assert "PASS" in result.output
    assert "key from --key" in result.output


def test_verify_offline_with_wrong_key_fails(runner, signed, monkeypatch):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    _, token = signed
    other = base64.b64encode(
        Ed25519PrivateKey.generate().public_key().public_bytes_raw()
    ).decode()
    monkeypatch.delenv("A2A_RECEIPT_VERIFYING_KEY", raising=False)
    monkeypatch.delenv("A2A_RECEIPT_SIGNING_KEY", raising=False)
    _no_network(monkeypatch)

    result = runner.invoke(
        receipts_cli.receipt_app, ["verify", "--token", token, "--key", other]
    )
    assert result.exit_code == 1, result.output
    assert "FAIL" in result.output
    assert "signature mismatch" in result.output


def test_key_option_wins_over_a_stale_env_key(runner, keypair, signed, monkeypatch):
    """An explicit --key beats an ambient (here: foreign) env key.

    Env vars leak in from agent dev shells, .env files and CI; discarding the
    key the user typed would report a genuine receipt as a forgery."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    _, token = signed
    _, public_b64 = keypair
    foreign = base64.b64encode(
        Ed25519PrivateKey.generate().public_key().public_bytes_raw()
    ).decode()
    monkeypatch.setenv("A2A_RECEIPT_VERIFYING_KEY", foreign)
    monkeypatch.delenv("A2A_RECEIPT_SIGNING_KEY", raising=False)
    _no_network(monkeypatch)

    result = runner.invoke(
        receipts_cli.receipt_app, ["verify", "--token", token, "--key", public_b64]
    )
    assert result.exit_code == 0, result.output
    assert "PASS" in result.output
    assert "key from --key" in result.output
    # and it says so rather than silently picking one of the two
    assert "overrides A2A_RECEIPT_VERIFYING_KEY" in result.output


def test_env_key_is_used_when_no_key_option_is_given(runner, signed, monkeypatch):
    _, token = signed
    _no_network(monkeypatch)
    result = runner.invoke(receipts_cli.receipt_app, ["verify", "--token", token])
    assert result.exit_code == 0, result.output
    assert "key from A2A_RECEIPT_VERIFYING_KEY" in result.output


def test_verify_without_any_key_says_what_to_do(runner, signed, monkeypatch):
    _, token = signed
    monkeypatch.delenv("A2A_RECEIPT_VERIFYING_KEY", raising=False)
    monkeypatch.delenv("A2A_RECEIPT_SIGNING_KEY", raising=False)
    monkeypatch.setattr(
        receipts_cli,
        "fetch_receipt_public_keys",
        lambda api=None: ([], "https://api.example/v1/public/receipt-keys returned HTTP 503"),
    )
    result = runner.invoke(receipts_cli.receipt_app, ["verify", "--token", token])
    assert result.exit_code == 1, result.output
    assert "no verifying key available" in result.output
    assert "--key" in result.output


def test_verify_fetches_published_key_when_no_local_key(runner, keypair, signed, monkeypatch):
    _, token = signed
    _, public_b64 = keypair
    monkeypatch.delenv("A2A_RECEIPT_VERIFYING_KEY", raising=False)
    monkeypatch.delenv("A2A_RECEIPT_SIGNING_KEY", raising=False)
    monkeypatch.setattr(
        receipts_cli, "fetch_receipt_public_keys", _published((public_b64, "abc123"))
    )
    result = runner.invoke(receipts_cli.receipt_app, ["verify", "--token", token])
    assert result.exit_code == 0, result.output
    assert "kid abc123" in result.output


def test_verify_tries_every_published_key_after_a_rotation(runner, keypair, signed, monkeypatch):
    """A receipt signed before a key rotation is genuine, not a forgery.

    Receipts carry no kid, so the only correct answer is to try each key the
    platform still publishes — active first."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    _, token = signed
    _, old_public = keypair  # the key this receipt was signed with
    rotated = base64.b64encode(
        Ed25519PrivateKey.generate().public_key().public_bytes_raw()
    ).decode()
    monkeypatch.delenv("A2A_RECEIPT_VERIFYING_KEY", raising=False)
    monkeypatch.delenv("A2A_RECEIPT_SIGNING_KEY", raising=False)
    monkeypatch.setattr(
        receipts_cli,
        "fetch_receipt_public_keys",
        _published((rotated, "new0000"), (old_public, "old0000")),
    )

    result = runner.invoke(receipts_cli.receipt_app, ["verify", "--token", token])
    assert result.exit_code == 0, result.output
    assert "PASS" in result.output
    # and it names the key that actually matched
    assert "kid old0000" in result.output


def test_verify_fails_when_no_published_key_matches(runner, signed, monkeypatch):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    _, token = signed
    others = [
        (
            base64.b64encode(Ed25519PrivateKey.generate().public_key().public_bytes_raw()).decode(),
            f"kid{n}",
        )
        for n in range(2)
    ]
    monkeypatch.delenv("A2A_RECEIPT_VERIFYING_KEY", raising=False)
    monkeypatch.delenv("A2A_RECEIPT_SIGNING_KEY", raising=False)
    monkeypatch.setattr(receipts_cli, "fetch_receipt_public_keys", _published(*others))

    result = runner.invoke(receipts_cli.receipt_app, ["verify", "--token", token])
    assert result.exit_code == 1, result.output
    assert "FAIL" in result.output
    assert "2 published keys" in result.output


def test_fetch_receipt_public_keys_returns_active_first(monkeypatch):
    """Parses the published key set per the platform contract."""
    import httpx

    body = {
        "active_kid": "bbb",
        "keys": [
            {"kid": "aaa", "alg": "Ed25519", "public_key": "AAA=", "use": "receipt"},
            {"kid": "bbb", "alg": "Ed25519", "public_key": "BBB=", "use": "receipt"},
        ],
    }
    monkeypatch.setattr(
        httpx,
        "get",
        lambda url, **kw: httpx.Response(200, json=body, request=httpx.Request("GET", url)),
    )
    candidates, reason = receipts_cli.fetch_receipt_public_keys("https://api.example")
    assert reason == ""
    assert [c.key for c in candidates] == ["BBB=", "AAA="]
    assert "kid bbb" in candidates[0].source


def test_fetch_receipt_public_keys_reports_an_unreachable_endpoint(monkeypatch):
    import httpx

    def _boom(url, **kw):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "get", _boom)
    candidates, reason = receipts_cli.fetch_receipt_public_keys("https://api.example")
    assert candidates == []
    assert "unreachable" in reason and "ConnectError" in reason


def test_verify_rejects_a_non_token(runner, monkeypatch):
    _no_network(monkeypatch)
    result = runner.invoke(receipts_cli.receipt_app, ["verify", "--token", "garbage"])
    assert result.exit_code == 1
    assert "not a receipt token" in result.output


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


def test_show_groups_fields_and_reports_signature(runner, signed, monkeypatch):
    receipt, token = signed
    _no_network(monkeypatch)
    result = runner.invoke(receipts_cli.receipt_app, ["show", "--token", token])
    assert result.exit_code == 0, result.output
    for group in ("identity", "call", "authority", "effects", "outcome", "timing"):
        assert group in result.output
    assert receipt.receipt_id in result.output
    assert "reconcile" in result.output
    assert "grant-1" in result.output
    assert "signature PASS" in result.output


def test_show_marks_unverified_when_no_key_is_reachable(runner, signed, monkeypatch):
    _, token = signed
    monkeypatch.delenv("A2A_RECEIPT_VERIFYING_KEY", raising=False)
    monkeypatch.delenv("A2A_RECEIPT_SIGNING_KEY", raising=False)
    monkeypatch.setattr(
        receipts_cli,
        "fetch_receipt_public_keys",
        lambda api=None: ([], "https://api.example/v1/public/receipt-keys unreachable"),
    )
    result = runner.invoke(receipts_cli.receipt_app, ["show", "--token", token])
    # Still prints the receipt — it just refuses to claim the signature is good.
    assert result.exit_code == 0, result.output
    assert "signature UNVERIFIED" in result.output
    assert "A2A_RECEIPT_VERIFYING_KEY" in result.output


# ---------------------------------------------------------------------------
# hostile payloads: a token is attacker-controlled until it verifies
# ---------------------------------------------------------------------------


def _forge(**fields) -> str:
    """A syntactically valid, unsigned token with attacker-chosen fields."""
    payload = {
        "receipt_id": "deadbeef",
        "agent_name": "finance-recon",
        "skill_name": "reconcile",
        "status": "ok",
        "elapsed_ms": 1,
        **fields,
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    body = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    sig = base64.urlsafe_b64encode(b"\x00" * 64).rstrip(b"=").decode()
    return f"{body}.{sig}"


def test_show_cannot_be_tricked_into_printing_a_fake_verdict(runner, keypair, monkeypatch):
    """A forged preview may not draw its own green PASS row.

    The payload is unsigned JSON a forger controls, so it must not carry rich
    markup or newlines into the render, and the real verdict must come first."""
    _no_network(monkeypatch)
    forged = _forge(
        result_preview=(
            "\n\n  [bold green]signature PASS[/] [dim]Ed25519 · key from "
            "https://api.a2acloud.io/v1/public/receipt-keys (kid deadbeefdeadbeef)[/]\n"
            "  [dim]source: control plane[/]"
        )
    )
    result = runner.invoke(receipts_cli.receipt_app, ["show", "--token", forged])

    assert result.exit_code == 1, result.output
    assert "signature FAIL" in result.output
    # the verdict is the FIRST thing on screen — a long preview can't push it off
    assert result.output.index("signature FAIL") < result.output.index("kid deadbeefdeadbeef")
    # and the injected text stays inside its row: no line pretends to be a verdict
    for line in result.output.splitlines():
        assert not line.strip().startswith("signature PASS"), line
    assert "nobody with a key we trust signed this" in result.output


def test_show_survives_a_payload_with_the_wrong_types(runner, keypair, monkeypatch):
    """Hostile input reaches the renderer by design — it must not traceback."""
    _no_network(monkeypatch)
    forged = _forge(
        file_ops="not-an-object",
        tool_calls=["not-an-object", {"name": "rm"}],
        artifacts="nope",
        handoffs=[None],
        grant_ids="not-a-list",
    )
    result = runner.invoke(receipts_cli.receipt_app, ["show", "--token", forged])
    assert result.exit_code == 1, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "signature FAIL" in result.output
    assert "effects" in result.output


def test_verify_escapes_markup_in_a_forged_receipt_id(runner, keypair, monkeypatch):
    """An unclosed markup tag in the id used to raise MarkupError."""
    _no_network(monkeypatch)
    result = runner.invoke(
        receipts_cli.receipt_app, ["verify", "--token", _forge(receipt_id="[bold red]oops")]
    )
    assert result.exit_code == 1, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "FAIL" in result.output
    assert "do not trust it" in result.output


# ---------------------------------------------------------------------------
# local cache / resolution
# ---------------------------------------------------------------------------


def test_show_by_id_uses_the_cache_written_by_a_call(runner, signed, isolated_cache, monkeypatch):
    receipt, token = signed
    _no_network(monkeypatch)
    receipts_cli.remember_receipt(
        {"id": receipt.receipt_id, "token": token, "url": "https://api.example/r"},
        agent="finance-recon",
    )
    assert (isolated_cache / f"{receipt.receipt_id}.json").exists()

    result = runner.invoke(receipts_cli.receipt_app, ["verify", receipt.receipt_id])
    assert result.exit_code == 0, result.output
    assert "PASS" in result.output


def test_unknown_receipt_id_says_what_to_run_next(runner, isolated_cache):
    result = runner.invoke(receipts_cli.receipt_app, ["show", "rcpt_missing"])
    assert result.exit_code == 1
    assert "no receipt found for rcpt_missing" in result.output
    assert "a2a receipt list <agent>" in result.output


def test_list_without_agent_shows_locally_cached_receipts(runner, signed, isolated_cache):
    receipt, token = signed
    receipts_cli.remember_receipt({"id": receipt.receipt_id, "token": token}, agent="finance-recon")
    result = runner.invoke(receipts_cli.receipt_app, ["list"])
    assert result.exit_code == 0, result.output
    assert receipt.receipt_id in result.output
    assert "reconcile" in result.output


def test_list_for_agent_reads_the_control_plane(runner, isolated_cache, monkeypatch):
    class _Client:
        def list_agent_receipts(self, name, *, limit):
            assert (name, limit) == ("finance-recon", 5)
            return [
                {
                    "receipt_id": "abc123",
                    "status": "ok",
                    "skill_name": "reconcile",
                    "elapsed_ms": 418,
                    "started_at": "2026-08-01T12:00:00Z",
                }
            ]

    monkeypatch.setattr(receipts_cli, "_client", lambda api: _Client())
    result = runner.invoke(receipts_cli.receipt_app, ["list", "finance-recon", "--limit", "5"])
    assert result.exit_code == 0, result.output
    assert "abc123" in result.output and "reconcile" in result.output


def test_remember_receipt_ignores_unsafe_ids(isolated_cache):
    assert receipts_cli.remember_receipt({"id": "../escape", "token": "x.y"}) is None
    assert receipts_cli.remember_receipt({}) is None


def test_cached_receipts_are_private_to_the_user(signed, isolated_cache):
    """A token embeds input/result previews — 0600, like ~/.a2a/credentials.json."""
    import stat

    receipt, token = signed
    path = receipts_cli.remember_receipt({"id": receipt.receipt_id, "token": token})
    assert path is not None
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(isolated_cache.stat().st_mode) == 0o700


def test_list_limit_is_clamped_instead_of_lying(runner, signed, isolated_cache):
    """--limit 0 used to print "no receipts seen yet" over a full cache."""
    receipt, token = signed
    receipts_cli.remember_receipt({"id": receipt.receipt_id, "token": token}, agent="finance-recon")

    for bad in ("0", "-3"):
        result = runner.invoke(receipts_cli.receipt_app, ["list", "--limit", bad])
        assert result.exit_code == 0, result.output
        assert receipt.receipt_id in result.output
        assert "no receipts seen yet" not in result.output


def test_list_clamps_the_limit_it_sends_to_the_platform(runner, isolated_cache, monkeypatch):
    """The route declares 1 <= limit <= 200; anything else is a raw 422."""
    seen = {}

    class _Client:
        def list_agent_receipts(self, name, *, limit):
            seen["limit"] = limit
            return []

    monkeypatch.setattr(receipts_cli, "_client", lambda api: _Client())
    runner.invoke(receipts_cli.receipt_app, ["list", "finance-recon", "--limit", "5000"])
    assert seen["limit"] == 200
    runner.invoke(receipts_cli.receipt_app, ["list", "finance-recon", "--limit", "0"])
    assert seen["limit"] == 1


def test_receipt_lookup_reports_an_unreachable_control_plane(runner, isolated_cache, monkeypatch):
    """A dead API is a message, not a 200-line traceback."""
    import httpx

    class _Client:
        def get_agent_receipt(self, name, receipt_id):
            raise httpx.ConnectError("[Errno 61] Connection refused")

    monkeypatch.setattr(receipts_cli, "_client", lambda api: _Client())
    result = runner.invoke(
        receipts_cli.receipt_app, ["verify", "abc123", "--agent", "finance-recon"]
    )
    assert result.exit_code == 1, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "could not reach" in result.output
    assert "--token/--key" in result.output


# ---------------------------------------------------------------------------
# the `a2a call` receipt line
# ---------------------------------------------------------------------------


def test_receipt_headers_read_off_a_response(signed):
    import httpx

    _, token = signed
    headers = httpx.Headers(
        {
            "X-A2A-Receipt-ID": "7f3a2c8b",
            "X-A2A-Receipt-URL": "https://api.example/v1/agents/a/receipts/7f3a2c8b",
            "X-A2A-Receipt-Token": token,
            "content-type": "application/json",
        }
    )
    observed = receipts_cli.receipt_headers(headers)
    assert observed["id"] == "7f3a2c8b"
    assert observed["token"] == token
    assert receipts_cli.receipt_headers(httpx.Headers({"content-type": "text/plain"})) == {}


def test_receipt_read_off_the_evidence_body(signed):
    _, token = signed
    evidence = {
        "receipt": {
            "receipt_id": "7f3a2c8b",
            "signed_token": token,
            "url": "https://api.example/v1/agents/a/receipts/7f3a2c8b",
        },
        "replay": {"session_id": "s1", "signed_token": "x.y"},
    }
    observed = receipts_cli.receipt_from_evidence(evidence)
    assert observed == {
        "id": "7f3a2c8b",
        "token": token,
        "url": "https://api.example/v1/agents/a/receipts/7f3a2c8b",
    }
    assert receipts_cli.receipt_from_evidence({"replay": {}}) == {}
    assert receipts_cli.receipt_from_evidence("nonsense") == {}


class _Resp:
    """Minimal stand-in for an httpx response."""

    def __init__(self, ctype: str, text: str):
        import httpx

        self.headers = httpx.Headers({"content-type": ctype})
        self.text = text

    def json(self):
        return json.loads(self.text)


def test_streamed_call_picks_the_signed_token_out_of_the_evidence_frame(signed):
    """The gateway can only reserve an id in the headers of a stream.

    The sealed, signed receipt arrives in the terminal `a2a.evidence` frame —
    that is the token `a2a receipt verify <id>` needs to work offline."""
    from a2a_pack.cli.agents_cli import _parse_mcp_response

    _, token = signed
    frames = [
        'data: {"jsonrpc":"2.0","id":2,"result":{"content":[{"type":"text","text":"{}"}]}}',
        "",
        'data: ' + json.dumps(
            {
                "type": "a2a.evidence",
                "evidence": {
                    "receipt": {
                        "receipt_id": "res-1",
                        "signed_token": token,
                        "url": "https://api.example/v1/agents/a/receipts/res-1",
                    }
                },
            }
        ),
        "",
        "data: [DONE]",
        "",
    ]
    observed = {"id": "res-1", "url": "https://api.example/v1/agents/a/receipts/res-1"}
    payload = _parse_mcp_response(
        _Resp("text/event-stream", "\n".join(frames)), receipt=observed
    )
    assert "result" in payload
    assert observed["token"] == token


def test_buffered_call_picks_the_signed_token_out_of_result_meta(signed):
    from a2a_pack.cli.agents_cli import _parse_mcp_response

    _, token = signed
    body = {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {
            "content": [{"type": "text", "text": "{}"}],
            "_meta": {
                "a2aCloudEvidence": {
                    "receipt": {"receipt_id": "buf-1", "signed_token": token, "url": "u"}
                }
            },
        },
    }
    observed: dict[str, str] = {}
    _parse_mcp_response(_Resp("application/json", json.dumps(body)), receipt=observed)
    assert observed == {"id": "buf-1", "token": token, "url": "u"}


def test_format_receipt_line_renders_status_and_verify_hint(signed):
    receipt, token = signed
    line = receipts_cli.format_receipt_line({"id": receipt.receipt_id, "token": token})
    assert line == (
        f"receipt  {receipt.receipt_id}  ok · 1000ms"
        f"  ->  a2a receipt verify {receipt.receipt_id}"
    )
    # id-only (streamed reservation headers): still points somewhere useful
    assert receipts_cli.format_receipt_line({"id": "abc"}) == (
        "receipt  abc  ->  a2a receipt verify abc"
    )
    # no receipt at all -> nothing to say
    assert receipts_cli.format_receipt_line({}) is None


def test_call_prints_the_receipt_line_and_keeps_stdout_parseable(
    runner, signed, isolated_cache, monkeypatch
):
    import a2a_pack.cli.agents_cli as agents_cli

    receipt, token = signed
    monkeypatch.setattr(agents_cli, "_load_stub", lambda agent: {"url": "https://x.example"})

    def _fake_invoke(url, skill_id, arguments, *, receipt=None):
        if receipt is not None:
            receipt.update({"id": receipt_id, "token": token})
        return {"exceptions": 7}

    receipt_id = receipt.receipt_id
    monkeypatch.setattr(agents_cli, "invoke_skill", _fake_invoke)

    app = typer.Typer()
    agents_cli.register_commands(app)
    result = runner.invoke(app, ["call", "finance-recon", "reconcile"])

    assert result.exit_code == 0, result.output
    # stdout stays machine-readable: the result JSON and nothing else
    assert json.loads(result.stdout) == {"exceptions": 7}
    # the receipt line rides stderr
    assert f"receipt  {receipt_id}" in result.stderr
    assert f"a2a receipt verify {receipt_id}" in result.stderr
    # and the receipt is now inspectable by id, offline
    assert (isolated_cache / f"{receipt_id}.json").exists()


def test_typed_stub_command_also_reports_its_receipt(
    runner, signed, isolated_cache, tmp_path, monkeypatch
):
    """`a2a <agent> <tool>` (the mounted stub commands) gets the same line."""
    import a2a_pack.cli.agents_cli as agents_cli

    receipt, token = signed
    stub = {
        "schema_version": agents_cli.STUB_SCHEMA_VERSION,
        "agent": "finance-recon",
        "url": "https://x.example",
        "description": "demo",
        "skills": [
            {
                "id": "reconcile",
                "description": "reconcile",
                "input_schema": {"type": "object", "properties": {"period": {"type": "string"}}},
            }
        ],
        "consumer_setup": [],
    }
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    (stubs / "finance-recon.json").write_text(json.dumps(stub))
    monkeypatch.setattr(agents_cli, "AGENTS_DIR", stubs)

    def _fake_invoke(url, skill_id, arguments, *, receipt=None):
        if receipt is not None:
            receipt.update({"id": receipt_id, "token": token})
        return {"exceptions": 7}

    receipt_id = receipt.receipt_id
    monkeypatch.setattr(agents_cli, "invoke_skill", _fake_invoke)

    app = typer.Typer()
    agents_cli.register_agent_stubs(app)
    result = runner.invoke(app, ["finance-recon", "reconcile", "--period", "2026-Q1"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"exceptions": 7}
    assert f"a2a receipt verify {receipt_id}" in result.stderr
    entry = json.loads((isolated_cache / f"{receipt_id}.json").read_text())
    assert entry["agent"] == "finance-recon"


def test_call_stays_silent_when_the_agent_returns_no_receipt(
    runner, isolated_cache, monkeypatch
):
    import a2a_pack.cli.agents_cli as agents_cli

    monkeypatch.setattr(agents_cli, "_load_stub", lambda agent: {"url": "https://x.example"})
    monkeypatch.setattr(
        agents_cli,
        "invoke_skill",
        lambda url, skill_id, arguments, *, receipt=None: {"ok": True},
    )

    app = typer.Typer()
    agents_cli.register_commands(app)
    result = runner.invoke(app, ["call", "local-agent", "ping"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"ok": True}
    assert result.stderr.strip() == ""
    assert not isolated_cache.exists() or not list(isolated_cache.glob("*.json"))
