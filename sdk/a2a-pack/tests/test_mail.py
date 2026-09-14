from __future__ import annotations

from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Any

import pytest

from a2a_pack import (
    A2AAgent,
    AgentMailbox,
    EMAIL_HANDLER_TAG,
    InboundEmailPayload,
    LocalRunContext,
    NoAuth,
    RunContext,
    compile_agent_to_dsl,
    skill,
    tool,
)
from a2a_pack.mail import _extract_body, _reply_subject, _strip_html

import a2a_pack.mail as mail_module


def _raw_message(
    *,
    sender: str = "Alice <alice@example.com>",
    subject: str = "Hello agent",
    body: str | None = "plain body",
    html: str | None = None,
    message_id: str | None = None,
    references: str | None = None,
    reply_to: str | None = None,
) -> bytes:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = "helper@agents.example.io"
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = message_id or make_msgid(domain="example.com")
    if references:
        msg["References"] = references
    if reply_to:
        msg["Reply-To"] = reply_to
    if body is not None:
        msg.set_content(body)
        if html is not None:
            msg.add_alternative(html, subtype="html")
    elif html is not None:
        msg.set_content(html, subtype="html")
    return bytes(msg)


class FakeIMAP:
    """Stands in for imaplib.IMAP4_SSL; mailbox is a uid -> raw bytes map."""

    mailbox: dict[bytes, bytes] = {}
    unseen: tuple[bytes, ...] = ()
    instances: list["FakeIMAP"] = []

    def __init__(self, host: str, port: int, ssl_context: Any = None) -> None:
        self.host = host
        self.port = port
        self.ssl_context = ssl_context
        self.logged_in: tuple[str, str] | None = None
        self.selected: tuple[str, bool] | None = None
        self.search_criteria: list[str] = []
        self.logged_out = False
        type(self).instances.append(self)

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        self.logged_in = (user, password)
        return "OK", [b"logged in"]

    def select(self, box: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        self.selected = (box, readonly)
        return "OK", [b"%d" % len(self.mailbox)]

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
        command = command.upper()
        if command == "SEARCH":
            criterion = str(args[-1])
            self.search_criteria.append(criterion)
            uids = self.unseen if criterion == "UNSEEN" else tuple(self.mailbox)
            return "OK", [b" ".join(uids)]
        if command == "FETCH":
            uid = args[0].encode("ascii") if isinstance(args[0], str) else args[0]
            raw = self.mailbox[uid]
            header = b"1 (UID " + uid + b" BODY[] {%d}" % len(raw)
            return "OK", [(header, raw), b")"]
        raise AssertionError(f"unexpected IMAP command: {command}")

    def close(self) -> tuple[str, list[bytes]]:
        return "OK", [b"closed"]

    def logout(self) -> tuple[str, list[bytes]]:
        self.logged_out = True
        return "BYE", [b"bye"]


class FakeSMTP:
    """Stands in for smtplib.SMTP; records the STARTTLS/login/send dance."""

    instances: list["FakeSMTP"] = []

    def __init__(self, host: str, port: int, timeout: float | None = None) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.ehlo_called = False
        self.starttls_context: Any = None
        self.credentials: tuple[str, str] | None = None
        self.sent: list[EmailMessage] = []
        self.closed = False
        type(self).instances.append(self)

    def __enter__(self) -> "FakeSMTP":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.closed = True

    def ehlo(self) -> None:
        self.ehlo_called = True

    def starttls(self, context: Any = None) -> None:
        self.starttls_context = context

    def login(self, user: str, password: str) -> None:
        self.credentials = (user, password)

    def send_message(self, msg: EmailMessage) -> None:
        assert self.credentials is not None, "send before login"
        self.sent.append(msg)


@pytest.fixture()
def mail_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("A2A_MAIL_ADDRESS", "helper@agents.example.io")
    monkeypatch.setenv("A2A_MAIL_PASSWORD", "sekret")
    monkeypatch.setenv("A2A_MAIL_IMAP_HOST", "mailu-imap.mail.svc")
    monkeypatch.setenv("A2A_MAIL_IMAP_PORT", "993")
    monkeypatch.setenv("A2A_MAIL_SMTP_HOST", "mailu-smtp.mail.svc")
    monkeypatch.setenv("A2A_MAIL_SMTP_PORT", "587")
    monkeypatch.delenv("A2A_MAIL_TLS_VERIFY", raising=False)


@pytest.fixture()
def fake_imap(monkeypatch: pytest.MonkeyPatch) -> type[FakeIMAP]:
    FakeIMAP.mailbox = {}
    FakeIMAP.unseen = ()
    FakeIMAP.instances = []
    monkeypatch.setattr(mail_module.imaplib, "IMAP4_SSL", FakeIMAP)
    return FakeIMAP


@pytest.fixture()
def fake_smtp(monkeypatch: pytest.MonkeyPatch) -> type[FakeSMTP]:
    FakeSMTP.instances = []
    monkeypatch.setattr(mail_module.smtplib, "SMTP", FakeSMTP)
    return FakeSMTP


def test_from_env_returns_none_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("A2A_MAIL_ADDRESS", "A2A_MAIL_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    assert AgentMailbox.from_env() is None

    monkeypatch.setenv("A2A_MAIL_ADDRESS", "helper@agents.example.io")
    assert AgentMailbox.from_env() is None  # still no password


def test_from_env_builds_mailbox(mail_env: None) -> None:
    mailbox = AgentMailbox.from_env()
    assert mailbox is not None
    assert mailbox.address == "helper@agents.example.io"
    assert mailbox._imap_host == "mailu-imap.mail.svc"
    assert mailbox._imap_port == 993
    assert mailbox._smtp_host == "mailu-smtp.mail.svc"
    assert mailbox._smtp_port == 587
    # TLS verification defaults off: in-cluster cert never matches.
    assert mailbox._tls_verify is False


def test_from_env_tls_verify_flag(
    mail_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("A2A_MAIL_TLS_VERIFY", "true")
    mailbox = AgentMailbox.from_env()
    assert mailbox is not None and mailbox._tls_verify is True


def test_list_messages_newest_first(
    mail_env: None, fake_imap: type[FakeIMAP]
) -> None:
    fake_imap.mailbox = {
        b"1": _raw_message(subject="oldest", body="first  body\nhere"),
        b"2": _raw_message(subject="middle", body="second"),
        b"3": _raw_message(subject="newest", body="third"),
    }
    mailbox = AgentMailbox.from_env()
    assert mailbox is not None

    listed = mailbox.list_messages(limit=2)
    assert [m["uid"] for m in listed] == ["3", "2"]
    assert [m["subject"] for m in listed] == ["newest", "middle"]
    assert listed[0]["sender"] == "Alice <alice@example.com>"
    assert listed[0]["snippet"] == "third"
    assert listed[0]["date"]

    conn = fake_imap.instances[0]
    assert conn.logged_in == ("helper@agents.example.io", "sekret")
    assert conn.selected == ("INBOX", True)
    assert conn.search_criteria == ["ALL"]
    assert conn.logged_out is True


def test_list_messages_unseen_only(
    mail_env: None, fake_imap: type[FakeIMAP]
) -> None:
    fake_imap.mailbox = {
        b"1": _raw_message(subject="seen"),
        b"2": _raw_message(subject="fresh"),
    }
    fake_imap.unseen = (b"2",)
    mailbox = AgentMailbox.from_env()
    assert mailbox is not None

    listed = mailbox.list_messages(unseen_only=True)
    assert [m["uid"] for m in listed] == ["2"]
    assert fake_imap.instances[0].search_criteria == ["UNSEEN"]


def test_read_prefers_text_plain(mail_env: None, fake_imap: type[FakeIMAP]) -> None:
    fake_imap.mailbox = {
        b"7": _raw_message(
            body="the plain part",
            html="<p>the <b>html</b> part</p>",
        )
    }
    mailbox = AgentMailbox.from_env()
    assert mailbox is not None

    message = mailbox.read(7)
    assert message["uid"] == "7"
    assert message["sender"] == "Alice <alice@example.com>"
    assert message["subject"] == "Hello agent"
    assert message["body"].strip() == "the plain part"


def test_read_falls_back_to_stripped_html(
    mail_env: None, fake_imap: type[FakeIMAP]
) -> None:
    fake_imap.mailbox = {
        b"8": _raw_message(
            body=None,
            html="<html><head><style>p{}</style></head>"
            "<body><p>Invoice <b>attached</b></p></body></html>",
        )
    }
    mailbox = AgentMailbox.from_env()
    assert mailbox is not None

    body = mailbox.read("8")["body"]
    assert "Invoice" in body and "attached" in body
    assert "<" not in body and "style" not in body.lower()


def test_read_truncates_large_bodies(
    mail_env: None, fake_imap: type[FakeIMAP]
) -> None:
    fake_imap.mailbox = {b"9": _raw_message(body="x" * (300 * 1024))}
    mailbox = AgentMailbox.from_env()
    assert mailbox is not None
    assert len(mailbox.read(9)["body"]) <= 256 * 1024


def test_send_sets_identity_and_auto_submitted(
    mail_env: None, fake_smtp: type[FakeSMTP]
) -> None:
    mailbox = AgentMailbox.from_env()
    assert mailbox is not None

    message_id = mailbox.send("bob@example.com", "Status", "All green.")

    smtp = fake_smtp.instances[0]
    assert (smtp.host, smtp.port) == ("mailu-smtp.mail.svc", 587)
    assert smtp.ehlo_called is True
    assert smtp.starttls_context is not None
    # Default TLS posture: no hostname check against the in-cluster cert.
    assert smtp.starttls_context.check_hostname is False
    assert smtp.credentials == ("helper@agents.example.io", "sekret")
    assert smtp.closed is True

    (sent,) = smtp.sent
    assert sent["From"] == "helper@agents.example.io"
    assert sent["To"] == "bob@example.com"
    assert sent["Subject"] == "Status"
    assert sent["Auto-Submitted"] == "auto-generated"
    assert sent.get_content().strip() == "All green."
    assert message_id == sent["Message-ID"]
    assert message_id.startswith("<") and message_id.endswith(">")


def test_reply_threads_to_original_sender(
    mail_env: None, fake_imap: type[FakeIMAP], fake_smtp: type[FakeSMTP]
) -> None:
    original_id = "<orig-123@example.com>"
    fake_imap.mailbox = {
        b"4": _raw_message(
            sender="Alice <alice@example.com>",
            subject="Question about invoices",
            message_id=original_id,
            references="<thread-root@example.com>",
        )
    }
    mailbox = AgentMailbox.from_env()
    assert mailbox is not None

    message_id = mailbox.reply(4, "Answer inline.")

    (sent,) = fake_smtp.instances[0].sent
    assert sent["To"] == "Alice <alice@example.com>"
    assert sent["Subject"] == "Re: Question about invoices"
    assert sent["In-Reply-To"] == original_id
    assert sent["References"] == f"<thread-root@example.com> {original_id}"
    assert sent["Auto-Submitted"] == "auto-generated"
    assert message_id == sent["Message-ID"]


def test_reply_keeps_existing_re_prefix_and_honours_reply_to(
    mail_env: None, fake_imap: type[FakeIMAP], fake_smtp: type[FakeSMTP]
) -> None:
    fake_imap.mailbox = {
        b"5": _raw_message(
            subject="RE: ping",
            reply_to="ops@example.com",
        )
    }
    mailbox = AgentMailbox.from_env()
    assert mailbox is not None

    mailbox.reply("5", "pong")

    (sent,) = fake_smtp.instances[0].sent
    assert sent["To"] == "ops@example.com"
    assert sent["Subject"] == "RE: ping"


def test_helpers_strip_html_and_reply_subject() -> None:
    assert _strip_html("<p>Hi <b>there</b></p><script>alert(1)</script>") == "Hi there"
    assert _reply_subject("Hello") == "Re: Hello"
    assert _reply_subject("re: hello") == "re: hello"

    msg = EmailMessage()
    msg.set_content("plain wins")
    msg.add_alternative("<p>html loses</p>", subtype="html")
    assert _extract_body(msg).strip() == "plain wins"


def test_ctx_mail_property_is_cached(
    mail_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = LocalRunContext(auth=NoAuth())
    first = ctx.mail
    assert isinstance(first, AgentMailbox)
    assert first.address == "helper@agents.example.io"

    # Cached: mutating the env does not rebuild the mailbox.
    monkeypatch.setenv("A2A_MAIL_ADDRESS", "other@agents.example.io")
    assert ctx.mail is first


def test_ctx_mail_property_none_without_provisioning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("A2A_MAIL_ADDRESS", "A2A_MAIL_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    ctx = LocalRunContext(auth=NoAuth())
    assert ctx.mail is None


# --- on_email skill declaration (platform email handler) -----------------


def _payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "sender": "Alice <alice@example.com>",
        "subject": "Need help",
        "body": "please advise",
        "message_id": "<m1@example.com>",
        "date": "Thu, 09 Jul 2026 10:00:00 +0000",
        "references": [],
        "attachments": [
            {
                "filename": "big.pdf",
                "content_type": "application/pdf",
                "size_bytes": 10_000_000,
                "content_b64": None,
            }
        ],
    }
    base.update(overrides)
    return base


class _MailAgent(A2AAgent):
    name = "mail-agent"
    description = "Answers its inbox"

    @tool(description="Handle inbound mail", on_email=True)
    async def handle_email(
        self, ctx: RunContext[NoAuth], email: InboundEmailPayload
    ) -> dict[str, str]:
        return {"body": f"got: {email['subject']}", "subject": "Re: handled"}


def test_on_email_accepts_typed_signature_and_tags_skill() -> None:
    spec = _MailAgent._skills["handle_email"]
    assert EMAIL_HANDLER_TAG in spec.tags
    assert spec.input_schema["properties"]["email"] == {
        "type": "object",
        "additionalProperties": True,
    }
    assert spec.input_schema["required"] == ["email"]


def test_on_email_accepts_dict_annotation_via_skill_alias() -> None:
    class _DictMail(A2AAgent):
        name = "dict-mail"

        @skill(on_email=True)
        async def inbox(self, ctx: RunContext[NoAuth], email: dict) -> str:
            return email["subject"]

    assert EMAIL_HANDLER_TAG in _DictMail._skills["inbox"].tags

    class _DictAnyMail(A2AAgent):
        name = "dict-any-mail"

        @tool(on_email=True)
        async def inbox(
            self, ctx: RunContext[NoAuth], email: dict[str, Any]
        ) -> str:
            return email["subject"]

    assert EMAIL_HANDLER_TAG in _DictAnyMail._skills["inbox"].tags


def test_on_email_rejects_wrong_param_name() -> None:
    with pytest.raises(TypeError, match=r"\(ctx, email: InboundEmailPayload\)"):

        class _Bad(A2AAgent):
            name = "bad-mail"

            @tool(on_email=True)
            async def inbox(
                self, ctx: RunContext[NoAuth], message: InboundEmailPayload
            ) -> str:
                return ""


def test_on_email_rejects_extra_params() -> None:
    with pytest.raises(TypeError, match=r"\(ctx, email: InboundEmailPayload\)"):

        class _Bad(A2AAgent):
            name = "bad-mail-2"

            @tool(on_email=True)
            async def inbox(
                self,
                ctx: RunContext[NoAuth],
                email: InboundEmailPayload,
                verbose: bool = False,
            ) -> str:
                return ""


def test_on_email_rejects_missing_email_param() -> None:
    with pytest.raises(TypeError, match=r"\(ctx, email: InboundEmailPayload\)"):

        class _Bad(A2AAgent):
            name = "bad-mail-3"

            @tool(on_email=True)
            async def inbox(self, ctx: RunContext[NoAuth]) -> str:
                return ""


def test_on_email_rejects_bad_annotation() -> None:
    with pytest.raises(TypeError, match="unsupported annotation"):

        class _Bad(A2AAgent):
            name = "bad-mail-4"

            @tool(on_email=True)
            async def inbox(self, ctx: RunContext[NoAuth], email: str) -> str:
                return email


def test_at_most_one_email_handler_per_agent() -> None:
    with pytest.raises(TypeError, match="at most one skill"):

        class _TwoHandlers(A2AAgent):
            name = "two-handlers"

            @tool(on_email=True)
            async def first(
                self, ctx: RunContext[NoAuth], email: InboundEmailPayload
            ) -> str:
                return ""

            @tool(on_email=True)
            async def second(
                self, ctx: RunContext[NoAuth], email: dict
            ) -> str:
                return ""


def test_email_handler_tag_lands_in_card_and_dsl() -> None:
    card = _MailAgent().card()
    (skill_card,) = [s for s in card.skills if s.id == "handle_email"]
    assert EMAIL_HANDLER_TAG in skill_card.tags
    assert skill_card.input_schema["properties"]["email"]["type"] == "object"
    assert (
        skill_card.input_schema["properties"]["email"]["additionalProperties"]
        is True
    )
    # And through the serialized card JSON the control plane consumes.
    dumped = card.model_dump(mode="json")
    (dumped_skill,) = [
        s for s in dumped["skills"] if s["id"] == "handle_email"
    ]
    assert EMAIL_HANDLER_TAG in dumped_skill["tags"]

    dsl = compile_agent_to_dsl(_MailAgent, entrypoint="agent:MailAgent")
    (dsl_skill,) = [s for s in dsl.skills if s.name == "handle_email"]
    assert EMAIL_HANDLER_TAG in dsl_skill.tags
    assert dsl_skill.input_schema["properties"]["email"] == {
        "type": "object",
        "additionalProperties": True,
    }


def test_email_handler_invocable_with_platform_payload() -> None:
    import asyncio

    agent = _MailAgent()
    result = asyncio.run(agent.local_invoke("handle_email", email=_payload()))
    assert result == {"body": "got: Need help", "subject": "Re: handled"}

    # Payload fields the SDK does not know about must not be rejected —
    # the platform may extend the payload over time.
    extended = _payload(spam_score=0.1)
    result = asyncio.run(agent.local_invoke("handle_email", email=extended))
    assert result["body"] == "got: Need help"


def test_sidecar_accepts_platform_email_arguments() -> None:
    from a2a_pack.sidecar import _validate_arguments

    dsl = compile_agent_to_dsl(_MailAgent, entrypoint="agent:MailAgent")
    (dsl_skill,) = [s for s in dsl.skills if s.name == "handle_email"]
    # The control plane calls the skill with arguments {"email": {...}}.
    _validate_arguments(dsl_skill, {"email": _payload()})


def test_manifest_resources_mailbox_passthrough() -> None:
    from a2a_pack import AgentPlatformResources

    parsed = AgentPlatformResources.from_mapping({"mailbox": True})
    assert parsed.mailbox is True
    assert parsed.enabled is True
    assert parsed.public_payload() == {"mailbox": True}

    options = AgentPlatformResources.from_mapping(
        {"mailbox": {"aliases": ["support"]}}
    )
    assert options.mailbox == {"aliases": ["support"]}
    assert options.public_payload()["mailbox"] == {"aliases": ["support"]}

    default = AgentPlatformResources.from_mapping({})
    assert default.mailbox is None
    assert default.enabled is False
