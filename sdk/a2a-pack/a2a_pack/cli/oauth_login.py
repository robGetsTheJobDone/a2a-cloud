"""Keycloak OAuth helpers for the ``a2a`` CLI."""
from __future__ import annotations

import base64
import hashlib
import json
import queue
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from . import credentials
from .api_client import ControlPlaneClient

DEFAULT_OAUTH_ISSUER = "https://auth.a2acloud.io/realms/a2acloud"
DEFAULT_OAUTH_CLIENT_ID = "a2acloud-cli"
DEFAULT_OAUTH_SCOPE = "openid email offline_access mcp:invoke agent:read"
DEFAULT_REDIRECT_PORT = 41873
CALLBACK_PATH = "/callback"


def login_with_browser(
    *,
    api_url: str = credentials.DEFAULT_API_URL,
    issuer: str = DEFAULT_OAUTH_ISSUER,
    client_id: str = DEFAULT_OAUTH_CLIENT_ID,
    scope: str = DEFAULT_OAUTH_SCOPE,
    port: int = DEFAULT_REDIRECT_PORT,
    open_browser: bool = True,
    timeout_seconds: int = 180,
    on_authorization_url: Callable[[str], None] | None = None,
) -> credentials.Credentials:
    api_url = api_url.rstrip("/")
    issuer = issuer.rstrip("/")
    cfg = discover_openid_configuration(issuer)
    verifier, challenge = create_pkce_pair()
    state = _url_safe(32)
    callback = _listen_for_callback(port=port, state=state)
    try:
        authorization_url = build_authorization_url(
            authorization_endpoint=cfg["authorization_endpoint"],
            client_id=client_id,
            redirect_uri=callback.redirect_uri,
            scope=scope,
            state=state,
            code_challenge=challenge,
        )
        if on_authorization_url is not None:
            on_authorization_url(authorization_url)
        if open_browser:
            webbrowser.open(authorization_url)
        code = callback.wait(timeout_seconds)
        token = exchange_authorization_code(
            token_endpoint=cfg["token_endpoint"],
            client_id=client_id,
            redirect_uri=callback.redirect_uri,
            code=code,
            code_verifier=verifier,
        )
        creds = credentials_from_token(
            api_url=api_url,
            token=token,
            issuer=issuer,
            client_id=client_id,
            scope=scope,
        )
        credentials.save_credentials(creds)
        return creds
    finally:
        callback.close()


def login_with_access_token(
    token: str,
    *,
    api_url: str = credentials.DEFAULT_API_URL,
    issuer: str = DEFAULT_OAUTH_ISSUER,
    client_id: str = DEFAULT_OAUTH_CLIENT_ID,
    scope: str = DEFAULT_OAUTH_SCOPE,
) -> credentials.Credentials:
    creds = credentials_from_token(
        api_url=api_url.rstrip("/"),
        token={"access_token": token},
        issuer=issuer.rstrip("/"),
        client_id=client_id,
        scope=scope,
    )
    credentials.save_credentials(creds)
    return creds


def refresh_credentials_if_needed(
    creds: credentials.Credentials,
    *,
    min_ttl_seconds: int = 60,
    force: bool = False,
) -> credentials.Credentials:
    if not creds.refresh_token or not creds.oauth_issuer or not creds.client_id:
        return creds
    if not force and _credentials_fresh(creds, min_ttl_seconds):
        return creds

    latest = credentials.load()
    if latest is not None and _same_oauth_client(latest, creds):
        if not force and _credentials_fresh(latest, min_ttl_seconds):
            return latest
        if latest.refresh_token and latest.oauth_issuer and latest.client_id:
            creds = latest

    try:
        return refresh_credentials(creds)
    except RuntimeError as exc:
        raise RuntimeError(
            f"Login expired or revoked. Run `a2a login` and retry. ({exc})"
        ) from exc


def refresh_credentials(creds: credentials.Credentials) -> credentials.Credentials:
    if not creds.refresh_token or not creds.oauth_issuer or not creds.client_id:
        return creds
    cfg = discover_openid_configuration(creds.oauth_issuer)
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(
            cfg["token_endpoint"],
            data={
                "grant_type": "refresh_token",
                "client_id": creds.client_id,
                "refresh_token": creds.refresh_token,
            },
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
    token = parse_token_response(resp)
    access_token = str(token["access_token"])
    next_creds = credentials.Credentials(
        api_url=creds.api_url,
        token=access_token,
        email=creds.email,
        user_id=creds.user_id,
        bucket=creds.bucket,
        refresh_token=str(token.get("refresh_token") or creds.refresh_token),
        expires_at=_expires_at(token.get("expires_in")) or _jwt_exp(access_token),
        oauth_issuer=creds.oauth_issuer,
        client_id=creds.client_id,
        scope=str(token.get("scope") or creds.scope or ""),
    )
    credentials.save_credentials(next_creds)
    return next_creds


def discover_openid_configuration(issuer: str) -> dict[str, str]:
    url = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(url, headers={"accept": "application/json"})
    if resp.status_code >= 400:
        raise RuntimeError(f"OpenID discovery failed: {resp.status_code}")
    parsed = resp.json()
    data = parsed if isinstance(parsed, dict) else {}
    authorization_endpoint = data.get("authorization_endpoint")
    token_endpoint = data.get("token_endpoint")
    if not isinstance(authorization_endpoint, str) or not isinstance(token_endpoint, str):
        raise RuntimeError("OpenID discovery document is missing authorization/token endpoints")
    return {
        "authorization_endpoint": authorization_endpoint,
        "token_endpoint": token_endpoint,
    }


def create_pkce_pair() -> tuple[str, str]:
    verifier = _url_safe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def build_authorization_url(
    *,
    authorization_endpoint: str,
    client_id: str,
    redirect_uri: str,
    scope: str,
    state: str,
    code_challenge: str,
) -> str:
    return (
        f"{authorization_endpoint}?"
        + urlencode(
            {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": scope,
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
    )


def exchange_authorization_code(
    *,
    token_endpoint: str,
    client_id: str,
    redirect_uri: str,
    code: str,
    code_verifier: str,
) -> dict[str, Any]:
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(
            token_endpoint,
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "code": code,
                "code_verifier": code_verifier,
            },
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
    return parse_token_response(resp)


def parse_token_response(resp: httpx.Response) -> dict[str, Any]:
    try:
        parsed = resp.json()
        data = parsed if isinstance(parsed, dict) else {}
    except ValueError:
        data = {}
    if resp.status_code >= 400 or not data.get("access_token"):
        message = data.get("error_description") or data.get("error") or resp.text or resp.reason_phrase
        raise RuntimeError(f"Keycloak token exchange failed: {message}")
    return data


def credentials_from_token(
    *,
    api_url: str,
    token: dict[str, Any],
    issuer: str,
    client_id: str,
    scope: str,
) -> credentials.Credentials:
    access_token = str(token["access_token"])
    me = ControlPlaneClient(api_url, token=access_token).me()
    user_id = me.get("id") if isinstance(me.get("id"), int) else None
    return credentials.Credentials(
        api_url=api_url,
        token=access_token,
        email=str(me.get("email") or ""),
        user_id=user_id,
        bucket=f"user-{user_id}-files" if user_id is not None else None,
        refresh_token=str(token["refresh_token"]) if token.get("refresh_token") else None,
        expires_at=_expires_at(token.get("expires_in")) or _jwt_exp(access_token),
        oauth_issuer=issuer,
        client_id=client_id,
        scope=str(token.get("scope") or scope),
    )


class _CallbackServer:
    def __init__(self, server: HTTPServer, result: queue.Queue[object]) -> None:
        self._server = server
        self._result = result
        port = int(server.server_address[1])
        self.redirect_uri = f"http://127.0.0.1:{port}{CALLBACK_PATH}"
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()

    def wait(self, timeout_seconds: int) -> str:
        try:
            result = self._result.get(timeout=timeout_seconds)
        except queue.Empty as exc:
            raise TimeoutError("timed out waiting for Keycloak login") from exc
        if isinstance(result, Exception):
            raise result
        return str(result)

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=1.0)


def _listen_for_callback(*, port: int, state: str) -> _CallbackServer:
    result: queue.Queue[object] = queue.Queue(maxsize=1)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path != CALLBACK_PATH:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"not found")
                return
            query = parse_qs(parsed.query)
            error = _first(query, "error")
            if error:
                result.put(RuntimeError(_first(query, "error_description") or error))
                _finish_browser(self, ok=False)
                return
            code = _first(query, "code")
            callback_state = _first(query, "state")
            if not code or callback_state != state:
                result.put(RuntimeError("invalid OAuth callback"))
                _finish_browser(self, ok=False)
                return
            result.put(code)
            _finish_browser(self, ok=True)

        def log_message(self, _format: str, *args: Any) -> None:
            return

    return _CallbackServer(HTTPServer(("127.0.0.1", port), Handler), result)


def _finish_browser(handler: BaseHTTPRequestHandler, *, ok: bool) -> None:
    body = (
        "<html><body><h1>"
        + ("a2a is connected" if ok else "a2a login failed")
        + "</h1><p>You can close this tab and return to your terminal.</p></body></html>"
    ).encode("utf-8")
    handler.send_response(200 if ok else 400)
    handler.send_header("content-type", "text/html; charset=utf-8")
    handler.send_header("content-length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _first(values: dict[str, list[str]], key: str) -> str:
    items = values.get(key) or []
    return items[0] if items else ""


def _url_safe(bytes_len: int) -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(bytes_len)).decode("ascii").rstrip("=")


def _expires_at(expires_in: Any) -> int | None:
    return int(time.time()) + int(expires_in) if isinstance(expires_in, int) else None


def _credentials_fresh(creds: credentials.Credentials, min_ttl_seconds: int) -> bool:
    expires_at = creds.expires_at or _jwt_exp(creds.token)
    return bool(expires_at and expires_at - int(time.time()) > min_ttl_seconds)


def _same_oauth_client(
    a: credentials.Credentials,
    b: credentials.Credentials,
) -> bool:
    return (
        a.api_url == b.api_url
        and a.oauth_issuer == b.oauth_issuer
        and a.client_id == b.client_id
    )


def _jwt_exp(token: str) -> int | None:
    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
    except (ValueError, OSError):
        return None
    exp = data.get("exp") if isinstance(data, dict) else None
    return exp if isinstance(exp, int) else None
