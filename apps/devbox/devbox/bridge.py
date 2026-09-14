"""WebSocket ⇄ sshd bridge for the a2a devbox.

The devbox runs sshd bound to 127.0.0.1 only. This aiohttp server is the sole
network-facing process (the Knative serving port). It accepts a WebSocket at
``/ssh`` and pipes its binary frames to a TCP connection to ``127.0.0.1:22`` —
so an OpenSSH client can reach sshd through the platform's HTTP(S) ingress via a
``ProxyCommand`` (``a2a ssh-proxy``). No raw-TCP ingress required.

Every other path is reverse-proxied to the agent's own dev server running on
``127.0.0.1:<A2A_DEVBOX_AGENT_PORT>`` (``a2a dev`` cloud mode runs the agent's
``uvicorn --reload`` there). That makes the box's single public origin,
``https://<agent>-devbox.<host>/``, a live, shareable URL for the agent while
``/ssh`` stays the file-sync + shell channel. HTTP responses stream (SSE-safe)
and WebSocket upgrades pass through, so agent chat/streaming endpoints work.

Two independent auth layers protect the box:

1. **Transport (this bridge)** — a stateless Ed25519 platform grant scoped to
   this box (audience = ``<agent>-devbox``), verified against
   ``A2A_GRANT_VERIFYING_KEY`` (see ``_authorized``). A legacy shared token is
   still accepted as a rollout fallback.
2. **Session (sshd)** — the caller's ephemeral public key in ``authorized_keys``.

A request that passes the bridge still cannot open a shell without the matching
SSH private key, and a request with the key still cannot reach sshd without a
valid access token.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import os

import aiohttp
from aiohttp import WSMsgType, web

log = logging.getLogger("devbox.bridge")

SSH_HOST = os.environ.get("A2A_DEVBOX_SSH_HOST", "127.0.0.1")
SSH_PORT = int(os.environ.get("A2A_DEVBOX_SSH_PORT", "22"))
CHUNK = 65536

# Where the agent's own dev server (uvicorn --reload) listens inside the box.
# The bridge fronts it on the public port; keep them distinct.
AGENT_HOST = os.environ.get("A2A_DEVBOX_AGENT_HOST", "127.0.0.1")
AGENT_PORT = int(os.environ.get("A2A_DEVBOX_AGENT_PORT", "8001"))
# Response/request headers the proxy must not forward verbatim (hop-by-hop per
# RFC 7230 §6.1, plus Host which we rewrite to the upstream).
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    }
)


def _expected_audience() -> str:
    """The grant audience this box accepts — its own Knative service name."""
    aud = os.environ.get("A2A_DEVBOX_AUDIENCE", "").strip()
    if aud:
        return aud
    agent = os.environ.get("A2A_AGENT_NAME", "").strip()
    return f"{agent}-devbox" if agent else ""


def _grant_ok(token: str) -> bool:
    """Verify a stateless Ed25519 platform grant scoped to this box. Signature +
    expiry are checked by a2a_pack against A2A_GRANT_VERIFYING_KEY; we add the
    audience check on top."""
    try:
        from a2a_pack.grants import verify_grant

        grant = verify_grant(token)
    except Exception:
        return False
    expected = _expected_audience()
    return bool(expected) and getattr(grant, "audience", None) == expected


def _expected_token() -> str | None:
    """Legacy shared token — kept only so a new bridge can accept an old
    control plane during a rollout. Prefer the grant path above."""
    token = os.environ.get("A2A_DEVBOX_ACCESS_TOKEN", "").strip()
    return token or None


def _presented_token(request: web.Request) -> str:
    """Pull the access token from the query string, an Authorization header, or
    the WebSocket subprotocol (``bearer.<token>``) so clients that cannot set
    arbitrary headers can still authenticate."""
    q = request.query.get("token")
    if q:
        return q.strip()
    auth = request.headers.get("Authorization", "")
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    proto = request.headers.get("Sec-WebSocket-Protocol", "")
    for part in proto.split(","):
        part = part.strip()
        if part.startswith("bearer."):
            return part[len("bearer.") :].strip()
    return ""


def _authorized(request: web.Request) -> bool:
    token = _presented_token(request)
    if not token:
        return False
    # Preferred: a stateless Ed25519 grant scoped to this box (audience check).
    if _grant_ok(token):
        return True
    # Rollout fallback: an old control plane still hands out a shared token.
    legacy = _expected_token()
    return legacy is not None and hmac.compare_digest(token, legacy)


async def health(request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def ssh(request: web.Request) -> web.WebSocketResponse:
    if not _authorized(request):
        raise web.HTTPUnauthorized(text="invalid devbox access token")

    ws = web.WebSocketResponse(protocols=("ssh",), heartbeat=30.0)
    await ws.prepare(request)

    try:
        reader, writer = await asyncio.open_connection(SSH_HOST, SSH_PORT)
    except OSError as exc:
        log.warning("sshd unreachable: %s", exc)
        await ws.close(code=1011, message=b"sshd unreachable")
        return ws

    peer = request.remote
    log.info("ssh session opened from %s", peer)

    async def ws_to_tcp() -> None:
        try:
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    writer.write(msg.data)
                    await writer.drain()
                elif msg.type == WSMsgType.TEXT:
                    writer.write(msg.data.encode())
                    await writer.drain()
                else:  # CLOSE / CLOSING / ERROR
                    break
        finally:
            writer.close()

    async def tcp_to_ws() -> None:
        try:
            while True:
                data = await reader.read(CHUNK)
                if not data:
                    break
                await ws.send_bytes(data)
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            if not ws.closed:
                await ws.close()

    await asyncio.gather(ws_to_tcp(), tcp_to_ws())
    log.info("ssh session closed from %s", peer)
    return ws


def _agent_url(request: web.Request) -> str:
    return f"http://{AGENT_HOST}:{AGENT_PORT}{request.rel_url}"


async def _proxy_ws(request: web.Request) -> web.WebSocketResponse:
    """Pass a WebSocket upgrade through to the agent (chat/streaming endpoints)."""
    client_ws = web.WebSocketResponse()
    await client_ws.prepare(request)
    session: aiohttp.ClientSession = request.app["agent_session"]
    try:
        async with session.ws_connect(_agent_url(request), max_msg_size=0) as up_ws:

            async def c2u() -> None:
                async for msg in client_ws:
                    if msg.type == WSMsgType.TEXT:
                        await up_ws.send_str(msg.data)
                    elif msg.type == WSMsgType.BINARY:
                        await up_ws.send_bytes(msg.data)
                    else:
                        break

            async def u2c() -> None:
                async for msg in up_ws:
                    if msg.type == WSMsgType.TEXT:
                        await client_ws.send_str(msg.data)
                    elif msg.type == WSMsgType.BINARY:
                        await client_ws.send_bytes(msg.data)
                    else:
                        break

            await asyncio.gather(c2u(), u2c())
    except aiohttp.ClientError as exc:
        log.info("agent ws unreachable: %s", exc)
        if not client_ws.closed:
            await client_ws.close(code=1011, message=b"agent starting")
    return client_ws


async def proxy(request: web.Request) -> web.StreamResponse:
    """Reverse-proxy anything that isn't /ssh or /healthz to the agent dev
    server. Streams the response body so SSE / chunked replies flow live."""
    if request.headers.get("Upgrade", "").lower() == "websocket":
        return await _proxy_ws(request)

    session: aiohttp.ClientSession = request.app["agent_session"]
    fwd_headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP}
    try:
        async with session.request(
            request.method,
            _agent_url(request),
            headers=fwd_headers,
            data=request.content if request.body_exists else None,
            allow_redirects=False,
        ) as up:
            resp = web.StreamResponse(status=up.status, reason=up.reason)
            for k, v in up.headers.items():
                if k.lower() not in _HOP_BY_HOP:
                    resp.headers[k] = v
            await resp.prepare(request)
            async for chunk in up.content.iter_chunked(CHUNK):
                await resp.write(chunk)
            await resp.write_eof()
            return resp
    except aiohttp.ClientError as exc:
        # Agent not up yet (cold start / reload in progress) — tell the caller
        # to retry rather than 500.
        log.info("agent unreachable, returning 503: %s", exc)
        return web.Response(status=503, text="agent starting", headers={"Retry-After": "1"})


async def _on_startup(app: web.Application) -> None:
    app["agent_session"] = aiohttp.ClientSession(auto_decompress=False)


async def _on_cleanup(app: web.Application) -> None:
    await app["agent_session"].close()


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/healthz", health)
    app.router.add_get("/ssh", ssh)
    # Everything else fronts the agent's own dev server (registered last so the
    # reserved paths above win).
    app.router.add_route("*", "/{tail:.*}", proxy)
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    return app


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    port = int(os.environ.get("A2A_DEVBOX_PORT", "8000"))
    web.run_app(build_app(), host="0.0.0.0", port=port, print=None)


if __name__ == "__main__":
    main()
