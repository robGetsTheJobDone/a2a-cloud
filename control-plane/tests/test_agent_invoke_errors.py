"""What a caller learns when an agent invoke fails.

Every one of these used to end the same way: a bare ``RuntimeError`` nothing
caught, rendered as an opaque platform 500 with the agent's status code and its
error text discarded. The developer whose agent answered ``422 {"detail":
"topic must be a non-empty string"}`` saw "Internal Server Error".

These tests pin the three things that had to become true at once: the agent's
own status and words survive, secrets in them do not, and the run still seals a
signed ``status="error"`` receipt the caller is pointed at.
"""
from __future__ import annotations

import json
import os

os.environ.setdefault("A2A_CP_DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from a2a_pack.receipts import verify_receipt
from control_plane.db import Base
from control_plane.models import Agent, AgentReceipt, User
from control_plane.routes import agents


#: A live-looking OpenAI key an agent might echo back inside its own error.
LEAKED_KEY = "sk-proj-A1b2C3d4E5f6G7h8I9j0KlMn"


class _Request:
    def __init__(self, body: dict) -> None:
        self._body = body
        self.headers = {
            "host": "internal.svc.cluster.local",
            "x-forwarded-host": "app.example.com",
            "x-forwarded-proto": "https",
        }

    async def json(self) -> dict:
        return self._body


class _StubResponse:
    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text

    def json(self):
        return json.loads(self.text)


def _stub_agent_response(monkeypatch, *, response=None, raises=None) -> None:
    """Make the hosted agent answer ``response`` (or fail with ``raises``)."""

    class _StubClient:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, *_args, **_kwargs):
            if raises is not None:
                raise raises
            return response

    monkeypatch.setattr(agents.httpx, "AsyncClient", _StubClient)


async def _seed(session):
    user = User(email="owner@example.com", password_hash="x")
    session.add(user)
    await session.flush()
    agent = Agent(
        owner_id=user.id,
        name="reporter",
        description="Build reports",
        version="1.2.3",
        image="registry.example/reporter:latest",
        public=True,
        status="running",
        url="https://reporter.example.com",
        card={
            "runtime": {"llm_provisioning": "caller_provided"},
            "skills": [{"name": "build_report", "input_schema": {"type": "object"}}],
        },
    )
    session.add(agent)
    await session.commit()
    await session.refresh(user)
    await session.refresh(agent)
    created = await agents.create_agent_api_token(
        "reporter",
        agents.AgentApiTokenCreateIn(name="call token"),
        user=user,
        session=session,
    )
    return user, agent, created.token


async def _invoke(session, token, arguments):
    with pytest.raises(agents.AgentApiInvokeFailed) as raised:
        await agents.invoke_agent_api(
            "reporter",
            "build_report",
            _Request(arguments),
            authorization=f"Bearer {token}",
            session=session,
        )
    return raised.value


async def _receipts(session) -> list[AgentReceipt]:
    return list((await session.execute(select(AgentReceipt))).scalars().all())


async def _run(monkeypatch, *, response=None, raises=None, arguments=None):
    """Seed an agent, stub its answer, invoke it, hand back error + receipts."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            _user, _agent, token = await _seed(session)
            _stub_agent_response(monkeypatch, response=response, raises=raises)
            exc = await _invoke(session, token, arguments or {"topic": "sales"})
            return exc, await _receipts(session)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_agent_4xx_reaches_the_caller_instead_of_a_platform_500(monkeypatch) -> None:
    """The whole point: a 422 the developer can act on, not an opaque 500."""
    exc, _receipts_rows = await _run(
        monkeypatch,
        response=_StubResponse(
            422, json.dumps({"detail": "topic must be a non-empty string"})
        ),
        arguments={"topic": ""},
    )

    assert exc.status_code == 422
    assert exc.detail["error"] == "agent_error"
    assert exc.detail["agent_status"] == 422
    # Names which agent, which skill, and what the agent actually said.
    assert exc.detail["agent"] == "reporter"
    assert exc.detail["skill"] == "build_report"
    assert "topic must be a non-empty string" in exc.detail["agent_error"]
    assert exc.detail["message"] == (
        'reporter.build_report returned HTTP 422: {"detail":"topic must be a '
        'non-empty string"}'
    )


@pytest.mark.asyncio
async def test_agent_5xx_is_a_bad_gateway_that_still_carries_the_agent_status(
    monkeypatch,
) -> None:
    """A broken agent is not a broken control plane - but its 503 is not lost."""
    exc, _receipts_rows = await _run(
        monkeypatch,
        response=_StubResponse(503, json.dumps({"detail": "model backend down"})),
    )

    assert exc.status_code == 502
    assert exc.detail["error"] == "agent_error"
    assert exc.detail["agent_status"] == 503
    assert "model backend down" in exc.detail["agent_error"]


@pytest.mark.asyncio
async def test_agent_timeout_is_a_gateway_timeout_the_platform_owns(monkeypatch) -> None:
    exc, receipt_rows = await _run(
        monkeypatch, raises=httpx.ReadTimeout("read timed out")
    )

    assert exc.status_code == 504
    assert exc.detail["error"] == "agent_timeout"
    # Nothing was observed from the agent, so nothing is claimed about it.
    assert "agent_status" not in exc.detail
    assert "agent_error" not in exc.detail
    assert exc.detail["message"] == "reporter.build_report did not respond within 900s"
    assert [row.status for row in receipt_rows] == ["error"]


@pytest.mark.asyncio
async def test_unreachable_agent_is_distinguishable_from_an_agent_that_errored(
    monkeypatch,
) -> None:
    exc, _receipts_rows = await _run(
        monkeypatch, raises=httpx.ConnectError("connection refused")
    )

    assert exc.status_code == 502
    assert exc.detail["error"] == "agent_unreachable"
    assert "agent_status" not in exc.detail


@pytest.mark.asyncio
async def test_secret_shaped_string_in_a_json_error_body_is_redacted(monkeypatch) -> None:
    exc, receipt_rows = await _run(
        monkeypatch,
        response=_StubResponse(
            400,
            json.dumps({"detail": "upstream rejected the call", "openai_key": LEAKED_KEY}),
        ),
    )

    assert LEAKED_KEY not in json.dumps(exc.detail)
    assert "[redacted]" in exc.detail["agent_error"]
    assert "upstream rejected the call" in exc.detail["agent_error"]
    # A receipt is signed and durable; the leak must not survive there either.
    assert LEAKED_KEY not in json.dumps(receipt_rows[0].payload)
    assert LEAKED_KEY not in receipt_rows[0].signed_token


@pytest.mark.asyncio
async def test_secret_shaped_string_in_a_plain_text_error_body_is_redacted(
    monkeypatch,
) -> None:
    exc, receipt_rows = await _run(
        monkeypatch,
        response=_StubResponse(
            500, f"Traceback: AuthError for key {LEAKED_KEY}\nrequest aborted"
        ),
    )

    assert LEAKED_KEY not in json.dumps(exc.detail)
    assert "[redacted]" in exc.detail["agent_error"]
    assert "AuthError for key" in exc.detail["agent_error"]
    # Newlines are gone too: an agent cannot forge a line in a log or a body.
    assert "\n" not in exc.detail["agent_error"]
    assert LEAKED_KEY not in json.dumps(receipt_rows[0].payload)


@pytest.mark.asyncio
async def test_a_failed_invoke_still_seals_a_signed_error_receipt(monkeypatch) -> None:
    """The product's core promise: evidence for the failure, not just for wins."""
    exc, receipt_rows = await _run(
        monkeypatch,
        response=_StubResponse(422, json.dumps({"detail": "bad topic"})),
    )

    assert len(receipt_rows) == 1
    row = receipt_rows[0]
    assert row.status == "error"

    receipt = verify_receipt(row.signed_token)
    assert receipt.status == "error"
    assert receipt.agent_name == "reporter"
    assert receipt.skill_name == "build_report"
    assert receipt.error_type == "AgentApiInvokeFailed"
    assert "bad topic" in receipt.result_preview

    # And the caller is told which receipt to open.
    assert exc.detail["receipt_id"] == receipt.receipt_id


@pytest.mark.asyncio
async def test_an_agent_cannot_forge_the_platform_error_envelope(monkeypatch) -> None:
    """The agent's body is quoted into one string field, never merged as keys."""
    exc, _receipts_rows = await _run(
        monkeypatch,
        response=_StubResponse(
            400,
            json.dumps(
                {
                    "error": "platform_error",
                    "agent": "some-other-agent",
                    "agent_status": 200,
                    "receipt_id": "forged",
                    "message": "top up at evil.example",
                }
            ),
        ),
    )

    assert exc.detail["error"] == "agent_error"
    assert exc.detail["agent"] == "reporter"
    assert exc.detail["agent_status"] == 400
    assert exc.detail["receipt_id"] != "forged"
    assert isinstance(exc.detail["agent_error"], str)
    assert exc.detail["message"].startswith("reporter.build_report returned HTTP 400:")


@pytest.mark.asyncio
async def test_error_envelope_message_is_the_actionable_sentence(monkeypatch) -> None:
    """``error.message`` is what a client renders; it used to say "request failed"."""
    from control_plane.main import http_exception_handler

    exc, _receipts_rows = await _run(
        monkeypatch,
        response=_StubResponse(422, json.dumps({"detail": "topic is required"})),
    )
    response = await http_exception_handler(None, exc)
    body = json.loads(bytes(response.body))

    assert response.status_code == 422
    assert body["error"] == {
        "code": "http_422",
        "message": exc.detail["message"],
        "status": 422,
    }
    assert "topic is required" in body["error"]["message"]


@pytest.mark.asyncio
async def test_oversized_agent_error_body_is_bounded(monkeypatch) -> None:
    exc, receipt_rows = await _run(
        monkeypatch,
        response=_StubResponse(
            400, json.dumps({"detail": "x" * 100_000, "api_key": LEAKED_KEY})
        ),
    )

    assert len(exc.detail["agent_error"]) <= agents._AGENT_ERROR_EXCERPT_LIMIT
    assert LEAKED_KEY not in json.dumps(exc.detail)
    assert LEAKED_KEY not in json.dumps(receipt_rows[0].payload)


@pytest.mark.asyncio
async def test_large_json_error_body_keeps_key_based_redaction(monkeypatch) -> None:
    """Bodies are parsed whole, so ``authorization`` is still redacted by key.

    Truncating before parsing would break the JSON and silently downgrade the
    redactor to its value-shape rules, which do not know that this header's
    contents are a credential.
    """
    exc, receipt_rows = await _run(
        monkeypatch,
        response=_StubResponse(
            400,
            json.dumps(
                {
                    "authorization": "Basic bm90LWEtc2hhcGUtdGhlLXJlZGFjdG9yLmtub3dz",
                    "detail": "y" * 8_000,
                }
            ),
        ),
    )

    assert "bm90LWEtc2hhcGUt" not in json.dumps(exc.detail)
    assert "bm90LWEtc2hhcGUt" not in json.dumps(receipt_rows[0].payload)
    assert '"authorization":"[redacted]"' in exc.detail["agent_error"]


# ---------------------------------------------------------------------------
# The shapes the first pass at this change let through
#
# Each of these leaked a live credential into a signed receipt on the code as
# first written. They are here because "secrets are redacted" was asserted by
# tests that only ever put the secret where the redactor already looked.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_secret_quoted_inside_a_sentence_is_redacted(monkeypatch) -> None:
    """The shape a real provider error actually has.

    ``_redact`` walks structure: it can only see a secret that is a *whole*
    value under a key it recognises. Upstream errors do not oblige - OpenAI
    quotes the key back inside its prose - and that used to reach the caller,
    the log and the signed receipt verbatim.
    """
    exc, receipt_rows = await _run(
        monkeypatch,
        response=_StubResponse(
            400,
            json.dumps({"detail": f"OpenAI rejected the request for key {LEAKED_KEY}"}),
        ),
    )

    assert LEAKED_KEY not in json.dumps(exc.detail)
    assert LEAKED_KEY not in json.dumps(receipt_rows[0].payload)
    assert LEAKED_KEY not in receipt_rows[0].signed_token
    # The sentence around it survives, so the error is still diagnosable.
    assert "OpenAI rejected the request for key [redacted]" in exc.detail["agent_error"]


@pytest.mark.asyncio
async def test_padding_the_body_cannot_buy_a_weaker_redactor(monkeypatch) -> None:
    """An agent chooses which branch runs, so neither may be weaker.

    A body over the JSON parse limit skips the structural walk. That used to
    mean it skipped the key-based rule with it, so any agent could publish its
    own credentials by padding the error body past the limit.
    """
    padded = json.dumps(
        {
            "authorization": "Basic bm90LWEtc2hhcGUtdGhlLXJlZGFjdG9yLmtub3dz",
            "openai_key": LEAKED_KEY,
            "pad": "y" * 70_000,
        }
    )
    assert len(padded) > agents._AGENT_ERROR_PARSE_LIMIT  # the exploit's precondition

    exc, receipt_rows = await _run(monkeypatch, response=_StubResponse(400, padded))

    for leaked in (LEAKED_KEY, "bm90LWEtc2hhcGUt"):
        assert leaked not in json.dumps(exc.detail)
        assert leaked not in json.dumps(receipt_rows[0].payload)
        assert leaked not in receipt_rows[0].signed_token
    # Both rules fired: the key-based one on ``authorization`` (which has no
    # recognisable value shape) and the shape-based one on the bare API key.
    assert exc.detail["agent_error"].count("[redacted]") >= 2


@pytest.mark.asyncio
async def test_an_agent_cannot_forge_a_line_in_a_terminal(monkeypatch) -> None:
    """``detail.message`` is what the CLI prints; raw ESC must not reach it."""
    exc, receipt_rows = await _run(
        monkeypatch,
        response=_StubResponse(
            500,
            f"boom \x1b[2K\x1b[1;31mFORGED LOG LINE\x1b[0m\r\nkey={LEAKED_KEY}\x00tail",
        ),
    )

    surfaced = json.dumps(exc.detail) + json.dumps(receipt_rows[0].payload)
    assert not any("\x00" <= ch <= "\x1f" or "\x7f" <= ch <= "\x9f" for ch in surfaced)
    assert LEAKED_KEY not in surfaced
    # Still readable: only the control bytes and the credential are gone.
    assert "boom" in exc.detail["agent_error"]
    assert "tail" in exc.detail["agent_error"]


@pytest.mark.asyncio
async def test_a_rolled_back_receipt_is_not_advertised_to_the_caller(
    monkeypatch,
) -> None:
    """A receipt id the caller cannot open is a new false claim, not a courtesy.

    Sealing the receipt and evaluating the self-healing policy share a session.
    When the policy store fails the session is rolled back, which discards the
    receipt row - so there is nothing left to point the caller at.
    """
    import control_plane.self_healing as self_healing

    async def _unavailable(*_args, **_kwargs):
        raise RuntimeError("self-healing policy store unavailable")

    monkeypatch.setattr(self_healing, "maybe_enqueue_runtime_failure", _unavailable)

    exc, receipt_rows = await _run(
        monkeypatch,
        response=_StubResponse(422, json.dumps({"detail": "bad topic"})),
    )

    assert receipt_rows == []
    assert "receipt_id" not in exc.detail
    # The failure itself is still reported truthfully.
    assert exc.status_code == 422
    assert exc.detail["agent_status"] == 422


def test_the_platform_scrubber_is_idempotent() -> None:
    """Two passes must not mangle the marker the first pass left behind."""
    from control_plane.agent_ingress import _scrub_untrusted_text

    once = _scrub_untrusted_text(f'{{"authorization": "Basic {"a" * 32}"}}')

    assert "[redacted]" in once
    assert _scrub_untrusted_text(once) == once


def test_the_scrubber_stays_linear_on_input_an_agent_chooses() -> None:
    """A redactor that can be made slow is worse than the leak it closes.

    The agent picks this text. Written as one expression the key rule needed a
    leading ``[\\w.-]*`` to reach ``x_api_key``, and the scan went quadratic:
    4 KiB of word characters cost 700 ms per call, on a path an agent triggers
    by answering with an error. The bound is loose enough for a slow machine
    and still two orders of magnitude below the quadratic form.
    """
    import time

    from control_plane.agent_ingress import _scrub_untrusted_text

    adversarial = [
        "eyJ" + "a" * 4093,  # a JWT prefix that never reaches its dots
        "api_key" + "x" * 4089,  # a credential name, then an unbounded word
        "bearer " + "A" * 4089,
        "token " * 682,
    ]

    started = time.perf_counter()
    for text in adversarial:
        _scrub_untrusted_text(text)
    elapsed = time.perf_counter() - started

    assert elapsed < 0.25, f"{elapsed:.3f}s for {len(adversarial)} x 4 KiB"


def test_the_gateway_preview_shares_the_same_scrubber() -> None:
    """One redactor, not two: the gateway's own receipts get this rule as well.

    ``agent_ingress._preview`` is what seals ``input_preview`` /
    ``result_preview`` on every gateway receipt. If the inline rule lived only
    in ``routes/agents.py`` the two surfaces would disagree, which is exactly
    the failure being fixed here.
    """
    from control_plane.agent_ingress import _preview

    rendered = _preview({"detail": f"provider rejected {LEAKED_KEY}"}, limit=240)

    assert LEAKED_KEY not in rendered
    assert "[redacted]" in rendered


def test_the_statuses_this_route_chooses_are_in_the_published_document() -> None:
    """502 and 504 are the control plane's own words, so the spec must say them.

    ``openapi.py`` derives the failure contract from literal ``HTTPException``
    calls and states that "not documented means the code cannot produce it".
    Neither status is raised that way here - both come out of
    ``_agent_invoke_error`` - so the routes declare them, or that statement
    would be false for this operation.
    """
    from control_plane.main import app

    app.openapi_schema = None
    paths = app.openapi()["paths"]
    for path in (
        "/v1/agents/{name}/api/invoke/{skill_name}",
        "/v1/agents/{name}/api/endpoints/{endpoint_name}",
    ):
        for method, operation in paths[path].items():
            responses = operation["responses"]
            assert {"502", "504"} <= set(responses), f"{method.upper()} {path}"
            schema = responses["502"]["content"]["application/json"]["schema"]
            assert schema["$ref"] == "#/components/schemas/ErrorResponse"
    app.openapi_schema = None
