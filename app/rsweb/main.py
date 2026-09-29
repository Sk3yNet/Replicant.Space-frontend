"""FastAPI application factory."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .api import RSClient
from .config import Settings, settings as default_settings
from .db import DB
from .hub import Hub
from .ingest import Worker
from . import web

HERE = Path(__file__).parent


def create_app(settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    s = settings or default_settings
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = DB(s.db_path)
        await db.open()
        api = RSClient(s, transport=transport)
        hub = Hub()
        worker = Worker(s, db, api, hub)
        app.state.settings, app.state.db, app.state.api, app.state.hub, app.state.worker = s, db, api, hub, worker
        if not s.disable_background:
            worker.start()
        try:
            yield
        finally:
            await worker.stop()
            await api.close()
            await db.close()

    app = FastAPI(title="Replicant Space client", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.include_router(web.router)
    app.add_exception_handler(web.AuthError, web.auth_error_handler)
    return app


app = create_app()
