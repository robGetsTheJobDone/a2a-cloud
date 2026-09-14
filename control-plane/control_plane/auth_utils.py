from __future__ import annotations

import re
from urllib.parse import unquote, urlencode, urlsplit

from fastapi import HTTPException

from .config import settings


def slugify_org(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:96] or "org"


def normalize_domains(values: list[str]) -> list[str]:
    out: list[str] = []
    for raw in values:
        domain = raw.strip().lower().removeprefix("@")
        if not domain or "." not in domain:
            continue
        if domain not in out:
            out.append(domain)
    return out


def domain_verification_record(domain: str, token: str) -> tuple[str, str]:
    return f"_a2a-sso.{domain}", f"a2a-sso-verification={token}"


def verify_domain_txt_record(domain: str, token: str) -> bool:
    name, expected = domain_verification_record(domain, token)
    try:
        import dns.resolver
    except ImportError as exc:  # pragma: no cover - depends on runtime image
        raise HTTPException(500, "DNS resolver dependency is not installed") from exc
    try:
        answers = dns.resolver.resolve(name, "TXT")
    except Exception:
        return False
    for answer in answers:
        parts = []
        for raw in getattr(answer, "strings", []):
            parts.append(raw.decode() if isinstance(raw, bytes) else str(raw))
        if not parts:
            parts = [str(answer).strip('"')]
        if "".join(parts).strip().strip('"') == expected:
            return True
    return False


def sanitize_redirect(value: str | None) -> str:
    if not value:
        return "/"
    candidate = value[:512]
    if not candidate.startswith("/") or candidate.startswith("//"):
        return "/"
    if "\\" in candidate or any(ord(char) < 32 or ord(char) == 127 for char in candidate):
        return "/"
    parsed = urlsplit(candidate)
    if parsed.scheme or parsed.netloc:
        return "/"

    # Validate a few decoding layers without rewriting the returned path. This
    # catches encoded network paths, backslashes, and header-control bytes.
    decoded = candidate
    for _ in range(4):
        next_value = unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
        if (
            decoded.startswith("//")
            or "\\" in decoded
            or any(ord(char) < 32 or ord(char) == 127 for char in decoded)
        ):
            return "/"
    return candidate


def dashboard_error_url(message: str) -> str:
    return f"{settings.dashboard_url.rstrip('/')}/#{urlencode({'auth_error': message})}"
