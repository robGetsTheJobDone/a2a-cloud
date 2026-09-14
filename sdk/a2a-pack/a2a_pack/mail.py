"""Per-agent mailbox access over IMAP/SMTP.

The control plane provisions a Mailu mailbox for pro-plan agents and
injects credentials into the pod via a runtime secret:

- ``A2A_MAIL_ADDRESS`` / ``A2A_MAIL_PASSWORD`` — mailbox identity.
- ``A2A_MAIL_IMAP_HOST`` / ``A2A_MAIL_IMAP_PORT`` — IMAPS (default 993).
- ``A2A_MAIL_SMTP_HOST`` / ``A2A_MAIL_SMTP_PORT`` — STARTTLS (default 587).
- ``A2A_MAIL_TLS_VERIFY`` — the in-cluster mail cert does not match the
  service hostname, so certificate verification is off unless this is
  explicitly truthy.

Skill code should reach for :attr:`RunContext.mail` rather than
constructing this class directly — the property returns ``None`` when the
platform did not provision a mailbox, so agents degrade gracefully on
plans without email.
"""
from __future__ import annotations

import email
import email.policy
import imaplib
import os
import re
import smtplib
import ssl
from email.message import EmailMessage, Message
from email.utils import formatdate, make_msgid
from html.parser import HTMLParser
from typing import Any, Sequence, TypedDict


class EmailAttachment(TypedDict):
    """One attachment of an inbound platform-delivered email.

    ``content_b64`` is the base64-encoded attachment content, or ``None``
    when the attachment was too large to inline — in that case only the
    metadata (filename, content type, size) is delivered.
    """

    filename: str
    content_type: str
    size_bytes: int
    content_b64: str | None


class InboundEmailPayload(TypedDict):
    """Inbound email delivered by the platform to an ``on_email`` skill.

    When an agent declares a skill with ``@a2a.tool(on_email=True)``, the
    control plane invokes that skill with ``{"email": {...}}`` whenever the
    agent's mailbox receives mail. This is the shape of that payload.

    Return contract for the handler (not enforced by the SDK):

    - return ``str`` — sent back as the reply body;
    - return ``{"body": str, "subject": str (optional)}`` — reply with an
      explicit body and optional subject override;
    - return ``None`` or an empty value — no email reply is sent.
    """

    sender: str
    subject: str
    body: str
    message_id: str
    date: str | None
    references: list[str]
    attachments: list[EmailAttachment]


_BODY_MAX_CHARS = 256 * 1024
_SNIPPET_MAX_CHARS = 160
_TRUTHY = {"1", "true", "yes", "on"}


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in _TRUTHY


class _HTMLTextExtractor(HTMLParser):
    """Collect visible text from an HTML body, skipping script/style."""

    _SKIP_TAGS = {"script", "style", "head", "template"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag.lower() in self._SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data:
            self._chunks.append(data)

    def text(self) -> str:
        return "".join(self._chunks)


def _strip_html(markup: str) -> str:
    """Reduce an HTML body to whitespace-normalised plain text."""
    extractor = _HTMLTextExtractor()
    try:
        extractor.feed(markup)
        extractor.close()
    except Exception:  # noqa: BLE001 - malformed markup falls back to regex
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", markup)).strip()
    return re.sub(r"[ \t\r\f\v]*\n[ \t\r\f\v]*", "\n", extractor.text()).strip()


def _part_text(part: Message) -> str:
    """Decode one MIME part to text, tolerating charset lies."""
    payload = part.get_payload(decode=True)
    if payload is None:
        raw = part.get_payload()
        return raw if isinstance(raw, str) else ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except (LookupError, UnicodeDecodeError):
        return payload.decode("utf-8", errors="replace")


def _extract_body(msg: Message, *, max_chars: int = _BODY_MAX_CHARS) -> str:
    """Best-effort text body: text/plain preferred, tag-stripped HTML fallback."""
    plain: str | None = None
    html_body: str | None = None
    for part in msg.walk():
        if part.is_multipart():
            continue
        content_type = part.get_content_type()
        disposition = str(part.get("Content-Disposition", "")).lower()
        if "attachment" in disposition:
            continue
        if content_type == "text/plain" and plain is None:
            plain = _part_text(part)
        elif content_type == "text/html" and html_body is None:
            html_body = _part_text(part)
    body = plain if plain is not None else _strip_html(html_body or "")
    return body[:max_chars]


def _snippet(msg: Message, *, max_chars: int = _SNIPPET_MAX_CHARS) -> str:
    body = _extract_body(msg, max_chars=max_chars * 8)
    collapsed = re.sub(r"\s+", " ", body).strip()
    return collapsed[:max_chars]


def _reply_subject(subject: str) -> str:
    if re.match(r"^\s*re\s*:", subject, flags=re.IGNORECASE):
        return subject
    return f"Re: {subject}".strip()


def _fetch_payload(data: Sequence[Any]) -> bytes:
    """Pull the raw RFC822 bytes out of an imaplib FETCH response."""
    for item in data or ():
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
            return item[1]
    raise LookupError("IMAP fetch returned no message payload")


class AgentMailbox:
    """Synchronous IMAP/SMTP client for the agent's platform mailbox.

    Every connection is short-lived: each method dials, authenticates,
    does its work, and logs out, so instances hold no sockets and are safe
    to cache on a :class:`~a2a_pack.context.RunContext`.
    """

    def __init__(
        self,
        *,
        address: str,
        password: str,
        imap_host: str,
        smtp_host: str,
        imap_port: int = 993,
        smtp_port: int = 587,
        tls_verify: bool = False,
    ) -> None:
        self._address = address
        self._password = password
        self._imap_host = imap_host
        self._imap_port = imap_port
        self._smtp_host = smtp_host
        self._smtp_port = smtp_port
        self._tls_verify = tls_verify

    @classmethod
    def from_env(cls) -> "AgentMailbox | None":
        """Build a mailbox from runtime-injected env vars, or ``None``.

        Returns ``None`` when ``A2A_MAIL_ADDRESS`` or ``A2A_MAIL_PASSWORD``
        is absent — i.e. the control plane did not provision a mailbox for
        this agent.
        """
        address = (os.environ.get("A2A_MAIL_ADDRESS") or "").strip()
        password = os.environ.get("A2A_MAIL_PASSWORD") or ""
        if not address or not password:
            return None
        domain = address.rsplit("@", 1)[-1]
        return cls(
            address=address,
            password=password,
            imap_host=os.environ.get("A2A_MAIL_IMAP_HOST") or domain,
            imap_port=int(os.environ.get("A2A_MAIL_IMAP_PORT") or 993),
            smtp_host=os.environ.get("A2A_MAIL_SMTP_HOST") or domain,
            smtp_port=int(os.environ.get("A2A_MAIL_SMTP_PORT") or 587),
            tls_verify=_env_flag("A2A_MAIL_TLS_VERIFY", default=False),
        )

    @property
    def address(self) -> str:
        """The mailbox's own email address (always the From identity)."""
        return self._address

    # --- IMAP -----------------------------------------------------------

    def list_messages(
        self, limit: int = 20, unseen_only: bool = False
    ) -> list[dict[str, Any]]:
        """List INBOX messages, newest first.

        Returns dicts with ``uid``, ``sender``, ``subject``, ``date`` and a
        short whitespace-collapsed ``snippet`` of the body.
        """
        conn = self._imap()
        try:
            conn.select("INBOX", readonly=True)
            criterion = "UNSEEN" if unseen_only else "ALL"
            _typ, data = conn.uid("SEARCH", None, criterion)
            uids = (data[0] or b"").split() if data else []
            out: list[dict[str, Any]] = []
            for raw_uid in reversed(uids[-max(limit, 0):]):
                uid = raw_uid.decode("ascii", errors="replace")
                msg = self._fetch(conn, uid)
                out.append(
                    {
                        "uid": uid,
                        "sender": str(msg.get("From", "")),
                        "subject": str(msg.get("Subject", "")),
                        "date": str(msg.get("Date", "")),
                        "snippet": _snippet(msg),
                    }
                )
            return out
        finally:
            self._close(conn)

    def read(self, uid: str | int) -> dict[str, Any]:
        """Fetch one message by UID and return its decoded text body.

        Prefers ``text/plain``; falls back to tag-stripped ``text/html``.
        The body is truncated to ~256KB.
        """
        conn = self._imap()
        try:
            conn.select("INBOX", readonly=True)
            msg = self._fetch(conn, uid)
        finally:
            self._close(conn)
        return {
            "uid": str(uid),
            "sender": str(msg.get("From", "")),
            "subject": str(msg.get("Subject", "")),
            "date": str(msg.get("Date", "")),
            "body": _extract_body(msg),
        }

    # --- SMTP -----------------------------------------------------------

    def send(self, to: str, subject: str, body: str) -> str:
        """Send a plain-text message and return its Message-ID.

        The From address is always the agent's own mailbox, and every
        outbound message carries ``Auto-Submitted: auto-generated`` so
        receiving MTAs can recognise agent traffic.
        """
        return self._send(to=to, subject=subject, body=body)

    def reply(self, uid: str | int, body: str) -> str:
        """Reply to a message by UID: Re: subject, threading headers set."""
        conn = self._imap()
        try:
            conn.select("INBOX", readonly=True)
            original = self._fetch(conn, uid)
        finally:
            self._close(conn)
        to = str(original.get("Reply-To") or original.get("From") or "").strip()
        if not to:
            raise LookupError(f"message {uid} has no sender to reply to")
        original_id = str(original.get("Message-ID", "")).strip()
        references = " ".join(
            token
            for token in (str(original.get("References", "")).strip(), original_id)
            if token
        )
        return self._send(
            to=to,
            subject=_reply_subject(str(original.get("Subject", ""))),
            body=body,
            in_reply_to=original_id or None,
            references=references or None,
        )

    # --- internals ------------------------------------------------------

    def _ssl_context(self) -> ssl.SSLContext:
        context = ssl.create_default_context()
        if not self._tls_verify:
            # In-cluster mail certs don't match the service hostname.
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        return context

    def _imap(self) -> imaplib.IMAP4_SSL:
        conn = imaplib.IMAP4_SSL(
            self._imap_host, self._imap_port, ssl_context=self._ssl_context()
        )
        conn.login(self._address, self._password)
        return conn

    @staticmethod
    def _close(conn: imaplib.IMAP4_SSL) -> None:
        for step in ("close", "logout"):
            try:
                getattr(conn, step)()
            except Exception:  # noqa: BLE001 - teardown is best-effort
                pass

    @staticmethod
    def _fetch(conn: imaplib.IMAP4_SSL, uid: str | int) -> Message:
        typ, data = conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
        if typ != "OK":
            raise LookupError(f"IMAP fetch failed for uid {uid}: {typ}")
        return email.message_from_bytes(
            _fetch_payload(data), policy=email.policy.default
        )

    def _send(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        in_reply_to: str | None = None,
        references: str | None = None,
    ) -> str:
        msg = EmailMessage()
        msg["From"] = self._address
        msg["To"] = to
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain=self._address.rsplit("@", 1)[-1])
        msg["Auto-Submitted"] = "auto-generated"
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
        if references:
            msg["References"] = references
        msg.set_content(body)
        with smtplib.SMTP(self._smtp_host, self._smtp_port, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls(context=self._ssl_context())
            smtp.login(self._address, self._password)
            smtp.send_message(msg)
        return str(msg["Message-ID"])
