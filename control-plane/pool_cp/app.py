"""FastAPI application factory.

    uvicorn pool_cp.app:create_app --factory --ws-max-size 1048576
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from . import agent_ws
from .api import devices, groups, jobs
from .auth import TokenVerifier
from .auth.dev_idp import DEV_AUDIENCE, DEV_ISSUER, DevIdP
from .auth.dev_idp import router as dev_router
from .ca import EnvelopeSigner, LocalCA
from .config import Settings, get_settings
from .db import make_engine, make_sessionmaker

log = logging.getLogger("pool_cp")

async def _api_heartbeat(app: FastAPI) -> None:
    """Tell the scheduler this API process is up, so it only expires leases agents could renew."""
    instance = f"api-{socket.gethostname()}-{os.getpid()}"[:64]
    stmt = text("INSERT INTO service_heartbeats (instance, service, at) VALUES (:i, 'api', now()) "
                "ON CONFLICT (instance) DO UPDATE SET at = now()")
    while True:
        try:
            async with app.state.sessionmaker() as s:
                await s.execute(stmt, {"i": instance})
                await s.commit()
        except Exception:  # database briefly unavailable; try again next tick
            log.warning("api heartbeat failed", exc_info=True)
        await asyncio.sleep(app.state.settings.api_heartbeat_s)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    engine = make_engine(settings.database_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(_api_heartbeat(app))
        yield
        task.cancel()
        await engine.dispose()

    app = FastAPI(title="pool control plane", version="0.1.0", lifespan=lifespan,
                  description="Group compute platform API (plan §5). Agent protocol v1alpha1.")
    app.state.settings = settings
    app.state.engine = engine
    app.state.sessionmaker = make_sessionmaker(engine)
    app.state.ca = LocalCA(settings.ca_key_file, settings.ca_cert_file, timedelta(hours=settings.cert_ttl_hours),
                           create=bool(settings.autocreate_keys))
    app.state.signer = EnvelopeSigner(settings.signing_key_file, create=bool(settings.autocreate_keys))
    app.state.agent_conns = {}

    if settings.dev_mode:
        log.warning("POOL_DEV_MODE is on: /dev/token issues tokens without a password. Never use this on a "
                    "shared server.")
        idp = DevIdP()
        app.state.dev_idp = idp
        app.state.token_verifier = TokenVerifier(DEV_ISSUER, DEV_AUDIENCE, static_key=idp.key.public_key())
        app.include_router(dev_router)
    else:
        app.state.token_verifier = TokenVerifier(settings.oidc_issuer, settings.oidc_audience,
                                                 settings.oidc_jwks_url)

    app.include_router(groups.router)
    app.include_router(devices.router)
    app.include_router(jobs.router)
    app.include_router(agent_ws.router)

    @app.get("/healthz", tags=["ops"])
    async def healthz() -> dict:
        return {"ok": True}

    @app.get("/readyz", tags=["ops"])
    async def readyz() -> dict:
        async with app.state.sessionmaker() as s:
            await s.execute(text("SELECT 1"))
        return {"ok": True}

    web_dist = Path(__file__).resolve().parents[2] / "web" / "dist"
    if web_dist.is_dir():
        app.mount("/assets", StaticFiles(directory=web_dist / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str):
            if path.startswith(("api/", "dev/")):
                from fastapi import HTTPException

                raise HTTPException(404)
            return FileResponse(web_dist / "index.html")

    return app
