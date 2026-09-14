"""``GET /v1/public/receipt-keys`` — the published receipt verifying key.

The endpoint exists so a third party holding an ``X-A2A-Receipt-Token`` can
verify it without trusting us. Two things must hold forever: the key it serves
really verifies our signatures, and it never hands out private key material.

The second one has teeth because a raw Ed25519 private seed and a raw public key
are both 32 opaque bytes, and the two secrets sit side by side in
``platform-secrets``. So whenever the process can sign, the tests below pin the
published key to the *signer* and require a disagreeing
``A2A_RECEIPT_VERIFYING_KEY`` to fail closed.
"""
from __future__ import annotations

import base64
import hashlib

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient

from a2a_pack.receipts import seal_receipt, verify_receipt
from control_plane.routes.public import receipt_keys_router


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(receipt_keys_router)
    return TestClient(app)


def _set_signing_key_only(monkeypatch: pytest.MonkeyPatch) -> Ed25519PrivateKey:
    """Configure only the *private* key — the worst case for a leak."""
    private_key = Ed25519PrivateKey.generate()
    monkeypatch.setenv(
        "A2A_RECEIPT_SIGNING_KEY",
        base64.b64encode(private_key.private_bytes_raw()).decode("ascii"),
    )
    monkeypatch.delenv("A2A_RECEIPT_VERIFYING_KEY", raising=False)
    return private_key


def _encodings_of(raw: bytes, public_raw: bytes | None = None) -> set[str]:
    """Every plausible way ``raw`` could show up in a JSON body."""
    encodings = {
        base64.b64encode(raw).decode("ascii"),
        base64.b64encode(raw).decode("ascii").rstrip("="),
        base64.urlsafe_b64encode(raw).decode("ascii"),
        base64.urlsafe_b64encode(raw).decode("ascii").rstrip("="),
        raw.hex(),
    }
    if public_raw is not None:
        # Ed25519 "expanded" form: private || public, as some libraries encode it.
        encodings.add(base64.b64encode(raw + public_raw).decode("ascii"))
    return encodings


def test_receipt_keys_happy_path_shape() -> None:
    with _client() as client:
        response = client.get("/v1/public/receipt-keys")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["cache-control"] == "public, max-age=300"
    # Offline verification from a browser needs this; nothing here is private.
    assert response.headers["access-control-allow-origin"] == "*"
    body = response.json()
    assert set(body) == {"active_kid", "keys"}
    assert len(body["keys"]) == 1
    key = body["keys"][0]
    assert set(key) == {"kid", "alg", "public_key", "use"}
    assert key["alg"] == "Ed25519"
    assert key["use"] == "receipt"
    assert key["kid"] == body["active_kid"]
    assert len(base64.b64decode(key["public_key"])) == 32


def test_receipt_keys_requires_no_auth_and_verifies_a_real_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_key = _set_signing_key_only(monkeypatch)
    with _client() as client:
        response = client.get("/v1/public/receipt-keys")
    assert response.status_code == 200

    published = base64.b64decode(response.json()["keys"][0]["public_key"])
    assert published == private_key.public_key().public_bytes_raw()

    # A third party pointing A2A_RECEIPT_VERIFYING_KEY at the published key can
    # verify a receipt we sealed — that is the whole point of the endpoint.
    _, token = seal_receipt(agent_name="acme", skill_name="run", started_at=1)
    monkeypatch.delenv("A2A_RECEIPT_SIGNING_KEY", raising=False)
    monkeypatch.setenv(
        "A2A_RECEIPT_VERIFYING_KEY", base64.b64encode(published).decode("ascii")
    )
    assert verify_receipt(token).agent_name == "acme"


def test_receipt_keys_kid_is_stable_sha256_prefix() -> None:
    with _client() as client:
        first = client.get("/v1/public/receipt-keys").json()
        second = client.get("/v1/public/receipt-keys").json()

    assert first == second
    raw = base64.b64decode(first["keys"][0]["public_key"])
    assert first["active_kid"] == hashlib.sha256(raw).hexdigest()[:16]
    assert len(first["active_kid"]) == 16


def test_receipt_keys_503_when_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("A2A_RECEIPT_SIGNING_KEY", raising=False)
    monkeypatch.delenv("A2A_RECEIPT_VERIFYING_KEY", raising=False)
    with _client() as client:
        response = client.get("/v1/public/receipt-keys")

    assert response.status_code == 503
    assert response.json()["detail"]


def test_receipt_keys_503_when_key_material_is_garbage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("A2A_RECEIPT_VERIFYING_KEY", raising=False)
    monkeypatch.setenv("A2A_RECEIPT_SIGNING_KEY", "not-a-key")
    with _client() as client:
        response = client.get("/v1/public/receipt-keys")

    assert response.status_code == 503
    assert "not-a-key" not in response.text


def test_receipt_keys_never_emit_private_material(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the signing key is configured; the response must stay public-only."""
    private_key = _set_signing_key_only(monkeypatch)
    private_raw = private_key.private_bytes_raw()

    with _client() as client:
        response = client.get("/v1/public/receipt-keys")
    assert response.status_code == 200
    body = response.text

    public_raw = private_key.public_key().public_bytes_raw()
    for needle in _encodings_of(private_raw, public_raw):
        assert needle not in body

    published = base64.b64decode(response.json()["keys"][0]["public_key"])
    assert published == public_raw
    assert len(published) == 32


def test_receipt_keys_refuse_a_private_seed_in_the_verifying_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The catastrophic operator error: the signing secret in *both* variables.

    ``Ed25519PublicKey.from_public_bytes`` accepts any 32 bytes, so serving
    ``A2A_RECEIPT_VERIFYING_KEY`` verbatim would publish the seed itself.
    """
    private_key = Ed25519PrivateKey.generate()
    private_raw = private_key.private_bytes_raw()
    seed_b64 = base64.b64encode(private_raw).decode("ascii")
    monkeypatch.setenv("A2A_RECEIPT_SIGNING_KEY", seed_b64)
    monkeypatch.setenv("A2A_RECEIPT_VERIFYING_KEY", seed_b64)

    with _client() as client:
        response = client.get("/v1/public/receipt-keys")

    assert response.status_code == 503
    assert response.json()["detail"]
    for needle in _encodings_of(private_raw, private_key.public_key().public_bytes_raw()):
        assert needle not in response.text


def test_receipt_keys_refuse_a_fully_swapped_key_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Signing/verifying values transposed: the seed lands in the public slot."""
    private_key = Ed25519PrivateKey.generate()
    private_raw = private_key.private_bytes_raw()
    public_raw = private_key.public_key().public_bytes_raw()
    monkeypatch.setenv(
        "A2A_RECEIPT_SIGNING_KEY", base64.b64encode(public_raw).decode("ascii")
    )
    monkeypatch.setenv(
        "A2A_RECEIPT_VERIFYING_KEY", base64.b64encode(private_raw).decode("ascii")
    )

    with _client() as client:
        response = client.get("/v1/public/receipt-keys")

    assert response.status_code == 503
    for needle in _encodings_of(private_raw, public_raw):
        assert needle not in response.text


def test_receipt_keys_refuse_a_verifying_key_that_is_not_the_signer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mismatched pair would publish a key that verifies nothing we sign."""
    signer = Ed25519PrivateKey.generate()
    stranger = Ed25519PrivateKey.generate()
    monkeypatch.setenv(
        "A2A_RECEIPT_SIGNING_KEY",
        base64.b64encode(signer.private_bytes_raw()).decode("ascii"),
    )
    monkeypatch.setenv(
        "A2A_RECEIPT_VERIFYING_KEY",
        base64.b64encode(stranger.public_key().public_bytes_raw()).decode("ascii"),
    )

    with _client() as client:
        response = client.get("/v1/public/receipt-keys")

    assert response.status_code == 503
    stranger_b64 = base64.b64encode(stranger.public_key().public_bytes_raw()).decode(
        "ascii"
    )
    assert stranger_b64 not in response.text


def test_receipt_keys_publish_the_verifying_key_when_this_process_cannot_sign(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify-only deployments have nothing to cross-check against."""
    public_raw = Ed25519PrivateKey.generate().public_key().public_bytes_raw()
    monkeypatch.delenv("A2A_RECEIPT_SIGNING_KEY", raising=False)
    monkeypatch.setenv(
        "A2A_RECEIPT_VERIFYING_KEY", base64.b64encode(public_raw).decode("ascii")
    )

    with _client() as client:
        response = client.get("/v1/public/receipt-keys")

    assert response.status_code == 200
    assert base64.b64decode(response.json()["keys"][0]["public_key"]) == public_raw


def test_receipt_keys_route_is_mounted_on_the_app() -> None:
    pytest.importorskip("qdrant_client")  # full app import needs the optional dep
    from control_plane.main import app

    # Router inclusion is lazy on this FastAPI version; the schema resolves it.
    assert "/v1/public/receipt-keys" in app.openapi()["paths"]
