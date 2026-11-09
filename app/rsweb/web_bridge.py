"""The command screen (1.359.0): every page drawn as a window over the galaxy map, with an icon rail and an outliner in
place of the tabs. It is chosen per browser (cookie ui=bridge, /bridge/on and /bridge/off); the classic tabbed
interface stays the default. Pages are the same: base.html draws them as a window (web.ui_mode)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import notify
from .web import UI_COOKIE, current_user, partial

router = APIRouter()
YEAR = 365 * 86400


@router.get("/bridge")
@router.get("/bridge/on")
async def bridge_on(request: Request, user: str = Depends(current_user)):
    r = RedirectResponse("/map", status_code=303)
    r.set_cookie(UI_COOKIE, "bridge", max_age=YEAR, samesite="lax", httponly=True)
    return r


@router.get("/bridge/off")
async def bridge_off(request: Request, user: str = Depends(current_user)):
    r = RedirectResponse("/account", status_code=303)
    r.delete_cookie(UI_COOKIE)
    return r


@router.get("/bridge/outliner", response_class=HTMLResponse)
async def outliner(request: Request, user: str = Depends(current_user)):
    st = request.app.state
    db, eng = st.db, st.worker.automations
    reps = await db.kv_get("replicants", {}) or {}
    reps = sorted(((c, r) for c, r in reps.items() if isinstance(r, dict)), key=lambda x: str(x[1].get("name") or x[0]).casefold())
    devices = await db.kv_get("devices", []) or []
    fleets = sorted(await eng.fleets(), key=lambda f: str(f.get("name") or f.get("id") or "").casefold())
    jobs = [j for j in await eng.jobs() if j.get("status") in ("running", "waiting")]
    needs = await db.fetchall("SELECT id, title, body, link, created_at FROM notifications WHERE read=0 AND level IN "
                              "('error','mention') ORDER BY id DESC LIMIT 8")
    return partial(request, "partials/bridge_outliner.html", replicants=reps, n_devices=len(devices), fleets=fleets,
                   jobs=jobs, needs=needs, needs_total=await notify.unread_errors(db))
