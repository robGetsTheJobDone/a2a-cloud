from __future__ import annotations

import pytest
from fastapi import HTTPException

from control_plane.routes.agents import _reject_unsafe_user_card


def test_user_card_cannot_request_platform_grant_signer() -> None:
    with pytest.raises(HTTPException) as raised:
        _reject_unsafe_user_card({"runtime": {"grant_signing": True}})

    assert raised.value.status_code == 400
    assert "private signing keys are isolated" in str(raised.value.detail)


def test_user_card_may_receive_public_grant_verification() -> None:
    _reject_unsafe_user_card({"runtime": {"grant_signing": False}})
