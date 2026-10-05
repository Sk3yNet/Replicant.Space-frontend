"""Turn a blueprint plan into a job: queue the prints, and if stock is short, put the system's
AMI mining controller (and transport controller, if the printer isn't where the mining happens)
to work on exactly the shortfall.

The prints are queued first: an autofactory holds a queued job in `waiting_for_resources` until
the materials arrive, so nothing has to wait on our side.
"""
from __future__ import annotations

import math

from .automations import step


def star_of(loc: str | None) -> str:
    return (loc or "").split("-")[0]


def controllers_in(devices: list[dict], star: str, kind: str) -> list[dict]:
    """AMI controllers of a kind ("mining" / "transport") in a system, idle ones first."""
    out = [d for d in devices
           if kind in (d.get("device_type") or "") and "ami" in (d.get("features") or ["ami"])
           and "controller" in (d.get("device_type") or "") and star_of(d.get("location")) == star
           and not (kind == "transport" and "ferry" in (d.get("tags") or []))]  # the ferry controller is for interstellar runs
    return sorted(out, key=lambda d: (not str(d.get("status", "")).startswith("idle"), d.get("device_code")))


def production_steps(printer_device: str, printer_label: str, lines: list[tuple[str, int]], short: dict[str, float],
                     mining: dict | None, transport: dict | None, deliver_to: str | None, gather: bool,
                     vessel_replicant: str | None = None, assign: list[tuple[str, str, int]] | None = None) -> list[dict]:
    """`assign` [(autofactory, device_type, n)] spreads the lines over several autofactories instead of `printer_device`."""
    steps = []
    for code, dtype, n in assign or []:
        steps.append(step(f"queue {n}× {dtype} on autofactory {code}", f"/devices/{code}",
                          {"command": "enqueue_print", "device_type": dtype, "quantity": n}))
    for dtype, n in [] if assign else lines:
        if vessel_replicant:  # heaven vessels have no queue: one print, straight away
            steps.append(step(f"print {dtype} on {printer_label}", f"/replicants/{vessel_replicant}/print", {"device_type": dtype}))
            continue
        steps.append(step(f"queue {n}× {dtype} on {printer_label}", f"/devices/{printer_device}",
                          {"command": "enqueue_print", "device_type": dtype, "quantity": n}))
    want = {r: int(math.ceil(q)) for r, q in short.items() if q > 0}
    if gather and want and mining:
        mc = mining["device_code"]
        steps.append(step(f"{mc}: gather {', '.join(f'{q} {r}' for r, q in want.items())}", f"/devices/{mc}",
                          {"command": "set_directive", "directive": "gather_resources", "configuration": want}))
        steps.append(step(f"{mc}: launch", f"/devices/{mc}", {"command": "launch"}))
        collect_from = mining.get("location")
        if transport and deliver_to and collect_from and collect_from != deliver_to:
            tc = transport["device_code"]
            steps.append(step(f"{tc}: deliver {collect_from} → {deliver_to}", f"/devices/{tc}",
                              {"command": "set_directive", "directive": "delivery",
                               "configuration": {"route": {"collect": collect_from, "deliver": deliver_to},
                                                 "requirement": want}}))
            steps.append(step(f"{tc}: launch", f"/devices/{tc}", {"command": "launch"}))
    return steps
