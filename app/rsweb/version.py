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

VERSION = "1.166.2"

# newest first: (version, date, summary). Entries before 1.4.0 were reconstructed when versioning was added,
# so their dates are approximate and they group several drops each.
CHANGES: list[tuple[str, str, str]] = [
    ("1.166.2", "2026-10-20", "Logistics: an approved shop trade in a system warded by someone else (SOL) is a request "
     "again; the ward lock only keeps contracts out, as for missions."),
    ("1.166.1", "2026-10-20", "While an approved shop trade waits on logistics, the loadout pass (device moves, relay "
     "runs) leaves the logistics vessels with a replicant aboard free, so one can fulfill the trade before it expires."),
    ("1.166.0", "2026-10-20", "Every vessel in a logistics fleet is available to automation, replicant vessels included: "
     "the loadout setting 'Allow replicant vessels as carriers' and the civ-beacon rule's 'use replicant vessels' option "
     "are gone. Another fleet's replicant vessel still isn't borrowed inside the network."),
    ("1.165.1", "2026-10-20", "Device page › Carrier: a device too far to cruise over to the carrier is picked up by "
     "the carrier (nearest stop first) instead of being sent on a long cruise that the engine then refused."),
    ("1.165.0", "2026-10-20", "Logistics: an idle carrier drops off leftover cargo and devices unrelated to its work "
     "(not logistics devices, its replicant or matrix, or devices bound somewhere) at the nearest receiving fleet or hub."),
    ("1.164.1", "2026-10-20", "Galaxy map: a label that would cover another fades out where it is instead of jumping "
     "below it, and fades back in when the spot is clear; crowded star names fade rather than pop."),
    ("1.164.0", "2026-10-20", "Relay rollout: no fixed number of orders at a time; as many stay open as logistics "
     "vessels with a replicant have free hold slots. Loadouts: a carrier fills its free hold with relays bound for "
     "several systems at the same pick-up point and drops them system by system, nearest first."),
    ("1.163.0", "2026-10-19", "No automatic assignment outside the network: a fleet's next home and an explorer's or "
     "scout's next system must be commandable (an active relay network, one with a replicant in it, or a replicant "
     "riding along), not just have a relay. Salvage counts like a belt when ranking systems."),
    ("1.162.1", "2026-10-19", "Fixes from a snapshot: a loadout carrier's room counts what the game lists aboard "
     "(haulers already attached made attaches fail with full capacity); no belt search for 90 minutes after the game "
     "refuses one with Maximum active sites; a fleet doesn't relocate (or fly solo) to a home outside an active relay "
     "network without a replicant aboard: it stays pending, and a relocation under way stalls before traveling."),
    ("1.162.0", "2026-10-19", "Map › Relays: Relay rollout puts a relay in every system around seed systems you name, "
     "nearest first and chained outward, a few orders at a time; failures are skipped until retried."),
    ("1.161.0", "2026-10-19", "Galaxy map: markers get their own shapes (replicant diamond, devices square, hub node, "
     "new-find starburst, scanned dotted ring, prospected brackets) instead of nested rings; markers, mining sparkles "
     "and names shrink and fade with distance from the view's center; crowded star names keep the most important one."),
    ("1.160.1", "2026-10-19", "BobNet: the live feed shows the system each poster is in, and Recent history includes "
     "NPC posts."),
    ("1.160.0", "2026-10-19", "Season changes: a new season number in /overview pauses automations until you resume them "
     "(banner, Dashboard › Story, or pause by hand); a copy of devices, fleets, loadouts, orders and star positions is "
     "kept every 6 hours and frozen while paused."),
    ("1.159.0", "2026-10-19", "Dashboard: a Story card from the game's new GET /overview (season, storylines, events "
     "with their location, opportunities, NPCs), read every 30 minutes."),
    ("1.158.1", "2026-10-19", "A component the game refuses to print (climate processors need a terraform monitor, "
     "whose blueprint is locked) no longer blocks the item's own print: it is logged and the item queued."),
    ("1.158.0", "2026-10-18", "A print that needs components uses the ones already at the factory and pins spares "
     "before printing new ones; Contracts: Restart prints for prints that never came out."),
    ("1.157.0", "2026-10-18", "Long automated trips go via one of your hubs when the game's previews say that is at "
     "least 10 min and 5 % faster (arriving at your hub is 25 % faster); a refused waypoint goes direct."),
    ("1.156.0", "2026-10-18", "Systems page: a distances from picker (as on Mining prospects), a Distance column, and "
     "the list sorted nearest first."),
    ("1.155.0", "2026-10-18", "Galaxy map: the layer checkboxes, the legend and the info panel fold away (remembered in this "
     "browser); selecting a star or measuring opens the info panel again."),
    ("1.154.0", "2026-10-18", "Components (and simulation cores) are printed at an autofactory where they're needed first, "
     "then in that system, before the least busy one elsewhere."),
    ("1.153.0", "2026-10-18", "Comms coverage corrected: a relay's range only links it to other relays, and it delivers "
     "commands to its own system alone. A system is commandable only with a relay or hub of an active network in it, or "
     "a replicant. Logistics, rescues, scouting and auto-relocation follow; the relay map's coverage too."),
    ("1.152.0", "2026-10-18", "Comms coverage: a system where your devices report out of comms range (and none in range) "
     "isn't counted as commandable, whatever the relay distances say, so logistics sends a replicant along or doesn't go."),
    ("1.151.0", "2026-10-18", "Component prints: every print the app queues puts its component devices ahead of it on the "
     "same factory, tagged like the item; a factory waiting for components gets spares sent or them printed at another "
     "autofactory. Blueprints list component devices and an all-in material cost."),
    ("1.150.0", "2026-10-18", "Defense: a propulsor still pushing is deactivated before it's recalled (it won't travel while active)."),
    ("1.149.0", "2026-10-18", "Fleets: a recall's wait for a drone ends as soon as the drone is aboard a carrier at the "
     "meeting point (a survey controller with recall on stows them itself; each wait used to run out its hour). Lockers: "
     "a Deliver button sends a locker's price (spares pinned there, the rest printed), and the System page lists its lockers."),
    ("1.148.0", "2026-10-18", "Systems: a scan's system objects (megastructures at STAR-OBJ-n, like TWAFFY's Derelict Ring) "
     "are listed as travel destinations and on the System page."),
    ("1.147.0", "2026-10-18", "Logistics: pick-ups any job still has to make (fleet missions and relocations too), and "
     "loads made since the last stockpile poll, are reserved; a replicant a job is about to move no longer counts as "
     "covering its system, so such deliveries get an escort. Asteroid alerts no longer repeat the impact time; a "
     "stalled prospecting tour no longer reports the move that brought it there."),
    ("1.146.0", "2026-10-18", "Loadouts: deliveries use logistics-fleet carriers (or spare carriers in no fleet); "
     "another fleet's carrier only picks up its own fleet's members and is never borrowed."),
    ("1.145.0", "2026-10-18", "Loadouts: a carrier borrowed away from its fleet's home goes home to its fleet after "
     "the delivery, not back to where it was borrowed (Miner 0.5's cargo vessel flew back to FRANZAN)."),
    ("1.144.0", "2026-10-18", "Device list: filter by owning replicant (by name), e.g. to reassign the devices of a "
     "replicant gone into a simulation with the bulk change_owner command."),
    ("1.143.0", "2026-10-18", "Terraforming training: a run's console shows and graphs only that run's reports (the "
     "training world keeps its name from run to run), and a run's end clears every report from the virtual galaxy. "
     "Real terraforming is unchanged."),
    ("1.142.0", "2026-10-18", "Terraforming console: the device cards keep a fixed order by what they primarily do "
     "(Control, then temperature, pressure, oxygen, toxicity, hydrosphere, biosphere) and never move; out-of-range "
     "values only mark the cards that fix them."),
    ("1.141.0", "2026-10-18", "Terraforming console: sliders no longer snap back. A world card is not redrawn while "
     "one of its controls is held (touchscreens too); a redraw arriving meanwhile is fetched again on release. A slider's "
     "number follows it, and a setting the game has not confirmed yet is dashed, like a switch."),
    ("1.140.0", "2026-10-17", "Terraforming console: the monitor and the regulator share one Control card, first; the "
     "device groups follow in order of what they fix (main effect on an out-of-range value, then side effects, then the "
     "rest), each marked with what it fixes."),
    ("1.139.0", "2026-10-17", "Simulations: queuing asks the interface at once whether it offers the scenario and at "
     "what entry cost (refused with what it does offer), then preps right away: cores counted, the replicant sent. "
     "A free run shows Entry cost: free."),
    ("1.138.0", "2026-10-17", "Logistics reaches every logistics device, however far: the supply range no longer "
     "limits it. Nearer devices (by relay hop) still go first, and a carrier to ride is picked nearest first too."),
    ("1.137.0", "2026-10-17", "Terraforming console: switches and settings no longer flip back. What you commanded "
     "shows (dashed while pending) until the game reports it, up to 2 min; a world's controls and refreshes no longer "
     "overlap"),
    ("1.136.0", "2026-10-17", "Terraforming console: each world refreshes itself every 15 s, with lights, a gauge and "
     "an hour-long graph per attribute (target range shaded), switches, strength sliders and direction pickers that "
     "answer at once. Fix: direction and strength now show as set (read from the device's settings), not reset to increase"),
    ("1.135.0", "2026-10-17", "Terraforming judges worlds for a target species: ranges from GET /species environment "
     "(Humans by default, selectable and remembered), including biosphere and tectonic; readiness, heading and the "
     "regulator warning follow them"),
    ("1.134.0", "2026-10-17", "Terraforming: the regulator doesn't lock in (it holds temperature, pressure and "
     "hydrosphere and can be switched off): wording fixed, and the page shows since when it has been regulating"),
    ("1.133.0", "2026-10-17", "Terraforming: each world says whether it's ready to lock in or which values are out of "
     "the habitable range, and the regulator's confirmation says so (the training didn't end with values out of range)"),
    ("1.132.0", "2026-10-17", "Terraforming: a Heading column (in range in N ticks, leaves the range, moving away; "
     "30 s ticks), and a running monitor's own report (terraform_status in the device list) shows when no event has"),
    ("1.131.0", "2026-10-17", "Terraforming training controls: the Terraforming page on a run (?sim=) with its devices, "
     "the monitor's values against the habitable ranges, all on / all off per kind, and a confirmation before the "
     "regulator locks the world in. The engine no longer plays a build order in that scenario. TODO.md started"),
    ("1.130.0", "2026-10-17", "Build orders mine carbon: the built-in orders now mine in a mining drone's cost ratio "
     "(mine: structural 4, conductive 2, carbon 1, silicates 1; stored copies updated). mine: takes weights and rejects "
     "unknown resources; saving refuses an order whose print needs a resource no earlier step mines"),
    ("1.129.0", "2026-10-17", "Simulations page: Available simulations lists every known replicant interface (also "
     "ones other players' device scans saw) with its scenarios, read every 6 h or on demand; Approve one and its entry "
     "cost and a replicant go there. Free scenarios (terraforming training) need no compute core. Build-order buttons "
     "for the new steps"),
    ("1.128.0", "2026-10-17", "Simulation build orders: new steps survey, controller: <directive>, next system, each "
     "system: / repeat. A built-in Mining Rush: spread out order prints drones, survey drones and a mining controller, "
     "hands each system to its controller and flies on. A yield quota counts every system mined"),
    ("1.127.0", "2026-10-17", "Keep in stock moves to the loadouts: a Stock column beside Want in each fleet's loadout "
     "editor and a second box per type in the Templates grid (followed by the template's fleets); printed at the fleet's "
     "home. The Fleets overview box is gone"),
    ("1.126.0", "2026-10-17", "Print queues: move a waiting print to the front, up or down (the queue is rebuilt, since the game "
     "can't reorder). Printing hubs (fleets taking materials in) can keep device types in stock as spares: printed one at "
     "a time on an idle factory, supplied by logistics last, kept behind other prints. Idle logistics devices gather at "
     "the nearest printing hub, towed by a free carrier when they can't fly"),
    ("1.125.0", "2026-10-17", "Simulations: when one of a run's jobs finishes, its next step starts at once instead of on the "
     "next engine tick (a minute of real time is much more of the run's faster clock). Galaxy and System maps draw stars "
     "by size: the scan's radius when known, else the typical radius of the spectral class. In a simulation's galaxy map "
     "the rings shrink to fit the virtual stars' tighter spacing"),
    ("1.124.0", "2026-10-17", "Galaxy map: a prospecting observatory shows only its wave (the cone is gone)"),
    ("1.123.0", "2026-10-16", "Galaxy map: a prospecting observatory's cone is gray, and a wave runs out from its system to "
               "the cone's reach, like the survey wave"),
    ("1.122.0", "2026-10-16", "Civilization beacons: the automatic pass never has a replicant print one on its vessel any more "
               "(a replicant sitting at the event body still did, since its print lands in the vessel's queue); the "
               "'Print on a replicant's vessel' option is gone. Place beacon on the Traffic page can still do it"),
    ("1.121.0", "2026-10-16", "Device page: stowed in, attached to, controller and the other device-code fields link to "
               "that device, and a carrier's stowed and attached devices are links too"),
    ("1.120.0", "2026-10-16", "Fleets: materials setting is receiving or supplying the nearest receiver (no need to pick a "
               "fleet); the overview shows which receiver that is; missions that ferry deliver to the receiver nearest "
               "their belt"),
    ("1.119.0", "2026-10-16", "Fleets: assemble first stops the fleet's AMI controllers (clears directives, withdraws launched "
               "ones, releases their drones) so drones sent to board aren't pulled back to work or into new scans; a move "
               "refused while a drone scans waits for the scan and tries once more"),
    ("1.118.0", "2026-10-16", "Prospector fleets: before each move the carrier collects every member it doesn't carry yet "
               "(the matrix containers stayed at home), then takes the observatory on; members it can't take are named "
               "in the tour note"),
    ("1.117.0", "2026-10-16", "From the OpenAPI spec: Leaderboards and Blog pages (write, edit, delete your posts); game "
               "API health on the dashboard; system pages show location events, the game's stockpiles and nearby stars "
               "(added to the map); replicant event log; device permissions with grant / revoke; console presets"),
    ("1.116.0", "2026-10-16", "Game v3.5.2: devices a print uses up count as used from print.started; a canceled print's "
               "returned devices aren't lost (and its countdown clears); tutorials show their graduation; terraforming "
               "explains a world with no effects (settled by a species); stars show their radius"),
    ("1.115.0", "2026-10-16", "Simulations: a run's device list and galaxy show only the virtual galaxy (the game lists the "
               "replicant's real devices too); the run player no longer counts real devices as the run's"),
    ("1.114.0", "2026-10-16", "Simulations: a run's devices and galaxy now open the normal Devices page and Galaxy map on "
               "that run's own data (?sim=<run>), with a banner; the separate world page is gone"),
    ("1.113.0", "2026-10-16", "Fleets: mining and survey fleets come home for repairs when a member is at or below 50 % "
               "(recall, return, unload as usual); at home, members below 85 % are deployed from their vessel so the "
               "maintenance drone repairs them"),
    ("1.112.0", "2026-10-16", "Simulations: Runs in the game replaces the raw view: each run with its replicant and time "
               "left, End early, and a devices & galaxy page per run (devices, stock, a top-down map of the virtual "
               "stars around its replicant)"),
    ("1.111.0", "2026-10-16", "Simulations: build-order steps as buttons (scan system, deploy all, mine until objective; "
               "mine: with resource checkboxes; print N of any blueprint; wait: N of a resource or total; undo)"),
    ("1.110.0", "2026-10-16", "Simulations: programmable build orders. Write named orders on the Simulations page (scan "
               "system, deploy all, mine: resources, print N type, wait: N resource, mine until objective); the next run "
               "follows the one you choose, the run card crosses off each step and the timeline records when"),
    ("1.109.0", "2026-10-16", "Simulations: the virtual galaxy (VIRT… systems) is kept apart from the real one. Its devices, "
               "stockpiles, stars and surveys are stored separately and its events only wake the run's own jobs (no "
               "alerts, timers, rules or survey marks); what an earlier run left among the real data is moved out at "
               "startup. The Simulations page shows it in a Virtual galaxy card"),
    ("1.108.0", "2026-10-16", "Simulations: a run the game reports as yours but the app has no record of is taken over "
               "(checked every 5 minutes); the prep pass no longer saves over a run started meanwhile; the game's view "
               "of the run shows whenever the interface is known"),
    ("1.107.0", "2026-10-15", "Simulations: the datacenter's replicant interface (Bill's F4791EDF at MIRFAKA-OBJ-1) is "
               "known from the start; a device scan from the replicant there confirms or corrects it once per run"),
    ("1.106.0", "2026-10-15", "Dashboard: Achievements panel (yours with XP and when, total XP, and the ones still to "
               "earn with how many players have them)"),
    ("1.105.0", "2026-10-15", "Jobs that move a scanning survey drone wait for its scan to finish first (no more "
               "'Cannot cruise while scanning' stopping an assemble or recall). Attach steps refused as already "
               "attached: done if it rides a carrier of the same fleet; otherwise it is deployed, then attached again"),
    ("1.104.0", "2026-10-15", "Attach steps: a device already attached to that carrier counts as done; attached to "
               "another carrier, it is detached there and attached again (no more stopped prospecting tours)"),
    ("1.103.0", "2026-10-15", "Fleets: the status table and the fleet cards list fleets alphabetically by name"),
    ("1.102.0", "2026-10-15", "Auto-scout: worn members of a fleet back home for repairs are deployed out of their "
               "vessel, since only deployed devices get repaired"),
    ("1.101.0", "2026-10-15", "New from the docs: Terraforming page (Map: each monitored world's status, couplings, bio "
               "seeder checklist, device strength / direction / on-off; dropping rocks: detect Kuiper objects, launch with a "
               "vector charge, steer — rocks you launch aren't diverted by asteroid defense); Simulations: research topics at "
               "the interface, all-player leaderboards, the game's view of a run; Economy › Lockers: equipment lockers. "
               "Fleets routes moved to their own module. CLAUDE.md: game facts and token-saving practice."),
    ("1.100.0", "2026-10-15", "Game messages saying \"Salvage discovered:\" are marked read (any text field, either id / "
               "read field name). Civilization beacons, asteroid-defense propulsors and maintenance drones are never printed "
               "on a vessel (replicant vessels take print orders too): only on an autofactory."),
    ("1.99.0", "2026-10-15", "Galaxy map: a violet hexagon marks megastructures (from system scans, your contributions and "
               "the MIRFAKA datacenter), with its own checkbox. Asteroids are named by their location designation: an "
               "impact warning (which names none, and comes from where your devices are) is filed under the asteroid "
               "tracked in that system, else the system's scan names it; the warned place is evacuated too and shown on "
               "the Defense card. Detections saved without a designation take their location as name."),
    ("1.98.0", "2026-10-15", "Relays: a delivered relay is switched on while its carrier is still there (one left off "
               "out of reach can't be commanded). Galaxy map: a relay out of range but switched on is a network of its own; "
               "a relay switched off gets a red ring unless its system has a working relay or hub. Whether an out-of-range "
               "relay is on comes from the last activate / deactivate, relay.activated, or the last time the device list "
               "showed it relaying or inactive (e.g. while a replicant was there)."),
    ("1.97.0", "2026-10-15", "Asteroids: diverted enough is a setting (impact likelihood, default 1 %), an asteroid with "
               "propulsors on it is read every 5 minutes, and a diverted-enough read recalls them at once; an event is over "
               "once its impact time passes (no more reads, alerts or evacuations; listed under Concluded); the warning's "
               "destination shows in the card title. Evacuation never sends devices to where they already are. A relay "
               "dropped off at an L4/L5 point is switched on. Galaxy map: one range sphere per system, from its working "
               "relay or hub. Snapshots carry the tracked asteroids, their events and the evacuation log."),
    ("1.96.0", "2026-10-15", "Fleets split: the landing page has fleet status (loaded just after) and templates; the slow "
               "parts (fleet cards, next pass, tags check, spare & on the way) moved to Fleet details. Propulsors are "
               "recalled as soon as an asteroid is diverted enough. Prospector fleets tour stars within 30 ly of home, one "
               "prospect per star (already surveyed uses up the star), emptiest direction first, with a stall alert. "
               "Galaxy map: saved marker checkboxes, rings sized by how many are shown, prospected-from marker, my-ward "
               "shield, survey waves, gold hub links (relay pairs sharing a hub hidden). Hubs link by distance only (15 ly) "
               "and are placed at L4/L5."),
    ("1.95.0", "2026-10-15", "Simulations page (Economy): queue a run (compute cores and a replicant to the datacenter's "
               "interface), a ready nag, Start, the bootstrap logic plays it recording its build-order step times, then "
               "everything from inside it is purged and the report kept; the next build order learns from the scores. "
               "Console: presets for every documented query with fill-in blanks. Relays: live links per relay/hub. "
               "Replicant page: transfer / teleport into an empty matrix. Megastructures: progress, Contribute, leaderboard. "
               "v3.5.1: attach_used counted, tracking_site shown. Lost page: explained disappearances with the reason. "
               "Logistics: cross-system print deliveries leave at least a quarter full. Fixes: stopping what isn't running "
               "counts as done; a device already aboard its own fleet's carrier isn't sent to board it; salvage messages "
               "don't raise the Messages badge; relocations prefer carriers where the riders are."),
    ("1.94.0", "2026-10-13", "Travel destination: one field to type any system or location code, suggesting your systems "
               "then the 150 nearest explored ones; the spot list follows. Trail: Bill leaving his shop resets to SOL, "
               "so a departure from his shop's system sends the follow to SOL, and his move posts clear the beacon logs "
               "and restart at SOL. Logistics: an In transit card (every hold with materials: what, where, heading for, "
               "which run, with totals); the dashboard's stockpiles show + N in transit."),
    ("1.93.1", "2026-10-13", "Galaxy map: labels next to arrows (ships in transit, other players' arrivals and departures) "
               "that land on each other are stacked a line apart; all map text hides when zoomed out so far that a "
               "light-year is under 1.5 px on screen."),
    ("1.93.0", "2026-10-13", "Fleet moves: surge-capable members that aren't needed as carriers (e.g. cargo freighters) no "
               "longer hold a relocation up; each flies straight to the new home from wherever it is once its current "
               "task is done. A move already waiting picks its solo flyers on the next pass."),
    ("1.92.0", "2026-10-13", "Checked against the published docs. Game news on the dashboard (changelog and blog every 6 h, "
               "new versions and posts ring the bell). Asteroids: an impact warning starts evacuation even for an "
               "object never seen detected; v3.5.0 diverted/impacted events close objects (a partial diversion keeps "
               "it open); naive ETAs read in the account's time zone. Devices lost to a diversion or sent to another "
               "replicant count as gone; devices used up by prints, megastructures or trade escrow aren't lost. AMI "
               "digests: survey scans are stored and wake jobs on adopted devices. Missed events are backfilled "
               "before the stream reconnects. Busy miners are deactivated before a move. Recalled arrivals don't "
               "complete waits. Per-endpoint 429s (catalog, feedback, webhook) no longer stall every request. "
               "Autofactory cancel uses deactivate; Repair now skips patrolling drones; withdraw keeps the "
               "directive; launch failures are logged; hub shield from the first activation and upkeep stock at the "
               "hub's location; shop listing needs an active relay; the catalog is re-read 5 min after a prospect; "
               "mining.relocated tracked; evicted_miners is a count; feedback up to 2000 characters; command form: "
               "travel via/dry_run, start_mining target, corrected descriptions."),
    ("1.91.0", "2026-10-13", "Speed: the to:-tag pass no longer rebuilds the star catalog lookup per device (it took ~37 s "
               "a tick and froze the server); the star catalog is decoded once per change; per-system resources are "
               "memoized until that system's events, details, scan or the stockpiles change; pages get served between "
               "engine stages. Dashboard: tutorial progress (GET /tutorials). Trail page: the sighting, follow log and "
               "departures update live. Follow-to-the-end text for Bill says it stops at his AMI trade controller."),
    ("1.90.0", "2026-10-13", "Diagnostics: a Performance panel (slowest pages and requests with database, game API, engine "
               "lock and saved-value costs; engine stage timings and lock holds; storage sizes), also in snapshots; a "
               "Server-Timing header on every page and slow requests logged. Large modular devices can be printed "
               "compacted (Blueprints, planner, print queue). The Fleets templates grid keeps its header row and type "
               "column in view while scrolling. Deleting a fleet takes its tag off its devices. Restart from SOL clears "
               "the trail first."),
    ("1.89.1", "2026-10-13", "Prints spread over the autofactories at the same stockpile by default: the Blueprints planner "
               "(only \"keep it on one\" stops it — an unticked box that hadn't been drawn yet put 3 matrices on one of two "
               "autofactories) and Queue a print."),
    ("1.89.0", "2026-10-13", "Trail: any of the followed replicant's devices a scan sees is a sighting (not his beacons), "
               "the newest sighting wins, and finding his AMI trade controller ends the follow and lists his shop there. "
               "Devices paid away in a trade or contract are no longer counted as lost."),
    ("1.88.0", "2026-10-13", "Logistics brings a deal's payout home: right after a contract or trade is fulfilled, the "
               "delivering ships load the materials it pays out and the convoy takes them to the nearest gather point; "
               "devices it pays out are sent there as deliveries once they show up at the site."),
    ("1.87.2", "2026-10-13", "Logistics counts what a trip on its way is going to pick up as gone from that stockpile "
               "(and what an AMI controller was handed), so later passes don't send more ships to a pile that's about to "
               "be empty (four haulers found KELMORNEA-3 bare)."),
    ("1.87.1", "2026-10-13", "Galaxy map on phones: lower resolution and 30 fps, one resize after the keyboard closes "
               "(picking a system could freeze it), a reload if the browser drops the graphics, labels moved only when "
               "the camera moves. Bill's move post clears the old beacon trail (keeps the last 3 hours and his beacons)."),
    ("1.87.0", "2026-10-13", "Hubs share their print reserves so each queue is paid for about as many hours ahead; a "
               "queued print may draw on its own system's reserve (stock no longer stuck on a belt pile next to its "
               "factory), and consolidation does the piles holding what prints lack first. Logistics fleets are never "
               "stationed nor send materials, and members an AMI controller adopted are released. The Blueprints page "
               "filters live by name and feature. Roadmap: GUPPI theming."),
    ("1.86.1", "2026-10-13", "A device riding in a carrier counts as where the carrier is: no more trips to a blank "
               "destination (a beacon stowed in E1029CB0 at SOLARES failed every pass). The carrier already holding it "
               "delivers it without reloading; one riding another vessel is deployed out of its hold or detached by its "
               "carrier first. Only a destination fleet's old stockpiles follow it to a new home."),
    ("1.86.0", "2026-10-13", "Asteroid evacuation (game v3.5.0): devices leave an asteroid due within the hour or on the "
               "impact warning (propulsors at 30 minutes; grounded ones board a carrier), their pins to it are dropped, "
               "and its stockpile is logistics' top priority; system.object_diverted / object_impacted close tracked "
               "objects. Logistics rescues stranded devices (a replicant goes to command them, or a carrier picks them "
               "up), consolidates home systems' piles, draws on piles beyond the network with an escort, and lists piles "
               "with no gather point. Relay ranges come from blueprints. to:<location> tags become to:<system> + at:<location>."),
    ("1.85.1", "2026-10-12", "Logistics: cargo freighters fly themselves (the game won't attach a large device); only "
               "haulers and drones ride a carrier. A ship still holding what a request needs (e.g. from a failed trip) is "
               "used first and delivers without loading again."),
    ("1.85.0", "2026-10-12", "Logistics fleets (Fleets › Logistics) replace trade fleets (each becomes a logistics fleet once "
               "its run ends): materials for queued prints, approved contracts (or a pre-approved civilization's) and "
               "approved trades are delivered on demand by priority, home systems keep their print reserve, stray stockpiles "
               "go to a gather point or follow a moved fleet; convoys share one escort and ride a free carrier; in-system "
               "work goes to a free AMI transport controller. Outside an active relay network nothing goes without a "
               "replicant (a fleet carrier with a matrix aboard counts). Map › Relays: relay networks by color, active or "
               "not, suggested bridges and Add route; galaxy map relay layer (off by default). A new device type's "
               "directives are read from the game and remembered."),
    ("1.84.0", "2026-10-12", "Bill's BobNet move posts ('NOSOXA is in the rear-view mirror') reset the trail: his old shop "
               "location is set aside, the trader directory is re-read, and a follow picks him up where he left. The "
               "Contracts page has a Trades section: every shop you know of, where it was last seen, and its trades."),
    ("1.83.0", "2026-10-12", "The Carrier card is on every carrier: attach carriers (surge plates, platforms, surge carriers, "
               "mobile fleets) list what's attached and what they can attach, with detach / attach; a cargo vessel stows "
               "what fits the hold and attaches the rest. Hold and attach points are counted separately."),
    ("1.82.0", "2026-10-12", "Device commands from either side: stow, deploy, attach and detach list the devices that fit "
               "(a carrier's hold or attachments, or carriers here) and go to whichever device the game expects. Only the "
               "side a device is capable of is shown."),
    ("1.81.0", "2026-10-12", "Waits on a trip follow its ETA: a refused step or an arrival wait keeps going until the ETA "
               "+30 min (one extra hour when no ETA is known), no longer up to 48 h."),
    ("1.80.0", "2026-10-12", "Trips log their expected arrival (UTC, and how long); a step refused while something is still "
               "traveling waits for that ETA instead of retrying every 5 min (up to 48 h on long trips), and the jobs list "
               "shows 'expected' / 'next try'. A carried device isn't 'arrived' while its carrier is still on the way."),
    ("1.79.0", "2026-10-12", "Fleet gathering only fetches spares and stray members within the loadout supply range "
               "(no 500 ly trip for one drone). Launch anyway still boards members standing by the carrier."),
    ("1.78.0", "2026-10-11", "Trades: shops from system scans and every replicant's directory are offered (newest sighting "
               "wins); trades paid in devices get spare ones delivered and deployed at the shop; a failed trade stalls the run "
               "and sets that shop aside until it's seen again, and a run notices when a newer scan has the shop elsewhere. "
               "Trail: Bill is picked up at SOL when there's nothing to follow or the trail goes cold (restart from SOL)."),
    ("1.77.0", "2026-10-11", "Long-cruise checks skip surge-capable devices: no warning on their manual Travel, and "
               "automations aren't held back for them."),
    ("1.76.0", "2026-10-11", "Long cruises: no automation sends a drone or AMI controller on an in-system cruise longer than "
               "the max cruise (Fleets › settings) — the step says why instead; a manual Travel that long asks first "
               "(Send anyway)."),
    ("1.75.0", "2026-10-11", "Survey crews stay aboard when they get home and don't deploy in a system that's already "
               "surveyed. A fleet member riding in another fleet's vessel that's on its way somewhere is left out of "
               "boarding and recall instead of failing them; 'in transit' refusals are retried."),
    ("1.74.0", "2026-10-11", "A job waiting for a device to arrive moves on once the device is already standing at the "
               "destination (its arrival may have come before the wait began)."),
    ("1.73.0", "2026-10-11", "Missions: nobody leaves until every passenger is aboard a carrier (a boarding step runs first; "
               "Launch anyway still overrides). Resuming a stalled recall re-runs the recall, not the wait before it, and an "
               "explore fleet's watch ends when its survey controller has no directive. 'Device is not in a star system' is "
               "retried like other in-transit refusals. Snapshots carry each mission's phase, note and recent log."),
    ("1.72.0", "2026-10-11", "Systems list: a Fleets column (homed here / on a mission here) instead of replicants; add, "
               "remove or cancel your ward, FTL relay and FTL beacon per system (activate one there, else send a spare, else "
               "print one); a civilization mark, and a requested beacon goes on to the civilization's body. Survey crews no "
               "longer drop relays or beacons, and the deploy-beacon rule is gone. Galaxy map: your systems load first, the "
               "rest of the catalog after; long-press measures on touch screens. A fleet with a new home moves once its "
               "loadout is together in any one system, not only the old home."),
    ("1.71.0", "2026-10-11", "Prospects: a stationed fleet's home is scored for review (marked home, sorted among the open "
               "systems, still never a mission target), and a flag marks every system where a fleet of yours is homed."),
    ("1.70.0", "2026-10-11", "Fleet recall: a launched AMI controller is withdrawn first (its drones called back, waiting "
               "for ami.withdrawn) before it travels to board; if it's still not deployed, the job stops saying so."),
    ("1.69.0", "2026-10-11", "Ward prints go to the autofactory with the shortest queue (counting wards already ordered), then the "
               "nearest; compacted or carried factories are skipped. A ward print still queued gives way to a spare ward that "
               "turns up in range, and removing a ward order drops its queued print. A to: tag naming a fleet means that "
               "fleet's home, and every to: tag is cleared on arrival. Snapshots carry the merged device list, ward orders "
               "and ward jobs."),
    ("1.68.0", "2026-10-11", "Prospects is a Map tab, with a Ward column (yours / theirs / your hub / on its way) and add "
     "ward (activate an idle one there, else send a spare, else print one in range; switched on once deployed, 25 cap) / "
     "remove (deactivate). Device list (renamed from Fleet): bulk add tags / remove tags on ticked devices."),
    ("1.67.0", "2026-10-10", "Long trips: waits for an arrival last until the game's ETA (+30 min) and are extended while "
     "the device is still moving (up to 48 h) — seen live: Surveyors' wait timed out mid-surge to the Sol zone and its "
     "deploys failed; 'Cannot … while travelling' refusals are retried every 5 min while still on course. A canceled "
     "trip, an arrival somewhere else, or a carrier that turned back now stops the job (missions stall) instead of "
     "carrying on, so nothing is deployed anywhere but the destination."),
    ("1.66.1", "2026-10-10", "Fleets › Add / remove devices: devices in other fleets are offered too, marked with their "
     "fleet (adding one moves it over), grouped and sorted — fleetless first, then by fleet, system and type."),
    ("1.66.0", "2026-10-10", "Fleets never leave with a partial loadout: a launch with gaps is refused (Launch anyway "
     "overrides), a mission waits at gather (refilling from spares) until everything is aboard, auto-scout and "
     "auto-deals wait, and a new home stays pending — the fleet keeps its current home, where it's filled — until the "
     "loadout is complete (Move anyway overrides)."),
    ("1.65.0", "2026-10-10", "Loadouts: a borrowed carrier only comes from within the supply range of the pick-up (seen "
     "live: five carriers flew ~500 ly empty to move one drone each) — a fleet's own carriers, and a new home across the "
     "gap, still go any distance; spares are only gathered to a depot in range. Fleets: Supplied from — a hub whose "
     "autofactories and carriers supply a fleet at any distance. Bootstrap: Back to a normal fleet."),
    ("1.64.0", "2026-10-10", "Bootstrap: Convert to bootstrap on a fleet with a replicant's vessel (its members become "
     "the bootstrap's own); mining controllers on salvage or resting at a dry belt are left to the salvage rule, and "
     "get their ratios back as soon as they return to an open belt."),
    ("1.63.0", "2026-10-10", "Bootstrap: the hub and every outpost get an AMI mining controller (maintain_ratios, the mix "
     "the bootstrap's missing devices cost, refreshed when it shifts) and an AMI survey controller (belt_search)."),
    ("1.62.0", "2026-10-10", "Devices › Lost: devices out of sight (surging, left out of the device list, out of relay "
     "range with no replicant there, or aboard a carrier that is) with their last known location and heading, until "
     "they're seen again; recent finds listed, write-off to stop tracking."),
    ("1.61.0", "2026-10-10", "Bootstrap fleets (Fleets page): a heaven vessel with three mining drones compounds at home "
     "(drones mine the belt, the vessel mines rares and volatiles, the replicant prints survey and mining drones), surveys "
     "every system within 10 ly, waits for your OK on the hub, moves there, prints an autofactory, then builds three "
     "outposts (your OK on each) that keep the relay chain and ferry materials to the hub; wards when they cost under 6 h "
     "of output. Self-reliant: only its own devices (boot:<id>). Mining prospect scores now weigh yield (scarce 1 to "
     "rich 10) × density × rarity, so rares-rich belts rate high."),
    ("1.60.0", "2026-10-10", "Contracts with several options: a Fulfill with picker per contract (default: whichever is "
     "ready first). The chosen option drives readiness, material deliveries and trade runs, device supply, auto-fulfill "
     "and the Fulfill button."),
    ("1.59.0", "2026-10-09", "System page, Other players here: each player's drone totals by type, a count of their "
     "mining drones on each belt's viability line, and the full device scan (table of everything it found, plus the raw "
     "JSON as the game sent it)."),
    ("1.58.0", "2026-10-09", "Relays: an explore fleet's relays and beacons wait at home as cargo (the placement step no "
     "longer sends them to the L4/L5 point and the wake-up rule no longer switches them on), and no relay is placed or "
     "activated in a system where one of yours already relays. Of a survey crew's relays already working at home, one "
     "stays as the system's relay (leaves the fleet); the others are switched off and boarded on the next mission."),
    ("1.57.1", "2026-10-09", "System page, Other players here: one row per device (owner, type, code, location) so you "
     "can tell which device is which."),
    ("1.57.0", "2026-10-09", "Other players on the maps: their fixed devices (beacons, relays, wards, hubs, factories, "
     "observatories, controllers) from the latest scan of each system (a replicant arriving scans it; System page has a "
     "button), someone else's ward or hub as a shield, and arrows for arrivals and departures your beacons logged in the "
     "last hour with the likely other end of the trip. On the Galaxy map they fade with distance from the view's center. "
     "The followed trail is drawn in gold; Trail has a Reset button. Traffic sets how often beacons are read."),
    ("1.56.0", "2026-10-09", "A stationed mining or explore fleet whose home changes (edited, moved on when dry, go now, or "
     "advancing) moves in one relocation mission whenever its carriers' hold slots and attach points cover every rider; "
     "otherwise the loadout pass carries it piecemeal, and the log says why. In a fleet's travel phase the vessel carrying "
     "a replicant gets its travel command last, so the others are still in control range when theirs goes out."),
    ("1.55.0", "2026-10-09", "Replicant cooperation: Account page sets replicant_cooperation (individual / shared), each "
     "replicant's profile its cohort_permission (private / public); boarding skips the owner hand-over when cooperation "
     "already allows it, and ownership errors carry a hint to these settings. An advancing explore fleet now moves in one "
     "relocation mission (observatory compacted, everyone aboard before anything leaves, travel together, unload), so "
     "nothing is stranded out of relay range. Path forward form shows the saved heading; re-saving keeps its vector."),
    ("1.54.1", "2026-10-09", "The loadout pass (and contract printing, the spare depot, decommissioning) never counts a vessel "
     "as an autofactory: a fleet's replenishment print had been queued on an idle heaven vessel, which never prints from a "
     "queue. Device list: a stowed or attached device shows its carrier's location ('in <carrier>'), and the system "
     "filter finds it."),
    ("1.54.0", "2026-10-09", "Explore fleets with an observatory get the Path forward box and 'advance along the heading': "
     "once the observatory has prospected every direction from home, the farthest star ahead (inside the cone, within the "
     "hop distance, default 30 ly; inside relay coverage unless a replicant rides along) becomes the fleet's home and the "
     "loadout pass carries it there, leaving a relay and a beacon behind when it has them."),
    ("1.53.0", "2026-10-09", "Contracts page: a collapsed list of civilizations with no open contract (from system scans, "
     "and places that asked before), each with the auto-fulfill approval checkbox. American spelling throughout the UI, "
     "logs, comments and docs; the Map tab reads Defense (the game's own names, like travel.cancelled, are unchanged)."),
    ("1.52.0", "2026-10-09", "Contracts: each contract has an 'Approve fulfilling contracts from <species> automatically' "
     "checkbox, off by default. The Work on contracts rule and trade fleets' auto-fulfil only take contracts whose species "
     "(from the body's scan; else the body itself) you approved; the Fulfill button and runs you start still work."),
    ("1.51.1", "2026-10-09", "Fleets page: the observatory note shows only on fleets that have an observatory or whose "
     "loadout wants one. README rewritten as a current per-tab overview; roadmap trimmed to open items."),
    ("1.51.0", "2026-10-09", "System page reorganized into Resources (one row per resource: richness, open sites, % left, "
     "≈ units left, salvage, stockpiled), Belts (per belt: richness, viability line, open sites with % left) and Bodies "
     "(planets, moons, L-points with their salvage, stock and devices) — replacing Resources available, Locations, "
     "Belts, Belt viability and Planets. ≈ units left: open sites' % left × what a site gave before running out "
     "(mining history; belts of the same richness until a belt has its own). Galaxy map: relay/hub range off by default."),
    ("1.50.0", "2026-10-09", "Galaxy map: a faint cone for each observatory prospecting now, from its system along the "
     "direction, darker as the scan progresses (at most ~15 %, never hiding what's behind); reach learned from past finds "
     "(≈25 ly until then); 'prospecting' toggle. The observatory pass no longer sends a second prospect to one already "
     "prospecting, and stands aside while loadouts is moving the observatory to another system (no unfurl/compact "
     "tug-of-war)."),
    ("1.49.0", "2026-10-08", "Auto-scout surveys the nearest unsurveyed systems (scanned, every body but the belts surveyed) "
     "within 100 ly of home, ring by ring, one system after another — only inside relay coverage when no replicant rides "
     "along; a member down to 50 % ends the run (it finishes that system and goes home), and a run starts only with every "
     "member at 85 % or more. Every fleet's observatory unfurls and prospects wherever it's deployed — the heading (else "
     "outward from Sol) then 13 more directions, each until it finds nothing new ('already surveyed' counts) — and compacts "
     "when every direction is tried or its fleet is leaving. enqueue_print on a replicant's vessel sends the replicant's "
     "own print (one at a time); bulk and contract printing skip vessels."),
    ("1.48.3", "2026-10-08", "Placed beacons and relays (deployed, monitoring or relaying where they are) are never spare, never "
     "sent to fill a shortfall or gathered at the spare depot; leftover spare tags on them are removed (seen live: 13 "
     "working beacons the Surveyors dropped carried an old spare tag). A stationed fleet's own placed beacon at home still "
     "counts toward its loadout; misplaced relays are still reported."),
    ("1.48.2", "2026-10-08", "Galaxy map is live: it redraws ships in transit, fleets, supply lines and mining when the live "
     "stream says something departed, arrived or changed (at most every few seconds), and a travel command's answer — "
     "yours or an automation's — goes on the cached device at once, so a new trip shows within seconds instead of after "
     "the next device sync."),
    ("1.48.1", "2026-10-08", "Contracts with alternative options (Famine Assistance: 3 orbital farms, or a nutrient synthesizer "
     "and resources …): a trade run counts the contract as ready at the site when any option is complete there, not only "
     "the one whose resources it set out with; a fulfill the game refuses with 'Event criteria not met' sends the run back "
     "to waiting at the site (up to 3 tries) instead of stalling. The Contracts page says the options are alternatives "
     "and which one devices are delivered for."),
    ("1.48.0", "2026-10-08", "Devices › List: tick devices (or all listed) and send them one command, with its fields. "
     "Before sending, the ticked devices that can't take it are listed and skipped — the command isn't available to "
     "them right now, an automation job is using them (unless included), or a deploy/detach while the carrier is between "
     "systems. Each device's result is shown. set_directive, prospect and message stay per device."),
    ("1.47.2", "2026-10-08", "A surge plate still tagged taxi but run by no controller and not in taxi mode is idle where its "
     "ferry left it: it now goes home to its fleet like any device left behind (seen live: Printing Hub 1's four plates in "
     "ITHVALAI, flagged by Tags & controllers but never moved). Plates the game reports in taxi mode aren't flagged."),
    ("1.47.1", "2026-10-08", "Supply range (Fleets › Settings) defaults to 100 ly instead of 15; a saved 15 (the old "
     "default) becomes 100 once."),
    ("1.47.0", "2026-10-08", "Contracts that ask for devices get them delivered: 'Deliver devices' on the Contracts page (or "
     "a trade fleet's run on the contract) tags fleetless spare or idle devices in supply range for it, and the loadouts "
     "pass flies or carries them to the exact location. Whatever no spare covers is printed only after you press "
     "'Authorize printing' (nearest autofactory in range, tagged for the contract). The trade run waits at the site "
     "until the devices are there; auto-fulfil takes a device contract once it's set delivering; stationed fleets "
     "leave the contract's devices alone; when the contract closes, leftovers become spare."),
    ("1.46.9", "2026-10-08", "Contracts that ask for devices: a trade run carries resources only, so auto-fulfil no longer "
     "picks a contract whose devices aren't already at its location; the Fleets page lists the devices a contract also "
     "needs, and starting a run on one warns that they must be sent there first."),
    ("1.46.8", "2026-10-08", "Blueprints page: the 'Can afford at' printers flow into as many columns as the screen allows "
     "(so each row stays about as tall as the device's description), and the blueprint table takes two thirds of the "
     "width with the plan beside it."),
    ("1.46.7", "2026-10-08", "Survey missions without a survey controller: the survey is only done once the drones have been "
     "idle for 3 minutes and no job (auto-survey drives them body by body) is using them. At DABAH the drone was caught "
     "idle between two moons, the survey was called done and the recall failed ('Cannot cruise while scanning')."),
    ("1.46.6", "2026-10-08", "Map › Stars: the unexplored stars' route buttons (one per replicant) are labeled with the "
     "replicant's name under 'route from', with a tooltip saying it's the game's preview and nothing moves."),
    ("1.46.5", "2026-10-08", "Galaxy map: a left-click no longer draws a line from your replicant to the star (the distance "
     "is still in the panel); lines come only from the right-click measuring tool."),
    ("1.46.4", "2026-10-08", "Missions: fleet members riding in a vessel that isn't one of the fleet's carriers (the "
     "Surveyors' survey controller and drones were stowed in Trader_1's heaven vessel in AEMEROTH) are now collected — "
     "deployed out of that vessel, then boarded by assemble (same system) or the gather tour (another system). A survey "
     "no longer runs with a controller that isn't in the target system: the mission stalls and says where it is."),
    ("1.46.3", "2026-10-08", "Survey missions: the relay / beacon check at launch counts the fleet's own relays and beacons, "
     "not only what's already aboard its carriers — it said '0 aboard' a minute before assemble stowed all of them. And "
     "the 'no relay there and no replicant rides along' warning is skipped when the crew carries a relay for each system."),
    ("1.46.2", "2026-10-08", "Contracts: when the game answers 'Event already completed by this account', the fulfill "
     "step counts as done — a trade run carries on to collect and go home instead of stalling with an error — and the "
     "contract is marked completed so it isn't picked again."),
    ("1.46.1", "2026-10-08", "Galaxy map: measuring is on the right mouse button only — the first right-click on a star sets "
     "the first point, the second sets the other and shows the distance, the third clears both (a right-drag still "
     "pans; the browser menu no longer opens on the map). Left-click just opens the star."),
    ("1.46.0", "2026-10-08", "Print queue panel: the tags of the print in progress and of each queued print can be edited. "
     "The game can't change a queued print, so the app keeps the edit and re-tags the device the moment it's printed; "
     "the loadout order behind it follows at once (moved to another fleet or system, or dropped when no to: tag is "
     "left), so the old fleet stops counting it as on its way."),
    ("1.45.5", "2026-10-08", "A replicant's vessel moves with its fleet: when a stationed fleet's home changes (by hand or "
     "when it picks its next home itself), its vessels hosting a replicant fly there too — devices stowed in them come "
     "along. Only after a home change, so a replicant you send elsewhere afterwards isn't pulled back."),
    ("1.45.4", "2026-10-08", "Loadouts: a replicant's vessel tagged into a fleet counts toward that fleet's loadout (it "
     "wasn't counted, so SOL's fleet got a second heaven_vessel printed); the planner still never spares or swaps it."),
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
     "until the catalog, devices, fleets or scans change), and those per-system reads use two new indexes (built once "
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
     "vessel heading there are waited for. The replicant riding with the trade fleet fulfills (contracts get it in the "
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
     "1.36 too), the catalog is followed across pages if the game ever pages it, and the map and 'refresh catalog' "
     "say where the stars came from (the game's catalog lists only the starter region's 12). Wards: a system where one "
     "of your drones is mining doesn't count as warded against you (the catalog flags every starter system has_ward)."),
    ("1.36.0", "2026-10-07", "Galactic observatory: the prospect command offers an aim — outward (default), toward Sol, "
     "sideways, toward a star (worked out from the star catalog), or a custom vector — instead of typing numbers. Stars "
     "a prospect finds are added to the map and the catalog is re-read when it reports back."),
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
    ("1.31.0", "2026-10-07", "Galaxy and wallpaper: what each system is mining right now, as colored four-point sparkles "
     "orbiting its star (one per drone mining, one orbit per resource; the legend and the wallpaper's stockpile rows give "
     "the colors). The markers around stars (your devices, scanned, hub, replicant, fleet) are thin rings now instead of "
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
     "that gets canceled stops waiting at once (the device turns back to where it started)."),
    ("1.26.4", "2026-10-07", "A manual deploy or detach is refused while the carrier is traveling (slingshot E28DBE58, deployed "
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
     "goes along) and fulfills, then loads the rewards and takes them to the nearest system whose fleet takes materials "
     "in before going home."),
    ("1.24.0", "2026-10-07", "Survey crews drop FTL relays and beacons: in each system without one of yours the carrier deploys a "
     "relay at the L4/L5 point (and activates it) and a beacon; if the survey finds a civilization, the beacon is moved to "
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
    ("1.22.1", "2026-10-06", "Mission targets are checked against known systems (did-you-mean on a typo) and offer every catalog "
                             "and census star. The production planner no longer gives gather orders to a fleet's controller that "
                             "isn't at its station. to:/at: tags that aren't locations are ignored and flagged; the print queue "
                             "refuses a typed destination that isn't a location."),
    ("1.22.0", "2026-10-06", "Drone badges on the system map (M mining, S survey, T transport, R maintenance; count, colored by "
                             "activity, hover for each drone; toggle). The galaxy map's system panel lists drones by kind."),
    ("1.21.1", "2026-10-06", "change_owner sends the new owner as `target` (the game rejected `replicant_code`)."),
    ("1.21.0", "2026-10-06", "BobNet channels on the Messages page: list them from a relay, tick the ones to listen to (or join "
                             "by name) and save to the account; recent messages from the relay. Fleets that aren't stationed now "
                             "stay aboard their carriers when they get home — only cargo is deposited."),
    ("1.20.0", "2026-10-06", "Devices in transit on the system and galaxy maps: an arrow on a dashed route pointing where they're "
                             "going, with progress and time left, updated live. Devices traveling together show as one. On a "
                             "system map, surges in or out sit on the rim toward the other star."),
    ("1.19.0", "2026-10-06", "Stellar census: new rule Stellar census on arrival (on) and Map › Stars with a census button per vessel; "
                             "census stars (beyond the catalog's ~70 ly) are merged into the catalog so the map, routes and "
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
                             "Blueprints planner (Spread over N autofactories), fleet Print missing and defense propulsors; "
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
                             "engine lock, then canceled the mission's job, which took it again) — seen live: no rule, event or "
                             "loadout pass ran for ~43 h. The engine lock is now re-entrant; a watchdog alerts when it is held for "
                             "over 10 min, and snapshots show the engine status and last tick. Fleet deploy: 'Device is already "
                             "deployed' counts as done. Loadouts: when a system has too many of a type, the device already tagged "
                             "home:<system> is kept (FALQUORYX's home autofactory was about to be made spare)."),
    ("1.12.2", "2026-10-03", "Moving a system's devices: when no surge carrier is in the system, the nearest free one elsewhere flies in, "
                             "picks them up and goes back afterwards (seen live: 26 devices in AEMEROTH stuck 'waiting for a carrier'). "
                             "Spare devices are no longer re-adopted by AMI schedules / Restart idle miners (released drones were being "
                             "taken back within minutes). The ferry's controller, freighters, drones and taxi plates are never made spare; "
                             "the beacon at a civilization's body is never made spare. Snapshots include the loadout config."),
    ("1.12.1", "2026-10-03", "Civilization beacons: an existing beacon in the system (e.g. the Kuiper/Oort one) is moved to the civ body "
                             "by a vessel before anything is printed; with no free vessel it waits instead of printing a second one. "
                             "Beacons already at a civilization's body are never taken."),
    ("1.12.0", "2026-10-03", "Redundant beacons (a system that already has a beacon at a civilization's body, or a second beacon in a "
                             "system) are tagged spare — Traffic page 'Mark spare' or the civ beacon rule. Loadouts gather idle spares "
                             "at a spare depot (set it, or automatic: a materials destination system with an autofactory); they stay "
                             "spare there. Carriers pick up devices that can't fly (beacons) by going to them."),
    ("1.11.1", "2026-10-03", "Beacons at civilization event sites: placed as soon as a survey discovers an event (not only on "
                             "completion). New ways to get one there: a vessel picks up a loose beacon tagged civ/spare and carries "
                             "it; otherwise one is printed on the system's autofactory (tagged civ) and fetched on a later pass. "
                             "Vessels hosting your replicant are only used if you allow it."),
    ("1.11.0", "2026-10-03", "Traffic page: each beacon's audit log read every 10 min, visitor alerts for other replicants, and "
                             "civilization contact (civ follow-up requests need a beacon AT the body where you completed an event — "
                             "Kuiper/Oort beacons don't count) with Place beacon + rule 'Beacons at civilization event sites'. "
                             "Defense page + rule: incoming asteroids, propulsors needed vs time left, activate/send/print. "
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
    ("1.2.0", "2026-10-01", "Contracts tracker + fulfill, re-open sites, mobile fleets (mining/explore/trade), ferry fixes, "
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
