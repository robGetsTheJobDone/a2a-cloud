from __future__ import annotations

import os

os.environ.setdefault("A2A_CP_DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from cryptography.fernet import Fernet

os.environ.setdefault("A2A_CP_LLM_CREDS_KEY", Fernet.generate_key().decode("ascii"))

from datetime import UTC, datetime, timedelta

import pytest
from a2a_pack import AgentDsl
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from control_plane.auth import current_user
from control_plane.config import settings
from control_plane.db import Base, get_session
from control_plane import mail_ingress as mail_ingress_module
from control_plane.mail_ingress import (
    EMAIL_HANDLER_TAG,
    AgentEmailReply,
    MailIngressWorker,
    email_handler_skill,
    extract_reply_text,
    extract_task_reply_text,
    parse_inbound_email,
    reply_from_task,
    sender_allowed,
    task_of,
)
from control_plane.mailbox_provisioner import (
    MAIL_SECRET_KEYS,
    MailboxProvisioner,
)
from control_plane.mailbox_resources import (
    agent_mailbox_address,
    read_agent_mailbox_declaration_from_manifest,
    reconcile_agent_mailbox,
)
from control_plane.models import (
    Agent,
    AgentMailbox,
    ChatThread,
    ChatThreadEmailLink,
    ChatThreadMessage,
    User,
)
from control_plane.routes import mailboxes as mailboxes_routes
from control_plane.safe_http import SafeHTTPError, SafeHTTPResponse


async def _session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _user(session, *, pro: bool = False) -> User:
    user = User(email="dev@example.com", password_hash="x")
    session.add(user)
    await session.flush()
    return user


async def _agent(session, user: User, name: str = "mailer") -> Agent:
    agent = Agent(
        owner_id=user.id,
        name=name,
        description="",
        version="1.0.0",
        image=f"registry.example/{name}:latest",
        public=True,
        status="running",
        url=f"https://{name}.a2acloud.io",
        card={"skills": [{"name": "run"}]},
    )
    session.add(agent)
    await session.flush()
    return agent


# --- declaration parsing -------------------------------------------------


def test_control_plane_sdk_accepts_generated_mailbox_dsl() -> None:
    dsl = AgentDsl.model_validate(
        {
            "language": "python",
            "name": "mail-test",
            "description": "Mailbox DSL compatibility probe",
            "version": "0.1.0",
            "entrypoint": {"module": "agent", "class_name": "Agent"},
            "skills": [
                {
                    "name": "echo",
                    "description": "Echo text",
                    "handler": "echo",
                    "input_schema": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                    "output_schema": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                    },
                }
            ],
            "auth": {
                "model": "NoAuth",
                "strategy": "public",
                "principal_schema": {"type": "object"},
                "required": False,
            },
            "runtime": {"platform_resources": {"mailbox": True}},
        }
    )

    assert dsl.runtime.platform_resources.mailbox is True


def test_manifest_mailbox_true():
    declaration = read_agent_mailbox_declaration_from_manifest(
        {"resources": {"mailbox": True}}
    )
    assert declaration is not None
    assert declaration.enabled
    assert declaration.allowed_senders == []


def test_manifest_mailbox_mapping_with_allowlist():
    declaration = read_agent_mailbox_declaration_from_manifest(
        {"resources": {"mailbox": {"allowed_senders": ["Ops@Example.com "]}}}
    )
    assert declaration is not None
    assert declaration.allowed_senders == ["ops@example.com"]


def test_manifest_mailbox_absent_or_disabled():
    assert read_agent_mailbox_declaration_from_manifest({}) is None
    assert (
        read_agent_mailbox_declaration_from_manifest({"resources": {"mailbox": False}})
        is None
    )
    assert (
        read_agent_mailbox_declaration_from_manifest(
            {"resources": {"mailbox": {"enabled": False}}}
        )
        is None
    )


def test_manifest_mailbox_invalid_sender_rejected():
    with pytest.raises(ValueError):
        read_agent_mailbox_declaration_from_manifest(
            {"resources": {"mailbox": {"allowed_senders": ["not-an-email"]}}}
        )


# --- reconcile -----------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_creates_and_removes_mailbox():
    engine, Session = await _session()
    async with Session() as session:
        user = await _user(session, pro=True)
        agent = await _agent(session, user)
        declaration = read_agent_mailbox_declaration_from_manifest(
            {"resources": {"mailbox": {"allowed_senders": ["a@b.co"]}}}
        )
        mailbox = await reconcile_agent_mailbox(
            session, agent=agent, user=user, declaration=declaration
        )
        await session.commit()
        assert mailbox.status == "pending"
        assert mailbox.address == agent_mailbox_address(agent.name)
        assert mailbox.allowed_senders_json == ["a@b.co"]

        removed = await reconcile_agent_mailbox(
            session, agent=agent, user=user, declaration=None
        )
        await session.commit()
        assert removed.status == "removed"

        # Re-declaring revives the same row.
        revived = await reconcile_agent_mailbox(
            session, agent=agent, user=user, declaration=declaration
        )
        await session.commit()
        assert revived.id == mailbox.id
        assert revived.status == "pending"
    await engine.dispose()


# --- provisioner ---------------------------------------------------------


class _FakeMailu:
    def __init__(self) -> None:
        self.domains: list[str] = []
        self.users: dict[str, dict] = {}

    async def ensure_domain(self, name: str) -> None:
        self.domains.append(name)

    async def upsert_user(self, *, email, password, quota_bytes, enabled=True, comment=""):
        self.users[email] = {
            "password": password,
            "quota": quota_bytes,
            "enabled": enabled,
        }

    async def set_user_enabled(self, email: str, enabled: bool) -> None:
        self.users.setdefault(email, {})["enabled"] = enabled

    async def delete_user(self, email: str) -> None:
        self.users.pop(email, None)


def _fake_secret_store():
    written: dict[str, str] = {}
    deleted: list[str] = []

    def upsert(*, agent_name, key, value, owner_id):
        written[key] = value

    def delete(*, agent_name, key):
        deleted.append(key)

    return written, deleted, upsert, delete


@pytest.mark.asyncio
async def test_provisioner_provisions_pending_mailbox():
    engine, Session = await _session()
    async with Session() as session:
        user = await _user(session, pro=True)
        agent = await _agent(session, user)
        mailbox = AgentMailbox(
            agent_id=agent.id,
            user_id=user.id,
            agent_name=agent.name,
            address=agent_mailbox_address(agent.name),
            status="pending",
        )
        session.add(mailbox)
        await session.commit()
        mailbox_id = mailbox.id

    client = _FakeMailu()
    written, _deleted, upsert, delete = _fake_secret_store()
    provisioner = MailboxProvisioner(
        session_maker=Session, client=client, upsert_secret=upsert, delete_secret=delete
    )
    await provisioner.reconcile_once()

    async with Session() as session:
        row = await session.get(AgentMailbox, mailbox_id)
        assert row.status == "ready"
        assert row.password_ciphertext
    assert settings.agent_mail_domain in client.domains
    assert row.address in client.users
    assert set(written) == set(MAIL_SECRET_KEYS)
    assert written["A2A_MAIL_ADDRESS"] == row.address
    await engine.dispose()


@pytest.mark.asyncio
async def test_provisioner_reenables_disabled_mailbox():
    engine, Session = await _session()
    async with Session() as session:
        user = await _user(session)
        agent = await _agent(session, user)
        mailbox = AgentMailbox(
            agent_id=agent.id,
            user_id=user.id,
            agent_name=agent.name,
            address=agent_mailbox_address(agent.name),
            status="disabled",
            disabled_at=datetime.now(UTC),
            password_ciphertext="pw",
        )
        session.add(mailbox)
        await session.commit()
        mailbox_id = mailbox.id

    client = _FakeMailu()
    client.users[mailbox.address] = {"enabled": False}
    written, _deleted, upsert, delete = _fake_secret_store()
    provisioner = MailboxProvisioner(
        session_maker=Session, client=client, upsert_secret=upsert, delete_secret=delete
    )
    await provisioner.reconcile_once()
    async with Session() as session:
        row = await session.get(AgentMailbox, mailbox_id)
        assert row.status == "ready"
        assert client.users[row.address]["enabled"] is True
    await engine.dispose()


@pytest.mark.asyncio
async def test_provisioner_purges_disabled_after_retention():
    engine, Session = await _session()
    async with Session() as session:
        user = await _user(session, pro=False)
        agent = await _agent(session, user)
        mailbox = AgentMailbox(
            agent_id=agent.id,
            user_id=user.id,
            agent_name=agent.name,
            address=agent_mailbox_address(agent.name),
            status="disabled",
            disabled_at=datetime.now(UTC)
            - timedelta(days=settings.mailbox_disabled_retention_days + 1),
        )
        session.add(mailbox)
        await session.commit()

    client = _FakeMailu()
    client.users[agent_mailbox_address("mailer")] = {"enabled": False}
    _written, deleted, upsert, delete = _fake_secret_store()
    provisioner = MailboxProvisioner(
        session_maker=Session, client=client, upsert_secret=upsert, delete_secret=delete
    )
    await provisioner.reconcile_once()

    async with Session() as session:
        rows = (await session.execute(select(AgentMailbox))).scalars().all()
        assert rows == []
    assert client.users == {}
    assert set(deleted) == set(MAIL_SECRET_KEYS)
    await engine.dispose()


# --- routes ---------------------------------------------------------------


def _mailbox_client(Session, user: User) -> AsyncClient:
    app = FastAPI()
    app.include_router(mailboxes_routes.router)

    async def _override_session():
        async with Session() as session:
            yield session

    async def _override_current_user() -> User:
        return user

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[current_user] = _override_current_user
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_request_patch_delete_mailbox_pro():
    engine, Session = await _session()
    async with Session() as session:
        user = await _user(session, pro=True)
        await _agent(session, user)
        await session.commit()

    async with _mailbox_client(Session, user) as client:
        created = await client.post("/v1/agents/mailer/mailbox")
        assert created.status_code == 201
        body = created.json()
        assert body["status"] == "pending"
        assert body["address"] == agent_mailbox_address("mailer")

        patched = await client.patch(
            "/v1/agents/mailer/mailbox",
            json={"allowed_senders": ["Client@Corp.io"]},
        )
        assert patched.status_code == 200
        assert patched.json()["allowed_senders"] == ["client@corp.io"]

        deleted = await client.delete("/v1/agents/mailer/mailbox")
        assert deleted.status_code == 204
        gone = await client.get("/v1/agents/mailer/mailbox")
        assert gone.status_code == 404
    await engine.dispose()


# --- ingress helpers -------------------------------------------------------


_RAW_EMAIL = b"""From: Client <client@corp.io>\r
To: mailer@agents.a2acloud.io\r
Subject: Need pricing\r
Message-ID: <root@corp.io>\r
Content-Type: text/plain\r
\r
How much for 100 seats?\r
"""


def test_parse_inbound_email_thread_key_root():
    email = parse_inbound_email(7, _RAW_EMAIL)
    assert email.sender == "client@corp.io"
    assert email.subject == "Need pricing"
    assert email.thread_key == "<root@corp.io>"
    assert not email.auto_submitted
    assert "100 seats" in email.body


def test_parse_inbound_email_reply_uses_references_root():
    raw = _RAW_EMAIL.replace(
        b"Message-ID: <root@corp.io>\r\n",
        b"Message-ID: <reply-2@corp.io>\r\nReferences: <root@corp.io> <reply-1@agents.a2acloud.io>\r\n",
    )
    email = parse_inbound_email(8, raw)
    assert email.thread_key == "<root@corp.io>"


def test_sender_allowed_rules():
    mailbox = AgentMailbox(allowed_senders_json=["client@corp.io"])
    assert sender_allowed(mailbox, "owner@x.io", "owner@x.io")
    assert sender_allowed(mailbox, "owner@x.io", "client@corp.io")
    assert not sender_allowed(mailbox, "owner@x.io", "stranger@spam.io")
    # own-domain senders never open threads (loop guard)
    assert not sender_allowed(
        mailbox, "owner@x.io", f"other@{settings.agent_mail_domain}"
    )
    assert not sender_allowed(mailbox, None, "")


def test_extract_reply_text_from_message_result():
    result = {"message": {"parts": [{"text": "Final answer"}]}}
    assert "Final answer" in extract_reply_text(result)


def test_extract_task_reply_prefers_artifacts_and_skips_user_history():
    task = {
        "kind": "task",
        "status": {"state": "TASK_STATE_COMPLETED"},
        "history": [
            {"role": "ROLE_USER", "parts": [{"text": "inbound email body"}]},
            {"role": "ROLE_AGENT", "parts": [{"text": "agent history answer"}]},
        ],
        "artifacts": [{"parts": [{"text": "artifact answer"}]}],
    }
    assert extract_task_reply_text(task) == "artifact answer"
    task["artifacts"] = []
    assert extract_task_reply_text(task) == "agent history answer"
    # User history must never leak back out as the reply.
    task["history"] = [{"role": "ROLE_USER", "parts": [{"text": "inbound email body"}]}]
    assert extract_task_reply_text(task) == ""


def test_task_of_shapes():
    assert task_of({"kind": "task", "id": "t1"}) == {"kind": "task", "id": "t1"}
    assert task_of({"task": {"kind": "task", "id": "t2"}}) == {"kind": "task", "id": "t2"}
    assert task_of({"message": {}}) is None
    assert task_of("text") is None


def test_email_handler_skill_from_card():
    card = {
        "skills": [
            {"id": "greet", "tags": ["demo"]},
            {"id": "handle_email", "tags": ["demo", EMAIL_HANDLER_TAG]},
        ]
    }
    assert email_handler_skill(card) == "handle_email"
    assert email_handler_skill({"skills": [{"id": "greet"}]}) is None
    assert email_handler_skill(None) is None


def test_reply_from_task_structured_and_text():
    structured = {
        "kind": "task",
        "status": {"state": "TASK_STATE_COMPLETED"},
        "artifacts": [
            {"parts": [{"data": {"body": "Structured answer", "subject": "Quote #42"}}]}
        ],
    }
    reply = reply_from_task(structured)
    assert reply == AgentEmailReply(body="Structured answer", subject="Quote #42")

    text_task = {
        "kind": "task",
        "status": {"state": "TASK_STATE_COMPLETED"},
        "artifacts": [{"parts": [{"text": "plain answer"}]}],
    }
    assert reply_from_task(text_task) == AgentEmailReply(body="plain answer")
    assert reply_from_task({"kind": "task", "status": {}, "artifacts": []}) is None


def test_inbound_email_payload_shape_with_attachment():
    raw = (
        b"From: Client <client@corp.io>\r\n"
        b"To: mailer@agents.a2acloud.io\r\n"
        b"Subject: With attachment\r\n"
        b"Message-ID: <att@corp.io>\r\n"
        b"Date: Thu, 10 Jul 2026 12:00:00 +0000\r\n"
        b'Content-Type: multipart/mixed; boundary="B"\r\n'
        b"\r\n"
        b"--B\r\n"
        b"Content-Type: text/plain\r\n"
        b"\r\n"
        b"see attached\r\n"
        b"--B\r\n"
        b"Content-Type: text/csv\r\n"
        b'Content-Disposition: attachment; filename="data.csv"\r\n'
        b"\r\n"
        b"a,b\r\n1,2\r\n"
        b"--B--\r\n"
    )
    email = parse_inbound_email(3, raw)
    payload = email.payload()
    assert payload["sender"] == "client@corp.io"
    assert payload["subject"] == "With attachment"
    assert payload["message_id"] == "<att@corp.io>"
    assert payload["date"] is not None
    assert len(payload["attachments"]) == 1
    attachment = payload["attachments"][0]
    assert attachment["filename"] == "data.csv"
    assert attachment["content_type"] == "text/csv"
    assert attachment["size_bytes"] > 0
    assert attachment["content_b64"]


def _mk_agent(card: dict) -> Agent:
    return Agent(
        owner_id=1,
        name="mailer",
        description="",
        version="1.0.0",
        image="x",
        public=True,
        status="running",
        url="https://mailer.a2acloud.io",
        card=card,
    )


@pytest.mark.asyncio
async def test_invoke_uses_structured_skill_call_when_handler_declared(monkeypatch):
    calls: list[tuple[str, str, dict, dict | None]] = []

    async def fake_invoke(base_url, agent_name, skill, arguments, headers, cp_jwt=None):
        calls.append((agent_name, skill, arguments, headers))
        return {"body": "handled", "subject": "Re: hi"}

    monkeypatch.setattr(mail_ingress_module, "_post_invoke", fake_invoke)
    agent = _mk_agent({"skills": [{"id": "handle_email", "tags": [EMAIL_HANDLER_TAG]}]})
    email = parse_inbound_email(7, _RAW_EMAIL)
    reply = await mail_ingress_module._invoke_agent_with_email(agent, email)
    assert reply == AgentEmailReply(body="handled", subject="Re: hi")
    agent_name, skill, arguments, headers = calls[0]
    assert (agent_name, skill) == ("mailer", "handle_email")
    assert arguments["email"]["subject"] == "Need pricing"
    assert arguments["email"]["body"].startswith("How much")
    assert headers and headers["Authorization"].startswith("Bearer ")


@pytest.mark.asyncio
async def test_invoke_uses_single_text_skill_without_handler(monkeypatch):
    calls: list[tuple[str, dict]] = []

    async def fake_manifest(base_url):
        return {
            "skills": [
                {
                    "id": "ask",
                    "input_schema": {
                        "type": "object",
                        "properties": {"prompt": {"type": "string"}},
                        "required": ["prompt"],
                    },
                }
            ]
        }

    async def fake_invoke(base_url, agent_name, skill, arguments, headers, cp_jwt=None):
        calls.append((skill, arguments))
        return "the answer"

    monkeypatch.setattr(mail_ingress_module, "_fetch_skills_manifest", fake_manifest)
    monkeypatch.setattr(mail_ingress_module, "_post_invoke", fake_invoke)
    agent = _mk_agent({"skills": [{"id": "ask", "tags": []}]})
    email = parse_inbound_email(7, _RAW_EMAIL)
    reply = await mail_ingress_module._invoke_agent_with_email(agent, email)
    assert reply == AgentEmailReply(body="the answer")
    skill, arguments = calls[0]
    assert skill == "ask"
    assert "New email:" in arguments["prompt"]
    assert "You are the agent behind" in arguments["prompt"]


@pytest.mark.asyncio
async def test_invoke_falls_back_to_message_send(monkeypatch):
    calls: list[tuple[str, dict]] = []

    async def fake_manifest(base_url):
        return None

    async def fake_rpc(base_url, method, params, headers=None):
        calls.append((method, params))
        return {"message": {"parts": [{"text": "plain reply"}]}}

    monkeypatch.setattr(mail_ingress_module, "_fetch_skills_manifest", fake_manifest)
    monkeypatch.setattr(mail_ingress_module, "_a2a_rpc", fake_rpc)
    agent = _mk_agent({"skills": [{"id": "a", "tags": []}, {"id": "b", "tags": []}]})
    email = parse_inbound_email(7, _RAW_EMAIL)
    reply = await mail_ingress_module._invoke_agent_with_email(agent, email)
    assert reply == AgentEmailReply(body="plain reply")
    part = calls[0][1]["message"]["parts"][0]
    assert "New email:" in part["text"]


@pytest.mark.asyncio
async def test_fetch_skills_manifest_retries_after_cold_start_failure(monkeypatch):
    calls = 0

    async def fake_fetch(url, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise SafeHTTPError("cold start")
        return SafeHTTPResponse(
            status_code=200,
            url=url,
            headers={"content-type": "application/json"},
            content=b'{"skills": []}',
        )

    monkeypatch.setattr(mail_ingress_module, "safe_fetch_url", fake_fetch)
    manifest = await mail_ingress_module._fetch_skills_manifest(
        "https://mailer.a2acloud.io"
    )
    assert manifest == {"skills": []}
    assert calls == 2


def test_single_text_skill_selection():
    ask = {
        "skills": [
            {
                "id": "ask",
                "input_schema": {
                    "properties": {"prompt": {"type": "string"}},
                    "required": ["prompt"],
                },
            }
        ]
    }
    assert mail_ingress_module.single_text_skill(ask) == ("ask", "prompt")
    # two skills → no guess
    two = {"skills": [ask["skills"][0], {"id": "other", "input_schema": {}}]}
    assert mail_ingress_module.single_text_skill(two) is None
    # multi-param → no guess
    multi = {
        "skills": [
            {
                "id": "ask",
                "input_schema": {
                    "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
                    "required": ["a", "b"],
                },
            }
        ]
    }
    assert mail_ingress_module.single_text_skill(multi) is None
    assert mail_ingress_module.single_text_skill(None) is None


# --- ingress bridge --------------------------------------------------------


@pytest.mark.asyncio
async def test_ingress_bridges_email_to_thread_and_replies(monkeypatch):
    engine, Session = await _session()
    async with Session() as session:
        user = await _user(session, pro=True)
        agent = await _agent(session, user)
        mailbox = AgentMailbox(
            agent_id=agent.id,
            user_id=user.id,
            agent_name=agent.name,
            address=agent_mailbox_address(agent.name),
            status="ready",
            password_ciphertext="pw",
            allowed_senders_json=["client@corp.io"],
        )
        session.add(mailbox)
        await session.commit()
        mailbox_id = mailbox.id

    sent: list[dict] = []

    def fake_fetch(*, address, password, last_uid):
        if last_uid >= 7:
            return []
        return [(7, _RAW_EMAIL)]

    async def fake_invoke(agent, email, history=None):
        return AgentEmailReply(body="We charge $5/seat.")

    def fake_send(**kwargs):
        sent.append(kwargs)
        return "<reply@agents.a2acloud.io>"

    monkeypatch.setattr(mail_ingress_module, "_fetch_new_messages_sync", fake_fetch)
    monkeypatch.setattr(mail_ingress_module, "_invoke_agent_with_email", fake_invoke)
    monkeypatch.setattr(mail_ingress_module, "_send_reply_sync", fake_send)

    worker = MailIngressWorker(session_maker=Session)
    await worker.poll_once()

    async with Session() as session:
        thread = (await session.execute(select(ChatThread))).scalar_one()
        assert thread.title == "Need pricing"
        assert thread.settings_json["source"] == "email"
        link = (await session.execute(select(ChatThreadEmailLink))).scalar_one()
        assert link.thread_key == "<root@corp.io>"
        assert link.remote_address == "client@corp.io"
        messages = (
            (
                await session.execute(
                    select(ChatThreadMessage).order_by(ChatThreadMessage.id)
                )
            )
            .scalars()
            .all()
        )
        assert [m.role for m in messages] == ["user", "assistant"]
        assert "100 seats" in messages[0].content
        assert "$5/seat" in messages[1].content
        row = await session.get(AgentMailbox, mailbox_id)
        assert row.last_imap_uid == 7
        assert row.outbound_count == 1
    assert len(sent) == 1
    assert sent[0]["to"] == "client@corp.io"
    assert sent[0]["in_reply_to"] == "<root@corp.io>"

    # Second poll: watermark prevents re-processing.
    await worker.poll_once()
    async with Session() as session:
        count = len((await session.execute(select(ChatThreadMessage))).scalars().all())
    assert count == 2

    # A second worker (concurrent process) also cannot double-bridge, even
    # when its IMAP fetch raced ahead of the watermark: the claim UPDATE
    # matches zero rows.
    monkeypatch.setattr(
        mail_ingress_module,
        "_fetch_new_messages_sync",
        lambda *, address, password, last_uid: [(7, _RAW_EMAIL)],
    )
    other = MailIngressWorker(session_maker=Session)
    await other.poll_once()
    async with Session() as session:
        count = len((await session.execute(select(ChatThreadMessage))).scalars().all())
    assert count == 2
    await engine.dispose()


@pytest.mark.asyncio
async def test_ingress_skips_unlisted_sender_and_auto_submitted(monkeypatch):
    engine, Session = await _session()
    async with Session() as session:
        user = await _user(session, pro=True)
        agent = await _agent(session, user)
        session.add(
            AgentMailbox(
                agent_id=agent.id,
                user_id=user.id,
                agent_name=agent.name,
                address=agent_mailbox_address(agent.name),
                status="ready",
                password_ciphertext="pw",
            )
        )
        await session.commit()

    stranger = _RAW_EMAIL.replace(b"client@corp.io", b"stranger@spam.io")
    auto = _RAW_EMAIL.replace(
        b"Content-Type: text/plain\r\n",
        b"Content-Type: text/plain\r\nAuto-Submitted: auto-replied\r\n",
    )

    def fake_fetch(*, address, password, last_uid):
        return [(uid, raw) for uid, raw in [(1, stranger), (2, auto)] if uid > last_uid]

    async def fail_invoke(agent, email):  # pragma: no cover - must not run
        raise AssertionError("agent should not be invoked for skipped mail")

    monkeypatch.setattr(mail_ingress_module, "_fetch_new_messages_sync", fake_fetch)
    monkeypatch.setattr(mail_ingress_module, "_invoke_agent_with_email", fail_invoke)

    worker = MailIngressWorker(session_maker=Session)
    await worker.poll_once()

    async with Session() as session:
        assert (await session.execute(select(ChatThread))).scalars().all() == []
        row = (await session.execute(select(AgentMailbox))).scalar_one()
        assert row.last_imap_uid == 2
    await engine.dispose()


@pytest.mark.asyncio
async def test_ingress_passes_thread_history_on_follow_up(monkeypatch):
    engine, Session = await _session()
    async with Session() as session:
        user = await _user(session, pro=True)
        agent = await _agent(session, user)
        session.add(
            AgentMailbox(
                agent_id=agent.id,
                user_id=user.id,
                agent_name=agent.name,
                address=agent_mailbox_address(agent.name),
                status="ready",
                password_ciphertext="pw",
                allowed_senders_json=["client@corp.io"],
            )
        )
        await session.commit()

    followup = _RAW_EMAIL.replace(
        b"Message-ID: <root@corp.io>\r\n",
        b"Message-ID: <reply-2@corp.io>\r\nReferences: <root@corp.io>\r\n",
    ).replace(b"How much for 100 seats?", b"And do you offer annual billing?")

    batches = {1: [(1, _RAW_EMAIL)], 2: [(2, followup)]}
    poll = {"n": 0}

    def fake_fetch(*, address, password, last_uid):
        poll["n"] += 1
        return [m for m in batches.get(poll["n"], []) if m[0] > last_uid]

    histories: list[list | None] = []

    async def fake_invoke(agent, email, history=None):
        histories.append(history)
        return AgentEmailReply(body=f"answer-{email.uid}")

    def fake_send(**kwargs):
        return f"<agent-reply-{kwargs['in_reply_to']}@agents.a2acloud.io>"

    monkeypatch.setattr(mail_ingress_module, "_fetch_new_messages_sync", fake_fetch)
    monkeypatch.setattr(mail_ingress_module, "_invoke_agent_with_email", fake_invoke)
    monkeypatch.setattr(mail_ingress_module, "_send_reply_sync", fake_send)

    worker = MailIngressWorker(session_maker=Session)
    await worker.poll_once()
    await worker.poll_once()

    assert len(histories) == 2
    assert histories[0] == []  # first email: nothing before it
    second = histories[1]
    assert [m["role"] for m in second] == ["user", "assistant"]
    assert "100 seats" in second[0]["content"]
    assert second[1]["content"] == "answer-1"
    # Both landed in the SAME thread via the References root.
    async with Session() as session:
        links = (await session.execute(select(ChatThreadEmailLink))).scalars().all()
        assert len(links) == 1
        messages = (
            (await session.execute(select(ChatThreadMessage))).scalars().all()
        )
        assert len(messages) == 4  # user, assistant, user, assistant
    await engine.dispose()


def test_rendered_email_prompt_frames_recipient_and_history():
    email = parse_inbound_email(7, _RAW_EMAIL)
    prompt = mail_ingress_module._rendered_email_prompt(
        "mailer",
        email,
        [
            {"role": "user", "content": "earlier question"},
            {"role": "assistant", "content": "earlier answer"},
        ],
    )
    assert "You are the agent behind the email address mailer@" in prompt
    assert "do not offer to draft" in prompt
    assert "[client@corp.io] earlier question" in prompt
    assert "[you] earlier answer" in prompt
    assert prompt.index("earlier question") < prompt.index("New email:")


@pytest.mark.asyncio
async def test_ingress_rate_limits_outbound(monkeypatch):
    engine, Session = await _session()
    async with Session() as session:
        user = await _user(session, pro=True)
        agent = await _agent(session, user)
        session.add(
            AgentMailbox(
                agent_id=agent.id,
                user_id=user.id,
                agent_name=agent.name,
                address=agent_mailbox_address(agent.name),
                status="ready",
                password_ciphertext="pw",
                allowed_senders_json=["client@corp.io"],
                outbound_count=settings.agent_mail_daily_send_limit,
                outbound_period_start=datetime.now(UTC).replace(
                    hour=0, minute=0, second=0, microsecond=0
                ),
            )
        )
        await session.commit()

    sent: list[dict] = []

    def fake_fetch(*, address, password, last_uid):
        return [(1, _RAW_EMAIL)] if last_uid < 1 else []

    async def fake_invoke(agent, email, history=None):
        return AgentEmailReply(body="reply")

    def fake_send(**kwargs):  # pragma: no cover - limit must block first
        sent.append(kwargs)
        return "<x@agents.a2acloud.io>"

    monkeypatch.setattr(mail_ingress_module, "_fetch_new_messages_sync", fake_fetch)
    monkeypatch.setattr(mail_ingress_module, "_invoke_agent_with_email", fake_invoke)
    monkeypatch.setattr(mail_ingress_module, "_send_reply_sync", fake_send)

    worker = MailIngressWorker(session_maker=Session)
    await worker.poll_once()

    assert sent == []
    async with Session() as session:
        # Reply still lands in the thread even when the email send is capped.
        messages = (
            (await session.execute(select(ChatThreadMessage))).scalars().all()
        )
        assert [m.role for m in messages] == ["user", "assistant"]
    await engine.dispose()
