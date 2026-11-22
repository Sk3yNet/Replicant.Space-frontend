"""FastAPI application factory."""
from __future__ import annotations

import asyncio
import logging
import os
import time
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


def _sizes(path: str) -> str:
    """The database file and its write-ahead log, in MB (a big leftover log takes a while to replay on open)."""
    out = []
    for suf in ("", "-wal"):
        try:
            out.append(f"{Path(path + suf).name} {os.path.getsize(path + suf) / 1e6:.0f} MB")
        except OSError:
            pass
    return ", ".join(out) or "new"


def _warm(path: str) -> None:
    """Read the database file once, front to back, so the OS keeps it in its cache: after a restart the engine's
    small random reads ran at about 0.4 MB/s on a slow disk and held the server for an hour; read straight through
    it took under 5 minutes (Joe 2026-11-22). Plain file reads in a thread: nothing touches SQLite."""
    from . import history
    t0, n = time.monotonic(), 0
    try:
        with open(path, "rb", buffering=0) as f:
            while chunk := f.read(4 << 20):
                n += len(chunk)
    except OSError:
        return
    finally:
        history.WARM["done"] = True   # the daily clean-up waits for this
    logging.getLogger("rsweb").info("database file cached: %.0f MB in %.0f s", n / 1e6, time.monotonic() - t0)


def create_app(settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    s = settings or default_settings
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = DB(s.db_path)
        boot = logging.getLogger("rsweb")
        t0 = time.monotonic()
        boot.info("opening the database %s (%s)", s.db_path, _sizes(s.db_path))   # a stuck start shows where (Joe 2026-11-22)
        await db.open()
        boot.info("database open in %.1f s", time.monotonic() - t0)
        from . import history
        history.WARM["done"] = s.disable_background or not Path(s.db_path).exists()
        app.state.warm = asyncio.create_task(asyncio.to_thread(_warm, s.db_path), name="warm-db") \
            if not s.disable_background and Path(s.db_path).exists() else None
        api = RSClient(s, transport=transport)
        from . import components as comp
        api.before_post = lambda a, path, body: comp.before_print(a, db, path, body)
        if s.virtual_queue:
            from .vqueue import VirtualQueue
            api.vq = VirtualQueue(db)
        hub = Hub()
        worker = Worker(s, db, api, hub)
        app.state.settings, app.state.db, app.state.api, app.state.hub, app.state.worker = s, db, api, hub, worker
        from . import version as ver
        me, prev = await ver.register_start(db)
        for line in ver.start_notes(me, prev):
            await worker.automations.log("engine", line, "info")
        logging.getLogger("rsweb").info("rsweb %s run %s", ver.label(), me["run"])

        async def beat():
            while True:
                await asyncio.sleep(60)
                try:
                    await ver.heartbeat(db)
                except Exception:  # never let the heartbeat take the app down
                    pass
        hb = asyncio.create_task(beat(), name="heartbeat")
        if not s.disable_background:
            worker.start()
        try:
            yield
        finally:
            hb.cancel()
            try:
                await ver.register_stop(db)
            except Exception:
                pass
            await worker.stop()
            await api.close()
            await db.close()

    app = FastAPI(title="Replicant Space client", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.include_router(web.router)
    from . import web_fleets
    app.include_router(web_fleets.router)
    from . import web_ops
    app.include_router(web_ops.router)
    from . import web_trail
    app.include_router(web_trail.router)
    from . import web_wards
    app.include_router(web_wards.router)
    from . import web_profile
    app.include_router(web_profile.router)
    from . import web_bulk
    app.include_router(web_bulk.router)
    from . import web_logistics
    app.include_router(web_logistics.router)
    from . import web_sim
    app.include_router(web_sim.router)
    from . import web_terraform
    app.include_router(web_terraform.router)
    from . import web_lockers
    app.include_router(web_lockers.router)
    from . import web_beacons
    app.include_router(web_beacons.router)
    from . import web_jobs
    app.include_router(web_jobs.router)
    from . import web_cockpit
    app.include_router(web_cockpit.router)
    from . import web_extras
    from . import web_chatter
    app.include_router(web_chatter.router)
    app.include_router(web_extras.router)
    from . import web_bridge
    app.include_router(web_bridge.router)
    app.add_exception_handler(web.AuthError, web.auth_error_handler)
    from . import perf
    app.add_middleware(perf.Middleware)
    from . import pagecache
    app.add_middleware(pagecache.ClearOnEdit)
    return app


app = create_app()
