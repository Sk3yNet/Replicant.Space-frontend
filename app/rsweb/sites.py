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

from datetime import timedelta

from .automations import STEP_TIMEOUT, step
from .salvage import belt_of

WORKING = ("tracking", "searching")
# A search refused with "Maximum active sites (5) reached at this belt" (live 2026-10-19 at ALPHARDAN-BELT-1, whose
# detail listed no sites: searches under way count toward the cap): no new search there for this long.
FULL_WAIT = timedelta(minutes=90)
MAX_SITES = 5   # the game's most active sites per belt ("Maximum active sites (5) reached at this belt")
# Belt surveyors search belts about four times faster than survey drones (owner 2026-10-24), so they get the belt's
# search slots first. Live 2026-10-23: features cruise + survey only (no stow, no deploy, no surge), commands
# scan/search/travel, never under an AMI controller: they search on their own.
BELT_SURVEYOR = "belt_surveyor"
SURVEYORS = (BELT_SURVEYOR, "survey_drone")


def is_surveyor(d: dict) -> bool:
    """A device that searches belts and scans: a belt surveyor or a survey drone."""
    t = d.get("device_type") or ""
    return t == BELT_SURVEYOR or "survey_drone" in t


def surveyors_first(ds: list[dict]) -> list[dict]:
    """Belt surveyors ahead of survey drones (stable otherwise)."""
    return sorted(ds, key=lambda d: d.get("device_type") != BELT_SURVEYOR)


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
    """Survey drones and belt surveyors already opening/holding a site at this belt."""
    return [d for d in devices if is_surveyor(d) and d.get("location") == belt
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
