from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
import base64
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import make_msgid, parseaddr
import imaplib
import json
import logging
import re
import smtplib
import ssl
from urllib.parse import quote
from typing import Any
import uuid

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .auth import issue_invocation_cp_credential
from .config import settings
from .db import SessionLocal
from .grants import mint_grant_token
from .models import (
    Agent,
    AgentMailbox,
    ChatThread,
    ChatThreadEmailLink,
    ChatThreadMessage,
    MailboxProvisionEvent,
    User,
)
from .secret_crypto import decrypt_secret
from .safe_http import safe_fetch_url, safe_request_url

log = logging.getLogger(__name__)

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_MAX_AGENT_HTTP_RESPONSE_BYTES = 5 * 1024 * 1024
_mail_agent_context: ContextVar[Agent | None] = ContextVar(
    "mail_agent_context",
    default=None,
)


@dataclass
class MailIngressMetrics:
    poll_runs: int = 0
    messages_seen: int = 0
    messages_bridged: int = 0
    messages_skipped: int = 0
    replies_sent: int = 0
    replies_rate_limited: int = 0
    failures: int = 0
    last_run_at: datetime | None = None
    last_error: str | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "poll_runs": self.poll_runs,
            "messages_seen": self.messages_seen,
            "messages_bridged": self.messages_bridged,
            "messages_skipped": self.messages_skipped,
            "replies_sent": self.replies_sent,
            "replies_rate_limited": self.replies_rate_limited,
            "failures": self.failures,
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "last_error": self.last_error,
        }


metrics = MailIngressMetrics()


EMAIL_HANDLER_TAG = "a2a:email-handler"


@dataclass(frozen=True)
class InboundEmail:
    uid: int
    sender: str
    subject: str
    message_id: str
    references: tuple[str, ...]
    in_reply_to: str | None
    auto_submitted: bool
    body: str
    date: str | None = None
    # Payload-shaped attachment dicts: filename, content_type, size_bytes,
    # content_b64 (None when over the inline cap).
    attachments: tuple[dict[str, Any], ...] = ()

    @property
    def thread_key(self) -> str:
        # Root of the References chain identifies the conversation; a fresh
        # message starts a conversation keyed by its own Message-ID.
        if self.references:
            return self.references[0]
        return self.message_id

    def payload(self) -> dict[str, Any]:
        """The canonical InboundEmailPayload handed to on_email skills.

        Mirrors a2a_pack.mail.InboundEmailPayload — change both together.
        """

        return {
            "sender": self.sender,
            "subject": self.subject,
            "body": self.body,
            "message_id": self.message_id,
            "date": self.date,
            "references": list(self.references),
            "attachments": [dict(item) for item in self.attachments],
        }


@dataclass(frozen=True)
class AgentEmailReply:
    body: str
    subject: str | None = None


def _tls_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    if not settings.agent_mail_tls_verify:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def parse_inbound_email(uid: int, raw: bytes) -> InboundEmail:
    message = message_from_bytes(raw, policy=policy.default)
    sender = parseaddr(str(message.get("From", "")))[1].strip().lower()
    subject = str(message.get("Subject", "")).strip()
    message_id = str(message.get("Message-ID", "")).strip()
    references = tuple(
        ref for ref in str(message.get("References", "")).split() if ref
    )
    in_reply_to = str(message.get("In-Reply-To", "")).strip() or None
    auto_submitted = (
        str(message.get("Auto-Submitted", "no")).strip().lower() not in {"", "no"}
        or message.get("X-Auto-Response-Suppress") is not None
    )
    body = _extract_body(message)
    return InboundEmail(
        uid=uid,
        sender=sender,
        subject=subject,
        message_id=message_id or f"<missing-{uid}@{settings.agent_mail_domain}>",
        references=references,
        in_reply_to=in_reply_to,
        auto_submitted=auto_submitted,
        body=body,
        date=str(message.get("Date", "")).strip() or None,
        attachments=_extract_attachments(message),
    )


def _extract_attachments(message: Any) -> tuple[dict[str, Any], ...]:
    items: list[dict[str, Any]] = []
    try:
        attachments = list(message.iter_attachments())
    except Exception:  # noqa: BLE001
        return ()
    for part in attachments[: settings.agent_mail_max_attachments]:
        try:
            content = part.get_content()
        except Exception:  # noqa: BLE001
            continue
        if isinstance(content, str):
            content = content.encode("utf-8")
        elif not isinstance(content, (bytes, bytearray)):
            continue
        size = len(content)
        encoded: str | None = None
        if size <= settings.agent_mail_max_attachment_bytes:
            encoded = base64.b64encode(bytes(content)).decode("ascii")
        items.append(
            {
                "filename": part.get_filename() or "attachment",
                "content_type": part.get_content_type(),
                "size_bytes": size,
                "content_b64": encoded,
            }
        )
    return tuple(items)


def _extract_body(message: Any) -> str:
    part = message.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        content = part.get_content()
    except Exception:  # noqa: BLE001
        return ""
    if not isinstance(content, str):
        return ""
    if part.get_content_type() == "text/html":
        content = _HTML_TAG_RE.sub(" ", content)
    content = content.strip()
    max_bytes = settings.agent_mail_max_body_bytes
    encoded = content.encode("utf-8")
    if len(encoded) > max_bytes:
        content = encoded[:max_bytes].decode("utf-8", errors="ignore") + "\n[truncated]"
    return content


def sender_allowed(mailbox: AgentMailbox, owner_email: str | None, sender: str) -> bool:
    """Owner is always allowed; otherwise the declared allowlist decides.

    An empty allowlist means owner-only — the spam-safe default.
    """

    if not sender:
        return False
    if sender.endswith(f"@{settings.agent_mail_domain}"):
        return False
    if owner_email and sender == owner_email.strip().lower():
        return True
    allowed = {str(item).strip().lower() for item in (mailbox.allowed_senders_json or [])}
    return sender in allowed


def _fetch_new_messages_sync(
    *,
    address: str,
    password: str,
    last_uid: int,
) -> list[tuple[int, bytes]]:
    context = _tls_context()
    with imaplib.IMAP4_SSL(
        settings.agent_mail_imap_host,
        settings.agent_mail_imap_port,
        ssl_context=context,
    ) as imap:
        imap.login(address, password)
        imap.select("INBOX", readonly=True)
        status, data = imap.uid("SEARCH", None, f"UID {last_uid + 1}:*")
        if status != "OK" or not data or not data[0]:
            return []
        uids = [int(item) for item in data[0].split() if int(item) > last_uid]
        results: list[tuple[int, bytes]] = []
        for uid in sorted(uids):
            status, payload = imap.uid("FETCH", str(uid), "(RFC822)")
            if status != "OK" or not payload or payload[0] is None:
                continue
            raw = payload[0][1] if isinstance(payload[0], tuple) else None
            if raw:
                results.append((uid, raw))
        return results


def _send_reply_sync(
    *,
    address: str,
    password: str,
    to: str,
    subject: str,
    body: str,
    in_reply_to: str | None,
    references: tuple[str, ...],
) -> str:
    message = EmailMessage()
    message["From"] = address
    message["To"] = to
    message["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    message_id = make_msgid(domain=settings.agent_mail_domain)
    message["Message-ID"] = message_id
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
        message["References"] = " ".join([*references, in_reply_to][-20:])
    # Mark platform replies as automated so remote autoresponders don't loop.
    message["Auto-Submitted"] = "auto-replied"
    message.set_content(body)
    with smtplib.SMTP(
        settings.agent_mail_smtp_host,
        settings.agent_mail_smtp_port,
        timeout=30,
    ) as smtp:
        smtp.starttls(context=_tls_context())
        smtp.login(address, password)
        smtp.send_message(message)
    return message_id


def _agent_base_url(agent: Agent) -> str:
    if agent.url:
        return agent.url
    host = settings.ingress_host_template.format(name=agent.name)
    return f"https://{host}"


def extract_reply_text(result: Any) -> str:
    """Collect text parts from an A2A message-style result."""

    chunks: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            text = node.get("text")
            if isinstance(text, str) and text.strip():
                chunks.append(text.strip())
            for key in ("parts", "message", "result"):
                if key in node:
                    walk(node[key])
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(result)
    return "\n\n".join(dict.fromkeys(chunks))


def task_of(result: Any) -> dict[str, Any] | None:
    """Return the task object from a message/send result, if it is one."""

    if not isinstance(result, dict):
        return None
    if result.get("kind") == "task":
        return result
    task = result.get("task")
    if isinstance(task, dict):
        return task
    return None


_TERMINAL_TASK_STATES = {
    "TASK_STATE_COMPLETED",
    "TASK_STATE_FAILED",
    "TASK_STATE_CANCELLED",
    "TASK_STATE_REJECTED",
    "completed",
    "failed",
    "cancelled",
    "canceled",
    "rejected",
}
_FAILED_TASK_STATES = {
    "TASK_STATE_FAILED",
    "TASK_STATE_CANCELLED",
    "TASK_STATE_REJECTED",
    "failed",
    "cancelled",
    "canceled",
    "rejected",
}


def _task_state(task: dict[str, Any]) -> str:
    status = task.get("status")
    if isinstance(status, dict):
        return str(status.get("state") or "")
    return ""


def email_handler_skill(card: dict[str, Any] | None) -> str | None:
    """Skill id of the agent's declared on_email handler, from its card.

    SDK agents declaring ``@a2a.tool(on_email=True)`` publish the
    ``a2a:email-handler`` tag on that skill.
    """

    for skill in (card or {}).get("skills") or []:
        if not isinstance(skill, dict):
            continue
        tags = skill.get("tags") or []
        if isinstance(tags, list) and EMAIL_HANDLER_TAG in tags:
            name = skill.get("id") or skill.get("name")
            if name:
                return str(name)
    return None


def _reply_from_value(value: Any) -> AgentEmailReply | None:
    """Normalize a handler return value ({body, subject?} or str) to a reply."""

    if isinstance(value, dict):
        body = value.get("body")
        if isinstance(body, str) and body.strip():
            subject = value.get("subject")
            return AgentEmailReply(
                body=body,
                subject=subject if isinstance(subject, str) and subject.strip() else None,
            )
        return None
    if isinstance(value, str) and value.strip():
        return AgentEmailReply(body=value)
    return None


def reply_from_task(task: dict[str, Any]) -> AgentEmailReply | None:
    """Pull the handler reply out of a terminal task.

    Structured returns surface as data parts ({body, subject?}); plain
    string returns surface as text parts.
    """

    def data_values(node: Any) -> list[Any]:
        found: list[Any] = []
        if isinstance(node, dict):
            if "data" in node:
                found.append(node["data"])
            for key in ("parts", "message", "result"):
                if key in node:
                    found.extend(data_values(node[key]))
        elif isinstance(node, list):
            for item in node:
                found.extend(data_values(item))
        return found

    for artifact in task.get("artifacts") or []:
        for value in data_values(artifact):
            reply = _reply_from_value(value)
            if reply is not None:
                return reply
    status = task.get("status")
    if isinstance(status, dict):
        for value in data_values(status.get("message")):
            reply = _reply_from_value(value)
            if reply is not None:
                return reply
    text = extract_task_reply_text(task)
    return AgentEmailReply(body=text) if text.strip() else None


def extract_task_reply_text(task: dict[str, Any]) -> str:
    """Collect the agent's output from a terminal A2A task.

    Artifacts first, then the status message, then agent-role history —
    never user-role history, which would echo the inbound email back to
    the sender.
    """

    chunks: list[str] = []
    for artifact in task.get("artifacts") or []:
        text = extract_reply_text(artifact)
        if text:
            chunks.append(text)
    if not chunks:
        status = task.get("status")
        if isinstance(status, dict):
            text = extract_reply_text(status.get("message"))
            if text:
                chunks.append(text)
    if not chunks:
        for message in reversed(task.get("history") or []):
            role = str((message or {}).get("role") or "").lower()
            if "agent" not in role and "assistant" not in role:
                continue
            text = extract_reply_text(message)
            if text:
                chunks.append(text)
                break
    return "\n\n".join(dict.fromkeys(chunks))


async def _a2a_rpc(
    base_url: str,
    method: str,
    params: dict,
    *,
    headers: dict[str, str] | None = None,
) -> Any:
    payload = {
        "jsonrpc": "2.0",
        "id": f"mail-{uuid.uuid4().hex[:12]}",
        "method": method,
        "params": params,
    }
    response = await safe_request_url(
        "POST",
        f"{base_url.rstrip('/')}/",
        sensitive_headers=headers,
        json_body=payload,
        max_response_bytes=_MAX_AGENT_HTTP_RESPONSE_BYTES,
        timeout_seconds=120.0,
        max_redirects=0,
        require_https=True,
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"A2A {method} returned HTTP {response.status_code}: {response.text[:300]}"
        )
    data = json.loads(response.text)
    if isinstance(data, dict) and data.get("error"):
        err = data["error"]
        raise RuntimeError(f"A2A {method} error {err.get('code')}: {err.get('message')}")
    return data.get("result") if isinstance(data, dict) else data


TASK_POLL_SECONDS = 5.0


async def _mint_mail_grant(
    agent_name: str,
    skill_name: str,
    *,
    agent: Agent | None = None,
) -> str:
    """Grant for a mail-triggered run, mirroring the orchestrator handoff:
    the token authorizes platform LLM access scoped to the litellm model."""

    from .agent_access import account_access_policy, resolve_account_llm_access

    llm_models = (settings.litellm_model,) if settings.litellm_model else ()
    byok_creds: dict[str, Any] | None = None
    decision = None
    if agent is None:
        async with SessionLocal() as session:
            agent = (
                await session.execute(select(Agent).where(Agent.name == agent_name))
            ).scalar_one_or_none()
    policy = account_access_policy(agent.card) if agent is not None else None
    if policy is not None and policy.enabled:
        from main_agent.config import load_settings as load_runtime_settings
        from .routes.chat import _litellm_model_alias
        from .routes.llm_creds import get_creds_for_user

        async with SessionLocal() as session:
            byok_creds = await get_creds_for_user(agent.owner_id, session)
            if byok_creds is None:
                runtime_settings = load_runtime_settings()
                llm_models = tuple(runtime_settings.platform_llm_models)
                if not llm_models:
                    raise RuntimeError("platform-funded LLM trial is unavailable")
            decision = await resolve_account_llm_access(
                session,
                agent=agent,
                user_id=agent.owner_id,
                skill_name=skill_name,
                has_byok=byok_creds is not None,
            )
        if decision.source == "byok":
            llm_models = (_litellm_model_alias(agent.owner_id, "default"),)

    token, payload = mint_grant_token(
        issuer="control-plane-mail",
        audience=agent_name,
        bucket=f"mail/{agent_name}",
        llm_models=llm_models,
        llm_max_budget_usd=1.0,
        llm_rpm_limit=60,
        llm_tpm_limit=200_000,
        ttl_seconds=int(settings.mail_ingress_agent_timeout_seconds) + 60,
    )
    if decision is not None and decision.source == "byok":
        from .routes.chat import _main_llm_runtime_creds

        await _main_llm_runtime_creds(
            byok_creds,
            user_id=agent.owner_id,
            llm_creds_name="default",
            runtime_litellm_key=token,
            litellm_metadata={
                "a2a_user_id": agent.owner_id,
                "a2a_grant_id": payload.get("grant_id"),
                "a2a_agent_name": agent.name,
                "a2a_skill_name": skill_name,
                "a2a_llm_source": "mail",
                "a2a_account_access_source": "byok",
            },
        )
    return token


async def _post_invoke(
    base_url: str,
    agent_name: str,
    skill: str,
    arguments: dict[str, Any],
    headers: dict[str, str] | None,
    cp_jwt: str | None = None,
) -> Any:
    """Synchronous sidecar skill call. With the caller's bearer attached the
    agent fetches its own platform LLM grant from the control plane, so
    LLM-backed skills complete instead of hanging credential-less.

    The same credential also rides in the body as ``cp_jwt``. The SDK builds
    ``ctx.cp_jwt`` from the body (then the cookie, then the pod's own
    ``A2A_CP_JWT``) and never from this header, so a header-only call silently
    handed the agent its *deploy-time* credential instead of the one minted for
    this email — the one path where a control-plane-driven invocation fell
    through to the long-lived pod value."""

    response = await safe_request_url(
        "POST",
        f"{base_url.rstrip('/')}/invoke/{quote(skill, safe='')}",
        sensitive_headers=headers,
        json_body={
            "arguments": arguments,
            "grant": await _mint_mail_grant(
                agent_name,
                skill,
                agent=_mail_agent_context.get(),
            ),
            **({"cp_jwt": cp_jwt} if cp_jwt else {}),
        },
        max_response_bytes=_MAX_AGENT_HTTP_RESPONSE_BYTES,
        timeout_seconds=settings.mail_ingress_agent_timeout_seconds,
        max_redirects=0,
        require_https=True,
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"invoke {skill} returned HTTP {response.status_code}: {response.text[:300]}"
        )
    data = json.loads(response.text)
    return data.get("result") if isinstance(data, dict) and "result" in data else data


async def _post_invoke_for_agent(
    agent: Agent,
    base_url: str,
    skill: str,
    arguments: dict[str, Any],
    headers: dict[str, str] | None,
    cp_jwt: str | None = None,
) -> Any:
    context_token = _mail_agent_context.set(agent)
    try:
        return await _post_invoke(
            base_url,
            agent.name,
            skill,
            arguments,
            headers,
            cp_jwt,
        )
    finally:
        _mail_agent_context.reset(context_token)


def single_text_skill(skills_manifest: dict[str, Any] | None) -> tuple[str, str] | None:
    """(skill_id, param_name) when the agent has exactly one skill taking
    exactly one required string parameter — the 'ask'-shaped agents that a
    rendered email maps onto safely. Anything else returns None."""

    skills = (skills_manifest or {}).get("skills") or []
    if len(skills) != 1 or not isinstance(skills[0], dict):
        return None
    skill = skills[0]
    schema = skill.get("input_schema")
    if not isinstance(schema, dict):
        return None
    properties = schema.get("properties") or {}
    required = schema.get("required") or []
    if len(properties) != 1 or len(required) != 1:
        return None
    param = required[0]
    prop = properties.get(param)
    if not isinstance(prop, dict) or prop.get("type") != "string":
        return None
    name = skill.get("id") or skill.get("name")
    return (str(name), str(param)) if name else None


async def _fetch_skills_manifest(base_url: str) -> dict[str, Any] | None:
    # First request often lands on a scale-to-zero agent mid-cold-start
    # (observed ~12s); a short timeout here silently demotes the run to the
    # message/send fallback, which task-model agents never complete. Give
    # the wake generous room and retry once.
    for attempt in (1, 2):
        try:
            response = await safe_fetch_url(
                f"{base_url.rstrip('/')}/.well-known/a2a-skills.json",
                headers={"accept": "application/json"},
                max_response_bytes=_MAX_AGENT_HTTP_RESPONSE_BYTES,
                timeout_seconds=75.0,
            )
            if response.status_code == 200:
                data = json.loads(response.text)
                return data if isinstance(data, dict) else None
        except Exception:  # noqa: BLE001
            pass
        if attempt == 1:
            await asyncio.sleep(2.0)
    return None


HISTORY_MESSAGE_LIMIT = 20
HISTORY_CHAR_BUDGET = 16_000


async def _thread_history(
    session: AsyncSession, thread_id: str
) -> list[dict[str, str]]:
    """Last messages of the linked chat thread, oldest first, bounded by
    count and total characters so long conversations can't blow the
    invocation payload."""

    rows = (
        await session.execute(
            select(ChatThreadMessage)
            .where(ChatThreadMessage.thread_id == thread_id)
            .order_by(ChatThreadMessage.id.desc())
            .limit(HISTORY_MESSAGE_LIMIT)
        )
    ).scalars().all()
    history: list[dict[str, str]] = []
    budget = HISTORY_CHAR_BUDGET
    for row in rows:  # newest first; keep the most recent within budget
        content = row.content or ""
        if len(content) > budget:
            break
        budget -= len(content)
        history.append({"role": row.role, "content": content})
    history.reverse()
    return history


def _rendered_email_prompt(
    agent_name: str,
    email: InboundEmail,
    history: list[dict[str, str]] | None = None,
) -> str:
    """Render the inbound email — plus the conversation so far — as one
    prompt.

    Two lessons from live traffic are baked in here. First, without the
    transcript every email is a cold start and the agent forgets the
    sender's name one message later. Second, without explicit role framing
    agents read a forwarded email as "help me draft a reply to this"
    instead of "you are the recipient — answer it".
    """

    lines = [
        f"You are the agent behind the email address {agent_name}@{settings.agent_mail_domain}.",
        f"{email.sender} is emailing YOU. You are the recipient and you are "
        "expected to answer directly, as yourself, in the first person. Your "
        "answer will be sent back as the email reply — do not offer to draft "
        "anything, do not ask who you are writing as.",
    ]
    if history:
        lines.append("\nThe conversation so far (oldest first):")
        for message in history:
            speaker = (
                email.sender if message.get("role") == "user" else "you"
            )
            lines.append(f"[{speaker}] {message.get('content', '')}")
    lines.append(
        f"\nNew email:\nFrom: {email.sender}\nSubject: {email.subject}\n\n{email.body}"
    )
    return "\n".join(lines)


async def _invoke_agent_with_email(
    agent: Agent,
    email: InboundEmail,
    history: list[dict[str, str]] | None = None,
) -> AgentEmailReply | None:
    base_url = _agent_base_url(agent)
    # Run as the mailbox owner: anonymous A2A calls leave LLM-backed agents
    # without platform credentials and their tasks hang forever. Same
    # pattern as other machine-triggered runs (schedules, reviews) — and the
    # same credential class: this is read by seller-controlled code, so it is
    # the agent-scoped invocation credential, never an hour-long session.
    #
    # It goes in the body as well as the header (see :func:`_post_invoke`):
    # ``ctx.cp_jwt`` is built from the body, so header-only meant the agent
    # used its deploy-time ``A2A_CP_JWT`` for this call instead.
    cp_jwt = (
        issue_invocation_cp_credential(agent.owner_id, agent=agent.name)
        if agent.owner_id is not None
        else None
    )
    headers = {"Authorization": f"Bearer {cp_jwt}"} if cp_jwt else None

    handler = email_handler_skill(agent.card)
    if handler:
        # Declared on_email handler: structured skill call with the
        # canonical InboundEmailPayload. The prior transcript rides
        # along as payload["thread"] ({role, content} rows, oldest
        # first) so handlers can be stateful per conversation.
        payload = email.payload()
        if history:
            payload["thread"] = history
        value = await _post_invoke_for_agent(
            agent,
            base_url,
            handler,
            {"email": payload},
            headers,
            cp_jwt,
        )
        return _reply_from_value(value)
    # No handler: an 'ask'-shaped agent (single skill, single required
    # string param) still gets the email as that parameter.
    manifest = await _fetch_skills_manifest(base_url)
    text_skill = single_text_skill(manifest)
    if text_skill:
        skill, param = text_skill
        value = await _post_invoke_for_agent(
            agent,
            base_url,
            skill,
            {param: _rendered_email_prompt(agent.name, email, history)},
            headers,
            cp_jwt,
        )
        reply = _reply_from_value(value)
        if reply is None and value not in (None, ""):
            reply = AgentEmailReply(body=json.dumps(value, ensure_ascii=False)[:4000])
        return reply

    # Last resort: protocol-generic message/send with task polling.
    parts: list[dict[str, Any]] = [
        {"text": _rendered_email_prompt(agent.name, email, history)}
    ]
    deadline = (
        asyncio.get_running_loop().time() + settings.mail_ingress_agent_timeout_seconds
    )
    result = await _a2a_rpc(
        base_url,
        "message/send",
        {
            "message": {
                "messageId": f"mail-{email.uid}-{uuid.uuid4().hex[:8]}",
                "role": "ROLE_USER",
                "parts": parts,
            }
        },
        headers=headers,
    )
    task = task_of(result)
    if task is None:
        return _reply_from_value(result) or (
            AgentEmailReply(body=extract_reply_text(result))
            if extract_reply_text(result).strip()
            else None
        )
    # Task-model agent: poll tasks/get until the run reaches a terminal
    # state (message/send only acknowledges with TASK_STATE_WORKING).
    while _task_state(task) not in _TERMINAL_TASK_STATES:
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError(
                f"agent task {task.get('id')} did not complete within "
                f"{settings.mail_ingress_agent_timeout_seconds:.0f}s"
            )
        await asyncio.sleep(TASK_POLL_SECONDS)
        result = await _a2a_rpc(
            base_url, "tasks/get", {"id": task.get("id")}, headers=headers
        )
        task = task_of(result) or task
    if _task_state(task) in _FAILED_TASK_STATES:
        detail = extract_task_reply_text(task) or _task_state(task)
        raise RuntimeError(f"agent task {task.get('id')} failed: {detail[:300]}")
    return reply_from_task(task)


@dataclass
class MailIngressWorker:
    session_maker: async_sessionmaker[AsyncSession] = SessionLocal
    metrics: MailIngressMetrics = field(default_factory=lambda: metrics)

    async def poll_once(self) -> MailIngressMetrics:
        self.metrics.poll_runs += 1
        self.metrics.last_run_at = datetime.now(UTC)
        async with self.session_maker() as session:
            mailboxes = (
                await session.execute(
                    select(AgentMailbox).where(AgentMailbox.status == "ready")
                )
            ).scalars().all()
            for mailbox in mailboxes:
                try:
                    await self._poll_mailbox(session, mailbox)
                except Exception as exc:  # noqa: BLE001
                    await session.rollback()
                    self.metrics.failures += 1
                    self.metrics.last_error = str(exc)
                    log.exception(
                        "mail ingress poll failed",
                        extra={"mailbox": mailbox.address},
                    )
        return self.metrics

    async def _poll_mailbox(self, session: AsyncSession, mailbox: AgentMailbox) -> None:
        if not mailbox.password_ciphertext:
            return
        password = decrypt_secret(mailbox.password_ciphertext)
        messages = await asyncio.to_thread(
            _fetch_new_messages_sync,
            address=mailbox.address,
            password=password,
            last_uid=mailbox.last_imap_uid or 0,
        )
        if not messages:
            return
        agent = await session.get(Agent, mailbox.agent_id)
        owner = (
            await session.get(User, mailbox.user_id)
            if mailbox.user_id is not None
            else None
        )
        for uid, raw in messages:
            self.metrics.messages_seen += 1
            email = parse_inbound_email(uid, raw)
            # Claim the message by advancing the watermark atomically. The
            # control plane runs several worker processes, each with its own
            # ingress loop; the conditional UPDATE lets exactly one of them
            # win a given UID (losers see rowcount 0 and skip). Committing
            # the claim before bridging also means a crash mid-bridge skips
            # one email instead of re-running every prior one.
            claimed = await session.execute(
                update(AgentMailbox)
                .where(AgentMailbox.id == mailbox.id, AgentMailbox.last_imap_uid < uid)
                .values(last_imap_uid=uid)
            )
            await session.commit()
            if claimed.rowcount == 0:
                continue
            if agent is None:
                continue
            if email.auto_submitted or not sender_allowed(
                mailbox, owner.email if owner else None, email.sender
            ):
                self.metrics.messages_skipped += 1
                session.add(
                    MailboxProvisionEvent(
                        mailbox_id=mailbox.id,
                        agent_id=mailbox.agent_id,
                        event_type="mail_skipped",
                        status="info",
                        message=(
                            "skipped auto-submitted email"
                            if email.auto_submitted
                            else f"skipped email from unlisted sender {email.sender}"
                        ),
                        data={"uid": uid, "subject": email.subject[:200]},
                    )
                )
                await session.commit()
                continue
            await self._bridge_message(session, mailbox, agent, owner, email, password)

    async def _bridge_message(
        self,
        session: AsyncSession,
        mailbox: AgentMailbox,
        agent: Agent,
        owner: User | None,
        email: InboundEmail,
        password: str,
    ) -> None:
        link = (
            await session.execute(
                select(ChatThreadEmailLink).where(
                    ChatThreadEmailLink.mailbox_id == mailbox.id,
                    ChatThreadEmailLink.thread_key == email.thread_key,
                )
            )
        ).scalar_one_or_none()
        if link is None:
            thread = ChatThread(
                id=str(uuid.uuid4()),
                user_id=mailbox.user_id or agent.owner_id,
                title=email.subject[:255] or f"Email from {email.sender}",
                settings_json={
                    "source": "email",
                    "agent_name": agent.name,
                    "remote_address": email.sender,
                    "mailbox_address": mailbox.address,
                },
            )
            session.add(thread)
            link = ChatThreadEmailLink(
                thread_id=thread.id,
                mailbox_id=mailbox.id,
                agent_id=agent.id,
                thread_key=email.thread_key,
                remote_address=email.sender,
                subject=email.subject[:998],
            )
            session.add(link)
            await session.flush()
        # Snapshot the transcript BEFORE appending the new mail: this is the
        # thread memory the agent gets alongside the inbound email.
        history = await _thread_history(session, link.thread_id)
        link.last_message_id = email.message_id
        link.references_json = list(email.references[-20:]) + [email.message_id]
        session.add(link)
        session.add(
            ChatThreadMessage(
                thread_id=link.thread_id,
                role="user",
                content=f"[email from {email.sender}] {email.subject}\n\n{email.body}",
            )
        )
        session.add(
            MailboxProvisionEvent(
                mailbox_id=mailbox.id,
                agent_id=mailbox.agent_id,
                event_type="mail_received",
                status="ok",
                message=f"bridged email from {email.sender}",
                data={
                    "uid": email.uid,
                    "subject": email.subject[:200],
                    "thread_id": link.thread_id,
                },
            )
        )
        await session.commit()
        self.metrics.messages_bridged += 1

        try:
            reply = await _invoke_agent_with_email(agent, email, history)
        except Exception as exc:  # noqa: BLE001
            self.metrics.failures += 1
            self.metrics.last_error = str(exc)
            session.add(
                MailboxProvisionEvent(
                    mailbox_id=mailbox.id,
                    agent_id=mailbox.agent_id,
                    event_type="mail_agent_run_failed",
                    status="failed",
                    message=str(exc)[:500],
                    data={"thread_id": link.thread_id},
                )
            )
            await session.commit()
            return
        if reply is None or not reply.body.strip():
            return
        session.add(
            ChatThreadMessage(
                thread_id=link.thread_id,
                role="assistant",
                content=reply.body,
            )
        )
        await session.commit()
        await self._send_reply(session, mailbox, link, email, reply, password)

    async def _send_reply(
        self,
        session: AsyncSession,
        mailbox: AgentMailbox,
        link: ChatThreadEmailLink,
        email: InboundEmail,
        reply: AgentEmailReply,
        password: str,
    ) -> None:
        if not self._consume_send_budget(mailbox):
            self.metrics.replies_rate_limited += 1
            session.add(
                MailboxProvisionEvent(
                    mailbox_id=mailbox.id,
                    agent_id=mailbox.agent_id,
                    event_type="mail_send_rate_limited",
                    status="failed",
                    message=(
                        f"daily send limit of {settings.agent_mail_daily_send_limit} "
                        f"reached; reply kept in thread only"
                    ),
                    data={"thread_id": link.thread_id},
                )
            )
            session.add(mailbox)
            await session.commit()
            return
        try:
            message_id = await asyncio.to_thread(
                _send_reply_sync,
                address=mailbox.address,
                password=password,
                to=email.sender,
                subject=reply.subject or email.subject,
                body=reply.body,
                in_reply_to=email.message_id,
                references=email.references,
            )
        except Exception as exc:  # noqa: BLE001
            self.metrics.failures += 1
            self.metrics.last_error = str(exc)
            session.add(
                MailboxProvisionEvent(
                    mailbox_id=mailbox.id,
                    agent_id=mailbox.agent_id,
                    event_type="mail_send_failed",
                    status="failed",
                    message=str(exc)[:500],
                    data={"thread_id": link.thread_id},
                )
            )
            session.add(mailbox)
            await session.commit()
            return
        self.metrics.replies_sent += 1
        link.last_message_id = message_id
        link.references_json = list((link.references_json or []) + [message_id])[-20:]
        session.add(link)
        session.add(
            MailboxProvisionEvent(
                mailbox_id=mailbox.id,
                agent_id=mailbox.agent_id,
                event_type="mail_sent",
                status="ok",
                message=f"replied to {email.sender}",
                data={"thread_id": link.thread_id, "to": email.sender},
            )
        )
        session.add(mailbox)
        await session.commit()

    def _consume_send_budget(self, mailbox: AgentMailbox) -> bool:
        today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        period = mailbox.outbound_period_start
        if period is not None and period.tzinfo is None:
            period = period.replace(tzinfo=UTC)
        if period is None or period < today:
            mailbox.outbound_period_start = today
            mailbox.outbound_count = 0
        if (mailbox.outbound_count or 0) >= settings.agent_mail_daily_send_limit:
            return False
        mailbox.outbound_count = (mailbox.outbound_count or 0) + 1
        return True


async def run_mail_ingress_loop(
    *,
    interval_seconds: float | None = None,
    worker: MailIngressWorker | None = None,
) -> None:
    interval = (
        settings.mail_ingress_interval_seconds
        if interval_seconds is None
        else interval_seconds
    )
    worker = worker or MailIngressWorker()
    while True:
        try:
            await worker.poll_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            metrics.failures += 1
            metrics.last_error = str(exc)
            log.exception("mail ingress poll loop failed")
        await asyncio.sleep(interval)


def start_mail_ingress(app: Any) -> None:
    if not settings.mail_ingress_enabled:
        return
    task = asyncio.create_task(run_mail_ingress_loop(), name="mail-ingress")
    app.state.mail_ingress = task


async def stop_mail_ingress(app: Any) -> None:
    task = getattr(app.state, "mail_ingress", None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass


