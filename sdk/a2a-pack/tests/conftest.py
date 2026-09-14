from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def _keypair() -> tuple[str, str]:
    private_key = Ed25519PrivateKey.generate()
    return (
        base64.b64encode(private_key.private_bytes_raw()).decode("ascii"),
        base64.b64encode(private_key.public_key().public_bytes_raw()).decode("ascii"),
    )


@pytest.fixture(autouse=True)
def ed25519_runtime_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Runtime signing tests must use configured Ed25519 keys."""
    grant_private, grant_public = _keypair()
    receipt_private, receipt_public = _keypair()
    replay_private, replay_public = _keypair()
    monkeypatch.setenv("A2A_GRANT_SIGNING_KEY", grant_private)
    monkeypatch.setenv("A2A_GRANT_VERIFYING_KEY", grant_public)
    monkeypatch.setenv("A2A_RECEIPT_SIGNING_KEY", receipt_private)
    monkeypatch.setenv("A2A_RECEIPT_VERIFYING_KEY", receipt_public)
    monkeypatch.setenv("A2A_REPLAY_SIGNING_KEY", replay_private)
    monkeypatch.setenv("A2A_REPLAY_VERIFYING_KEY", replay_public)
