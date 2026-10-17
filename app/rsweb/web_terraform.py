"""Map › Terraforming: each monitored world's latest status (attributes, projections, what drives them, couplings near,
bio seeder preconditions), your terraforming devices with their strength / direction and on/off, and dropping rocks
(detect Kuiper objects, launch one with a vector charge). terraform.py has the rules."""
from __future__ import annotations

import html
from collections import defaultdict

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from . import terraform as tf
from .db import now_iso
from .web import call_action, current_user, load_state, page, render_action, run_action, sim_view, star_of

router = APIRouter()
LAUNCHED_KV = "launched_rocks"   # {object_designation: detonate response + at}: rocks you sent; defense leaves them be


@router.get("/terraforming", response_class=HTMLResponse)
async def terraforming_page(request: Request, user: str = Depends(current_user)):
    from .sim import is_virtual
    db = request.app.state.db
    sid = sim_view(request)
    st = await load_state(request)
    rows = await db.fetchall("SELECT payload, location, created_at FROM events WHERE event = 'terraform.status' "
                             "ORDER BY seq DESC LIMIT 400")
    # a simulation's worlds (VIRT…) only on its own view (?sim=), never among your real ones
    bodies = {b: v for b, v in tf.latest_by_body(rows).items() if is_virtual(b) == bool(sid)}
    devs = [d for d in st["devices"] if tf.kind(d)]
    by_loc: dict[str, list[dict]] = defaultdict(list)
    for d in devs:
        by_loc[d.get("location") or "aboard / in transit"].append({**d, "_set": tf.settings_of(d)})
    worlds = []
    for body in sorted(set(bodies) | {d.get("location") for d in devs if d.get("device_type") in tf.DEVICES and d.get("location")}):
        s = bodies.get(body) or {}
        worlds.append({"body": body, "status": s, "coupling": tf.coupling(s) if s else [], "bio": tf.bio_ready(s) if s else [],
                       "devices": by_loc.get(body, [])})
    monitors = sum(1 for d in devs if d.get("device_type") == "terraform_monitor"
                   and not str(d.get("status") or "").startswith(("inactive", "idle", "stowed")))
    rock_tools = [{**d, "_set": tf.settings_of(d)} for d in devs if d.get("device_type") in tf.ROCK_TOOLS]
    scanners = [d for d in st["devices"] if "system_scan" in (d.get("features") or [])
                and "detect_object" in (d.get("available_commands") or ["detect_object"])]
    launched = await db.kv_get(LAUNCHED_KV, {}) or {}
    return await page(request, user, "terraforming.html", "terraforming", worlds=worlds, attrs=tf.ATTRS, units=tf.UNITS,
                      targets=tf.TARGETS, in_range=tf.in_range,
                      effects=tf.EFFECTS, directions=tf.DIRECTIONS, monitors=monitors, cap=tf.MONITOR_CAP,
                      rock_tools=rock_tools, scanners=scanners, launched=launched,
                      stars=sorted({star_of(d.get("location")) for d in scanners + rock_tools if d.get("location")}))


@router.post("/terraforming/{code}/settings", response_class=HTMLResponse)
async def terraforming_settings(request: Request, code: str, strength: str = Form(""), direction: str = Form(""),
                                user: str = Depends(current_user)):
    s: dict = {}
    v = tf.clamp_strength(strength) if strength.strip() else None
    if v is not None:
        s["strength"] = round(v, 3)
    if direction:
        s["direction"] = direction
    if not s:
        return HTMLResponse('<span class="result err">Nothing to set.</span>')
    return await run_action(request, user, "PATCH", f"/devices/{code}", {"configuration": {"settings": s}},
                            f"{code}: strength {s.get('strength', '—')} · direction {s.get('direction', '—')}")


@router.post("/terraforming/group/power", response_class=HTMLResponse)
async def terraforming_group_power(request: Request, action: str = Form(...), codes: str = Form(""), sim: str = Form(""),
                                   user: str = Depends(current_user)):
    """Switch several devices on or off at once (e.g. all six orbital mirrors), then reload the page."""
    if action not in ("activate", "deactivate"):
        return HTMLResponse('<span class="result err">activate or deactivate</span>')
    done, failed = [], []
    for c in [x for x in codes.replace(",", " ").split() if x][:40]:
        out = await call_action(request, user, "POST", f"/devices/{c}", {"command": action}, f"{action} {c}")
        (done if out.get("ok") else failed).append(f"{c}" + ("" if out.get("ok") else f" ({out.get('error')})"))
    if sim.isdigit():
        await request.app.state.db.kv_set(f"sim:world:{sim}", {})
    text = f"{action}: {len(done)} done" + (f" · failed: {', '.join(failed)}" if failed else "")
    return HTMLResponse(f'<span class="result {"err" if failed else "ok"}">{html.escape(text)}</span>',
                        headers={} if failed else {"HX-Refresh": "true"})


@router.post("/terraforming/{code}/power", response_class=HTMLResponse)
async def terraforming_power(request: Request, code: str, action: str = Form(...), user: str = Depends(current_user)):
    if action not in ("activate", "deactivate"):
        return HTMLResponse('<span class="result err">activate or deactivate</span>')
    return await run_action(request, user, "POST", f"/devices/{code}", {"command": action}, f"{action} {code}")


@router.post("/terraforming/{code}/detect", response_class=HTMLResponse)
async def terraforming_detect(request: Request, code: str, user: str = Depends(current_user)):
    """Sweep the outer system for Kuiper objects (about an hour; found ones stay 24 h)."""
    return await run_action(request, user, "POST", f"/devices/{code}", {"command": "detect_object"}, f"{code}: detect objects")


@router.get("/terraforming/kuiper/{star}", response_class=HTMLResponse)
async def terraforming_kuiper(request: Request, star: str, user: str = Depends(current_user)):
    """The Kuiper objects detected in a system, with a launch form for each vector charge of yours at one."""
    from .api import ApiError
    star = star.upper()
    try:
        body = await request.app.state.api.get(f"/locations/{star}-KUIPER") or {}
    except ApiError as e:
        return HTMLResponse(f'<div class="result err">{html.escape(e.message)}</div>')
    objs = body.get("kuiper_objects") or []
    st = await load_state(request)
    charges = [d for d in st["devices"] if d.get("device_type") == "vector_charge" and star_of(d.get("location")) == star]
    scan = await request.app.state.worker.automations.system_scan(star) or {}
    targets = [p.get("designation") for p in scan.get("planets") or [] if isinstance(p, dict) and p.get("designation")]
    targets += [m.get("designation") for p in scan.get("planets") or [] if isinstance(p, dict)
                for m in p.get("moons") or [] if isinstance(m, dict) and m.get("designation")]
    out = [f'<div class="small"><b>{html.escape(star)} Kuiper objects</b> ({len(objs)} found; each stays 24 h after detection)</div>']
    if not objs:
        out.append('<p class="muted small">None found — run a detection first.</p>')
    for o in objs:
        des = html.escape(str(o.get("designation") or "?"))
        here = [c for c in charges if c.get("location") == o.get("designation")]
        out.append(f'<div class="small">• <span class="mono">{des}</span> {html.escape(str(o.get("mass_class") or ""))} '
                   f'{html.escape(str(o.get("composition") or ""))}')
        if here:
            opts = "".join(f'<option>{html.escape(t)}</option>' for t in targets)
            out.append(f' <form class="inline" hx-post="/terraforming/{here[0]["device_code"]}/detonate" hx-target="next .tf-out" '
                       f'hx-confirm="Launch {des} with vector charge {here[0]["device_code"]}? The charge is used up and the rock '
                       f'will strike the planet or moon you pick.">'
                       f'<input type="hidden" name="target" value="{des}">'
                       f'<input name="destination" list="tf-dest-{star}" placeholder="planet or moon" size="12" required>'
                       f'<datalist id="tf-dest-{star}">{opts}</datalist> <button class="small">Launch</button></form>'
                       f'<span class="tf-out"></span>')
        else:
            out.append(' <span class="muted">— a vector charge of yours deployed here can launch it</span>')
        out.append('</div>')
    return HTMLResponse("".join(out))


@router.post("/terraforming/{code}/detonate", response_class=HTMLResponse)
async def terraforming_detonate(request: Request, code: str, target: str = Form(...), destination: str = Form(...),
                                user: str = Depends(current_user)):
    r = await call_action(request, user, "POST", f"/devices/{code}",
                          {"command": "detonate", "target": target.strip().upper(), "destination": destination.strip().upper()},
                          f"launch {target} at {destination}")
    resp = r.get("response") or {}
    if r["ok"] and isinstance(resp, dict) and resp.get("object_designation"):
        db = request.app.state.db
        rocks = await db.kv_get(LAUNCHED_KV, {}) or {}
        rocks[resp["object_designation"]] = {**resp, "at": now_iso(), "by": user}
        await db.kv_set(LAUNCHED_KV, rocks)
    return render_action(request, r)
