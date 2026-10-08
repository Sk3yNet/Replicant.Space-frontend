"""Your replicants' public profiles (PATCH /replicants/{code}) and species reputation (GET /accounts/reputation,
GET /replicants/{code}/reputation, GET /species). The reputation and species answers aren't documented, so they're
shown as they come (partials/records.html) and cached in kv "reputation" for the page."""
from __future__ import annotations

import html

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from .api import ApiError
from .db import now_iso
from .web import call_action, current_user, load_state, page, partial, render_action

router = APIRouter()

# PATCH /v1/replicants/{code}: name (unique), pronouns ≤50, description ≤500, plan ≤500, project ≤2000
PROFILE_FIELDS = {"name": 60, "pronouns": 50, "description": 500, "plan": 500, "project": 2000}


def profile_changes(form) -> tuple[dict, list[str]]:
    """The fields that differ from what the form was rendered with, and any that are too long."""
    body, bad = {}, []
    for f, n in PROFILE_FIELDS.items():
        if f not in form:
            continue
        v = str(form.get(f) or "").strip()
        if v == str(form.get(f"orig_{f}") or "").strip():
            continue
        if f == "name" and not v:
            bad.append("the name can't be empty")
            continue
        if len(v) > n:
            bad.append(f"{f} is {len(v)} characters (at most {n})")
            continue
        body[f] = v
    return body, bad


@router.post("/replicants/{code}/profile", response_class=HTMLResponse)
async def replicant_profile(request: Request, code: str, user: str = Depends(current_user)):
    form = await request.form()
    body, bad = profile_changes(form)
    if bad:
        return HTMLResponse(f'<div class="result err">{html.escape("; ".join(bad))}.</div>')
    if not body:
        return HTMLResponse('<div class="result">Nothing changed.</div>')
    out = await call_action(request, user, "PATCH", f"/replicants/{code}", body, f"{code}: profile ({', '.join(body)})")
    if out["ok"]:   # keep the cached replicant in step so the name shows everywhere before the next sync
        db = request.app.state.db
        reps = await db.kv_get("replicants", {}) or {}
        if code in reps:
            reps[code].update(body)
            await db.kv_set("replicants", reps)
    return render_action(request, out)


async def _get(api, path: str) -> tuple[object, str | None]:
    try:
        return await api.get(path), None
    except ApiError as e:
        return None, f"{e.status}: {e.message}"


@router.get("/replicants/{code}/reputation", response_class=HTMLResponse)
async def replicant_reputation(request: Request, code: str, user: str = Depends(current_user)):
    body, err = await _get(request.app.state.api, f"/replicants/{code}/reputation")
    return partial(request, "partials/reputation.html", body=_unwrap(body, "reputation"), err=err)


def _unwrap(body, key: str):
    """{"reputation": [...]} → [...]; anything else as it is."""
    if isinstance(body, dict) and len(body) == 1 and key in body:
        return body[key]
    return body


@router.get("/reputation", response_class=HTMLResponse)
async def reputation_page(request: Request, refresh: int = 0, user: str = Depends(current_user)):
    """Account reputation, each replicant's, and the species you know — cached; ?refresh=1 asks the game again."""
    db, api = request.app.state.db, request.app.state.api
    cache = await db.kv_get("reputation", {}) or {}
    st = await load_state(request)
    if refresh or not cache.get("at"):
        acct, e1 = await _get(api, "/accounts/reputation")
        species, e2 = await _get(api, "/species")
        per = {}
        for c in st["replicants"]:
            b, e = await _get(api, f"/replicants/{c}/reputation")
            per[c] = {"body": _unwrap(b, "reputation"), "err": e}
        cache = {"at": now_iso(), "account": _unwrap(acct, "reputation"), "account_err": e1,
                 "species": _unwrap(species, "species"), "species_err": e2, "replicants": per}
        await db.kv_set("reputation", cache)
    names = {c: r.get("name") or c for c, r in st["replicants"].items()}
    return await page(request, user, "reputation.html", "reputation", c=cache, names=names)
