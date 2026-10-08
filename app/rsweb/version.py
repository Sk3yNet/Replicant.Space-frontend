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

VERSION = "1.45.3"

# newest first: (version, date, summary). Entries before 1.4.0 were reconstructed when versioning was added,
# so their dates are approximate and they group several drops each.
CHANGES: list[tuple[str, str, str]] = [
    ("1.45.3", "2026-10-08", "Diagnostics snapshot: includes the last 60 commands sent (by you or the automations) and "
     "what the game answered, so a 'did that work?' can be checked from the snapshot."),
    ("1.45.2", "2026-10-08", "Supply range for fleet loadouts (Fleets › settings, default 15 ly; 0 = any): a fleet's "
     "missing devices are only printed on autofactories — and only taken from spares — within that distance of its "
     "system. Out of range, the loadout line says 'no autofactory within N ly' instead of printing far away (a fleet "
     "in SOL was being supplied from FALQUORYX)."),
    ("1.45.1", "2026-10-08", "Renamed fleets: taking a device out of the fleet while the game still has its old fleet: "
     "tag removes that tag too (otherwise it rejoined the fleet at the next sync)."),
    ("1.45.0", "2026-10-08", "Trail: 'follow to the end' — the replicant that scanned flies to the system the latest "
     "departure points at, finds and reads the target's beacons there, and goes on system by system (the next "
     "candidate if a system has no record of them) until one shows the target arriving and not leaving; then a "
     "notification the badge counts. Progress, a log and Stop on the page. System pages: a 'surveyed' checkbox beside "
     "each planet and belt (auto-survey skips surveyed bodies); once all are surveyed — or a survey controller finished "
     "the system — the boxes give way to a '✓ system surveyed' indicator. Renaming a fleet now renames its id and its "
     "fleet: tag: other fleets' materials, print orders and jobs follow at once, devices show the new tag straight away, "
     "and the engine retags them in the game (retrying every 10 minutes until all carry it)."),
    ("1.44.0", "2026-10-08", "Path forward for stationed mining fleets (Fleets page). A heading (outward from Sol through "
     "the home, toward a star, or x, y, z) fixed when set, with a cone either side. 'Observatory prospects along the "
     "heading': the fleet's galactic observatory (unfurled first if folded) prospects in that direction when fewer than "
     "3 known systems lie ahead, at most every 4 h. 'Move on when the home runs dry': after 2 h dry (no belt and no "
     "salvage left, every belt 'consider moving', or another player's ward), the fleet picks the best-ranked system "
     "ahead from the mining prospects — never one that's unscanned, outside your relay coverage (no relay within 7.5 ly, "
     "no hub within 15 ly), warded, another fleet's home, or another mining fleet's next home or mission target — "
     "announces it and makes it its home after 30 min unless you cancel (or 'go now'); the loadout pass then moves the "
     "fleet. Auto-scout explore fleets visit prospected stars ahead of a mining fleet's heading first."),
    ("1.43.4", "2026-10-08", "Fleets page faster. The mining-prospect ranking read every scanned system's resources from "
     "the events table once per mining fleet's home on every load; it now does that once and keeps it for a minute (or "
     "until the catalogue, devices, fleets or scans change), and those per-system reads use two new indexes (built once "
     "at the first start, which may take a few seconds on a big database) — about 5–10× faster each. Fleets sharing a "
     "home share one destination list, and the destination and unscanned-star lists no longer build a record for "
     "every star in the catalogue."),
    ("1.43.3", "2026-10-08", "Trail: a travel time beside each candidate system (and the likely next stop) for the "
     "replicant that scanned — the game's own estimate, loaded as the rows come into view and kept for half an hour; "
     "hover for the distance and the arrival time."),
    ("1.43.2", "2026-10-08", "Mining controllers no longer overheat on a dry belt. Seen live at LORSELAN: eight drones mined "
     "out five small sites in eight minutes, a ten-minute-old belt read still listed them, and the controller was "
     "relaunched onto nothing — it then logged ami_overheat every 20 s and lost about 9 % capacity an hour. Open-site "
     "counts now leave out sites mined out since the belt was read, and a controller exhausted at a belt with no open "
     "sites rests (its directive is cleared, drones stay put; option 'Rest a controller whose belt has no open sites' "
     "under Salvage when mining sites run out). AMI schedules leave a resting controller alone; once the survey drones "
     "open a site, back to the belt relaunches it with the directive and settings it had."),
    ("1.43.1", "2026-10-08", "Code review fixes. Safety: a command that timed out is no longer resent (it may have "
     "happened); the account-wipe blocklist can't be dodged with ./.. in a path; several places that put game text or "
     "URL values into HTML without escaping are fixed (a print-queue list, the cargo buttons, hx-vals with player "
     "channel names, error messages, the wallpaper caption). Robustness: an engine stage that fails no longer stops the "
     "stages after it (said once in the bell); an event with odd data no longer skips its timers and notifications; "
     "Backfill now runs the automations too; a device list that really shrank is accepted after three syncs; fleet "
     "edits wait for the engine instead of racing it; old actions and read notifications are pruned after 30 days. "
     "Fixes: Reset & reform no longer pulls devices out of a running mission; dequeue_print counts from 0 on the "
     "command form; a stalled or stopped fleet isn't sent recruiting; a paused trade run keeps its deal and two auto "
     "fleets can't spend the same stock; Trail doesn't burst notifications for a new beacon's backlog; a failed "
     "decommission is retried; belt_search finishing no longer marks the whole system surveyed; multi-leg trips move "
     "along their legs on the map; KEL no longer picks up KELMORNEA's sites; console presets work again."),
    ("1.43.0", "2026-10-08", "Map › Wards & hubs: your wards against the 25 cap (activate / deactivate; activating past the "
     "cap or in a system with your hub is refused), and every miner a ward evicts is logged and noted in the bell. Hubs: "
     "the 7-day shield, capacity as last reported less 10 %/day after it, last maintenance and what it used against the "
     "stockpile in the hub's system; a warning when the shield ends within a day, capacity drops under 50 % or the "
     "system can't cover one maintenance. Observatory prospects: the stars they find are highlighted on the Galaxy map "
     "until something scans them, and explore fleets get 'auto-scout prospects' (when free, visit the nearest unscanned "
     "ones, up to N a run). Replicant page: edit the public profile (name, pronouns, description, plan, project) and see "
     "its reputation; Economy › Reputation shows the account's standing and the species you know."),
    ("1.42.1", "2026-10-08", "Trail: the page keeps what was last entered (the replicant that scanned, the one stars were "
     "loaded around, the last beacon code and system). Each departure's candidate systems get a travel button that sends "
     "the replicant that scanned for the beacons (and its vessel) there."),
    ("1.42.0", "2026-10-08", "Map › Trail: follow a replicant (Bill) through the audit logs of their public FTL beacons. "
     "Find them in the directory, find their beacons in a replicant's system (or add a code), read the logs; each "
     "departure's direction vector is matched against the stars the map knows (smallest angle, nearer first, relay range "
     "marked) to give the next stop, and an arrival with nothing after it says where they are. 'Load the stars around' a "
     "replicant adds the nearby stars to match against. The beacons are re-read every 15 minutes and each new move raises "
     "a notification the badge counts."),
    ("1.41.0", "2026-10-07", "Trade runs: a run only waits at the site once the whole price is there; short with nothing on "
     "its way, it goes back to gathering (cargo still aboard at the site is deposited again), and materials on another "
     "vessel heading there are waited for. The replicant riding with the trade fleet fulfils (contracts get it in the "
     "request template). Trade fleets get 'auto-fulfil contracts' and 'auto-fulfil trades': when free, the fleet starts "
     "the nearest deal your stockpiles can pay for (contracts first, not in another player's warded system, a trade at "
     "most once a day)."),
    ("1.40.0", "2026-10-07", "Galaxy map loads progressively: the stars first (a small answer with only the fields the map "
     "uses), then drones, mining, ships in transit, fleets and supply lines. Mentions: a message naming Sk3y-1, Sk3y-4 or "
     "just Sk3y counts (base names without the -N), and mentions count in the bell's badge alongside errors. BobNet's "
     "repeat guard is per channel (the same text to another channel still goes)."),
    ("1.39.0", "2026-10-07", "BobNet: the same text to the same channel within 10 minutes isn't sent again (Messages page "
     "and a relay's message command); Send is disabled while sending and the box clears once it's sent. Messages that "
     "name one of your replicants (whole word, any case; your own posts aside) raise a warning notification, are listed "
     "first under 'Mentioning you', highlighted, and BobNet has a 'mentions only' filter."),
    ("1.38.0", "2026-10-07", "Decommission at an autofactory (device page): pick an autofactory (nearest first) and the "
     "device leaves its fleet, is carried there by the loadout pass (compacted first if large, a ward stops warding before "
     "it moves) and is decommissioned on arrival, so the autofactory learns its blueprint. Cancel keeps it where it is."),
    ("1.37.0", "2026-10-07", "Notifications are errors, warnings or info, like the Automations log, with toggles and counts "
     "on the Notifications page; the bell's badge counts unread errors only. Site and salvage depletions no longer raise "
     "notifications (they're expected; the event feed and digest still have them)."),
    ("1.36.2", "2026-10-07", "Devices tree: devices aboard a carrier count in the carrier's system (13 stowed devices showed "
     "as an empty '?' system), the device's own stowed-in / attached-to is used, and a device with no location says why. "
     "Contracts: another player's ward or hub has a species interaction lock, so contract missions there are refused and "
     "the contracts rule skips them (trades with traders still go)."),
    ("1.36.1", "2026-10-07", "Galaxy: stars from every stored observatory prospect are added to the map (finds from before "
     "1.36 too), the catalogue is followed across pages if the game ever pages it, and the map and 'refresh catalogue' "
     "say where the stars came from (the game's catalogue lists only the starter region's 12). Wards: a system where one "
     "of your drones is mining doesn't count as warded against you (the catalogue flags every starter system has_ward)."),
    ("1.36.0", "2026-10-07", "Galactic observatory: the prospect command offers an aim — outward (default), toward Sol, "
     "sideways, toward a star (worked out from the star catalogue), or a custom vector — instead of typing numbers. Stars "
     "a prospect finds are added to the map and the catalogue is re-read when it reports back."),
    ("1.35.1", "2026-10-07", "Another player's system hub keeps us out like a ward: a system flagged has_hub with no hub of "
     "ours counts as warded (missions, stationed fleets' miners, mining rules, prospects). Trades may still go there."),
    ("1.35.0", "2026-10-07", "Mining prospects (Systems › Mining prospects): every scanned system scored for a mining fleet "
     "— belt richness and density, known sites and salvage, belt viability, distance and relay cover — with what you're "
     "short of or a waiting print needs tipping it slightly; mining-mission targets are offered best prospect first. "
     "Another player's system ward: no mission can target that system (trades excepted) and one that finds its target "
     "warded stalls before unloading; a stationed fleet whose home is warded gets no mining controllers or drones sent, "
     "and the mining rules leave warded systems alone."),
    ("1.34.0", "2026-10-07", "System page: 'Your devices here' — every device of yours in the system (and aboard carriers "
     "there) with where it is, status, capacity, fleet and controller, plus a per-type summary. Galaxy: stars are crisp "
     "points with a small halo instead of soft glows. Every page's scripts and styles now load with the version in the "
     "URL, so a redeploy is never hidden by a cached copy (the Galaxy page could keep showing the old map)."),
    ("1.33.3", "2026-10-07", "Mining missions on salvage: when one salvage is used up, the fleet moves on to the next one "
     "in the system it hasn't used (back through the work phase) and only ends its work when none is left."),
    ("1.33.2", "2026-10-07", "Mining missions: the watch phase also ends when the salvage is used up (gather_salvage "
     "'depleted:complete' in a system without a belt) and, for a fleet without a controller, when no drone has mined for "
     "the grace period — it used to wait for an 'exhausted' belt that never came."),
    ("1.33.1", "2026-10-07", "Overheating mining controllers: the game warns that a controller running drones in many "
     "places multi-tasks and overheats (ami_overheat). A controller at a belt whose drones sit idle elsewhere (not on "
     "salvage) now gets them flown back and re-adopted; the diagnostics snapshot names controllers whose drones are in "
     "other places."),
    ("1.33.0", "2026-10-07", "Automations log: errors / warnings / info toggles with counts (remembered in this browser), "
     "and the whole log (last 300 entries) instead of the last 60. Errors are entries where something stopped or failed; "
     "other alerts (skipped steps, warnings) are warnings."),
    ("1.32.0", "2026-10-07", "Mining controllers parked away from a belt (e.g. left at a Lagrange point after a delivery, "
     "'gated:cold_repair', logging ami_overheat) go to their system's belt, taking their drones and the system's idle "
     "unassigned mining drones, which they adopt, then re-set the directive and launch. In a scanned system with no belt, "
     "miners go to salvage; with no salvage either, an alert once a day. Snapshots include the controllers' game logs and "
     "the diagnosis names controllers logging ami_overheat or gated by the game."),
    ("1.31.0", "2026-10-07", "Galaxy and wallpaper: what each system is mining right now, as coloured four-point sparkles "
     "orbiting its star (one per drone mining, one orbit per resource; the legend and the wallpaper's stockpile rows give "
     "the colours). The markers around stars (your devices, scanned, hub, replicant, fleet) are thin rings now instead of "
     "stacked glows. A 'mining' checkbox / production=0 hides the sparkles."),
    ("1.30.3", "2026-10-07", "Large devices: the app remembers a device is folded when the game says so (device.compacted, a "
     "compacted print, a compact refused as already compacted) even when its status doesn't, so the loadout pass sends "
     "the carrier instead of ordering compactions again and again; unfurling clears it. Fleet membership: adding a "
     "device that's already in the fleet no longer sends its tag in both add and remove (refused by the game)."),
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
