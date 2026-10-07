"""App version, change history and server-run tracking.

Every automation log entry and job is stamped with the version and the server run that wrote it, so anything from
an older version or an earlier run can be told apart. Each start appends a run record ({run, version, fingerprint,
started_at, last_seen, stopped_at}) and logs a "server started" note, including what changed since the previous
version and whether the previous run stopped cleanly (no stopped_at = it was killed / crashed / redeployed hard).

`fingerprint` is a short hash of the app's code, so a code change shows up even if VERSION wasn't bumped.
Bump VERSION and add a CHANGES entry with every release.
"""
from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

VERSION = "1.30.2"

# newest first: (version, date, summary). Entries before 1.4.0 were reconstructed when versioning was added,
# so their dates are approximate and they group several drops each.
CHANGES: list[tuple[str, str, str]] = [
    ("1.30.2", "2026-10-07", "Moves of large devices: a compact refused because the device is already compacted counts as "
     "done and the move goes on (already compacting: it waits for device.compacted); an unfurl refused because the device "
     "isn't folded counts as done too."),
    ("1.30.1", "2026-10-07", "Desktop wallpaper: its scripts are loaded with the client version in the URL and the page "
     "itself isn't cached, so a redeploy takes effect at once (a cached older script could ignore newer settings such as "
     "relay/hub range). The client and add-on versions show in the wallpaper's bottom-right corner."),
    ("1.30.0", "2026-10-07", "Supply lines on the galaxy and the desktop wallpaper: an arc from each fleet's system to the "
     "fleet its materials go to, from a mining mission to its drop point, and along a trade fleet's run. Amber for "
     "materials, blue for trade; faint and dotted while only planned, solid while a ferry or mission runs it, with dots "
     "flowing while something travels along it. The wallpaper panel lists them, system views show the ones touching that "
     "system, and supply=0 (or the Galaxy page's 'supply lines' checkbox) hides them."),
    ("1.29.0", "2026-10-07", "Desktop wallpaper: a dashboard panel (device activity, stockpiles with their 48-hour trend, "
     "your fleets' missions), fleet markers on the galaxy (where each fleet is, what it's doing, a dashed line to where "
     "it's headed), fleets listed under each system, and settings to hide relay/hub range, fleets or the panel. The "
     "Galaxy page shows fleets too (the new 'fleets' checkbox)."),
    ("1.28.0", "2026-10-07", "Desktop wallpaper: the galaxy and system maps as a live Windows wallpaper with the Octos add-on "
     "(github.com/sk3ynet/replicant-space-octos). Account › Desktop wallpaper makes revocable, read-only wallpaper links; "
     "/wallpaper/ pages show the galaxy, one system or a cycle through your systems. Off until you make a link."),
    ("1.27.0", "2026-10-07", "Diagnostics: send feedback (bug / idea / typo) to the game's developers. Device pages: Cancel travel "
     "while a device is moving (a vessel hosting a replicant cancels through its replicant). A job waiting for a trip "
     "that gets cancelled stops waiting at once (the device turns back to where it started)."),
    ("1.26.4", "2026-10-07", "A manual deploy or detach is refused while the carrier is travelling (slingshot E28DBE58, deployed "
     "mid-surge, came out between systems with no location)."),
    ("1.26.3", "2026-10-07", "Vessels hosting a replicant can be added to a fleet under Add / remove devices (marked 'hosts "
     "<name>'): the replicant then travels with the fleet."),
    ("1.26.2", "2026-10-07", "System wards go with their fleet instead of being left behind: put system_ward in a fleet's "
     "loadout; it's deployed and activated where the fleet unloads and deactivated ('stop warding') right before it "
     "boards when the fleet leaves, then activated again where it's unloaded next."),
    ("1.26.1", "2026-10-07", "Systems list: a Wards, beacons & relays checkbox adds a column for each (and 'only systems "
     "missing one'). Survey crews drop and activate a system ward too, in each system without one of yours, and warn "
     "when there aren't enough aboard (or the 25-per-account limit would be passed)."),
    ("1.26.0", "2026-10-07", "Owner hand-off: a device boarding a carrier another replicant owns is handed to that replicant first "
     "(change_owner), and its fleet's keep-owner setting takes it back at the destination; so loadout deliveries use any "
     "free carrier again (their own replicant's first). A device hosting a replicant is never handed over. Starting a "
     "mission to a system without a relay of yours warns when no replicant rides with the fleet."),
    ("1.25.4", "2026-10-07", "Large devices start compacting as soon as a move is planned, in a job of their own; the carrier is "
     "only assigned once they report compacted (no carrier idling for 2+ h). The compact step waits 30 % of the print time "
     "+ 30 min (4 h when unknown). Loadout prints of large devices bound for another system are queued flat-packed."),
    ("1.25.3", "2026-10-07", "Fleet moves: a stationed fleet's carrier away from home is only sent home when no delivery needs "
     "it (Miner 1's carrier kept being sent home instead of fetching its 31 devices); the biggest batches get carriers "
     "first; a carrier delivering to its own fleet's home stays there; devices tagged for a fleet's old home are "
     "re-tagged to the new one."),
    ("1.25.2", "2026-10-07", "Survey drones tracking a site are deactivated right before a job moves them (and activated once "
     "unloaded) — moving Miner 1 and 2 failed every pass with 'Cannot cruise while tracking a site'. Device commands "
     "have descriptions (above the fields, and as tooltips in the command picker)."),
    ("1.25.1", "2026-10-07", "Large devices (feature `modular`: autofactories, galactic observatories) are compacted before any "
     "job moves them and unfurled once they land ('Cannot attach a large device' when carrying new observatories)."),
    ("1.25.0", "2026-10-07", "Trade fleets take on contracts as well as trades: freighters pick up what the site is short of "
     "from the nearest stockpiles, deposit it at the site, the fleet waits for a replicant there (a fleet vessel hosting one "
     "goes along) and fulfils, then loads the rewards and takes them to the nearest system whose fleet takes materials "
     "in before going home."),
    ("1.24.0", "2026-10-07", "Survey crews drop FTL relays and beacons: in each system without one of yours the carrier deploys a "
     "relay at the L4/L5 point (and activates it) and a beacon; if the survey finds a civilisation, the beacon is moved to "
     "that body before the fleet leaves. Starting a survey warns when there aren't enough aboard for the systems in the "
     "list, and the fleet card shows what's aboard."),
    ("1.23.7", "2026-10-07", "A fleet with no autofactory of its own that prints on another fleet's (e.g. the printing hub's) "
     "spreads its prints over all of them again, not just the first (1.23.5 put Miner 1's and Miner 2's on one)."),
    ("1.23.6", "2026-10-07", "Prints taken off an autofactory queue stop counting as incoming for their fleet (Remove / Clear / "
     "Cancel in the queue panel forgets them; one the factory no longer holds is dropped within 10 minutes), so the "
     "loadout pass queues the rest again. A stationed fleet's card lists its queued prints with Forget these."),
    ("1.23.5", "2026-10-06", "A fleet with its own autofactory prints its loadout on it (waiting there for materials if "
     "short); a fleet without one uses a fleetless factory before another fleet's. A device pinned with an at: tag stays "
     "there instead of being sent to its fleet's home."),
    ("1.23.4", "2026-10-06", "Loadout prints spread over a system's autofactories across passes too: prints this app queued "
     "that the device list doesn't show yet count as that factory's load (one print a pass all went to the same factory)."),
    ("1.23.3", "2026-10-06", "Loadout deliveries only use a carrier owned by the same replicant as the devices it carries "
     "(others are refused: 'belongs to a different account'). A mission carrier that arrives in the Kuiper belt or Oort cloud "
     "flies to a Lagrange point (else a planet) before unloading. A stow that finds the device already aboard counts as done. "
     "Devices already headed home are no longer held by a site they track in the old system."),
    ("1.23.2", "2026-10-06", "Moving a stationed fleet's home brings its whole working group: controllers that are coordinating, "
     "the drones they run and survey drones holding sites in the old system now go home too (the controllers drop their "
     "directives and let their drones go as they leave). Ferry freighters and taxi plates stay at their jobs. "
     "Loadout deliveries no longer borrow a carrier from a fleet that is on a mission."),
    ("1.23.1", "2026-10-06", "Mining missions to a system with no asteroid belt salvage instead: the drones fly to the body of "
     "the biggest known salvage and the mining controller gets gather_salvage there (no made-up belt code). With no belt "
     "and no known salvage the mission stalls with that reason. change_owner on a device the replicant already owns counts as done."),
    ("1.23.0", "2026-10-06", "Fleet owner: pick the replicant that owns a fleet; Set owner now hands every member to it "
                             "(change_owner), and keep re-checks every 5 minutes. Devices hosting a replicant are left alone."),
    ("1.22.1", "2026-10-06", "Mission targets are checked against known systems (did-you-mean on a typo) and offer every catalogue "
                             "and census star. The production planner no longer gives gather orders to a fleet's controller that "
                             "isn't at its station. to:/at: tags that aren't locations are ignored and flagged; the print queue "
                             "refuses a typed destination that isn't a location."),
    ("1.22.0", "2026-10-06", "Drone badges on the system map (M mining, S survey, T transport, R maintenance; count, coloured by "
                             "activity, hover for each drone; toggle). The galaxy map's system panel lists drones by kind."),
    ("1.21.1", "2026-10-06", "change_owner sends the new owner as `target` (the game rejected `replicant_code`)."),
    ("1.21.0", "2026-10-06", "BobNet channels on the Messages page: list them from a relay, tick the ones to listen to (or join "
                             "by name) and save to the account; recent messages from the relay. Fleets that aren't stationed now "
                             "stay aboard their carriers when they get home — only cargo is deposited."),
    ("1.20.0", "2026-10-06", "Devices in transit on the system and galaxy maps: an arrow on a dashed route pointing where they're "
                             "going, with progress and time left, updated live. Devices travelling together show as one. On a "
                             "system map, surges in or out sit on the rim toward the other star."),
    ("1.19.0", "2026-10-06", "Stellar census: new rule Stellar census on arrival (on) and Map › Stars with a census button per vessel; "
                             "census stars (beyond the catalogue's ~70 ly) are merged into the catalogue so the map, routes and "
                             "travel know them. Map › Stars lists unexplored stars nearest a chosen system. Travel destinations "
                             "are a system-then-location picker (census stars included). Nearest stars on the Replicant page page "
                             "through 20 at a time."),
    ("1.18.2", "2026-10-06", "Fetch, don't fly: a cruise-only device more than Max cruise AU (default 30, Fleets › Settings) "
                             "from its carrier is picked up by the carrier, nearest first, instead of cruising across the system "
                             "to board — fleet boarding, recall, gather/fill and loadout deliveries. Deliveries now board everyone "
                             "at the pick-up point before the carrier leaves to fetch beacons or far devices."),
    ("1.18.1", "2026-10-06", "Fleet recall / boarding no longer orders a device that's already flying: one already on its way "
                             "to the carrier is just waited for (up to its ETA), one flying elsewhere lands first. Fixes the "
                             "Surveyors recall stalling on \"Device is already in motion\" after survey_system recalled its drones. "
                             "The mission log flags a controller that runs the fleet's drones but isn't in the fleet."),
    ("1.18.0", "2026-10-06", "Home fleets are gone: one Fleets page, and any fleet can be stationed at its home system, where the "
                             "loadout pass keeps it at its loadout (fleetless devices there join it, extras become spare, spares "
                             "and prints fill it) and the in-system rules work its devices. Materials are a fleet setting: send "
                             "to another fleet, or take materials in. Templates stay. On upgrade each system's home fleet becomes "
                             "a stationed fleet and the next pass turns home: tags into fleet tags."),
    ("1.17.1", "2026-10-06", "docker-compose.yml is now the multi-user stack (the single-user one moved to "
                             "docker-compose.single.yml). OWNER_EMAIL defaults to the first ALLOWED_EMAIL."),
    ("1.17.0", "2026-10-06", "Multi-user mode (docker-compose.multi.yml): one app server per Google account, each with its "
                             "own game key, database and automations. A manager starts and supervises them and routes each "
                             "signed-in user to theirs; newcomers get a walkthrough at /_tenant/ to register a game account, "
                             "find the API key in the verification email and paste it (checked with the game before it's "
                             "stored). Owners keep their key and history; All servers lists and restarts everyone's."),
    ("1.16.0", "2026-10-05", "Mobile fleets can follow a template (the loadout comes from it, so editing the template "
                             "updates every fleet on it). New rule Fill mobile fleets from spares (and a Fill from spares "
                             "button): an idle fleet takes the nearest idle spares of its missing types, its carrier picks them "
                             "up and flies back. Fleets › Reset & reform: wipe the assignment tags (home/to/fleet/spare/gather) "
                             "and rebuild fleet, home-fleet and spare assignments from where devices are, releasing drones run "
                             "from outside their group; previewed before anything is sent."),
    ("1.15.0", "2026-10-05", "Navigation: 19 tabs regrouped into 6 (Dashboard, Devices, Map, Fleets, Economy, Activity) with "
                             "sub-tabs; URLs unchanged; Account, Diagnostics and Console under the user menu. Rule settings moved "
                             "onto the pages they work on (Rules panel); Automations keeps an on/off overview, jobs and the log; "
                             "AMI schedules moved to the AMI page. Loadouts is now Fleets › Home fleets (phases are templates; "
                             "mobile fleets based in a system are listed with it); Fleets is Mobile fleets."),
    ("1.14.0", "2026-10-05", "Several autofactories in a system share the printing, evenly by print time (each print goes to the "
                             "factory that would finish it first, counting its current print and queue): loadout passes, the "
                             "Blueprints planner (Spread over N autofactories), fleet Print missing and defence propulsors; "
                             "maintenance drones and civ beacons go to the least-loaded one. Mining and explore missions can't "
                             "target a system with a home fleet (a loadout phase): only its home fleet works it."),
    ("1.13.2", "2026-10-05", "Late events (over 30 min old when they arrive, e.g. the stream catching up after the 1.12.2 "
                             "deadlock: ~44 h of site.depleted alerts at once) go in the Events feed without notifications, and "
                             "don't fire the arrival or salvage rules; the first live event after them posts one catch-up note."),
    ("1.13.1", "2026-10-05", "Slingshot reworked to match the game docs: fire only a slingshot at the replicant's location "
                             "(teleport to its linked matrix); Link pairs a slingshot with a matrix at the same location (stowed in a "
                             "vessel that then carries it away). Relays: L4/L5 only."),
    ("1.13.0", "2026-10-05", "Placement: FTL relays go to a Lagrange point (they only activate there) and are activated "
                             "there; AMI mining controllers go to the asteroid belt; AMI survey controllers go to the belt, or the "
                             "inner system when there is none — loadout passes, the explore fleet's work phase and the arrival "
                             "auto-survey. Working devices in the wrong spot are only reported. Replicant page: FTL slingshot card "
                             "(link to a matrix if needed, then teleport; refuses below 80 % capacity)."),
    ("1.12.3", "2026-10-05", "Fix: Stop/Resume/End on a stalled fleet mission deadlocked the automation engine (the page took the "
                             "engine lock, then cancelled the mission's job, which took it again) — seen live: no rule, event or "
                             "loadout pass ran for ~43 h. The engine lock is now re-entrant; a watchdog alerts when it is held for "
                             "over 10 min, and snapshots show the engine status and last tick. Fleet deploy: 'Device is already "
                             "deployed' counts as done. Loadouts: when a system has too many of a type, the device already tagged "
                             "home:<system> is kept (FALQUORYX's home autofactory was about to be made spare)."),
    ("1.12.2", "2026-10-03", "Moving a system's devices: when no surge carrier is in the system, the nearest free one elsewhere flies in, "
                             "picks them up and goes back afterwards (seen live: 26 devices in AEMEROTH stuck 'waiting for a carrier'). "
                             "Spare devices are no longer re-adopted by AMI schedules / Restart idle miners (released drones were being "
                             "taken back within minutes). The ferry's controller, freighters, drones and taxi plates are never made spare; "
                             "the beacon at a civilisation's body is never made spare. Snapshots include the loadout config."),
    ("1.12.1", "2026-10-03", "Civilisation beacons: an existing beacon in the system (e.g. the Kuiper/Oort one) is moved to the civ body "
                             "by a vessel before anything is printed; with no free vessel it waits instead of printing a second one. "
                             "Beacons already at a civilisation's body are never taken."),
    ("1.12.0", "2026-10-03", "Redundant beacons (a system that already has a beacon at a civilisation's body, or a second beacon in a "
                             "system) are tagged spare — Traffic page 'Mark spare' or the civ beacon rule. Loadouts gather idle spares "
                             "at a spare depot (set it, or automatic: a materials destination system with an autofactory); they stay "
                             "spare there. Carriers pick up devices that can't fly (beacons) by going to them."),
    ("1.11.1", "2026-10-03", "Beacons at civilisation event sites: placed as soon as a survey discovers an event (not only on "
                             "completion). New ways to get one there: a vessel picks up a loose beacon tagged civ/spare and carries "
                             "it; otherwise one is printed on the system's autofactory (tagged civ) and fetched on a later pass. "
                             "Vessels hosting your replicant are only used if you allow it."),
    ("1.11.0", "2026-10-03", "Traffic page: each beacon's audit log read every 10 min, visitor alerts for other replicants, and "
                             "civilisation contact (civ follow-up requests need a beacon AT the body where you completed an event — "
                             "Kuiper/Oort beacons don't count) with Place beacon + rule 'Beacons at civilisation event sites'. "
                             "Defence page + rule: incoming asteroids, propulsors needed vs time left, activate/send/print. "
                             "Upkeep page + rule: maintenance drones kept on patrol, wear per system. Shop page: open a trade shop, "
                             "add/remove trades (escrow check), buy from other traders. README roadmap."),
    ("1.10.1", "2026-10-03", "A print bound for another system is dispatched as soon as it comes out (print.completed → device "
                             "list refreshed → its delivery or own surge started), instead of waiting for the next loadout pass."),
    ("1.10.0", "2026-10-03", "Print queue 'deliver to': choose a system and a location (or type one) when adding a print. Same system: "
                             "the game's oncomplete travel takes it there; another system: to:<system> + at:<location> tags, the loadout "
                             "pass delivers it and then sends it on to the spot. at: pins keep devices at their spot (sent back if "
                             "they wander; only a controller already there adopts them)."),
    ("1.9.3", "2026-10-03", "Loadouts fill shortfalls by role across all systems — AMI controllers, then survey drones, then mining "
                            "drones, then the rest — so surveyors are queued before the miners that depend on their sites."),
    ("1.9.2", "2026-10-03", "Loadouts: no extra miners sent to (or printed for) a system whose belts have no open sites; spares that "
                            "are still working (mining, tracking …) wait until idle instead of failing 'Cannot cruise while mining', "
                            "and aren't replaced by prints meanwhile. Tags check flags devices run by a fleet's controller that "
                            "aren't in the fleet."),
    ("1.9.1", "2026-10-02", "Fix: print queue Remove sent a 1-based index; dequeue_print is 0-based, so it removed the next item."),
    ("1.9.0", "2026-10-02", "New rule 'Consolidate stockpiles at the autofactory': stray piles in a system (e.g. contract leftovers) are "
                            "hauled to its autofactory by a free in-system transport controller (delivery directive), piles with what "
                            "the factory is waiting for first; open contract locations are left alone. Shown on the Loadouts page."),
    ("1.8.1", "2026-10-02", "Loadout changes reassign controllers: devices made spare are released by their controller (no more spare "
                            "tag flip-flopping while a controller still runs them). New 'Tags & controllers check' on the Loadouts "
                            "page lists any mismatch between tags, controller assignments and the loadouts, and whether the next pass fixes it."),
    ("1.8.0", "2026-10-02", "Belt viability: tracks search times and site lifetimes per belt (from data already read), shows search ÷ "
                            "site life, survey drones needed to keep the miners busy and a verdict on each System page and in "
                            "Diagnostics; 'Belt viability alerts' rule warns once when a belt passes the threshold and names the "
                            "nearest cheaper belt."),
    ("1.7.2", "2026-10-02", "Diagnostics: a belt with no open sites shows the survey searches under way there (drones, % done, when "
                            "the first new site is due) instead of telling you to start a search."),
    ("1.7.1", "2026-10-02", "Devices missing from GET /devices (e.g. freighters surging between systems) are kept for up to 12 h "
                            "at their last position, flagged in transit, unless an event says they're gone — fleets and loadouts "
                            "keep counting them. Sites/salvage only show on the map and Locations list while they're live; sites at "
                            "0% everywhere count as used up; belts your devices are at are re-read every 20 min."),
    ("1.7.0", "2026-10-02", "Fleets: 'End mission & board' — stop the mission now, clear controller directives, bring every device "
                            "back aboard its carriers (stow or attach) and stay where it is. 'Board everyone' does the same for an "
                            "idle fleet. 'Recall now' is now 'Recall & return home'."),
    ("1.6.3", "2026-10-02", "Diagnostics: an idle fleet controller with no directive is shown as waiting for a mission, not as a problem."),
    ("1.6.2", "2026-10-02", "Back to the belt leaves a controller alone while its drones are mining (partial exhaustion, e.g. "
                            "exhausted:['silicates','structural'] while mining the rest). A detach that fails with 'not attached to "
                            "this carrier' counts as done (carriers release cargo on arrival)."),
    ("1.6.1", "2026-10-02", "Fleets: whether a device fits in a hold comes from its own features/commands (stow), not a type list; "
                            "live data confirms drones and controllers stow, transport drones/haulers don't."),
    ("1.6.0", "2026-10-02", "Fleets: cargo vessels (50 hold + 3 attach) and other vessels with a hold count as carriers. The carrying "
                            "budget shows hold slots and attach points separately; stowable riders go in a hold first, transport "
                            "drones/haulers need attach points. Boarding stows or attaches accordingly; unloading deploys or detaches."),
    ("1.5.0", "2026-10-02", "Back to the belt: mining controllers left 'exhausted' at used-up salvage (or stale-exhausted / paused at a "
                            "belt that has re-opened) bring their drones back, re-adopt and relaunch. AMI schedules no longer write off "
                            "a stale 'exhausted'. Salvage recall defaults on. Snapshot reads every belt in mining systems and the "
                            "last 3 h of events. Clean shutdown within Docker's stop timeout."),
    ("1.4.1", "2026-10-02", "Fix: snapshot failed on devices with no location (stowed: location null). A diagnosis error no longer "
                            "loses the capture; failures name the file and line."),
    ("1.4.0", "2026-10-02", "Version + server-run history: log entries and jobs are stamped with version/run; restarts are logged "
                            "with what changed; snapshots carry the history so old information can be told apart."),
    ("1.3.3", "2026-10-02", "Diagnostics page: live read-only snapshot (download as JSON) and a per-drone mining diagnosis."),
    ("1.3.2", "2026-10-02", "Salvage read from its body's resource_sites (resources_remaining_pct); refresh finds belts without a "
                            "stored scan; salvage no longer listed by its body counts as used up."),
    ("1.3.1", "2026-10-02", "Arrivals: idle unmanaged drones join their system's controller; maintenance drones get the patrol "
                            "directive at home (no activate); bound (to:) devices skipped by in-system rules; leaving controllers "
                            "release drones and clear their directive."),
    ("1.3.0", "2026-10-02", "Fleet builder: line-list loadout editor (auto-save, qty 0 removes), attach points available vs needed."),
    ("1.2.0", "2026-10-01", "Contracts tracker + fulfil, re-open sites, mobile fleets (mining/explore/trade), ferry fixes, "
                            "partial device-list guard while the replicant travels."),
    ("1.1.0", "2026-09-30", "AMI schedules, print queue panel, tree by type, loadouts with phases/spares/prints/deliveries, "
                            "salvage when depleted, system resources on the map."),
    ("1.0.0", "2026-09-29", "Web client: dashboard, devices, systems, events stream, since-last-login digest, automations engine."),
]

HERE = Path(__file__).parent


def _fingerprint() -> str:
    h = hashlib.sha256()
    for p in sorted(HERE.rglob("*")):
        if p.is_file() and p.suffix in (".py", ".html", ".js", ".css") and "__pycache__" not in p.parts:
            h.update(p.relative_to(HERE).as_posix().encode())
            h.update(p.read_bytes())
    return h.hexdigest()[:10]


FINGERPRINT = _fingerprint()
BUILD = os.environ.get("APP_BUILD") or os.environ.get("GIT_COMMIT") or ""
RUN_ID = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
MAX_RUNS = 50


def label() -> str:
    return f"v{VERSION}" + (f" ({BUILD[:10]})" if BUILD else "") + f" · {FINGERPRINT}"


def _vt(v: str) -> tuple:
    try:
        return tuple(int(x) for x in v.split("."))
    except ValueError:
        return (0,)


def changes_since(old: str | None) -> list[tuple[str, str, str]]:
    """CHANGES entries newer than `old` (all of them if old is unknown)."""
    if not old:
        return []
    return [c for c in CHANGES if _vt(c[0]) > _vt(old)]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def register_start(db) -> tuple[dict, dict | None]:
    """Record this run; returns (this run, previous run)."""
    runs = await db.kv_get("server_runs", []) or []
    prev = runs[-1] if runs else None
    me = {"run": RUN_ID, "version": VERSION, "fingerprint": FINGERPRINT, "build": BUILD,
          "started_at": _now(), "last_seen": _now(), "stopped_at": None}
    runs.append(me)
    await db.kv_set("server_runs", runs[-MAX_RUNS:])
    return me, prev


async def heartbeat(db) -> None:
    runs = await db.kv_get("server_runs", []) or []
    for r in reversed(runs):
        if r.get("run") == RUN_ID:
            r["last_seen"] = _now()
            break
    await db.kv_set("server_runs", runs)


async def register_stop(db) -> None:
    runs = await db.kv_get("server_runs", []) or []
    for r in reversed(runs):
        if r.get("run") == RUN_ID:
            r["stopped_at"] = r["last_seen"] = _now()
            break
    await db.kv_set("server_runs", runs)


def start_notes(me: dict, prev: dict | None) -> list[str]:
    """The log lines a start writes."""
    lines = [f"server started — {label()}, run {me['run']}"]
    if not prev:
        lines.append("no earlier run on record (history starts here)")
        return lines
    how = (f"stopped cleanly at {prev['stopped_at']}" if prev.get("stopped_at")
           else f"did not stop cleanly — last seen {prev.get('last_seen')} (killed, crashed or hard redeploy)")
    lines.append(f"previous run {prev.get('run')} (v{prev.get('version')} · {prev.get('fingerprint')}) started {prev.get('started_at')}, {how}")
    if prev.get("version") != VERSION:
        lines.append(f"upgraded v{prev.get('version')} → v{VERSION}")
        for v, d, text in changes_since(prev.get("version")):
            lines.append(f"  v{v} ({d}): {text}")
    elif prev.get("fingerprint") != FINGERPRINT:
        lines.append(f"same version, but the code changed ({prev.get('fingerprint')} → {FINGERPRINT}) — VERSION wasn't bumped")
    return lines


def age_of(entry: dict, runs: list[dict]) -> str:
    """'current' for this run; 'earlier run' for this version's earlier runs; 'older version' otherwise;
    'unversioned' for entries written before versioning existed."""
    if not entry.get("run"):
        return "unversioned"
    if entry.get("run") == RUN_ID:
        return "current"
    return "earlier run" if entry.get("v") == VERSION else "older version"
