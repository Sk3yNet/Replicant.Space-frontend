"""Activity › Chatter: the NPC chatter library (chatter.py), its settings, a bulk import with a per-message approval,
and what was queued and posted."""
from __future__ import annotations

import html
from collections import defaultdict

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from . import chatter as ch
from .db import now_iso
from .web import current_user, load_state, page, partial

router = APIRouter()


async def _cfg(request: Request) -> dict:
    return ch.normalize(await request.app.state.db.kv_get(ch.KV, {}))


async def _save(request: Request, cfg: dict) -> None:
    await request.app.state.db.kv_set(ch.KV, cfg)


@router.get("/chatter", response_class=HTMLResponse)
async def chatter_page(request: Request, user: str = Depends(current_user)):
    st = await load_state(request)
    reps = st["replicants"]
    cfg = await _cfg(request)
    state = ch.norm_state(await request.app.state.db.kv_get(ch.STATE_KV, {}))
    groups = defaultdict(list)
    for m in cfg["lines"]:
        groups[m.get("on")].append(m)
    answers = defaultdict(list)
    for r in cfg["replies"]:
        answers[r.get("after") or f"to:{r.get('to')}"].append(r)
    names = {c: r.get("name") or c for c, r in reps.items()}
    npc_list = [names[c] for c in ch.npcs(reps)]
    not_npc = [names[c] for c in reps if c not in ch.npcs(reps)]
    return await page(request, user, "chatter.html", "chatter", cfg=cfg, state=state, by_trigger=groups, answers=answers,
                      names=names, triggers=ch.TRIGGERS, slots=ch.SLOTS,
                      channels=await channels(request.app.state.db, cfg), npc_list=npc_list, not_npc=not_npc,
                      today=ch.posted_today(state), describe=lambda m: ch.describe(m, reps),
                      channel=lambda m: ch.channel_of(m, cfg, "(same as the post)"))


async def channels(db, cfg: dict) -> list[str]:
    """The two chatter channels, #general and #trade, then every channel a saved BobNet post was in."""
    seen = [r["ch"] for r in await db.fetchall(
        "SELECT DISTINCT json_extract(payload, '$.channel') AS ch FROM events WHERE event='bobnet.new' LIMIT 50")]
    out = []
    for c in [cfg["general"], cfg["bots"], "#general", "#trade", *sorted(str(x) for x in seen if x)]:
        if c not in out:
            out.append(c)
    return out


@router.post("/chatter/write", response_class=HTMLResponse)
async def chatter_write(request: Request, code: str = Form(...), channel: str = Form(""), other: str = Form(""),
                        text: str = Form(""), user: str = Depends(current_user)):
    """Write a message by hand as any of your replicants (NPC or not), to a listed channel or one typed in."""
    from .web import replicant_message
    ch_name = (other or channel).strip()
    if not ch_name:
        return HTMLResponse('<div class="result err">Pick or type a channel.</div>')
    if not ch_name.startswith("#"):
        ch_name = "#" + ch_name
    st = await load_state(request)
    if code not in st["replicants"]:
        return HTMLResponse('<div class="result err">Not one of your replicants.</div>')
    resp = await replicant_message(request, code, ch_name, text, user)
    if resp.headers.get("X-Sent") == "1":
        db = request.app.state.db
        state = ch.norm_state(await db.kv_get(ch.STATE_KV, {}))
        state["posted"] = (state["posted"] + [{"at": now_iso(), "id": "", "by": code, "channel": ch_name,
                                               "text": text.strip(), "why": "written by hand", "ok": True}])[-ch.POSTED_KEEP:]
        await db.kv_set(ch.STATE_KV, state)
    return resp


@router.post("/chatter/settings", response_class=HTMLResponse)
async def chatter_settings(request: Request, user: str = Depends(current_user)):
    form = await request.form()
    cfg = await _cfg(request)
    cfg["enabled"] = "enabled" in form
    bad = []
    for k in ("general", "bots"):
        v = str(form.get(k) or "").strip()
        if v:
            cfg[k] = v if v.startswith("#") else "#" + v
    for k, lo, hi in (("chance", 0, 1), ("reply_chance", 0, 1), ("npc_reply_chance", 0, 1), ("every_hours", 0, 720),
                      ("daily_cap", 0, 500), ("delay_min", 0, 1440), ("delay_max", 0, 1440), ("max_depth", 0, 10)):
        if k not in form:
            continue
        try:
            v = float(form.get(k))
        except (TypeError, ValueError):
            bad.append(k)
            continue
        if not lo <= v <= hi:
            bad.append(f"{k} (between {lo} and {hi})")
            continue
        cfg[k] = int(v) if k in ("daily_cap", "max_depth") else v
    for k in ("general_hours", "bots_hours"):   # blank: the channel uses every_hours
        if k not in form:
            continue
        v = str(form.get(k) or "").strip()
        if not v:
            cfg[k] = None
            continue
        try:
            f = float(v)
            assert 0 <= f <= 720
            cfg[k] = f
        except (ValueError, AssertionError):
            bad.append(f"{k} (between 0 and 720)")
    await _save(request, cfg)
    msg = f"Saved. Posting is {'on' if cfg['enabled'] else 'off'}."
    if bad:
        msg += " Not saved: " + ", ".join(bad) + "."
    return HTMLResponse(f'<div class="result">{html.escape(msg)}</div>')


async def _rows(request: Request, text: str):
    st = await load_state(request)
    cfg = await _cfg(request)
    rows, err = ch.parse_import(text, st["replicants"], cfg)
    return cfg, rows, err


@router.post("/chatter/preview", response_class=HTMLResponse)
async def chatter_preview(request: Request, messages: str = Form(""), user: str = Depends(current_user)):
    cfg, rows, err = await _rows(request, messages)
    return partial(request, "partials/chatter_import.html", rows=rows, err=err, done=False, text=messages, cfg=cfg,
                   triggers=ch.TRIGGERS)


@router.post("/chatter/import", response_class=HTMLResponse)
async def chatter_import(request: Request, user: str = Depends(current_user)):
    """Only the messages ticked in the preview go in the library."""
    form = await request.form()
    text = str(form.get("messages") or "")
    cfg, rows, err = await _rows(request, text)
    approved = {int(x) for x in form.getlist("ok") if str(x).isdigit()}
    n = 0
    if not err:
        n = ch.add_rows(cfg, rows, approved, replace="replace" in form)
        await _save(request, cfg)
    for i, r in enumerate(rows):
        r["result"] = "added" if i in approved and not r["errors"] else "skipped"
    return partial(request, "partials/chatter_import.html", rows=rows, err=err, done=True, text=text, cfg=cfg,
                   triggers=ch.TRIGGERS, added=n)


@router.post("/chatter/remove", response_class=HTMLResponse)
async def chatter_remove(request: Request, id: str = Form(...), user: str = Depends(current_user)):
    cfg = await _cfg(request)
    cfg["lines"] = [m for m in cfg["lines"] if m.get("id") != id]
    cfg["replies"] = [m for m in cfg["replies"] if m.get("id") != id]
    await _save(request, cfg)
    return HTMLResponse('<span class="muted">removed</span>')


@router.post("/chatter/clear", response_class=HTMLResponse)
async def chatter_clear(request: Request, what: str = Form("queue"), user: str = Depends(current_user)):
    db = request.app.state.db
    if what == "library":
        cfg = await _cfg(request)
        cfg["lines"], cfg["replies"] = [], []
        await _save(request, cfg)
        return HTMLResponse('<div class="result">Library cleared. Reload to see it.</div>')
    state = ch.norm_state(await db.kv_get(ch.STATE_KV, {}))
    state["queue"] = []
    await db.kv_set(ch.STATE_KV, state)
    return HTMLResponse('<div class="result">Queue cleared.</div>')


@router.post("/chatter/post", response_class=HTMLResponse)
async def chatter_post(request: Request, id: str = Form(...), user: str = Depends(current_user)):
    """Queue one message to go out on the next engine tick (an NPC speaks it; posting must be on)."""
    st = await load_state(request)
    reps = st["replicants"]
    cfg = await _cfg(request)
    m = next((x for x in cfg["lines"] + cfg["replies"] if x.get("id") == id), None)
    if not m:
        return HTMLResponse('<span class="lv-alert">not in the library</span>')
    by = ch.speaker_for(m, None, reps)
    if not by:
        return HTMLResponse('<span class="lv-alert">no NPC to say it (mark the replicant NPC on its page)</span>')
    if not cfg["enabled"]:
        return HTMLResponse('<span class="lv-alert">posting is off (Settings)</span>')
    db = request.app.state.db
    state = ch.norm_state(await db.kv_get(ch.STATE_KV, {}))
    state["queue"].append(ch.post_now(m, by, cfg))
    await db.kv_set(ch.STATE_KV, state)
    return HTMLResponse(f'<span class="muted">queued for {html.escape((reps.get(by) or {}).get("name") or by)}</span>')
