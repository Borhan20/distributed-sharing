"""Group role checks (plan §6.2).

SECURITY-SENSITIVE: needs security review before merge (plan §8.10).

Non-members get 404 (the group's existence is not revealed); members without
the right role get 403.
"""

from __future__ import annotations

from collections.abc import Iterable

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import Principal
from ..models import Group, Membership

COORDINATOR = frozenset({"coordinator"})
MANAGERS = frozenset({"coordinator", "admin"})
VOTERS = frozenset({"coordinator", "admin", "member"})
SUBMITTERS = frozenset({"coordinator", "admin", "member", "submitter"})
SHARERS = frozenset({"coordinator", "admin", "member", "contributor"})
ANY_MEMBER = frozenset({"coordinator", "admin", "member", "contributor", "submitter"})
PRIVILEGED = MANAGERS


def not_found(what: str = "group") -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, f"{what} not found")


async def load_group(session: AsyncSession, name: str) -> Group:
    group = (await session.execute(select(Group).where(Group.name == name))).scalar_one_or_none()
    if group is None:
        raise not_found()
    return group


async def membership(session: AsyncSession, group: Group, principal: Principal) -> Membership | None:
    return (await session.execute(
        select(Membership).where(Membership.group_id == group.id, Membership.user_id == principal.user.id)
    )).scalar_one_or_none()


async def require_role(session: AsyncSession, group: Group, principal: Principal, roles: Iterable[str],
                       *, require_active: bool = True, mfa_required: bool = False) -> Membership:
    m = await membership(session, group, principal)
    if m is None:
        raise not_found()
    if m.role not in roles:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "your role in this group does not allow this")
    if require_active and group.state != "active":
        raise HTTPException(status.HTTP_409_CONFLICT, f"group is {group.state}")
    if mfa_required and m.role in PRIVILEGED and not principal.has_mfa:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "multi-factor authentication is required for this action")
    return m
