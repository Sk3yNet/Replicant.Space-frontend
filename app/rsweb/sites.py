"""Re-open resource sites at worked-out belts.

From the game docs: belts are effectively infinite, but only *open sites* can be mined. A survey drone
that runs `search` at a belt opens a new site and then stays there `tracking` it — move it away or
deactivate it and the site closes. Each new search takes longer (diminishing returns), and mined-out
sites regenerate slowly. Mining drones use every open site at their location without being told which.

So when a mining controller reports `exhausted` at a belt (or a drone is refused with "Belt exhausted"),
this sends survey capacity there:
  • an AMI survey controller in the system → (fly to the belt), adopt idle survey drones, `belt_search`, launch
  • otherwise idle unmanaged survey drones in the system → fly to the belt and `search` (they stay tracking)
"""
from __future__ import annotations

from .automations import STEP_TIMEOUT, step
from .salvage import belt_of

WORKING = ("tracking", "searching")


def star_of(loc: str | None) -> str:
    return (loc or "").split("-")[0]


def exhausted_belts(devices: list[dict], learned: list[str]) -> dict[str, str]:
    """belt -> why. From mining controllers' `_eval_state` (exhausted:[…]:<place>) and refusals we've seen."""
    out: dict[str, str] = {}
    for d in devices:
        state = str(((d.get("ami_directive") or {}).get("_eval_state")) or "")
        if "mining" in (d.get("device_type") or "") and state.startswith("exhausted"):
            place = state.rsplit(":", 1)[-1] if state.count(":") >= 2 else ""
            belt = belt_of(place) or belt_of(d.get("location"))
            if belt:
                out[belt] = f"{d['device_code']} reports exhausted"
    for p in learned:
        b = belt_of(p)
        if b:
            out.setdefault(b, "a drone was refused: belt exhausted")
    return out


def survey_at(devices: list[dict], belt: str) -> list[dict]:
    """Survey drones already opening/holding a site at this belt."""
    return [d for d in devices if "survey_drone" in (d.get("device_type") or "") and d.get("location") == belt
            and str(d.get("status") or "").startswith(WORKING)]


def ami_steps(ctrl: dict, belt: str, adopt: list[str]) -> list[dict]:
    code = ctrl["device_code"]
    steps = []
    if ctrl.get("location") != belt:
        st = step(f"{code} → {belt}", f"/devices/{code}", {"command": "travel", "destination": belt},
                  wait=["travel.arrived"], match={"destination": belt}, critical=True)
        st["wait_device"] = code
        steps.append(st)
    if adopt:
        steps.append(step(f"{code}: adopt {len(adopt)} survey drone(s)", f"/devices/{code}", {"command": "adopt", "devices": adopt}))
    steps.append(step(f"{code}: belt_search at {belt}", f"/devices/{code}",
                      {"command": "set_directive", "directive": "belt_search", "configuration": {}}, critical=True))
    steps.append(step(f"{code}: launch", f"/devices/{code}", {"command": "launch"}))
    return steps


def drone_steps(drone: dict, belt: str) -> list[dict]:
    code = drone["device_code"]
    steps = []
    if drone.get("location") != belt:
        st = step(f"{code} → {belt}", f"/devices/{code}", {"command": "travel", "destination": belt},
                  wait=["travel.arrived"], match={"destination": belt}, timeout=STEP_TIMEOUT, critical=True)
        st["wait_device"] = code
        steps.append(st)
    steps.append(step(f"{code}: search {belt} (then keeps tracking the new site)", f"/devices/{code}", {"command": "search"}))
    return steps
