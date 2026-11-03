"""Registry credentials for private images (docs/user-guide.md §6): a group's, and each member's own.

SECURITY-SENSITIVE: stores credentials. Needs security review (plan §8.10).

Group credentials: members see which registries are set up (so they know where to push); coordinators
and admins add or remove them. Personal credentials (/me/registries): each person manages their own, and
they are used only for jobs that person submits. Which one a job uses: image_access.credential_for. Secrets are
write-only: encrypted at rest and never returned.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit
from ..auth import Principal, current_principal, get_session
from ..authz import ANY_MEMBER, MANAGERS, load_group, not_found, require_role
from ..models import GroupRegistryCredential, User, UserRegistryCredential
from ..registry import RegistryError, check_credentials, normalize_registry
from ..timeutil import utcnow

router = APIRouter(prefix="/api/v1", tags=["registries"])


class RegistryCredentialIn(BaseModel):
    registry: str = Field(min_length=1, max_length=255, description="e.g. ghcr.io or docker.io")
    username: str = Field(min_length=1, max_length=255)
    token: str = Field(min_length=1, max_length=4096, description="A read-only access token; never shown again")


class RegistryCredentialOut(BaseModel):
    registry: str
    username: str
    updated_by: str | None
    updated_at: datetime


async def _out(s: AsyncSession, c: GroupRegistryCredential) -> RegistryCredentialOut:
    who = await s.get(User, c.updated_by) if c.updated_by else None
    return RegistryCredentialOut(registry=c.registry_host, username=c.username, updated_by=who.handle if who else None,
                                 updated_at=c.updated_at)


@router.get("/groups/{name}/registries", response_model=list[RegistryCredentialOut])
async def list_registries(name: str, p: Principal = Depends(current_principal), s: AsyncSession = Depends(get_session)):
    g = await load_group(s, name)
    await require_role(s, g, p, ANY_MEMBER, require_active=False)
    rows = (await s.execute(select(GroupRegistryCredential).where(GroupRegistryCredential.group_id == g.id)
                            .order_by(GroupRegistryCredential.registry_host))).scalars().all()
    return [await _out(s, c) for c in rows]


@router.put("/groups/{name}/registries", response_model=RegistryCredentialOut)
async def set_registry(name: str, body: RegistryCredentialIn, request: Request,
                       p: Principal = Depends(current_principal), s: AsyncSession = Depends(get_session)):
    settings = request.app.state.settings
    g = await load_group(s, name)
    await require_role(s, g, p, MANAGERS, mfa_required=settings.require_mfa_for_admins)
    try:
        registry = normalize_registry(body.registry)
        await asyncio.to_thread(check_credentials, registry, body.username, body.token,
                                allow_private=settings.registry_private_networks)
    except RegistryError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None
    sealed = request.app.state.secretbox.seal(body.token)
    c = await s.get(GroupRegistryCredential, (g.id, registry))
    if c is None:
        c = GroupRegistryCredential(group_id=g.id, registry_host=registry)
        s.add(c)
    c.username, c.secret, c.updated_by, c.updated_at = body.username, sealed, p.user.id, utcnow()
    await audit.record(s, actor_type="user", actor_id=p.user.id, action="registry.set", group_id=g.id,
                       target_type="registry", target_id=registry, details={"registry": registry,
                                                                            "username": body.username})
    await s.commit()
    return await _out(s, c)


@router.delete("/groups/{name}/registries/{registry}", status_code=204)
async def remove_registry(name: str, registry: str, request: Request, p: Principal = Depends(current_principal),
                          s: AsyncSession = Depends(get_session)):
    settings = request.app.state.settings
    g = await load_group(s, name)
    await require_role(s, g, p, MANAGERS, require_active=False, mfa_required=settings.require_mfa_for_admins)
    try:
        key = normalize_registry(registry)
    except RegistryError:
        raise not_found("registry") from None
    c = await s.get(GroupRegistryCredential, (g.id, key))
    if c is None:
        raise not_found("registry")
    await s.delete(c)
    await audit.record(s, actor_type="user", actor_id=p.user.id, action="registry.remove", group_id=g.id,
                       target_type="registry", target_id=key, details={"registry": key})
    await s.commit()


# --------------------------------------------------------------------------- my own tokens


class MyRegistryOut(BaseModel):
    registry: str
    username: str
    updated_at: datetime


@router.get("/me/registries", response_model=list[MyRegistryOut])
async def list_my_registries(p: Principal = Depends(current_principal), s: AsyncSession = Depends(get_session)):
    rows = (await s.execute(select(UserRegistryCredential).where(UserRegistryCredential.user_id == p.user.id)
                            .order_by(UserRegistryCredential.registry_host))).scalars().all()
    return [MyRegistryOut(registry=c.registry_host, username=c.username, updated_at=c.updated_at) for c in rows]


@router.put("/me/registries", response_model=MyRegistryOut)
async def set_my_registry(body: RegistryCredentialIn, request: Request, p: Principal = Depends(current_principal),
                          s: AsyncSession = Depends(get_session)):
    """Store my own read-only token. It is used only for jobs I submit, in any of my groups."""
    settings = request.app.state.settings
    try:
        registry = normalize_registry(body.registry)
        await asyncio.to_thread(check_credentials, registry, body.username, body.token,
                                allow_private=settings.registry_private_networks)
    except RegistryError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None
    c = await s.get(UserRegistryCredential, (p.user.id, registry))
    if c is None:
        c = UserRegistryCredential(user_id=p.user.id, registry_host=registry)
        s.add(c)
    c.username, c.secret, c.updated_at = body.username, request.app.state.secretbox.seal(body.token), utcnow()
    await audit.record(s, actor_type="user", actor_id=p.user.id, action="registry.set_mine", target_type="registry",
                       target_id=registry, details={"registry": registry, "username": body.username})
    await s.commit()
    return MyRegistryOut(registry=c.registry_host, username=c.username, updated_at=c.updated_at)


@router.delete("/me/registries/{registry}", status_code=204)
async def remove_my_registry(registry: str, p: Principal = Depends(current_principal),
                             s: AsyncSession = Depends(get_session)):
    try:
        key = normalize_registry(registry)
    except RegistryError:
        raise not_found("registry") from None
    c = await s.get(UserRegistryCredential, (p.user.id, key))
    if c is None:
        raise not_found("registry")
    await s.delete(c)
    await audit.record(s, actor_type="user", actor_id=p.user.id, action="registry.remove_mine",
                       target_type="registry", target_id=key, details={"registry": key})
    await s.commit()
