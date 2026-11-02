"""Development-only identity provider.

SECURITY-SENSITIVE: needs security review before merge (plan §8.10).

Mounted ONLY when POOL_DEV_MODE=true. It issues tokens for any username
without a password so that tests and local demos run without Keycloak. The
tokens go through the same verification path as real OIDC tokens.
"""

from __future__ import annotations

from datetime import timedelta

import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, generate_private_key
from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from ..timeutil import utcnow

DEV_ISSUER = "pool-dev-idp"
DEV_AUDIENCE = "pool"


class DevIdP:
    def __init__(self) -> None:
        self.key: RSAPrivateKey = generate_private_key(public_exponent=65537, key_size=2048)

    def issue(self, username: str, mfa: bool = True, ttl: timedelta = timedelta(hours=8)) -> str:
        now = utcnow()
        claims = {
            "iss": DEV_ISSUER,
            "aud": DEV_AUDIENCE,
            "sub": f"dev-{username}",
            "preferred_username": username,
            "iat": int(now.timestamp()),
            "exp": int((now + ttl).timestamp()),
            "amr": ["pwd", "otp"] if mfa else ["pwd"],
        }
        return jwt.encode(claims, self.key, algorithm="RS256")


class DevTokenRequest(BaseModel):
    username: str = Field(min_length=1, max_length=48, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    mfa: bool = True


router = APIRouter(prefix="/dev", tags=["dev-only"])


@router.post("/token")
async def dev_token(body: DevTokenRequest, request: Request) -> dict:
    idp: DevIdP = request.app.state.dev_idp
    return {"access_token": idp.issue(body.username, body.mfa), "token_type": "Bearer"}
