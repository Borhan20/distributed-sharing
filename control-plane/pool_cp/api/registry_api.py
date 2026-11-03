"""The built-in registry's sign-in (docs/design/pool-run.md §3.1).

SECURITY-SENSITIVE: decides who may push and pull which images. Needs security review (plan §8.10).

Docker asks this token service for access when it pushes or pulls (the registry's `WWW-Authenticate: Bearer
realm=...`). It signs in with Basic auth: the member's pool user name and pool access token, the same token
the `pool` command uses (`pool run` passes it to Docker; nobody types it). Members get push and pull for
`<group>/<handle>/...` in groups where they may submit jobs, and nothing else. Computers never come here: the
server gives them a pull token for their task's one image (agent_ws.py).
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import builtin_registry
from ..auth import Principal, TokenError, current_principal, get_session, upsert_user
from ..authz import ANY_MEMBER, SUBMITTERS, load_group, require_role
from ..models import Group, Membership
from ..registry import RegistryError

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["registry"])

_GRANTABLE = {"pull", "push"}


def _unauthorized(message: str) -> JSONResponse:
    return JSONResponse({"errors": [{"code": "UNAUTHORIZED", "message": message}]}, status.HTTP_401_UNAUTHORIZED,
                        headers={"WWW-Authenticate": 'Basic realm="pool"'})


@router.get("/registry/token", include_in_schema=False)
async def registry_token(request: Request, service: str = Query(""), scope: list[str] = Query(default_factory=list),
                         s: AsyncSession = Depends(get_session)):
    """Token service for the built-in registry (Docker's registry token protocol)."""
    settings = request.app.state.settings
    issuer: builtin_registry.Issuer | None = request.app.state.registry_issuer
    if issuer is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "this server has no built-in registry")
    if service and service != settings.registry_service:
        return _unauthorized("unknown registry service")
    scheme, _, encoded = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "basic":
        return _unauthorized("sign in with your pool user name and access token (`pool run` does this for you)")
    try:
        name, _, secret = base64.b64decode(encoded, validate=True).decode().partition(":")
    except (binascii.Error, UnicodeDecodeError):
        return _unauthorized("unreadable credentials")
    try:
        claims = request.app.state.token_verifier.verify(secret)
    except TokenError:
        return _unauthorized("the pool access token is not valid or has expired: run `pool login` again")
    user = await upsert_user(s, claims, settings.oidc_username_claim)
    await s.commit()
    if name.strip().lower() != user.handle:
        return _unauthorized(f"the user name must be your pool user name, {user.handle}")

    access: list[dict] = []
    allowed_groups: dict[str, bool] = {}
    for raw in scope:
        for item in raw.split(" "):
            parsed = builtin_registry.parse_scope(item)
            if parsed is None or parsed[0] != "repository":
                continue
            kind, repository, wanted = parsed
            who = builtin_registry.owner(repository)
            granted: list[str] = []
            if who and who[1] == user.handle and builtin_registry.valid_repository(repository):
                group = who[0]
                if group not in allowed_groups:
                    role = (await s.execute(
                        select(Membership.role).join(Group, Group.id == Membership.group_id)
                        .where(Group.name == group, Group.state == "active", Membership.user_id == user.id)
                    )).scalar()
                    allowed_groups[group] = role in SUBMITTERS
                if allowed_groups[group]:
                    granted = sorted(wanted & _GRANTABLE)
            access.append({"type": kind, "name": repository, "actions": granted})
            if wanted and not granted:
                log.info("registry: %s asked for %s on %s, granted nothing", user.handle, sorted(wanted), repository)
    issued = dt.datetime.now(dt.UTC)
    token, _ = issuer.token(user.handle, access)
    # Docker reuses a token until issued_at + expires_in, so issued_at must be the time it was made.
    return {"token": token, "access_token": token, "expires_in": builtin_registry.TOKEN_TTL_S,
            "issued_at": issued.isoformat()}


class GroupRegistryOut(BaseModel):
    enabled: bool
    address: str | None = None
    repository_prefix: str | None = None  # where I push: <address>/<group>/<handle>
    retention_days: int  # in effect for this group
    group_retention_days: int | None = None  # the group's own setting, if any
    default_retention_days: int


@router.get("/groups/{name}/registry", response_model=GroupRegistryOut)
async def group_registry(name: str, request: Request, p: Principal = Depends(current_principal),
                         s: AsyncSession = Depends(get_session)):
    """Where I push images for this group, and how long unused images are kept."""
    settings = request.app.state.settings
    g = await load_group(s, name)
    await require_role(s, g, p, ANY_MEMBER, require_active=False)
    default = settings.registry_retention_days
    out = GroupRegistryOut(enabled=builtin_registry.enabled(settings), retention_days=g.registry_retention_days
                           or default, group_retention_days=g.registry_retention_days, default_retention_days=default)
    if out.enabled:
        out.address = builtin_registry.address(settings)
        try:
            out.repository_prefix = f"{out.address}/{builtin_registry.namespace(g.name, p.user.handle)}"
        except RegistryError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    return out
