"""Field specs for device commands and AMI directives, the suggestions that fill each field's
drop-down, and turning a submitted form back into the JSON body the API expects.

Field kinds:
  resource        closed list of resource types            -> <select>
  resources       one number per resource ({res: n})       -> number inputs
  resource_list   ordered subset of resources ([res, ...]) -> checkboxes
  location        any location code                         -> text + suggestions
  device          one device code                           -> text + suggestions
  devices         several device codes ([code, ...])        -> checkboxes
  device_type     a blueprint / device type                 -> text + suggestions
  replicant       a replicant code                          -> text + suggestions
  channel         a BobNet channel                          -> text + suggestions
  tags            comma-separated tags ([tag, ...])         -> text + suggestions
  choice          fixed options (field["options"])          -> <select>
  bool            true / false                              -> <select>
  int / float     number
  vector          [x, y, z] direction                       -> 3 numbers
  text            free text
Field `name` may be a dotted path ("route.collect", "oncomplete.destination") to build nested objects.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

RESOURCES = ["structural", "conductive", "silicates", "carbon", "volatiles", "rares"]


@dataclass
class Field:
    name: str
    kind: str = "text"
    label: str = ""
    required: bool = False
    default: Any = None
    help: str = ""
    options: list = field(default_factory=list)
    filter: dict = field(default_factory=dict)  # e.g. {"device_type": "empty_replicant_matrix"} or {"feature": "ami"}

    @property
    def title(self) -> str:
        return self.label or self.name.split(".")[-1].replace("_", " ")


F = Field

# --- device commands --------------------------------------------------------------------------
COMMANDS: dict[str, list[Field]] = {
    "travel": [F("destination", "location", required=True, help="Location code (planet, belt, L4/L5, star …)")],
    "stow": [F("target", "device", help="Device to stow into (leave blank to stow on the default carrier)")],
    "deploy": [],
    "recall": [],
    "start_mining": [F("resource_type", "resource", required=True)],
    "retarget": [F("resource_type", "resource", required=True)],
    "collect_resources": [F("resources", "resources", help="Amounts to load from the stock here")],
    "deposit_resources": [F("resources", "resources", help="Leave blank to empty the whole hold")],
    "attach": [F("device", "device", required=True, label="device to attach to")],
    "detach": [],
    "system_scan": [],
    "scan": [],
    "search": [],
    "stellar_census": [],
    "prospect": [F("direction", "vector", help="Optional direction to look in, e.g. 0, 1, 0")],
    "enqueue_print": [
        F("device_type", "device_type", required=True),
        F("quantity", "int", default=1),
        F("tags", "tags", help="Tags applied to each printed device"),
        F("controller", "device", label="hand to AMI controller", filter={"feature": "ami"}),
        F("oncomplete.command", "choice", label="when printed", options=["", "travel", "start_mining"]),
        F("oncomplete.destination", "location", label="…travel to", filter={"scope": "system"}),
        F("oncomplete.resource_type", "resource", label="…mine resource"),
        F("flatpack", "bool", default=False),
    ],
    "dequeue_print": [F("index", "int", required=True, default=0, help="Queue position, counted from 0 (0 = first waiting)")],
    "clear_queue": [],
    "repair": [F("target", "device", help="Device to repair (if the drone needs a target)")],
    "replicate": [F("target", "device", required=True, label="empty matrix",
                    filter={"device_type": "empty_replicant_matrix"})],
    "adopt": [F("devices", "devices", required=True, filter={"not_feature": "ami"})],
    "release": [F("devices", "devices", required=True, filter={"not_feature": "ami"})],
    "set_directive": [F("directive", "choice", required=True)],  # AMI page renders the directive's own fields
    "clear_directive": [],
    "launch": [],
    "withdraw": [],
    "assemble": [F("destination", "location", help="Where to assemble the fleet (if required)")],
    "activate": [],
    "deactivate": [],
    "compact": [],
    "unfurl": [],
    "decommission": [],
    # live 2026-10-06: the game wants the new owner as `target` ("replicant_code: Unknown field")
    "change_owner": [F("target", "replicant", required=True, label="new owner",
                       help="The replicant that takes over this device")],
    "set_welcome_message": [F("message", "text", required=True)],
    "message": [F("channel", "channel", required=True, default="#general"), F("text", "text", required=True)],
}

# What each command does (shown above its fields and as the picker's tooltip). From the game docs and live use.
DESCRIPTIONS: dict[str, str] = {
    "travel": "Fly to a location. Inside a system this is a cruise; to another system the game plans surge legs (needs the "
              "surge feature, else ride a carrier). A drone tracking a site must be deactivated first.",
    "stow": "Board a carrier's hold (vessels, mobile fleets). The device must be at the carrier's location; it then rides "
            "along and doesn't show a location of its own.",
    "deploy": "Leave the carrier it's stowed in, at the carrier's location. Beacons, relays and slingshots start working "
              "once deployed (a relay also needs activate).",
    "recall": "Return to the AMI controller (or carrier) that runs it.",
    "start_mining": "Mine one resource at the belt site or salvage it's at. Mined resources pile up at that location.",
    "retarget": "Switch a mining drone to another resource without moving it.",
    "collect_resources": "Load resources from your stockpile at this location into the hold (up to its cargo capacity).",
    "deposit_resources": "Unload the hold into your stockpile at this location (leave blank to empty it).",
    "attach": "A carrier (surge plate, platform, carrier, mobile fleet, cargo vessel) takes a device on its attach "
              "points. Sent to the carrier, naming the cargo; both must be at the same location. Large (modular) devices "
              "must be compacted first.",
    "detach": "A carrier lets go of an attached device at its current location.",
    "system_scan": "Scan the whole system: planets, moons, belts and the entry point.",
    "scan": "A survey drone scans the body it's at (life, resources, salvage, events).",
    "search": "A survey drone searches the belt it's at and opens a mining site, then stays tracking it (moving or "
              "deactivating it closes the site).",
    "stellar_census": "List the stars around this vessel: positions, entry points and whether they're explored. Adds "
                      "them to the star catalog (Map › Stars).",
    "prospect": "A galactic observatory looks for resources and events in a direction (optional).",
    "enqueue_print": "Add a print to this autofactory's queue. It waits for materials at the factory's location; tags "
                     "and an on-complete command apply to the new device.",
    "dequeue_print": "Remove one waiting print from the queue (1 = first waiting item).",
    "clear_queue": "Remove every waiting print (the one already printing carries on).",
    "repair": "A maintenance drone repairs a device at its location.",
    "replicate": "Copy this replicant matrix into an empty matrix at the same location (a new replicant).",
    "adopt": "An AMI controller takes these drones under its control; its directive then drives them.",
    "release": "An AMI controller lets these drones go; they stop where they are, idle.",
    "set_directive": "Give an AMI controller its standing orders (gather, survey, ferry …). Launch to start it.",
    "clear_directive": "Stop an AMI controller's directive; its drones stay adopted.",
    "launch": "Start the controller's directive: it deploys and sends out its drones.",
    "withdraw": "The AMI controller calls its drones back in.",
    "assemble": "A fleet controller gathers its adopted devices (at a destination, if given).",
    "activate": "Switch a device on: relays (at an L4/L5 point), hubs, wards, AMI controllers, propulsors, or a drone "
                "that was deactivated.",
    "deactivate": "Switch a device off. A survey drone tracking a site must be deactivated before it can move "
                  "(this closes the site).",
    "compact": "Fold a large (modular) device for transport: autofactories, observatories, hubs. Takes ~30% of its "
               "print time; carriers refuse it unfolded.",
    "unfurl": "Unfold a compacted modular device so it works again (~30% of its print time).",
    "decommission": "Scrap the device for part of its materials. Can't be undone.",
    "change_owner": "Hand the device to another of your replicants. A replicant can only command devices it owns.",
    "set_welcome_message": "The message a system hub shows to visitors.",
    "message": "Post to a BobNet channel through this FTL relay.",
}

# --- AMI directives (configuration fields) ------------------------------------------------------
DIRECTIVES: dict[str, dict[str, list[Field]]] = {
    "mining": {
        "gather_resources": [F("", "resources", label="target amounts", help="Stop when these amounts are gathered")],
        "gather_evenly": [],
        "maintain_ratios": [F("", "resources", label="ratios", help="Decimals, e.g. structural 0.5, conductive 0.3",
                              options=["float"])],
        "deplete_smallest": [],
        "gather_salvage": [F("location", "location", required=True, label="body with salvage",
                             help="The planet/moon the salvage orbits (picking a -SAL- entry sends its body)",
                             filter={"scope": "system", "targets": ["salvage", "moon", "planet", "object"]}),
                           F("recall", "bool", default=True)],
    },
    "survey": {
        "survey_system": [F("planets", "choice", options=["all", "none"], default="all"),
                          F("moons", "choice", options=["all", "none"], default="all"),
                          F("recall", "bool", default=True)],
        "belt_search": [],
    },
    "transport": {
        "delivery": [F("route.collect", "location", required=True, label="collect from",
                       filter={"scope": "system", "targets": ["stockpile", "belt", "site", "salvage"]}),
                     F("route.deliver", "location", required=True, label="deliver to",
                       filter={"scope": "system", "targets": ["lagrange", "stockpile", "planet", "moon", "belt", "site", "object", "outer", "star"]}),
                     F("requirement", "resources", label="deliver until")],
        "shuttle": [F("collect", "location", required=True, filter={"scope": "system", "targets": ["stockpile", "belt", "site", "salvage"]}),
                    F("deliver", "location", required=True, filter={"scope": "system", "targets": ["lagrange", "stockpile", "planet", "moon", "belt", "site", "object", "outer", "star"]}),
                    F("priority", "resource_list")],
        "ferry": [F("collect", "location", required=True, filter={"scope": "system", "targets": ["stockpile", "belt", "site", "salvage"]}),
                  F("deliver", "location", required=True, help="Another system"),
                  F("priority", "resource_list")],
        "consolidate": [F("deliver", "location", required=True, filter={"scope": "system", "targets": ["lagrange", "stockpile", "planet", "moon", "belt", "site", "object", "outer", "star"]}),
                        F("priority", "resource_list")],
    },
    "maintenance": {"patrol": []},
    "trade": {"trade": [F("name", "text", required=True, label="shop name"), F("description", "text"),
                        F("announcement", "text")]},
    "fleet": {},
}


ALL_DIRECTIVE_FIELDS: dict[str, list[Field]] = {n: f for kind in DIRECTIVES.values() for n, f in kind.items()}


def directives_for(device: dict, blueprints: list[dict]) -> list[str]:
    """Directive names a device can take, most authoritative source first:
    the device itself, its blueprint (`directives`), then our built-in list for its controller kind."""
    names: list[str] = []

    def add(xs) -> None:
        for x in xs or []:
            if isinstance(x, dict):
                x = x.get("name") or x.get("directive")
            if isinstance(x, str) and x and x not in names:
                names.append(x)

    add(device.get("directives"))
    dtype = device.get("device_type")
    add(next((b.get("directives") for b in blueprints if b.get("device_type") == dtype), None))
    kind = controller_kind(dtype)
    if kind != "fleet":
        add(DIRECTIVES[kind].keys())
    return names


def directive_fields(name: str) -> list[Field]:
    return ALL_DIRECTIVE_FIELDS.get(name, [])


def controller_kind(dtype: str | None) -> str:
    for k in DIRECTIVES:
        if k in (dtype or ""):
            return k
    return "fleet"


# --- suggestions -----------------------------------------------------------------------------
def build_suggestions(state: dict, blueprints: list[dict], systems: list[dict], stars: dict, here: str | None) -> dict:
    """Everything a drop-down might offer, ranked so things near `here` come first."""
    devices = state.get("devices") or []
    here_star = (here or "").split("-")[0]
    locs: dict[str, str] = {}

    def add(code: Any, note: str = "") -> None:
        if isinstance(code, str) and code and code not in locs:
            locs[code] = note

    for d in devices:
        add(d.get("location"), "your devices")
    for r in (state.get("replicants") or {}).values():
        add(r.get("location") or r.get("current_location"), f"replicant {r.get('name', '')}".strip())
    for inv in state.get("inventory") or []:
        add(inv.get("location"), "stockpile")
    for code in (state.get("locations") or {}):
        add(code, "presence")
    for sysrow in systems:
        scan = sysrow.get("data") or {}
        for p in scan.get("planets") or []:
            des = p.get("designation")
            add(des, p.get("type", "planet"))
            for lp in ("L4", "L5"):
                add(f"{des}-{lp}", "Lagrange")
        for b in ((scan.get("asteroid_belt") or {}).get("belts")) or []:
            add(b.get("designation"), "belt")
        for k in ("kuiper", "oort"):
            add(((scan.get("outer_system") or {}).get(k) or {}).get("designation"), k)
        add(scan.get("entry_point"), "entry point")
    for s in (stars or {}).get("stars") or []:
        add(s.get("designation"), "star")
        add(s.get("entry_point"), "entry point")

    def rank(code: str) -> tuple:
        return (0 if here and code.startswith(here_star + "-") or code == here_star else 1, code)

    locations = [{"value": c, "label": n} for c, n in sorted(locs.items(), key=lambda kv: rank(kv[0]))][:3000]

    types = sorted({b.get("device_type") for b in blueprints if b.get("device_type")} |
                   {d.get("device_type") for d in devices if d.get("device_type")})
    tags = sorted({t for d in devices for t in (d.get("tags") or [])})
    reps = [{"value": c, "label": r.get("name") or c} for c, r in (state.get("replicants") or {}).items()]
    channels = (state.get("account") or {}).get("bobnet_channels") or ["#general", "#trade"]
    from .census import destination_systems
    yours = {(d.get("location") or "").split("-")[0] for d in devices if d.get("location")} - {""}
    scanned = {row.get("star") for row in systems}
    star_opts = destination_systems(stars or {}, scanned | yours, yours, here)
    return {"locations": locations, "device_types": types, "tags": tags, "replicants": reps,
            "channels": channels, "devices": devices, "here": here, "systems": star_opts}


def device_options(sugg: dict, f: Field, self_code: str | None) -> list[dict]:
    """Devices for a device/devices field: same location first, filtered by the field's filter."""
    here = sugg.get("here")
    out = []
    for d in sugg["devices"]:
        if d.get("device_code") == self_code:
            continue
        flt = f.filter or {}
        feats = d.get("features") or []
        if "device_type" in flt and d.get("device_type") != flt["device_type"]:
            continue
        if flt.get("feature") and flt["feature"] not in feats:
            continue
        if flt.get("not_feature") and flt["not_feature"] in feats:
            continue
        out.append({"value": d["device_code"], "label": f"{(d.get('device_type') or '').replace('_', ' ')} · "
                    f"{d.get('location')} · {d.get('status')}", "near": d.get("location") == here})
    out.sort(key=lambda o: (not o["near"], o["label"]))
    return out


# --- form -> JSON ------------------------------------------------------------------------------
class FormError(ValueError):
    pass


def _set(body: dict, path: str, value: Any) -> None:
    if not path:  # empty name = merge into the object itself (used by directive resource maps)
        if isinstance(value, dict):
            body.update(value)
        return
    parts = path.split(".")
    cur = body
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value


def _num(v: str, as_int: bool) -> float | int:
    v = v.strip()
    try:
        return int(v) if as_int and v.lstrip("-").isdigit() else (int(float(v)) if as_int else float(v))
    except ValueError as e:
        raise FormError(f"'{v}' is not a number") from e


def parse_fields(fields: list[Field], form) -> dict:
    """Read `f.<name>` inputs (as produced by partials/fields.html) into a nested dict."""
    body: dict = {}
    for f in fields:
        key = f"f.{f.name}"
        if f.kind == "resources":
            as_int = "float" not in f.options
            amounts = {}
            for r in RESOURCES:
                raw = (form.get(f"{key}.{r}") or "").strip()
                if raw:
                    amounts[r] = _num(raw, as_int)
            if amounts:
                _set(body, f.name, amounts)
            elif f.required:
                raise FormError(f"{f.title}: enter at least one amount")
            continue
        if f.kind in ("devices", "resource_list"):
            vals = [v for v in form.getlist(key) if v]
            if vals:
                _set(body, f.name, vals)
            elif f.required:
                raise FormError(f"{f.title}: pick at least one")
            continue
        if f.kind == "vector":
            raw = [(form.get(f"{key}.{i}") or "").strip() for i in range(3)]
            if any(raw):
                _set(body, f.name, [float(x or 0) for x in raw])
            elif f.required:
                raise FormError(f"{f.title} is required")
            continue
        # a location picker sends the typed code (__custom), the exact spot, or just the system (__star)
        raw = (form.get(key + "__custom") or form.get(key) or form.get(key + "__star") or "").strip()
        if not raw:
            if f.required:
                raise FormError(f"{f.title} is required")
            continue
        if f.kind == "int":
            val: Any = _num(raw, True)
        elif f.kind == "float":
            val = _num(raw, False)
        elif f.kind == "bool":
            val = raw.lower() in ("true", "1", "yes", "on")
        elif f.kind == "tags":
            val = [t.strip() for t in raw.split(",") if t.strip()]
        elif f.kind in ("device", "replicant", "location"):
            val = raw.upper()
        else:
            val = raw
        _set(body, f.name, val)
    # oncomplete only makes sense with a command
    oc = body.get("oncomplete")
    if isinstance(oc, dict):
        if not oc.get("command"):
            body.pop("oncomplete")
        elif oc["command"] == "travel":
            oc.pop("resource_type", None)
        elif oc["command"] == "start_mining":
            oc.pop("destination", None)
    return body
