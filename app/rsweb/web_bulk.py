"""Devices › List: tick several devices and send them all the same command.

Before sending, each ticked device is checked (bulk_check) and the ones that can't take it are listed and skipped:
the command isn't in its available commands, the device isn't in the list any more, an automation job is using it
(unless you include those), or — for deploy / detach — its carrier is between systems. Commands that need a picker
per device (set_directive, prospect) or that shouldn't go out many times (message) stay on the device page."""
from __future__ import annotations

import html

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from . import commands as cmdspec
from . import fleets as fl
from .web import (DANGEROUS, call_action, current_user, load_state, order_commands, parse_json_field, partial,
                  suggestions)

router = APIRouter()

MAX_DEVICES = 100
NOT_BULK = {"set_directive": "pick the directive per device on its page",
            "prospect": "aim each observatory on its page",
            "message": "send BobNet messages from one relay on its page"}


def bulk_commands(devices: list[dict]) -> list[tuple[str, int]]:
    """Commands any of the listed devices takes, with how many take each (destructive ones last)."""
    n: dict[str, int] = {}
    for d in devices:
        for c in d.get("available_commands") or []:
            n[c] = n.get(c, 0) + 1
    return [(c, n[c]) for c in order_commands(sorted(n, key=lambda c: (-n[c], c))) if c not in NOT_BULK]


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
                skip.append((code, f"{carrier} is travelling to {trip['destination']} — it would come out between systems"))
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


__all__ = ["router", "bulk_check", "bulk_commands", "DANGEROUS"]
