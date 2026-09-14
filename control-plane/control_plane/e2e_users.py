from __future__ import annotations

import argparse
import asyncio
import json
import secrets
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .auth import issue_token
from .db import SessionLocal, init_models
from .models import User
from .org_provisioning import create_personal_organization

DEFAULT_E2E_EMAIL = "kernel-sim-e2e@a2acloud.test"


async def ensure_e2e_user_token(
    session: AsyncSession,
    *,
    email: str = DEFAULT_E2E_EMAIL,
    ensure_org: bool = True,
) -> dict[str, Any]:
    normalized = email.strip().lower()
    if not normalized:
        raise ValueError("email is required")
    user = (
        await session.execute(select(User).where(User.email == normalized))
    ).scalar_one_or_none()
    created = False
    if user is None:
        user = User(
            email=normalized,
            password_hash=f"e2e:{secrets.token_urlsafe(24)}",
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        created = True
    org = await create_personal_organization(session, user) if ensure_org else None
    await session.commit()
    return {
        "email": user.email,
        "user_id": user.id,
        "created": created,
        "organization_slug": org.slug if org is not None else None,
        "token": issue_token(user.id),
    }


async def _main() -> None:
    parser = argparse.ArgumentParser(
        description="Mint or reuse the stable kernel-simulation E2E user and print a bearer token."
    )
    parser.add_argument("--email", default=DEFAULT_E2E_EMAIL)
    parser.add_argument(
        "--no-org",
        action="store_true",
        help="only ensure the user row; skip personal organization provisioning",
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()

    await init_models()
    async with SessionLocal() as session:
        payload = await ensure_e2e_user_token(
            session,
            email=args.email,
            ensure_org=not args.no_org,
        )
    if args.as_json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(payload["token"])


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
