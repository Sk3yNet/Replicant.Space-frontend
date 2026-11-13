"""Events, notifications, messages and BobNet, account and console.

Routes split out of web.py by page area; they register on the shared router in the original order."""
from __future__ import annotations

from .web_core import *  # noqa: F401,F403


@router.get("/events", response_class=HTMLResponse)
async def events_page(request: Request, category: str = "", event: str = "", device: str = "", q: str = "",
                      before: int | None = None, user: str = Depends(current_user)):
    evs = await query_events(request, category, event, device, q, before)
    kw = dict(events=evs, f={"category": category, "event": event, "device": device, "q": q},
              last_seq=evs[-1]["seq"] if evs else None)
    if request.headers.get("hx-request") and request.headers.get("hx-target") in ("event-rows", "more-row"):
        return partial(request, "partials/event_rows.html", **kw)
    cats = [r["category"] for r in await request.app.state.db.fetchall(
        "SELECT DISTINCT category FROM events WHERE category IS NOT NULL ORDER BY category")]
    total = await request.app.state.db.fetchone("SELECT COUNT(*) AS n FROM events")
    return await page(request, user, "events.html", "events", categories=cats, total=total["n"], **kw)


@router.post("/events/backfill", response_class=HTMLResponse)
async def events_backfill(request: Request, user: str = Depends(current_user)):
    try:
        n = await request.app.state.worker.backfill()
        return HTMLResponse(f"Backfilled {n} event(s).")
    except ApiError as e:
        return HTMLResponse(f"Backfill failed: {html.escape(e.message)}")


# --- notifications & messages ----------------------------------------------------------------
@router.get("/notifications", response_class=HTMLResponse)
async def notifications(request: Request, show: str = "unread", user: str = Depends(current_user)):
    where = "WHERE read=0" if show == "unread" else ""
    rows = [{**r, "kind": notify.kind(r["level"])}
            for r in await request.app.state.db.fetchall(f"SELECT * FROM notifications {where} ORDER BY id DESC LIMIT 300")]
    return await page(request, user, "notifications.html", "notifications", rows=rows, show=show,
                      kind_counts=Counter(r["kind"] for r in rows))


@router.post("/notifications/read", response_class=HTMLResponse)
async def notifications_read(request: Request, id: int | None = Form(None), user: str = Depends(current_user)):
    if id:
        await request.app.state.db.execute("UPDATE notifications SET read=1 WHERE id=?", (id,))
    else:
        await request.app.state.db.execute("UPDATE notifications SET read=1 WHERE read=0")
    return HTMLResponse("", headers={"HX-Refresh": "true"} if not id else {})


@router.get("/messages", response_class=HTMLResponse)
async def messages(request: Request, user: str = Depends(current_user)):
    err = None
    try:
        if not request.headers.get("x-live"):   # a live redraw shows what's synced, it doesn't ask the game
            await request.app.state.worker.sync_messages()
    except ApiError as e:
        err = e.message
    msgs = await request.app.state.db.kv_get("messages", []) or []
    st = await load_state(request)
    # messages naming one of your replicants come first and are highlighted (your own posts aside)
    my = await notify.my_names(request.app.state.db)
    bobnet = await _bobnet_events(request.app.state.db, my, set(st["replicants"]), 200)
    for m in msgs:
        m["mention"] = bool(my) and notify.mentions(" ".join(str(m.get(k) or "") for k in ("title", "body", "text", "message")), my)
        m["unread"] = message_unread(m)
    # unread mentions first, then unread ones; a read mention is acknowledged and sits with the read ones
    msgs = sorted(msgs, key=lambda m: (not (m["mention"] and m["unread"]), not m["unread"]))
    db = request.app.state.db
    # a BobNet mention stays on the card until its notification is read (acknowledged here or on Notifications)
    open_ids = {r["event_id"] for r in await db.fetchall("SELECT event_id FROM notifications WHERE level='mention' AND read=0")}
    mentioned = [e for e in bobnet if e["mention"] and e["id"] in open_ids]
    subscribed = list(((await db.kv_get("account", {})) or {}).get("bobnet_channels") or [])
    cache = await db.kv_get("bobnet_channels", {}) or {}
    names = {c.get("name"): c for c in cache.get("channels") or [] if c.get("name")}
    channels = sorted(({"name": n, "last_active": (names.get(n) or {}).get("last_active"), "subscribed": n in subscribed,
                        "listed": n in names} for n in set(names) | set(subscribed)),
                      key=lambda c: (not c["subscribed"], -(_ts_num(c["last_active"])), c["name"]))
    return await page(request, user, "messages.html", "messages", msgs=msgs, err=err, bobnet=bobnet_novel_first(bobnet[:BOBNET_PAGE]),
                      older=bobnet[BOBNET_PAGE - 1]["seq"] if len(bobnet) > BOBNET_PAGE else None, mentioned=mentioned,
                      unread=sum(1 for m in msgs if m["unread"]), auto_read=await auto_read_cfg(db),
                      my_names=my,
                      replicants=st["replicants"], channels=channels, subscribed=subscribed, channel_cache=cache,
                      relay=bobnet_relay(st["devices"]))


@router.get("/bobnet/older", response_class=HTMLResponse)
async def bobnet_older(request: Request, before: int | None = None, q: str = "", user: str = Depends(current_user)):
    """A page of saved BobNet messages (the app keeps every one it has received) as feed rows: the next one older
    than `before`, or the first matching search `q`."""
    db = request.app.state.db
    rows = await _bobnet_events(db, await notify.my_names(db), set((await load_state(request))["replicants"]),
                                BOBNET_PAGE + 1, before, q)
    return partial(request, "partials/bobnet_rows.html", bobnet=bobnet_novel_first(rows[:BOBNET_PAGE]), before=before, q=q.strip(),
                   older=rows[BOBNET_PAGE - 1]["seq"] if len(rows) > BOBNET_PAGE else None)


@router.post("/bobnet/channels/refresh", response_class=HTMLResponse)
async def bobnet_channels_refresh(request: Request, user: str = Depends(current_user)):
    """Read the channel list from a relay (GET /devices/<relay>/channels)."""
    relay = bobnet_relay((await load_state(request))["devices"])
    if not relay:
        return HTMLResponse('<div class="result err">The channel list comes from an FTL relay, and you have none.</div>')
    try:
        body = await request.app.state.api.get(f"/devices/{relay['device_code']}/channels")
    except ApiError as e:
        return HTMLResponse(f'<div class="result err">{html.escape(relay["device_code"])}: {html.escape(e.message)}</div>')
    chans = [c for c in (body or {}).get("channels") or [] if isinstance(c, dict) and c.get("name")]
    await request.app.state.db.kv_set("bobnet_channels", {"at": now_iso(), "relay": relay["device_code"], "channels": chans})
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.post("/bobnet/subscribe", response_class=HTMLResponse)
async def bobnet_subscribe(request: Request, user: str = Depends(current_user)):
    """Set the account's BobNet channels (PATCH /accounts/me bobnet_channels): the ticked ones plus any typed in."""
    form = await request.form()
    chans = [c for c in form.getlist("channel") if c]
    for raw in re.split(r"[,\s]+", form.get("new") or ""):
        raw = raw.strip()
        if raw:
            chans.append(raw if raw.startswith("#") else "#" + raw)
    chans = list(dict.fromkeys(c.strip() for c in chans if c.strip() not in ("", "#")))
    out = await call_action(request, user, "PATCH", "/accounts/me", {"bobnet_channels": chans}, "BobNet channels")
    if not out["ok"]:
        return render_action(request, out)
    try:
        await request.app.state.worker.sync_account()
    except ApiError:
        pass
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.post("/bobnet/history", response_class=HTMLResponse)
async def bobnet_history(request: Request, channel: str = Form(""), cursor: int | None = Form(None),
                         user: str = Depends(current_user)):
    """BobNet messages a relay has heard (GET /devices/<relay>/messages), optionally one channel: the latest 50, or
    with `cursor` the page the game's next_cursor points to (an Older button follows while there is one)."""
    relay = bobnet_relay((await load_state(request))["devices"])
    if not relay:
        return HTMLResponse('<p class="muted small">Reading BobNet history needs an FTL relay.</p>')
    # NPCs too (Dash's posts show where the Ark is); latest is incompatible with cursor
    q = {"cursor": cursor} if cursor is not None else {"latest": "true"}
    try:
        body = await request.app.state.api.get(f"/devices/{relay['device_code']}/messages", limit=50,
                                                 include_npcs="true", **q)
    except ApiError as e:
        return HTMLResponse(f'<div class="result err">{html.escape(e.message)}</div>')
    body = body or {}
    msgs = [m for m in body.get("messages") or [] if not channel or m.get("channel") == channel]
    nxt = body.get("next_cursor")
    older = ""
    if nxt is not None and nxt != cursor:
        vals = html.escape(json.dumps({"channel": channel, "cursor": nxt}))
        older = (f'<div class="bn-older"><button type="button" class="small ghost" hx-post="/bobnet/history" hx-vals="{vals}"'
                 f' hx-target="closest div" hx-swap="outerHTML">Older</button></div>')
    if not msgs:
        where = " on " + html.escape(channel) if channel else ""
        return HTMLResponse(f'<p class="muted small">{"Nothing further back on this page" if cursor is not None else "Nothing recent"}'
                            f'{where}.</p>{older}')
    rows = "".join(f'<li><span class="t">{html.escape(f_local(m.get("time"), "%m-%d %H:%M"))}</span><span>'
                   f'[{html.escape(m.get("channel") or "")}] <b>{html.escape(m.get("replicant_name") or "?")}</b>'
                   f'<span class="muted"> @ {html.escape(m.get("current_star") or "?")}</span>: {html.escape(m.get("message") or "")}'
                   f'</span></li>' for m in msgs)
    return HTMLResponse(f'<ul class="feed">{rows}</ul>{older}')


@router.post("/messages/mentions/ack", response_class=HTMLResponse)
async def mentions_ack(request: Request, event_id: str = Form(""), user: str = Depends(current_user)):
    """Acknowledge a BobNet mention (or all of them): its notification is marked read and it leaves the card."""
    db = request.app.state.db
    if event_id:
        await db.execute("UPDATE notifications SET read=1 WHERE level='mention' AND event_id=?", (event_id,))
    else:
        await db.execute("UPDATE notifications SET read=1 WHERE level='mention' AND read=0")
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.post("/messages/auto-read", response_class=HTMLResponse)
async def messages_auto_read(request: Request, salvage: str = Form(""), civ: str = Form(""), user: str = Depends(current_user)):
    """Messages page checkboxes: which kinds of game message are marked read as they arrive. Unread ones already here
    are marked read at once."""
    from .ingest import AUTO_READ_KV
    await request.app.state.db.kv_set(AUTO_READ_KV, {"salvage": bool(salvage), "civ": bool(civ)})
    try:
        await request.app.state.worker.sync_messages()
    except ApiError as e:
        return HTMLResponse(f'<div class="result err">Saved, but reading messages failed: {html.escape(e.message)}</div>')
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.post("/messages/read", response_class=HTMLResponse)
async def messages_read(request: Request, id: int | None = Form(None), user: str = Depends(current_user)):
    body = {"ids": [id]} if id else {"mark_all": True}
    return await run_action(request, user, "POST", "/messages/read", body, "mark messages read")


# --- account & console ------------------------------------------------------------------------
@router.get("/account", response_class=HTMLResponse)
async def account(request: Request, user: str = Depends(current_user)):
    db = request.app.state.db
    actions = await db.fetchall("SELECT * FROM actions ORDER BY id DESC LIMIT 50")
    sync = {k: await db.kv_get(f"sync:{k}") for k in ("account", "devices", "inventory", "messages", "catalogue")}
    runs = list(reversed(await db.kv_get("server_runs", []) or []))
    return await page(request, user, "account.html", "account", runs=runs, changes=appver.CHANGES, **await wallpaper_ctx(request),
                      account=await db.kv_get("account", {}),
                      achievements=await db.kv_get("achievements", {}), actions=actions, sync=sync,
                      hub_listeners=request.app.state.hub.listeners)


__all__ = [n for n in dir() if not n.startswith("__")]   # everything, underscore helpers too
