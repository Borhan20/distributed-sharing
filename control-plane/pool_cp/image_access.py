"""Which registry credential a job's image is pulled with.

SECURITY-SENSITIVE: decides whose credentials open an image. Needs security review (plan §8.10).

The submitter's own token (My images) comes first, then the group's (Images tab). A member's own token
is never used for anyone else's job.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from .models import GroupRegistryCredential, UserRegistryCredential
from .secretbox import SecretBox


async def credential_for(s: AsyncSession, secretbox: SecretBox, *, user_id: uuid.UUID, group_id: uuid.UUID,
                         registry: str) -> tuple[str, str, str] | None:
    """(username, secret, whose) for a job's image: the submitter's own token first, then the group's.

    whose is "submitter" or "group" (for the audit log). None: no token, the image must be public.
    """
    mine = await s.get(UserRegistryCredential, (user_id, registry))
    if mine is not None:
        return mine.username, secretbox.open(mine.secret), "submitter"
    group = await s.get(GroupRegistryCredential, (group_id, registry))
    if group is not None:
        return group.username, secretbox.open(group.secret), "group"
    return None


async def has_credential(s: AsyncSession, *, user_id: uuid.UUID, group_id: uuid.UUID, registry: str) -> bool:
    return (await s.get(UserRegistryCredential, (user_id, registry)) is not None
            or await s.get(GroupRegistryCredential, (group_id, registry)) is not None)
