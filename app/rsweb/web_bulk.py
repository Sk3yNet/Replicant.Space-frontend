"""Devices › Device list: tick several devices and send them all the same command.

Before sending, each ticked device is checked (bulk_check) and the ones that can't take it are listed and skipped:
the command isn't in its available commands, the device isn't in the list any more, an automation job is using it
(unless you include those), or — for deploy / detach — its carrier is between systems. Commands that need a picker
per device (set_directive, prospect) or that shouldn't go out many times (message) stay on the device page."""
from __future__ import annotations

import html
import re

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from . import commands as cmdspec
from . import fleets as fl
from . import loadouts as lo
from .web import (DANGEROUS, add_repeats, call_action, current_user, load_state, order_commands, parse_json_field, partial,
                  suggestions)

router = APIRouter()

MAX_DEVICES = 100
TAG_CMDS = {"add_tags": "add tags", "remove_tags": "remove tags"}   # not game commands: a PATCH of the device's tags
# also tag PATCHes: to:/at: tags for logistics to carry the device somewhere (loadouts.delivery_tags), or taking them off
DELIVERY_CMDS = {"deliver": "deliver to", "cancel_delivery": "cancel delivery"}
PSEUDO_CMDS = {**TAG_CMDS, **DELIVERY_CMDS}
NOT_BULK = {"set_directive": "pick the directive per device on its page",
            "prospect": "aim each observatory on its page",
            "message": "send BobNet messages from one relay on its page"}


def bulk_commands(devices: list[dict]) -> list[tuple[str, int]]:
    """Commands any of the listed devices takes, with how many take each (destructive ones last)."""
    n: dict[str, int] = {}
    for d in devices:
        for c in d.get("available_commands") or []:
            n[c] = n.get(c, 0) + 1
    return [(c, len(devices)) for c in PSEUDO_CMDS] + \
        [(c, n[c]) for c in order_commands(sorted(n, key=lambda c: (-n[c], c))) if c not in NOT_BULK]


def clean_tags(raw: str) -> list[str]:
    """'Spare, to:SOL  x' → ['spare', 'to:sol', 'x']: the game's tags are lower case, [a-z0-9-_:.], at most 32 long."""
    out = []
    for t in re.split(r"[,\s]+", raw or ""):
        t = re.sub(r"[^a-z0-9\-_:.]", "", t.lower())[:32]
        if t and t not in out:
            out.append(t)
    return out


def hosts_replicant(d: dict, devices: list[dict]) -> bool:
    """A replicant is in it: its host (hosting_replicant), a filled matrix (replicant_matrix), or anything with a filled
    matrix inside. An empty matrix (empty_replicant_matrix) or an empty matrix container is cargo like any other."""
    if d.get("hosting_replicant") or d.get("device_type") == fl.MATRIX_TYPE:
        return True
    code = d.get("device_code")
    by = {x.get("device_code"): x for x in devices}
    return bool(code) and any(code in {c.get("device_code") for c in fl._carriers_up(x, by)}
                              for x in devices if x.get("device_type") == fl.MATRIX_TYPE)


def deliver_refusal(d: dict, devices: list[dict]) -> str | None:
    """Why Deliver won't take `d` (None: it may). A replicant never goes as cargo (drop-offs never unload one): say how
    to move it instead, by its own drive, the vessel it rides in, or the replicant's transfer / teleport."""
    if not hosts_replicant(d, devices):
        return None
    if lo.can_travel(d) or lo.can_surge(d):
        return "it carries a replicant: move it with its own travel command"
    by = {x.get("device_code"): x for x in devices}
    ride = next((c for c in fl._carriers_up(d, by) if lo.can_travel(c) or lo.can_surge(c)), None)
    if ride:
        return (f"it holds a replicant and has no drive: fly {ride['device_code']} "
                f"({(ride.get('device_type') or 'vessel').replace('_', ' ')}), which it rides in, with its travel command")
    return ("it holds a replicant and has no drive: stow or attach it on a vessel (Stow into on its device page) and fly "
            "that, or move the replicant into an empty matrix with Transfer / Teleport on its Replicant page")


def bulk_check(codes: list[str], command: str, devices: list[dict], busy: set[str], stowed: dict[str, list[str]],
               include_busy: bool = False) -> tuple[list[str], list[tuple[str, str]]]:
    """(devices to send it to, [(device, why it's skipped)])."""
    by = {d.get("device_code"): d for d in devices}
    ok, skip = [], []
    if command in NOT_BULK:
        return [], [(c, NOT_BULK[command]) for c in codes]
    for code in codes:
        d = by.get(code)
        if not d:
            skip.append((code, "no longer in the device list"))
            continue
        cmds = d.get("available_commands")
        why = deliver_refusal(d, devices) if command == "deliver" else None
        if why:
            skip.append((code, why))
            continue
        if command in PSEUDO_CMDS:
            if code in busy and not include_busy:
                skip.append((code, "an automation job is using it (tick 'include devices automations are using' to send anyway)"))
            else:
                ok.append(code)
            continue
        if cmds is not None and command not in cmds:
            skip.append((code, f"{(d.get('device_type') or 'device').replace('_', ' ')} doesn't take {command} "
                               f"right now ({d.get('status') or 'status unknown'})"))
            continue
        if command == "enqueue_print" and "vessel" in (d.get("device_type") or ""):
            skip.append((code, "a vessel prints through its replicant, one at a time — use its device page"))
            continue
        if code in busy and not include_busy:
            skip.append((code, "an automation job is using it (tick 'include devices automations are using' to send anyway)"))
            continue
        if command in ("deploy", "detach"):
            carrier = code if command == "detach" else (d.get("stowed_in_device_code") or d.get("attached_to_device_code")
                                                       or next((c for c, kids in stowed.items() if code in kids), None))
            trip = fl.in_flight(by.get(carrier) or {}) if carrier else None
            if trip:
                skip.append((code, f"{carrier} is traveling to {trip['destination']} — it would come out between systems"))
                continue
        ok.append(code)
    return ok, skip


async def _context(request: Request, form) -> dict:
    codes = list(dict.fromkeys(c for c in form.getlist("codes") if c))
    command = (form.get("command") or "").strip()
    st = await load_state(request)
    eng = request.app.state.worker.automations
    busy = eng.busy_devices(await eng.jobs())
    stowed = await request.app.state.db.kv_get("stowed_map", {}) or {}
    ok, skip = bulk_check(codes, command, st["devices"], busy, stowed, form.get("include_busy") == "on") if command else ([], [])
    return {"codes": codes, "command": command, "ok": ok, "skip": skip}


def _warnings(ctx: dict) -> str:
    if not ctx["codes"]:
        return '<span class="muted small">Tick devices in the list below.</span>'
    if not ctx["command"]:
        return f'<span class="muted small">{len(ctx["codes"])} ticked — pick a command.</span>'
    out = [f'<span class="small"><b>{len(ctx["ok"])}</b> of {len(ctx["codes"])} ticked will get '
           f'<b>{html.escape(ctx["command"])}</b>.</span>']
    if len(ctx["ok"]) > MAX_DEVICES:
        out.append(f'<div class="lv-alert small">At most {MAX_DEVICES} at a time — tick fewer.</div>')
    if ctx["skip"]:
        out.append(f'<div class="lv-alert small">⚠ Skipped ({len(ctx["skip"])}):</div><ul class="small">'
                   + "".join(f'<li><span class="mono">{html.escape(c)}</span>: {html.escape(w)}</li>' for c, w in ctx["skip"])
                   + "</ul>")
    return "".join(out)


@router.get("/fleet/bulk-form", response_class=HTMLResponse)
async def bulk_form(request: Request, command: str = "", user: str = Depends(current_user)):
    if not command:
        return HTMLResponse("")
    if command == "deliver":
        return HTMLResponse(await deliver_field(request) + ' <span class="muted small">a star (another system: logistics '
                            'carries it there) or a spot (SOL-3, FALQUORYX-BELT-1: it goes on to that spot). Replaces earlier '
                            'to:/at: tags; spare and gather go, and so does its fleet unless the spot is in the fleet&#39;s home.</span>')
    if command == "cancel_delivery":
        return HTMLResponse('<span class="muted small">Takes the to:, at: and lift tags off the ticked devices.</span>')
    if command in TAG_CMDS:
        st = await load_state(request)
        known = sorted({t for d in st["devices"] for t in d.get("tags") or []})
        opts = "".join(f'<option value="{html.escape(t)}">' for t in known)
        return HTMLResponse(f'<label class="small" style="text-transform:none">Tags to {"add" if command == "add_tags" else "remove"} '
                            f'<input name="tags" list="bulk-tags" size="30" placeholder="e.g. spare, keep" required></label>'
                            f'<datalist id="bulk-tags">{opts}</datalist> <span class="muted small">comma or space separated; '
                            'lower case, letters, digits and - _ : . (fleet membership: use the Fleets page)</span>')
    fields = cmdspec.COMMANDS.get(command)
    return partial(request, "partials/command_form.html", code="", command=command, fields=fields or [],
                   description=cmdspec.DESCRIPTIONS.get(command, ""), known=fields is not None,
                   sugg=await suggestions(request, None), sys_targets=None, uid="bulk", self_code="", directives=None, rid="")


@router.post("/fleet/bulk-check", response_class=HTMLResponse)
async def bulk_preview(request: Request, user: str = Depends(current_user)):
    return HTMLResponse(_warnings(await _context(request, await request.form())))


@router.post("/fleet/bulk", response_class=HTMLResponse)
async def bulk_run(request: Request, user: str = Depends(current_user)):
    form = await request.form()
    ctx = await _context(request, form)
    if not ctx["codes"] or not ctx["command"]:
        return HTMLResponse('<div class="result err">Tick at least one device and pick a command.</div>')
    if len(ctx["ok"]) > MAX_DEVICES:
        return HTMLResponse(f'<div class="result err">At most {MAX_DEVICES} devices at a time.</div>')
    if ctx["command"] in DELIVERY_CMDS:
        return await deliver_devices(request, user, ctx["ok"], form.get("destination") or "",
                                     ctx["command"] == "cancel_delivery", ctx["skip"], len(ctx["codes"]))
    if ctx["command"] in TAG_CMDS:
        return await _bulk_tags(request, user, ctx, clean_tags(form.get("tags") or ""))
    body, extra = {}, {}
    try:
        if ctx["ok"]:   # with nothing to send, only the skipped list is shown
            body = cmdspec.parse_fields(cmdspec.COMMANDS.get(ctx["command"], []), form)
            extra = parse_json_field(form.get("args")) or {}
        if not isinstance(extra, dict):
            raise ValueError("extra arguments must be a JSON object")
    except cmdspec.FormError as e:
        return HTMLResponse(f'<div class="result err">{html.escape(str(e))}</div>')
    except ValueError as e:
        return HTMLResponse(f'<div class="result err">Bad arguments: {html.escape(str(e))}</div>')
    if form.get("repeat"):
        return await _bulk_repeat(request, user, ctx, {"command": ctx["command"], **body, **extra}, form)
    rows, sent = [], 0
    for code in ctx["ok"]:
        out = await call_action(request, user, "POST", f"/devices/{code}", {"command": ctx["command"], **body, **extra},
                                f"{ctx['command']} on {code} (bulk)")
        sent += out["ok"]
        rows.append(f'<li><a class="mono" href="/devices/{code}">{code}</a> '
                    + ('<span class="lv-done">✓</span>' if out["ok"] else
                       f'<span class="lv-alert">✗ {out["status"]}: {html.escape(str(out["error"] or ""))}</span>') + "</li>")
    rows += [f'<li><span class="mono">{html.escape(c)}</span> <span class="muted">skipped: {html.escape(w)}</span></li>'
             for c, w in ctx["skip"]]
    cls = "ok" if sent == len(ctx["ok"]) and not ctx["skip"] else "err" if not sent else ""
    return HTMLResponse(f'<div class="result {cls}"><strong>{html.escape(ctx["command"])}: {sent} of {len(ctx["codes"])} '
                        f'sent</strong><ul class="small">{"".join(rows)}</ul></div>',
                        headers={"HX-Trigger": "fleet-refreshed"} if sent else {})


async def _bulk_repeat(request: Request, user: str, ctx: dict, body: dict, form) -> HTMLResponse:
    """One repeat per ticked device (Console › Repeating), each sent once now."""
    from . import repeats as rp
    try:
        every = max(1, int(form.get("every_minutes") or rp.DEFAULT_MINUTES))
    except ValueError:
        return HTMLResponse('<div class="result err">Repeat every: give a number of minutes.</div>')
    if not ctx["ok"]:
        return HTMLResponse('<div class="result err">None of the ticked devices can take this command.</div>')
    name = (form.get("repeat_name") or "").strip()
    made = await add_repeats(request, user, [(f"{name or ctx['command']} {code}", "POST", f"/devices/{code}", body)
                                             for code in ctx["ok"]], every, form.get("stop_on_find") == "on")
    rows = [f'<li><a class="mono" href="/devices/{r["path"].split("/")[-1]}">{r["path"].split("/")[-1]}</a> '
            + ('<span class="lv-done">✓</span>' if r.get("last_result") == "ok" else
               f'<span class="lv-alert">✗ {html.escape(str(r.get("last_result") or ""))}</span>') + "</li>" for r in made]
    rows += [f'<li><span class="mono">{html.escape(c)}</span> <span class="muted">skipped: {html.escape(w)}</span></li>'
             for c, w in ctx["skip"]]
    return HTMLResponse(f'<div class="result ok"><strong>{html.escape(ctx["command"])}: repeating on {len(made)} device(s) '
                        f'every {every} min</strong> (first send now; stop them on the device page or under '
                        f'<a href="/console#repeats">Console › Repeating</a>)<ul class="small">{"".join(rows)}</ul></div>',
                        headers={"HX-Trigger": "fleet-refreshed"})


async def _bulk_tags(request: Request, user: str, ctx: dict, tags: list[str]) -> HTMLResponse:
    """Add or remove tags on the ticked devices (PATCH configuration add_tags / remove_tags); the cached device list
    shows the change at once."""
    if not tags:
        return HTMLResponse('<div class="result err">Give at least one tag.</div>')
    add = ctx["command"] == "add_tags"
    db = request.app.state.db
    devices = await db.kv_get("devices", []) or []
    by = {d.get("device_code"): d for d in devices}
    rows, sent, done = [], 0, {}
    for code in ctx["ok"]:
        have = set((by.get(code) or {}).get("tags") or [])
        todo = [t for t in tags if (t not in have) == add]
        if not todo:
            rows.append(f'<li><span class="mono">{html.escape(code)}</span> <span class="muted">already '
                        f'{"has" if add else "lacks"} {", ".join(tags)}</span></li>')
            continue
        out = await call_action(request, user, "PATCH", f"/devices/{code}",
                                {"configuration": {("add_tags" if add else "remove_tags"): todo}},
                                f"{'add' if add else 'remove'} tags {', '.join(todo)} on {code} (bulk)")
        if out["ok"]:
            sent += 1
            done[code] = todo
        rows.append(f'<li><a class="mono" href="/devices/{code}">{code}</a> '
                    + ('<span class="lv-done">✓</span>' if out["ok"] else
                       f'<span class="lv-alert">✗ {out["status"]}: {html.escape(str(out["error"] or ""))}</span>') + "</li>")
    if sent:   # on a fresh read: the orders' own device refreshes wrote the list meanwhile
        async with request.app.state.worker.devices_lock:
            devices = await db.kv_get("devices", []) or []
            for d in devices:
                todo = done.get(d.get("device_code"))
                if todo:
                    have = set(d.get("tags") or [])
                    d["tags"] = sorted(have | set(todo)) if add else sorted(have - set(todo))
            await db.kv_set("devices", devices)
    rows += [f'<li><span class="mono">{html.escape(c)}</span> <span class="muted">skipped: {html.escape(w)}</span></li>'
             for c, w in ctx["skip"]]
    verb = "added" if add else "removed"
    return HTMLResponse(f'<div class="result {"ok" if sent else ""}"><strong>{", ".join(tags)} {verb}: {sent} of '
                        f'{len(ctx["codes"])} device(s)</strong><ul class="small">{"".join(rows)}</ul></div>',
                        headers={"HX-Trigger": "fleet-refreshed"} if sent else {})


async def deliver_field(request: Request, value: str = "") -> str:
    """The destination box with known stars and device spots to pick from."""
    db = request.app.state.db
    st = await load_state(request)
    opts = {r["star"] for r in await db.fetchall("SELECT star FROM systems") if r["star"]}
    opts |= {d.get("location") for d in st["devices"] if d.get("location")}
    opts |= {lo.star_of(x) for x in list(opts)}
    dl = "".join(f'<option value="{html.escape(o)}">' for o in sorted(opts))
    return (f'<label class="small" style="text-transform:none">Destination <input name="destination" list="deliver-dests" '
            f'size="22" placeholder="SOL or SOL-3" value="{html.escape(value)}" required></label>'
            f'<datalist id="deliver-dests">{dl}</datalist>')


async def _known_star(request: Request, star: str, devices: list[dict]) -> bool:
    if any(lo.star_of(d.get("location")) == star for d in devices):
        return True
    if await request.app.state.db.fetchall("SELECT star FROM systems WHERE star = ?", (star,)):
        return True
    cat = await request.app.state.db.kv_get("stars", {}) or {}
    return any(isinstance(s, dict) and s.get("designation") == star for s in cat.get("stars") or [])


async def deliver_devices(request: Request, user: str, codes: list[str], dest: str, clear: bool = False,
                          skip: list[tuple[str, str]] | None = None, ticked: int | None = None) -> HTMLResponse:
    """Hand delivery (Joe 2026-11-07): tag each device for logistics to carry it to `dest` (loadouts.delivery_tags), or
    with `clear` take its delivery tags off. One PATCH per device; the cached device list shows the change at once."""
    db = request.app.state.db
    dest = (dest or "").strip().upper()
    devices = await db.kv_get("devices", []) or []
    by = {d.get("device_code"): d for d in devices}
    if not clear:
        if not dest or not lo.is_place(dest):
            return HTMLResponse(f'<div class="result err">{html.escape(dest or "No destination")} isn&#39;t a star or location '
                                'code (e.g. SOL, SOL-3, FALQUORYX-BELT-1).</div>')
        if not await _known_star(request, lo.star_of(dest), devices):
            return HTMLResponse(f'<div class="result err">{html.escape(lo.star_of(dest))} isn&#39;t a known star.</div>')
    homes = {fl.fleet_tag(f["id"]): f.get("home") or "" for f in await db.kv_get("fleets", []) or [] if f.get("id")}
    rows, done, sent = [], {}, 0
    for code in codes:
        d = by.get(code) or {}
        if clear:
            add, remove, why = [], lo.delivery_clear_tags(d), None if lo.delivery_clear_tags(d) else "no delivery tags"
        else:
            add, remove, why = lo.delivery_tags(d, dest, homes)
        if why:
            rows.append(f'<li><span class="mono">{html.escape(code)}</span> <span class="muted">{html.escape(why)}</span></li>')
            continue
        cfg = {k: v for k, v in (("add_tags", add), ("remove_tags", remove)) if v}
        out = await call_action(request, user, "PATCH", f"/devices/{code}", {"configuration": cfg},
                                f"{'cancel delivery of' if clear else 'deliver'} {code}{'' if clear else ' to ' + dest}"
                                f" ({' '.join(['+' + t for t in add] + ['-' + t for t in remove])})")
        if out["ok"]:
            sent += 1
            done[code] = (add, remove)
        rows.append(f'<li><a class="mono" href="/devices/{code}">{code}</a> '
                    + (f'<span class="lv-done">✓</span> <span class="muted small">{html.escape(" ".join(add) or "—")}'
                       f'{" (off: " + html.escape(" ".join(remove)) + ")" if remove else ""}</span>' if out["ok"] else
                       f'<span class="lv-alert">✗ {out["status"]}: {html.escape(str(out["error"] or ""))}</span>') + "</li>")
    if sent:   # on a fresh read, as _bulk_tags does
        async with request.app.state.worker.devices_lock:
            devices = await db.kv_get("devices", []) or []
            for d in devices:
                ch = done.get(d.get("device_code"))
                if ch:
                    d["tags"] = sorted((set(d.get("tags") or []) - set(ch[1])) | set(ch[0]))
            await db.kv_set("devices", devices)
    rows += [f'<li><span class="mono">{html.escape(c)}</span> <span class="muted">skipped: {html.escape(w)}</span></li>'
             for c, w in skip or []]
    head = (f"delivery canceled: {sent}" if clear else f"tagged for delivery to {html.escape(dest)}: {sent}") + \
        f" of {ticked if ticked is not None else len(codes)} device(s)"
    note = "" if clear or not sent else ('<div class="muted small">The loadouts pass (Fleets) picks it up: a carrier takes it '
                                         'to the system, and it goes on to its spot there. The tags go on arrival.</div>')
    return HTMLResponse(f'<div class="result {"ok" if sent else ""}"><strong>{head}</strong><ul class="small">{"".join(rows)}'
                        f'</ul>{note}</div>', headers={"HX-Trigger": "fleet-refreshed"} if sent else {})


@router.get("/devices/{code}/deliver-form", response_class=HTMLResponse)
async def device_deliver_form(request: Request, code: str, user: str = Depends(current_user)):
    return HTMLResponse(await deliver_field(request))


@router.post("/devices/{code}/deliver", response_class=HTMLResponse)
async def device_deliver(request: Request, code: str, user: str = Depends(current_user)):
    form = await request.form()
    st = await load_state(request)
    d = next((x for x in st["devices"] if x.get("device_code") == code), None)
    clear = form.get("clear") == "1"
    why = None if clear or not d else deliver_refusal(d, st["devices"])
    if why:
        return HTMLResponse(f'<div class="result err">{html.escape(why[0].upper() + why[1:])}.</div>')
    return await deliver_devices(request, user, [code], form.get("destination") or "", clear)


__all__ = ["router", "bulk_check", "bulk_commands", "DANGEROUS"]
