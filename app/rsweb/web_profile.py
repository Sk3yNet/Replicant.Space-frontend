"""Your replicants' public profiles (PATCH /replicants/{code}) and species reputation (GET /accounts/reputation,
GET /replicants/{code}/reputation, GET /species). The reputation and species answers aren't documented, so they're
shown as they come (partials/records.html) and cached in kv "reputation" for the page."""
from __future__ import annotations

import html
import json

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from .api import ApiError
from .db import now_iso
from .web import call_action, current_user, load_state, page, partial, render_action

router = APIRouter()

# PATCH /v1/replicants/{code}: name (unique), pronouns ≤50, description ≤500, plan ≤500, project ≤2000
PROFILE_FIELDS = {"name": 60, "pronouns": 50, "description": 500, "plan": 500, "project": 2000}
# cohort_permission: whether sibling replicants may operate on this one's devices (only matters while the account's
# replicant_cooperation is "individual"); docs: replicant.space/docs/concepts/replicants
CHOICE_FIELDS = {"cohort_permission": ("private", "public")}


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
    for f, allowed in CHOICE_FIELDS.items():
        v = str(form.get(f) or "").strip()
        if f in form and v != str(form.get(f"orig_{f}") or "").strip():
            if v not in allowed:
                bad.append(f"{f} must be one of {', '.join(allowed)}")
            else:
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


def parse_import(text: str, reps: dict[str, dict]) -> tuple[list[dict], list[str]]:
    """Bulk profile import: {code: {name, description, plan, project, pronouns}} (or a list of such objects with a
    replicant_code). A key may also be a replicant's current name. Each row keeps only the fields that differ from the
    cached profile, and lists what's wrong with it (unknown replicant, too long, empty name)."""
    try:
        data = json.loads(text or "")
    except ValueError as e:
        return [], [f"not valid JSON: {e}"]
    if isinstance(data, list):
        data = {str(x.get("replicant_code") or x.get("code") or ""): x for x in data if isinstance(x, dict)}
    if not isinstance(data, dict) or not data:
        return [], ["expected an object of replicant code → profile fields"]
    by_name = {str(r.get("name") or ""): c for c, r in reps.items()}
    rows, seen = [], {}
    for key, fields in data.items():
        code = key if key in reps else by_name.get(key, key)
        cur = reps.get(code) or {}
        row = {"code": code, "current": cur.get("name") or code, "body": {}, "errors": [], "same": []}
        if not cur:
            row["errors"].append("not one of your replicants")
        if not isinstance(fields, dict):
            row["errors"].append("expected an object of fields")
            fields = {}
        for f, v in fields.items():
            if f in ("replicant_code", "code"):
                continue
            if f not in PROFILE_FIELDS:
                row["errors"].append(f"unknown field {f}")
                continue
            v = str(v or "").strip()
            if f == "name" and not v:
                row["errors"].append("the name can't be empty")
            elif len(v) > PROFILE_FIELDS[f]:
                row["errors"].append(f"{f} is {len(v)} characters (at most {PROFILE_FIELDS[f]})")
            elif v == str(cur.get(f) or "").strip():
                row["same"].append(f)
            else:
                row["body"][f] = v
        name = row["body"].get("name")
        if name:
            if name in seen:
                row["errors"].append(f"name also given to {seen[name]}")
            seen[name] = code
        rows.append(row)
    renamed = {r["code"] for r in rows if r["body"].get("name")}
    for r in rows:
        owner = by_name.get(r["body"].get("name") or "")
        if owner and owner != r["code"] and owner not in renamed:
            r["errors"].append(f"name already used by {owner}")
    return rows, []


async def _import_rows(request: Request, text: str) -> tuple[list[dict], list[str]]:
    st = await load_state(request)
    return parse_import(text, st["replicants"])


@router.post("/profiles/preview", response_class=HTMLResponse)
async def profiles_preview(request: Request, profiles: str = Form(""), user: str = Depends(current_user)):
    rows, err = await _import_rows(request, profiles)
    return partial(request, "partials/profile_import.html", rows=rows, err=err, done=False, text=profiles)


@router.post("/profiles/import", response_class=HTMLResponse)
async def profiles_import(request: Request, profiles: str = Form(""), user: str = Depends(current_user)):
    """Send one PATCH /replicants/{code} per valid row with changes; rows with errors are skipped."""
    rows, err = await _import_rows(request, profiles)
    db = request.app.state.db
    for row in rows:
        if row["errors"] or not row["body"]:
            continue
        out = await call_action(request, user, "PATCH", f"/replicants/{row['code']}", row["body"],
                                f"{row['code']}: profile import ({', '.join(row['body'])})")
        row["result"] = out
        if out["ok"]:
            reps = await db.kv_get("replicants", {}) or {}
            if row["code"] in reps:
                reps[row["code"]].update(row["body"])
                await db.kv_set("replicants", reps)
    return partial(request, "partials/profile_import.html", rows=rows, err=err, done=True, text=profiles)


async def _get(api, path: str) -> tuple[object, str | None]:
    try:
        return await api.get(path), None
    except ApiError as e:
        return None, f"{e.status}: {e.message}"


@router.post("/account/cooperation", response_class=HTMLResponse)
async def account_cooperation(request: Request, replicant_cooperation: str = Form(...), user: str = Depends(current_user)):
    """Account-level replicant cooperation (PATCH /accounts/me): "individual" (each replicant's cohort_permission
    decides) or "shared" (every replicant may operate on every sibling's devices)."""
    if replicant_cooperation not in ("individual", "shared"):
        return HTMLResponse('<div class="result err">Pick individual or shared.</div>')
    out = await call_action(request, user, "PATCH", "/accounts/me", {"replicant_cooperation": replicant_cooperation},
                            f"replicant cooperation: {replicant_cooperation}")
    if out["ok"]:
        db = request.app.state.db
        acct = await db.kv_get("account", {}) or {}
        acct["replicant_cooperation"] = replicant_cooperation
        await db.kv_set("account", acct)
    return render_action(request, out)


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
