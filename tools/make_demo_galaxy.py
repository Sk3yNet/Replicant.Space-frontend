"""Build the mock server's demo galaxy (app/rsweb/demo_galaxy.json) from a Diagnostics snapshot.

    python tools/make_demo_galaxy.py <snapshot.json> [--stars map.json] [--out app/rsweb/demo_galaxy.json]

Only the shape of the account is kept: which device types sit in which systems, their statuses, cargo and stockpiles.
Every star except SOL gets a made-up name, every device and replicant code a made-up code, and every replicant a made-up
name; free text (tags, location names, welcome messages) is dropped. `--stars` is the app's /api/map.json?part=core
(saved from the browser): stars keep their real positions, and the stars near the account's systems come along. Without
it the systems are placed at random in two clusters 500 ly apart (the home sector and the Sol zone).

Start the mock with it: RS_MOCK_DEMO=1 uvicorn rsweb.mock:app --port 9000
"""
import argparse
import json
import math
import random
import re
import zipfile
from datetime import datetime, timezone

KEEP = {"device_code", "device_type", "features", "available_commands", "location", "status", "operational_capacity",
        "replicant_code", "controller_device_code", "attached_to_device_code", "stowed_in_device_code", "linked_device",
        "in_control_range", "queue_size", "created_at", "cargo", "cargo_capacity", "cargo_used", "ami_directive",
        "ami_directive_status", "available_directives", "attach_capacity", "attach_used", "attached_devices", "taxi_mode",
        "stow_capacity", "stow_used", "hosting_replicant", "travel", "stowed_devices", "mining", "beacon_only",
        "tracking_site", "scan", "settings", "deployed_at", "is_prospecting", "prospect", "prospect_completes_at",
        "prospect_direction", "prospect_started_at", "compact", "system_status", "upkeep_requirements"}
REP, HOST = "77F75255", "11ADA230"   # the mock's own replicant and vessel: its replicant commands act on these
NEAR_LY = 30.0                       # catalog stars this close to one of the account's systems come along
SYL = ["al", "ar", "bel", "cor", "dra", "el", "fen", "gal", "hy", "ith", "ka", "lor", "mer", "nor", "oth", "pha", "qua",
       "ren", "syl", "tor", "ul", "vex", "wyn", "xan", "yr", "zel", "bra", "cae", "dun", "eth", "fir", "gan", "hol", "ion",
       "jar", "kel", "lun", "mos", "nyx", "ora", "pel", "rho", "sar", "tha", "ven", "zor"]
NAMES = ["Arden", "Bramwell", "Corwin", "Delphine", "Ellery", "Fenwick", "Galen", "Hollis", "Isolde", "Jasper", "Kestrel",
         "Linden", "Marlow", "Nerys", "Orrin", "Peregrine", "Quill", "Rowan", "Sorrel", "Tamsin", "Ulric", "Vesper"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("snapshot")
    ap.add_argument("--stars", action="append", help="map.json (or a .zip of it); may repeat, a cut-off file is read up to the cut")
    ap.add_argument("--out", default="app/rsweb/demo_galaxy.json")
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    rnd = random.Random(a.seed)
    snap = json.load(open(a.snapshot))
    devices = (snap.get("app") or {}).get("devices") or []
    inventory = [loc for c in snap.get("calls") or [] if c.get("path") == "/inventory" and c.get("status") == 200
                 for loc in (c.get("body") or {}).get("locations") or []]

    def star_of(loc):
        return str(loc or "").split("-")[0]

    mine = {star_of(d.get("location")) for d in devices if d.get("location")}
    mine |= {star_of((d.get("travel") or {}).get("final_destination") or (d.get("travel") or {}).get("destination"))
             for d in devices if d.get("travel")}
    mine |= {star_of(i.get("location")) for i in inventory}
    mine.discard("")

    # stars: real positions when given, else two random clusters
    if a.stars:
        cat = {s["designation"]: s for f in a.stars for s in read_stars(f) if s.get("position")}
        pos = {n: cat[n]["position"] for n in mine if n in cat}
        if missing := sorted(mine - set(pos)):
            print(f"not in the star files, left off the map: {len(missing)} systems")
        keep = [s for s in cat.values() if s["designation"] in mine or any(
            math.dist(xyz(s["position"]), xyz(p)) <= NEAR_LY for p in pos.values())]
    else:
        keep = []
        for n in sorted(mine):
            c = (0, 0, 0) if n.startswith("SOL") or n in ("FRANZAN", "MIRFAKA") else (480, 120, -40)
            keep.append({"designation": n, "spectral_type": rnd.choice(["G2", "K3", "M4", "F6", "A1", "M2"]),
                         "position": {k: round(v + rnd.uniform(-25, 25), 2) for k, v in zip("xyz", c)}})

    used, names = {"SOL"}, {"SOL": "SOL"}
    for s in sorted(keep, key=lambda s: s["designation"]):
        if s["designation"] == "SOL":
            continue
        while True:
            n = "".join(rnd.choice(SYL) for _ in range(rnd.choice((2, 3, 3)))).upper()
            if 5 <= len(n) <= 10 and n not in used and n not in mine:
                break
        used.add(n)
        names[s["designation"]] = n

    codes = {}

    def code(c):
        if c not in codes:
            while True:
                n = "%08X" % rnd.getrandbits(32)
                if n not in codes.values() and n not in (REP, HOST):
                    break
            codes[c] = n
        return codes[c]

    # the replicant on the heaven vessel with the most devices becomes the mock's own (REP on HOST)
    hosts = [d for d in devices if d.get("hosting_replicant") and d.get("location")]
    count = {}
    for d in devices:
        count[d.get("replicant_code")] = count.get(d.get("replicant_code"), 0) + 1
    main_host = max(hosts, key=lambda d: (d["device_type"] == "heaven_vessel", count.get(d["replicant_code"], 0)))
    codes[main_host["hosting_replicant"]["replicant_code"]] = REP
    codes[main_host["device_code"]] = HOST
    rep_names = {}

    star_re = re.compile(r"\b(" + "|".join(sorted(map(re.escape, names), key=len, reverse=True)) + r")\b")
    hex_re = re.compile(r"\b[0-9A-F]{8}\b")

    def scrub(v):
        if isinstance(v, dict):
            return {k: scrub(x) for k, x in v.items() if not k.endswith("_name") and k not in ("tags", "welcome_message")}
        if isinstance(v, list):
            return [scrub(x) for x in v]
        if isinstance(v, str):
            return star_re.sub(lambda m: names[m.group(1)], hex_re.sub(lambda m: code(m.group(0)), v))
        return v

    out_devices = []
    for d in devices:
        x = scrub({k: v for k, v in d.items() if k in KEEP})
        if d.get("hosting_replicant"):
            rc = x["hosting_replicant"]["replicant_code"]
            rep_names.setdefault(rc, "bob-1" if rc == REP else f"{NAMES[len(rep_names) % len(NAMES)]}_{len(rep_names) + 1}")
            x["hosting_replicant"] = {"replicant_code": rc, "name": rep_names[rc]}
        x["tags"] = []
        out_devices.append(x)

    sol = xyz(next((s["position"] for s in keep if s["designation"] == "SOL"), {}))
    stars = []
    for s in keep:
        st = {k: s[k] for k in ("spectral_type", "color", "estimated_planets", "has_hub", "has_ward", "explored") if k in s}
        n = names[s["designation"]]
        stars.append({"designation": n, **st, "position": s["position"], "entry_point": f"{n}-3-L4",
                      "region": "solzone" if math.dist(xyz(s["position"]), sol) < 100 else "home"})
    replicants = [{"replicant_code": rc, "name": nm,
                   "hosted_device_code": next(d["device_code"] for d in out_devices
                                              if (d.get("hosting_replicant") or {}).get("replicant_code") == rc),
                   "device_count": sum(1 for d in out_devices if d.get("replicant_code") == rc)}
                  for rc, nm in rep_names.items()]
    by_code = {d["device_code"]: d for d in out_devices}
    for r in replicants:
        h = next(d for d in out_devices if d["device_code"] == r["hosted_device_code"])
        for _ in range(4):   # in a hold or towed: where its carrier is
            nxt = by_code.get(h.get("stowed_in_device_code") or h.get("attached_to_device_code"))
            if h.get("location") or not nxt:
                break
            h = nxt
        r["current_location"] = h.get("location") or (h.get("travel") or {}).get("destination")
        r["current_star"] = star_of(h.get("location")) or None
    doc = {"captured_at": snap.get("captured_at") or datetime.now(timezone.utc).isoformat(),
           "stars": sorted(stars, key=lambda s: s["designation"]), "devices": out_devices, "replicants": replicants,
           "inventory": scrub([{"location": i["location"], "items": i.get("items") or []} for i in inventory])}
    with open(a.out, "w") as f:
        json.dump(doc, f, separators=(",", ":"))
    print(f"{len(stars)} stars ({len(mine)} with the account's devices), {len(out_devices)} devices, "
          f"{len(replicants)} replicants, {len(inventory)} stockpiles -> {a.out}")


def read_stars(path):
    """The stars of a saved /api/map.json, also when it's zipped or cut off partway (uploads are capped)."""
    if path.endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            text = z.read(next(n for n in z.namelist() if n.endswith(".json"))).decode()
    else:
        text = open(path).read()
    i, dec, out = text.index("[", text.index('"stars"')) + 1, json.JSONDecoder(), []
    while True:
        while i < len(text) and text[i] in ", \n":
            i += 1
        try:
            s, i = dec.raw_decode(text, i)
        except ValueError:
            return out
        out.append(s)


def xyz(p):
    return (float(p.get("x") or 0), float(p.get("y") or 0), float(p.get("z") or 0))


if __name__ == "__main__":
    main()
