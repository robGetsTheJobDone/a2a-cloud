from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx


_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_SAFE_CROSS_ORIGIN_HEADERS = {"accept", "accept-encoding", "accept-language", "user-agent"}
_MAX_URL_LENGTH = 8 * 1024

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


class SafeHTTPError(ValueError):
    """Raised when a guarded outbound HTTP request cannot be completed."""


class UnsafeURLError(SafeHTTPError):
    """Raised when a URL could reach a non-public network target."""


class InsecureTransportError(UnsafeURLError):
    """Raised before sensitive request data could cross cleartext HTTP."""


class ResponseTooLargeError(SafeHTTPError):
    """Raised when a response exceeds its configured decoded-byte limit."""


class TooManyRedirectsError(SafeHTTPError):
    """Raised when a response exceeds its configured redirect limit."""


class SafeHTTPTimeoutError(SafeHTTPError):
    """Raised when the complete guarded request exceeds its time limit."""


@dataclass(frozen=True)
class SafeHTTPResponse:
    status_code: int
    url: str
    headers: Mapping[str, str]
    content: bytes
    encoding: str = "utf-8"

    @property
    def text(self) -> str:
        try:
            return self.content.decode(self.encoding or "utf-8")
        except (LookupError, UnicodeDecodeError):
            return self.content.decode("utf-8", errors="replace")


@dataclass(frozen=True)
class _ValidatedURL:
    url: str
    scheme: str
    hostname: str
    port: int
    authority: str
    addresses: tuple[IPAddress, ...]

    @property
    def origin(self) -> tuple[str, str, int]:
        return self.scheme, self.hostname, self.port


@dataclass(frozen=True)
class _NetworkResponse:
    status_code: int
    headers: Mapping[str, str]
    content: bytes
    encoding: str


async def safe_fetch_url(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    sensitive_headers: Mapping[str, str] | None = None,
    params: Mapping[str, Any] | None = None,
    max_response_bytes: int,
    timeout_seconds: float,
    max_redirects: int = 5,
    require_https: bool = False,
) -> SafeHTTPResponse:
    return await safe_request_url(
        "GET",
        url,
        headers=headers,
        sensitive_headers=sensitive_headers,
        params=params,
        max_response_bytes=max_response_bytes,
        timeout_seconds=timeout_seconds,
        max_redirects=max_redirects,
        require_https=require_https,
    )


async def safe_request_url(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    sensitive_headers: Mapping[str, str] | None = None,
    params: Mapping[str, Any] | None = None,
    json_body: Any | None = None,
    form_data: Mapping[str, Any] | None = None,
    max_response_bytes: int,
    timeout_seconds: float,
    max_redirects: int = 0,
    client_options: Mapping[str, Any] | None = None,
    require_https: bool = False,
) -> SafeHTTPResponse:
    """Request a public URL without allowing DNS or redirects to bypass checks.

    Each hop is resolved and validated independently. The actual connection is
    made to one of those numeric addresses, while Host and TLS SNI retain the
    validated hostname. This prevents a second resolver lookup from rebinding
    the request to a private address after validation.
    """
    if max_response_bytes <= 0:
        raise ValueError("max_response_bytes must be positive")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if max_redirects < 0:
        raise ValueError("max_redirects must not be negative")
    request_method = method.strip().upper()
    if request_method not in {"GET", "POST"}:
        raise ValueError("safe_request_url only supports GET and POST")
    if json_body is not None and form_data is not None:
        raise ValueError("json_body and form_data are mutually exclusive")
    safe_client_options = dict(client_options or {})
    unsupported_options = set(safe_client_options) - {"cert", "verify"}
    if unsupported_options:
        raise ValueError("unsupported guarded HTTP client option")

    request_headers = {str(key): str(value) for key, value in (headers or {}).items()}
    sensitive_header_names = {
        str(key).lower() for key in (sensitive_headers or {}).keys()
    }
    request_headers = {
        key: value
        for key, value in request_headers.items()
        if key.lower() not in sensitive_header_names
    }
    request_headers.update(
        {str(key): str(value) for key, value in (sensitive_headers or {}).items()}
    )
    # Query authentication, credential headers, client certificates, and POST
    # bodies must never cross cleartext HTTP. ``require_https`` lets a caller
    # classify a payload as sensitive even when its field names are opaque to
    # this transport layer. POST is conservative by design: agent arguments and
    # grants are user data even when the request has no separate auth header.
    initial_parts = _parse_http_url(url)
    confidential_transport = (
        require_https
        or request_method == "POST"
        or bool(sensitive_headers)
        or bool(params)
        or bool(urlsplit(initial_parts.url).query)
        or json_body is not None
        or form_data is not None
        or bool(safe_client_options.get("cert"))
    )
    _require_confidential_transport(initial_parts, confidential_transport)
    initial_url = initial_parts.url
    try:
        current_url = str(httpx.URL(initial_url).copy_merge_params(params or {}))
    except (TypeError, ValueError, httpx.InvalidURL):
        raise UnsafeURLError("URL or query parameters are invalid") from None
    redirects = 0

    try:
        async with asyncio.timeout(timeout_seconds):
            while True:
                target = await _validate_public_url(current_url)
                network_response = await _request_validated_url(
                    target,
                    method=request_method,
                    headers=request_headers,
                    json_body=json_body,
                    form_data=form_data,
                    max_response_bytes=max_response_bytes,
                    timeout_seconds=timeout_seconds,
                    client_options=safe_client_options,
                )

                location = network_response.headers.get("location")
                if network_response.status_code not in _REDIRECT_STATUSES or not location:
                    return SafeHTTPResponse(
                        status_code=network_response.status_code,
                        url=_response_url(target.url),
                        headers=network_response.headers,
                        content=network_response.content,
                        encoding=network_response.encoding,
                    )

                if redirects >= max_redirects:
                    raise TooManyRedirectsError(
                        f"remote URL exceeded the {max_redirects}-redirect limit"
                    )
                redirects += 1
                next_url = urljoin(target.url, location)
                next_parts = _parse_http_url(next_url)
                _require_confidential_transport(
                    next_parts,
                    confidential_transport or bool(urlsplit(next_parts.url).query),
                )
                next_origin = (
                    next_parts.scheme,
                    next_parts.hostname,
                    next_parts.port,
                )
                if next_origin != target.origin:
                    if params:
                        raise UnsafeURLError(
                            "URL with query credentials must not redirect cross-origin"
                        )
                    request_headers = {
                        key: value
                        for key, value in request_headers.items()
                        if key.lower() in _SAFE_CROSS_ORIGIN_HEADERS
                        and key.lower() not in sensitive_header_names
                    }
                if network_response.status_code == 303 or (
                    network_response.status_code in {301, 302}
                    and request_method == "POST"
                ):
                    request_method = "GET"
                    json_body = None
                    form_data = None
                current_url = next_parts.url
    except TimeoutError:
        raise SafeHTTPTimeoutError(
            f"remote URL exceeded the {timeout_seconds:g}-second time limit"
        ) from None
    except httpx.TimeoutException:
        raise SafeHTTPTimeoutError(
            f"remote URL exceeded the {timeout_seconds:g}-second time limit"
        ) from None


async def validate_public_url(url: str) -> str:
    """Resolve and reject non-public targets without making an HTTP request."""
    return (await _validate_public_url(url)).url


def _require_confidential_transport(
    target: _ParsedURL,
    required: bool,
) -> None:
    if required and target.scheme != "https":
        raise InsecureTransportError("HTTPS is required for sensitive outbound requests")


def _response_url(url: str) -> str:
    """Return a persistence-safe URL without request or redirect query secrets."""
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


async def _validate_public_url(url: str) -> _ValidatedURL:
    parsed = _parse_http_url(url)
    addresses = await _resolve_host_addresses(parsed.hostname, parsed.port)
    if not addresses:
        raise SafeHTTPError("URL host did not resolve to an IP address")
    if any(not _is_public_address(address) for address in addresses):
        raise UnsafeURLError("URL host resolves to a non-public IP address")
    return _ValidatedURL(
        url=parsed.url,
        scheme=parsed.scheme,
        hostname=parsed.hostname,
        port=parsed.port,
        authority=parsed.authority,
        addresses=addresses,
    )


@dataclass(frozen=True)
class _ParsedURL:
    url: str
    scheme: str
    hostname: str
    port: int
    authority: str


def _parse_http_url(url: str) -> _ParsedURL:
    value = str(url).strip()
    if not value or len(value) > _MAX_URL_LENGTH:
        raise UnsafeURLError("URL is empty or too long")
    if "\\" in value or any(ord(char) < 32 or char.isspace() for char in value):
        raise UnsafeURLError("URL contains invalid characters")

    try:
        parsed = urlsplit(value)
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname
        explicit_port = parsed.port
    except ValueError as exc:
        raise UnsafeURLError("URL host or port is invalid") from exc
    if scheme not in {"http", "https"}:
        raise UnsafeURLError("URL must use http:// or https://")
    if not hostname:
        raise UnsafeURLError("URL must include a host")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeURLError("URL must not include credentials")
    if explicit_port == 0 or parsed.netloc.endswith(":"):
        raise UnsafeURLError("URL port is invalid")
    if "%" in hostname:
        raise UnsafeURLError("URL host must not include percent escapes or a zone identifier")

    literal: IPAddress | None
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None

    if literal is not None:
        canonical_host = literal.compressed
    else:
        try:
            canonical_host = hostname.rstrip(".").encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise UnsafeURLError("URL host is invalid") from exc
        if not canonical_host or len(canonical_host) > 253:
            raise UnsafeURLError("URL host is invalid")
        if canonical_host == "localhost" or canonical_host.endswith(".localhost"):
            raise UnsafeURLError("localhost URLs are not allowed")

    port = explicit_port or (443 if scheme == "https" else 80)
    host_for_authority = (
        f"[{canonical_host}]" if isinstance(literal, ipaddress.IPv6Address) else canonical_host
    )
    authority = host_for_authority
    if explicit_port is not None and explicit_port != (443 if scheme == "https" else 80):
        authority = f"{authority}:{explicit_port}"
    normalized = urlunsplit((scheme, authority, parsed.path or "/", parsed.query, ""))
    return _ParsedURL(
        url=normalized,
        scheme=scheme,
        hostname=canonical_host,
        port=port,
        authority=authority,
    )


async def _resolve_host_addresses(hostname: str, port: int) -> tuple[IPAddress, ...]:
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None:
        return (literal,)

    loop = asyncio.get_running_loop()
    try:
        records = await loop.getaddrinfo(
            hostname,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except socket.gaierror as exc:
        raise SafeHTTPError("URL host could not be resolved") from exc

    addresses: list[IPAddress] = []
    seen: set[IPAddress] = set()
    for _family, _type, _proto, _canonical_name, sockaddr in records:
        try:
            address = ipaddress.ip_address(sockaddr[0])
        except ValueError as exc:
            raise UnsafeURLError("URL host resolved to an invalid IP address") from exc
        if address not in seen:
            seen.add(address)
            addresses.append(address)
    return tuple(addresses)


def _is_public_address(address: IPAddress) -> bool:
    if not address.is_global:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        embedded: list[ipaddress.IPv4Address] = []
        if address.ipv4_mapped is not None:
            embedded.append(address.ipv4_mapped)
        if address.sixtofour is not None:
            embedded.append(address.sixtofour)
        if address.teredo is not None:
            embedded.extend(address.teredo)
        if address in ipaddress.ip_network("64:ff9b::/96"):
            embedded.append(ipaddress.IPv4Address(int(address) & 0xFFFFFFFF))
        if any(not candidate.is_global for candidate in embedded):
            return False
    return True


async def _request_validated_url(
    target: _ValidatedURL,
    *,
    method: str,
    headers: Mapping[str, str],
    json_body: Any | None,
    form_data: Mapping[str, Any] | None,
    max_response_bytes: int,
    timeout_seconds: float,
    client_options: Mapping[str, Any],
) -> _NetworkResponse:
    last_error: httpx.TransportError | None = None
    for address in target.addresses:
        try:
            return await _request_address(
                target,
                address,
                method=method,
                headers=headers,
                json_body=json_body,
                form_data=form_data,
                max_response_bytes=max_response_bytes,
                timeout_seconds=timeout_seconds,
                client_options=client_options,
            )
        except httpx.TransportError as exc:
            last_error = exc
    if isinstance(last_error, httpx.TimeoutException):
        raise last_error
    raise SafeHTTPError("remote URL could not be reached") from None


async def _request_address(
    target: _ValidatedURL,
    address: IPAddress,
    *,
    method: str = "GET",
    headers: Mapping[str, str],
    json_body: Any | None = None,
    form_data: Mapping[str, Any] | None = None,
    max_response_bytes: int,
    timeout_seconds: float,
    client_options: Mapping[str, Any] | None = None,
) -> _NetworkResponse:
    address_host = f"[{address.compressed}]" if address.version == 6 else address.compressed
    pinned_url = urlunsplit(
        (
            target.scheme,
            f"{address_host}:{target.port}",
            urlsplit(target.url).path,
            urlsplit(target.url).query,
            "",
        )
    )
    request_headers = {
        key: value for key, value in headers.items() if key.lower() != "host"
    }
    if not any(key.lower() == "accept-encoding" for key in request_headers):
        request_headers["accept-encoding"] = "identity"
    request_headers["host"] = target.authority
    phase_timeout = max(0.1, min(timeout_seconds, 5.0))
    timeout = httpx.Timeout(
        timeout_seconds,
        connect=phase_timeout,
        read=phase_timeout,
        write=phase_timeout,
        pool=phase_timeout,
    )
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
        **dict(client_options or {}),
    ) as client:
        async with client.stream(
            method,
            pinned_url,
            headers=request_headers,
            json=json_body,
            data=form_data,
            extensions={"sni_hostname": target.hostname},
        ) as response:
            response_headers = dict(response.headers)
            if response.status_code in _REDIRECT_STATUSES and response_headers.get("location"):
                return _NetworkResponse(
                    status_code=response.status_code,
                    headers=response_headers,
                    content=b"",
                    encoding="utf-8",
                )
            content = await _read_limited_body(response, max_response_bytes)
            return _NetworkResponse(
                status_code=response.status_code,
                headers=response_headers,
                content=content,
                encoding=response.encoding or "utf-8",
            )


async def _read_limited_body(response: httpx.Response, max_bytes: int) -> bytes:
    content_length = response.headers.get("content-length")
    if content_length:
        try:
            declared_length = int(content_length)
        except ValueError:
            declared_length = None
        if declared_length is not None and declared_length > max_bytes:
            raise ResponseTooLargeError(
                f"remote response exceeds the {max_bytes}-byte limit"
            )

    body = bytearray()
    async for chunk in response.aiter_bytes():
        if len(body) + len(chunk) > max_bytes:
            raise ResponseTooLargeError(
                f"remote response exceeds the {max_bytes}-byte limit"
            )
        body.extend(chunk)
    return bytes(body)
