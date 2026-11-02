"""User authentication with OIDC bearer tokens (plan §8.6).

SECURITY-SENSITIVE: needs security review before merge (plan §8.10).
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any

import jwt
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import User

ALGORITHMS = ["RS256", "ES256", "EdDSA"]
MFA_AMR = {"mfa", "otp", "hwk", "swk", "fido", "webauthn"}
_HANDLE_RE = re.compile(r"[^a-z0-9._-]+")


class TokenError(Exception):
    pass


@dataclass
class Principal:
    user: User
    claims: dict[str, Any] = field(default_factory=dict)

    @property
    def has_mfa(self) -> bool:
        amr = self.claims.get("amr") or []
        if isinstance(amr, str):
            amr = [amr]
        return bool(MFA_AMR.intersection(a.lower() for a in amr))


class TokenVerifier:
    """Verifies signed JWT access tokens against the IdP's JWKS (or a static key in dev mode)."""

    def __init__(self, issuer: str, audience: str, jwks_url: str | None = None, static_key: Any = None) -> None:
        self.issuer = issuer
        self.audience = audience
        self.static_key = static_key
        if static_key is None:
            url = jwks_url or issuer.rstrip("/") + "/protocol/openid-connect/certs"
            self._jwks = jwt.PyJWKClient(url, cache_keys=True, lifespan=300)

    def verify(self, token: str) -> dict[str, Any]:
        try:
            if self.static_key is not None:
                key = self.static_key
            else:
                key = self._jwks.get_signing_key_from_jwt(token).key
            return jwt.decode(
                token,
                key=key,
                algorithms=ALGORITHMS,
                audience=self.audience,
                issuer=self.issuer,
                options={"require": ["exp", "iat", "sub", "iss"]},
                leeway=30,
            )
        except (jwt.PyJWTError, jwt.PyJWKClientError) as exc:
            raise TokenError(str(exc)) from exc


def normalise_handle(raw: str) -> str:
    h = _HANDLE_RE.sub("-", raw.strip().lower()).strip("-.")
    return (h or "user")[:48]


async def upsert_user(session: AsyncSession, claims: dict[str, Any], username_claim: str) -> User:
    subject = f"{claims['iss']}|{claims['sub']}"
    user = (await session.execute(select(User).where(User.subject == subject))).scalar_one_or_none()
    if user:
        return user
    base = normalise_handle(str(claims.get(username_claim) or claims["sub"]))
    for candidate in (base, f"{base}-{uuid.uuid4().hex[:6]}", f"{base}-{uuid.uuid4().hex[:10]}"):
        user = User(subject=subject, handle=candidate)
        session.add(user)
        try:
            await session.commit()
            return user
        except IntegrityError:
            await session.rollback()
            existing = (await session.execute(select(User).where(User.subject == subject))).scalar_one_or_none()
            if existing:  # concurrent first login of the same user
                return existing
    raise HTTPException(status.HTTP_409_CONFLICT, "could not allocate a user handle")


def bearer_token(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    scheme, _, token = auth.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token",
                            headers={"WWW-Authenticate": "Bearer"})
    return token.strip()


async def get_session(request: Request):
    async with request.app.state.sessionmaker() as session:
        yield session


async def current_principal(request: Request, session: AsyncSession = Depends(get_session)) -> Principal:
    token = bearer_token(request)
    verifier: TokenVerifier = request.app.state.token_verifier
    try:
        claims = verifier.verify(token)
    except TokenError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token",
                            headers={"WWW-Authenticate": "Bearer"}) from None
    user = await upsert_user(session, claims, request.app.state.settings.oidc_username_claim)
    return Principal(user=user, claims=claims)
