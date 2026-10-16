"""Endpoints from the OpenAPI spec (2026-10-16) the app hadn't used: leaderboards, the game's health, a system's events,
stockpiles and nearby stars, a replicant's event log, device permissions, and the blog (read, write, edit your own)."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from .api import ApiError
from .web import current_user, load_state, page, partial, run_action

router = APIRouter()
HEALTH_KV, HEALTH_TTL = "api_health", 300


async def _get(request: Request, path: str, **params) -> tuple[dict, str | None]:
    try:
        return (await request.app.state.api.get(path, background=True, **params) or {}), None
    except ApiError as e:
        return {}, e.message


# --- leaderboards -------------------------------------------------------------------------------------------------
@router.get("/leaderboards", response_class=HTMLResponse)
async def leaderboards(request: Request, user: str = Depends(current_user)):
    """GET /leaderboards (the boards there are), each board loaded after the page (GET /leaderboards/{key})."""
    body, err = await _get(request, "/leaderboards")
    return await page(request, user, "leaderboards.html", "leaderboards", boards=body.get("boards") or [], err=err)


@router.get("/partials/leaderboard/{key}", response_class=HTMLResponse)
async def leaderboard(request: Request, key: str, user: str = Depends(current_user)):
    body, err = await _get(request, f"/leaderboards/{key}")
    st = await load_state(request)
    return partial(request, "partials/leaderboard.html", entries=body.get("entries") or body.get("leaderboard") or [],
                   err=err, mine=set(st["replicants"]))


# --- the game's health ---------------------------------------------------------------------------------------------
@router.get("/partials/health", response_class=HTMLResponse)
async def health(request: Request, user: str = Depends(current_user)):
    """GET /health (API, database, Redis), cached five minutes."""
    db = request.app.state.db
    c = await db.kv_get(HEALTH_KV, {}) or {}
    now = datetime.now(timezone.utc).timestamp()
    if now - float(c.get("t") or 0) > HEALTH_TTL:
        body, err = await _get(request, "/health")
        c = {"t": now, "body": body, "err": err}
        await db.kv_set(HEALTH_KV, c)
    return partial(request, "partials/health.html", h=c.get("body") or {}, err=c.get("err"))


# --- a system: its events, the game's stockpiles there, stars nearby ------------------------------------------------
@router.get("/partials/system-extra/{star}", response_class=HTMLResponse)
async def system_extra(request: Request, star: str, user: str = Depends(current_user)):
    star = star.upper()
    events, e1 = await _get(request, f"/locations/{star}/events")
    inv, e2 = await _get(request, f"/locations/{star}/inventory")
    near, e3 = await _get(request, f"/locations/{star}/stars", per_page=20)
    stars = [s for s in near.get("stars") or [] if s.get("designation") != star]
    if stars:   # stars the catalog may lack (far from Sol): onto the map, as a census would
        from .census import merge
        db = request.app.state.db
        await db.kv_set("stars", merge(await db.kv_get("stars", {}) or {}, {s["designation"]: s for s in stars
                                                                             if s.get("designation")}))
    return partial(request, "partials/system_extra.html", star=star, events=events.get("events") or [],
                   inv=inv.get("locations") or ([{"location": inv.get("location"), "items": inv.get("items") or []}]
                                                if inv.get("items") else []),
                   stars=stars, errs=[e for e in (e1, e2, e3) if e])


# --- a replicant's event log ---------------------------------------------------------------------------------------
@router.get("/partials/replicant-events/{code}", response_class=HTMLResponse)
async def replicant_events(request: Request, code: str, event_type: str = "", user: str = Depends(current_user)):
    params = {"latest": "true", "limit": 30, "exclude_mining_cycles": "true", "exclude_ami_evaluations": "true"}
    if event_type:
        params["event_type"] = event_type
    body, err = await _get(request, f"/replicants/{code}/events", **params)
    return partial(request, "partials/replicant_events.html", code=code, events=body.get("events") or [], err=err,
                   event_type=event_type)


# --- device permissions ---------------------------------------------------------------------------------------------
@router.get("/partials/permissions/{code}", response_class=HTMLResponse)
async def permissions(request: Request, code: str, user: str = Depends(current_user)):
    body, err = await _get(request, f"/devices/{code}/permissions")
    return partial(request, "partials/permissions.html", code=code, perms=body.get("permissions") or [], err=err)


@router.post("/devices/{code}/permissions", response_class=HTMLResponse)
async def permissions_change(request: Request, code: str, permission_type: str = Form(...), account_id: str = Form(...),
                             revoke: str = Form(""), user: str = Depends(current_user)):
    """Grant or revoke (the spec names no request body: sent as permission_type + account_id, the fields its answer
    and list use, granted_account_id too)."""
    try:
        acct: int | str = int(account_id)
    except ValueError:
        acct = account_id.strip()
    body = {"permission_type": permission_type.strip(), "account_id": acct, "granted_account_id": acct}
    method = "DELETE" if revoke else "POST"
    return await run_action(request, user, method, f"/devices/{code}/permissions", body,
                            f"{code}: {'revoke' if revoke else 'grant'} {permission_type} for account {account_id}")


# --- the blog ---------------------------------------------------------------------------------------------------------
@router.get("/blog", response_class=HTMLResponse)
async def blog(request: Request, q: str = "", user: str = Depends(current_user)):
    params = {"latest": "true", "limit": 20, "full_content": "true"}
    if q.strip():
        params["keywords"] = q.strip()
    body, err = await _get(request, "/blog", **params)
    st = await load_state(request)
    names = sorted({str(r.get("name")) for r in st["replicants"].values() if isinstance(r, dict) and r.get("name")})
    return await page(request, user, "blog.html", "blog", posts=body.get("posts") or [], err=err, q=q, my_names=names)


@router.post("/blog", response_class=HTMLResponse)
async def blog_post(request: Request, replicant_name: str = Form(...), subject: str = Form(...), content: str = Form(...),
                    post_id: str = Form(""), delete: str = Form(""), user: str = Depends(current_user)):
    if post_id and delete:
        return await run_action(request, user, "DELETE", f"/blog/{post_id}", None, f"delete blog post {post_id}")
    body = {"subject": subject.strip(), "content": content.strip()}
    if post_id:
        return await run_action(request, user, "PATCH", f"/blog/{post_id}", body, f"edit blog post {post_id}")
    return await run_action(request, user, "POST", "/blog", {**body, "replicant_name": replicant_name},
                            f"blog post by {replicant_name}: {subject[:40]}")
