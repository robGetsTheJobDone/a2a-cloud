from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException

ENCRYPTED_PREFIX = "fernet:"
SECRET_KEY_ENV = "A2A_CP_LLM_CREDS_KEY"


def _fernet() -> Fernet:
    raw = os.environ.get(SECRET_KEY_ENV, "").strip()
    if not raw:
        raise HTTPException(500, f"{SECRET_KEY_ENV} is required")
    try:
        return Fernet(raw.encode("ascii"))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            500,
            f"{SECRET_KEY_ENV} must be a valid Fernet key",
        ) from exc


def encrypt_secret(value: str) -> str:
    token = _fernet().encrypt(value.encode("utf-8")).decode("ascii")
    return ENCRYPTED_PREFIX + token


def decrypt_secret(stored: str) -> str:
    if not stored.startswith(ENCRYPTED_PREFIX):
        return stored
    token = stored[len(ENCRYPTED_PREFIX):]
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise HTTPException(500, "stored secret could not be decrypted") from exc
