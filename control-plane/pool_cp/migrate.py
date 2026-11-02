"""Run database migrations: `python -m pool_cp.migrate` (uses POOL_DATABASE_URL)."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config


def alembic_config(database_url: str | None = None) -> Config:
    cfg = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parent / "migrations"))
    if database_url:
        cfg.attributes["database_url"] = database_url
    return cfg


def upgrade(database_url: str | None = None) -> None:
    command.upgrade(alembic_config(database_url), "head")


if __name__ == "__main__":
    upgrade()
