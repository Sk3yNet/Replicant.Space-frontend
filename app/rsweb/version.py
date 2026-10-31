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

VERSION = "1.320.1"

# newest first: (version, date, summary). Entries before 1.4.0 were reconstructed when versioning was added,
# so their dates are approximate and they group several drops each.
CHANGES: list[tuple[str, str, str]] = [
    ("1.320.1", "2026-10-31", "Contracts page checkboxes stay on their own contract (Joe: ticks seemed to move, unapproved contracts showed checked). Approving a quiet civilization moved its row to the bottom and the page reloaded, so Firefox restored the ticks by position onto other rows; the boxes now opt out of that restore (also on Fleets, Automations, AMI schedules and system pages), quiet civilizations keep a fixed A–Z order, and live updates match each contract card by its designation instead of its title. Display only: each box saves only its own contract."),
    ("1.320.0", "2026-10-31", "Adopt lists only devices the controller could adopt (Joe). On a controller's device page and the AMI page: no other controllers (a fleet controller still takes fleet controllers), nothing already under a controller, only the types that kind runs (mining drones; survey drones and belt surveyors; haulers, transport drones and freighters; surge-capable devices for a fleet controller), and only in the controller's own system (a fleet controller reaches anywhere). Maintenance and trade controllers have no known type rule, so only the controller checks apply there."),
    ("1.319.0", "2026-10-31", "Device pages show every running command's finish time (Joe). A Running card lists printing, scans, prospecting, repairs, the current leg of a multi-leg trip, and the app's own countdowns for the device (compacting, unfurling, triangulation), each with a live countdown, clock time and progress bar. The game's status ETAs win over the app's timer of the same kind; the whole trip keeps its own card."),
    ("1.318.0", "2026-10-31", "A survey drone's scan of a planet or moon now counts for civilizations (Joe: ETHIREL-2's spacefaring Drafel, found by a planet scan, showed nowhere). Contracts › Civilizations not asking for anything and the Traffic page's civilization list read the latest body scans as well as the stored system scans, so a civilization shows there (and can be approved ahead of its first request) as soon as a scan finds it."),
    ("1.317.0", "2026-10-31", "Fewer game reads (Joe). An order whose answer names the device and its status (most commands) puts that answer on the device list instead of re-reading the device now and 20 s later; it shows as unconfirmed on device lists until an event about it or the next device list. Other devices the order names are still re-read. Pages that read a belt or body (location page, system belt refresh, scans, Terraforming candidates) now stamp the read, so the engine's belt checks take that copy instead of asking again."),
    ("1.316.0", "2026-10-31", "Less waiting on the server (Joe). The relay rollout and auto scout passes, the two costliest engine stages in the 02:12 snapshot (about 1.2 s and 1.4 s of CPU every tick), stop copying the whole star catalog and run their star searches in a thread, so pages and the event stream keep their turn; auto scout also checks distance before reading a system's scan. Fleet pages' hourly plans are served from the old copy while one refresh runs in the background, and pages asking at the same time share one run. The diagnostics snapshot download is written out in a thread."),
    ("1.315.3", "2026-10-30", "Ships flown by their replicant surge hop now. Logistics, loadouts and fleets send a vessel that hosts a replicant through the replicant's travel order, and route planning (surge hops, hubs, the off-ramp) only looked at device travel orders, so those trips took the game's own route: a repair patrol sent 5DF3161B from ISOLITHOR-1-L4 to ISOLITHOR-BELT-1 on a 499 AU cruise (56 min) instead of a 30 s hop to the Kuiper belt."),
    ("1.315.2", "2026-10-30", "The device page's Stow into list and Console command suggestions no longer offer a carrier's attach points to a device the game refused to attach."),
    ("1.315.1", "2026-10-30", "Messages: civilizations' requests for help are recognized by their \"Broadcast intercepted:\" wording (Joe), instead of a guess at it."),
    ("1.315.0", "2026-10-30", "Errors are now only what no automation will fix and needs you: a trip waiting for your Confirm, a job you started by hand that stopped, a fleet mission out of automatic retries, an evacuation that can't be done, a stalled engine, a paused relay rollout or season, a shop that moved, nothing to mine in a system. Everything the engine plans again by itself (stopped automated jobs, failed stages, belt progress) is a warning. Refusals the engine sorts out (Nothing to activate after a move, Maximum active sites) are skipped quietly. A trip refused for cruising over the Fleets › settings limit now tags the device at:<destination> and lift, and a carrier takes it there and drops both tags (27925A12 at ISOLITHOR-1 was refused every pass, 639 AU from the belt). The Messages page has checkboxes to mark salvage site discoveries and civilizations' requests for help read as they arrive."),
    ("1.314.0", "2026-10-30", "The galaxy map and Map › Relays work out suggested relay bridges in a background thread, so the rest of the server keeps running meanwhile. The 23:27 swoop snapshot showed that search (18-21 s of CPU per map refresh from an open map tab) was what froze the server, not the salvage rule it was blamed on. Diagnostics › Performance now credits a stall that a page request causes to \"(page request)\" instead of the engine stage running then."),
    ("1.313.1", "2026-10-30", "NPC chatter: posts you make by hand (Write a message, Post now) no longer count toward the daily cap."),
    ("1.313.0", "2026-10-29", "Device list: the tag filter folds pins (at:), trips (to:), contracts, home and bootstrap tags into one entry per kind that matches them all, lists fleets in their own group and shows a device count per tag. Delivered devices already lose their to: tag on arrival; the long list was the permanent beacon pins (31 places). A single pin is still found by typing it in the search box."),
    ("1.312.1", "2026-10-29", "During a swoop run the rules that game events set off wait too, in order, until the run ends. Live, a site.depleted event mid-run ran the salvage rule for 19 s and the event handling held the server for 56 s, so every round of the run came late and moves took 42 s to send. The wallpaper's map and panel also serve their last data during a run."),
    ("1.312.0", "2026-10-29", "Cockpit shows the automation engine: busy (the step it is on and about how long is left), held (time left), or idle (next pass in). Hold 5 min pauses automations, and pressing it again adds 5 minutes; Release lets them run. Start while a pass is running holds automations and starts the run as soon as the pass stops at its next step; press Start again to go at once."),
    ("1.311.4", "2026-10-29", "Swoop runs get the server to themselves: from Start (or the swoop.started event) until swoop.complete, at most 3 minutes, no automation tick starts, a running tick stops at its next stage, and the galaxy map serves its last data instead of redrawing. Live, ticks of 18 to 47 s and 21 s map redraws stalled the server, so round 4 of two runs in a row reached the cockpit too late to play."),
    ("1.311.3", "2026-10-29", "A device counts as stowable when its type's blueprint lists the stow feature, even if the device's own features don't (vector charges)."),
    ("1.311.2", "2026-10-29", "Vector charges count as stowable, so rescues and loadouts put them in a hold instead of trying to attach them (the game refuses: Cannot attach a large device)."),
    ("1.311.1", "2026-10-29", "Fixes. The loadout pass crashed (NameError: devices) since 1.308.1 whenever a relaying relay was due at its pin, which stopped everything after it in the pass. A device type the game refuses to attach (Cannot attach a large device: vector charge 42B4755C, rescued twice) is remembered, and carriers then offer it only a hold slot; the rescue says so when none has one."),
    ("1.311.0", "2026-10-29", "Long-range stations step down when a relay will do. Once a plain FTL relay would link a station's system just as well (every network keeps the same systems with the station cut to 7.5 ly), a relay is ordered there; when it works, the station is switched off and marked spare for the next gap, one at a time. Stations pinned to a contract or spot are left alone."),
    ("1.310.0", "2026-10-29", "Acknowledged BobNet mentions drop off the Messages page's Mentioning you card. Each mention has an acknowledge button, and the card has Acknowledge all; reading the mention's notification (bell or Notifications page) counts too. Game messages that mention you lead the list only while unread; once read they sit with the other read ones. The BobNet feed keeps its mention highlight."),
    ("1.309.1", "2026-10-29", "Relay links go back to either end's reach (Joe: relays link to hubs further than 7.5 ly away), so a single deep space relay station bridges a gap of up to 10 ly to any relay or hub, and the rollout no longer adds a second station in the system it links from. Bridge and rollout searches look as far as the longest reach around, so a station or hub's link to a farther system is found."),
    ("1.309.0", "2026-10-29", "Long-range relay stations. The deep space relay station (deep_space_relay_station, 10 ly) counts in your relay networks at its own range, and a link now needs both ends in reach (a hub links a relay only within the relay's 7.5 ly, as seen live). Map › Relays has a long-range stations option: the bridges then put a station where it saves relays, one at each end of a gap no relay chain crosses (Add route orders stations and relays), and the rollout gives a station to a system only a station reaches, plus one in the system it links from. The page shows the station blueprint's cost, and the Systems list has a Station column for ordering one by hand. Stations ride with relays, are placed at L4/L5 and switched on at the drop; pausing relay prints stops station prints too."),
    ("1.308.2", "2026-10-29", "Trips you send by hand inside a system hop too. From the entry point the game refuses the Kuiper belt as a waypoint (\"Loop detected\"), and the device and replicant travel forms then fell back to the game's own route: 5DF3161B cruised 499 AU (about an hour) from ISOLITHOR-1-L4 to ISOLITHOR-BELT-1. Now Go starts a short job that hops to the Kuiper belt first (30 s) and cruises the rest, and the route preview says so. A hand-built chain with the planned waypoints hops the same way."),
    ("1.308.1", "2026-10-29", "A relay pinned to a spot in its system but switched on somewhere else is switched off and flown there, as long as another relay keeps the system covered. Before, only idle devices went to their pin, so the relay sent to the OTHILETH-1-EVT-007 Trade Route Request sat relaying at OTHILETH-1-L4 and the contract waited for it forever."),
    ("1.308.0", "2026-10-29", "Contracts page: each option that asks for devices shows how many of each type are tagged spare (new Spare column) and the option's total cost in resources: what it asks for plus each device's blueprint cost times the quantity. A device type with no known blueprint is named. Version 1.307.0 was left for a parallel thread."),
    ("1.306.2", "2026-10-29", "Game events no longer wait for the automation engine. Each event was handed to the automations before the next one was read, and those wait for the engine's lock, which a tick can hold for over a minute (80 s right after a restart). So during a swoop run the rounds reached the cockpit only after the run had ended. Now events show at once, and the automations catch up in order once the tick is done. The cockpit's What works table also lists the overhead and blocked debris seen live."),
    ("1.306.1", "2026-10-29", "Cockpit throttle has two positions: boost pushes it full up and brake full down (it no longer steps back through the middle). It starts in the middle and goes back there at the start of each round."),
    ("1.306.0", "2026-10-29", "Cockpit follows the real swoop run (from the first live run): a warmup countdown where moves are practice, then each round's debris status (flanking, incoming, clear), description and time limit, with right or wrong on the first move and the buttons locked until the next round. Rounds name only the replicant, so the cockpit now matches them to the vessel's pilot; before, they didn't count as the vessel's events. A What works table tallies every graded move by debris status and lights up the move that worked before. The Flight box is gone, and the events list always has room."),
    ("1.305.0", "2026-10-29", "Travel destinations and system target lists include every moon of a scanned planet (STAR-P-1 up to its moon count), so a moon such as ETHIREL-2-4 is offered before any device has been there. Before, only moons the app had seen were listed; typing the code always worked."),
    ("1.304.0", "2026-10-29", "Cockpit: when the game refuses Start swoop because the run starts somewhere else (live: Vessel must be deployed at ETHIREL-2-4, a moon), the cockpit remembers that spot and fills it into a new Flight box, which previews the trip and flies there on a planned route (surge hops, hubs). An arrival at the spot says you're ready to swoop. Travel forms now offer known swoop spots, and the cockpit lists the moons the app knows in that system."),
    ("1.303.0", "2026-10-29", "Devices › Cockpit: a full-screen cockpit to fly the new swoop debris run by hand (Start swoop, then boost, dodge, dive, roll or brake by button, keyboard or game controller). Debris warnings from the game show in the viewport with their direction and countdown, the move the game names lights up, and run time, score, dodges, hits and ping show as gauges. The debris event isn't in the game's spec, so every event about the vessel shows raw and event types never seen before are marked new. A vessel that lists swoop links to its cockpit from its device page and command form."),
    ("1.302.0", "2026-10-29", "NPC chatter: placeholders {pilot} (who an announcer's post is about, such as Patch calling the exploding moon run at DELTA) and {score} (the number after Score:), so the crowd can cheer or groan for the pilot by name."),
    ("1.301.2", "2026-10-29", "NPC chatter settings: one limiter per channel, how often one of the NPCs may start a post there (all NPCs together; default 24 hours for the general and bots channels, blank means never, other channels such as #trade can get a row). It replaces the per-NPC timer and its two overrides. Replies inside a conversation still don't count."),
    ("1.301.1", "2026-10-29", "NPC chatter: a replicant can post on BobNet only from a system with an active relay (the game answers No active relay in this system). Replies that any NPC may give go to an NPC in a system with one of our active relays or hubs first; an event line from an NPC where the game refused for lack of a relay is skipped for 6 hours without using up its timer."),
    ("1.301.0", "2026-10-29", "NPC chatter: each channel has its own post timer, so an NPC can post once a day on #general and once a day on the bots channel (the general and bots channels can set their own hours). When the game refuses a reply that any NPC may give, another NPC tries it on the next tick (up to 3); an event line stays with the NPC that took part."),
    ("1.300.1", "2026-10-29", "Test update for the {arrival} placeholder (no change in behavior)."),
    ("1.300.0", "2026-10-29", "NPC chatter: a new placeholder, {arrival}, names who a post says just arrived (Riker: Jim-1 just arrived back in SOL), so NPCs can welcome them by name."),
    ("1.299.2", "2026-10-29", "Activity › Chatter no longer fails with a server error once BobNet posts are saved (the channel list read the rows wrongly)."),
    ("1.299.1", "2026-10-29", "BobNet mentions also catch each part of a replicant name with underscores: a post saying Towline or hester counts as mentioning Hester_Towline (parts of 3 letters or more)."),
    ("1.299.0", "2026-10-29", "NPC chatter: Write a message sends a BobNet post now as any replicant you pick, to a channel you pick (the chatter channels, #general, #trade, every channel seen) or type in. Messages can use placeholders, {from} (the system a post names, such as the one Bill just left), {star}, {region} and {poster}, filled from the post or event they answer; a message whose placeholders can't be filled is skipped. Answers cycle through every message before any repeats."),
    ("1.298.0", "2026-10-29", "NPC chatter (Activity › Chatter): replicants marked NPC post canned BobNet lines about game events they took part in (survey done, belt or site mined out, their vessel arriving, salvage, contracts), answer each other in short conversations, and answer posts by other NPCs such as Bill, Riker and Dash. Messages come in by bulk import with a tick per message, go to the general or a bots channel, and post at most once a day per NPC by default (adjustable, with chances and a daily cap); posting is off until switched on. The replicant page and the bulk profile import set the game's NPC flag."),
    ("1.297.0", "2026-10-28", "Relay, ward and beacon orders (relay rollout, system page, Prospects) take spares in reach before printing, and now take a printing hub's own keep-in-stock spares last: the extras at FALQUORYX go out first instead of SOLARES's stock of 8, which would only be printed again. A queued print no longer gives way to a hub's own stock spare."),
    ("1.296.2", "2026-10-28", "Fixes from the 21:03 snapshot. A deal bought several times whose next unit is refused (its price not all there yet) skips the other units at once and the job carries on; before, three refusals stopped it with four alerts (Trajectory Deflector ×10 at ALKALAROPS-OORT, 5 of 50 propulsors there). A fleet's automatic retry now really re-runs the stalled phase: it left the failed job in place, so each retry stalled again at once one phase earlier (Surveyor 3: recall, watch, work, deploy), each a new alert. A survey fleet's watch waits 3 minutes for a fresh device list before it calls the survey done; the cached list could predate the launch (Surveyor 3 recalled its drones a minute after launch)."),
    ("1.296.1", "2026-10-28", "Rescue carriers are sized to the job: the seats every device stranded in a system (for the same home) needs pick the smallest carrier that seats them all, so a big carrier is not sent for one device; when none fits, the one that seats the most goes and the rest get another. Seats to spare pick up rescues in systems within 15 ly on the way."),
    ("1.296.0", "2026-10-28", "Rescues: devices stranded in the same system for the same home share one carrier, and the carrier with the most free seats for them is chosen first (two trade rewards at ALKALAROPS had a carrier each). Out of command range the game names only the star, so once the replicant is in the system the pick-up carrier reads each device's own spot and goes there (or skips the trip when it is already beside it). An out-of-range device pinned (at:) in the system it is in is no longer rescued."),
    ("1.295.1", "2026-10-28", "Blueprints › Can afford at: hide vessels now also hides printing vessels listed as devices (racing vessels and any other vessel type), not only replicants' own vessels."),
    ("1.295.0", "2026-10-28", "Contracts and trades: the stockpile at a deal's location is kept for it and is no longer a source for other deliveries (two trade runs to MENKUNT-2-L4 had loaded 40 carbon and 20 volatiles from the KAMUYIS-4 contract's pile). Devices a deal asks for count as there when they are out of command range at the deal's star and pinned to its location: the game names only the star then, so a shop trade at ALKALAROPS-OORT paid in propulsors would have waited for its devices forever."),
    ("1.294.0", "2026-10-28", "Dashboard › Logistics backlog: resources at a contract's or trade's location, up to what its open deals still pay, get their own line (set aside for contracts and trades) instead of counting as waiting to move; a trade's payout and leftovers there still count as waiting."),
    ("1.293.2", "2026-10-28", "Shop trades: the approve checkbox is gone. The Logistics buys number is the approval: 0 by default (not approved), any other number is how many units logistics still buys, counting down as they are bought."),
    ("1.293.1", "2026-10-28", "An approved shop trade for several units stays approved until all of them are bought. Before, the approval was dropped as soon as a delivery left, so when the replicant arrived before the rest of the price (or the game refused a unit) only one or two were bought (10 vector charges ordered). Now the units a job did not buy stay approved and logistics brings the rest; a refused unit no longer stops the job; changing the quantity of an approved trade takes effect at once."),
    ("1.293.0", "2026-10-28", "Fewer game reads (the app neared the game's 120 GETs a minute). The read limiter counts a rolling minute: the old refilling budget started full and could let through about twice its limit in one minute. The device list is read every 2 minutes instead of every minute (25 GETs a read at 1,214 devices; POLL_DEVICES). A replicant's details are re-read only when it moved, an event names its vessel, you order it, or every 10 minutes (POLL_REPLICANT), instead of all 22 every minute. A device you order is re-read 20 s later only if no event about it already refreshed it. Diagnostics › Performance shows Game reads: GETs a minute over the last 10 minutes by who asked (page, poller, engine stage) and path."),
    ("1.292.0", "2026-10-28", "Printed components are tagged as components: a print's component devices carry the `component` tag, pinned to the factory, without the item's spare, stock or fleet tags, so no loadout makes them spare and no other factory takes them. A factory waiting for components claims the ones already at its location first. Both printing hubs' vector charge prints sat at 4 of 6 impulse charges: the charges came out tagged like the stock print, the hub loadout (impulse charge: 0) kept making them spare, and each hub's component pass sent the other's spares over."),
    ("1.291.1", "2026-10-28", "The way back no longer takes a pile that a request delivers to: F10B1A2F dropped 200 silicates at RILXORAE-3 for Dyson Swarm Seed and took the 500 conductive an earlier trip had left there back to FALQUORYX, round after round (KELMORNEA-3 for Planetary Shield Project the same). A surge's arrival counts at the spot it landed: the game names the star as the destination (15DFB014 landed at BELNULEON-10-L4 with destination BELNULEON), so trips ending at an entry point waited out their hour; a trip step also finishes once its device stands at the destination (F7E30727 and 3E6D7DD7 sat at ISOLITHOR-1-L4)."),
    ("1.291.0", "2026-10-28", "Tags & controllers check: a fleetless device run by the controller of a fleet that is not stationed (such as a logistics fleet) now joins that fleet while the fleet has room for its type, and is released by the controller when the fleet is full (live: hauler F0A908ED under Delta Logistics' consolidate controller A0B8F5C7 joins as its 4th hauler). A device that reached its to: system while still aboard its carrier, or on a running delivery job, is shown as handled: that job detaches it and drops the to: tag (live: transport drone 7B1B81BA at ANLOREN-7-L4)."),
    ("1.290.0", "2026-10-28", "Deliveries use the way back: a logistics ship that drops materials for a print, contract, trade or hub upkeep outside a printing hub's system no longer flies home empty. It loads a stray stockpile in that system or within a relay hop (not in a hub system, at least 25 units, not farther from the hub than the drop-off) and brings it to the nearest printing hub, what the hub's prints lack first. The job row says \"on the way back ... from ... → ...\"; that pile isn't sent another ship the same pass. Ships riding a carrier and payout trips are left as before."),
    ("1.289.0", "2026-10-28", "Four automation fixes from the 10-27 and 10-28 snapshots. Drones left standing `recall_waiting` are adopted again: Miner_2's 8 drones had stood at XERLARNIS-5-L4 since 10-09 and Miner_1's at BELNULEON-10-L4 since 10-26, with no controller. AMI deliveries ask for the level the destination should hold (the game delivers until it holds the requirement): SOLARES hub upkeep asked for the 57/130 shortfall while 69/158 was there, so every delivery ended at once with nothing moved, every 5 min for hours; the same applied to contract deliveries and consolidation. Trips into a scanned or hub system land at its entry point, so the Kuiper off-ramp is now a 30 s hop of its own after arriving instead of a via the game cruised 527 AU to (Delta trips ISOLITHOR to ANLOREN and back took about an hour and their waits gave up), and a wait lasts until the current leg's ETA when that is later than the trip's. Contract and print runs take their materials from a system that can spare all of it before a nearer one with only a little (one contract had sent three freighters, two for 19 and 10 units)."),
    ("1.288.1", "2026-10-28", "Device page Stow into lists every carrier here with room: a free hold slot (stowed) or, on plates, platforms, carriers and mobile fleets, a free attach point (the carrier attaches it). Before, only vessel holds were offered, so the list was empty wherever the carriers had no hold; full holds are left out, and an empty list now says why."),
    ("1.288.0", "2026-10-28", "Automations: every job row (active and recent) says what its carriers carry or will pick up: device types and codes with their state (to pick up, aboard, dropped) and materials to load, read from the job's stow, attach and load steps (and a loadouts delivery's device list)."),
    ("1.287.0", "2026-10-28", "A printing hub short of keep-in-stock devices first takes the other hub's extras: spares there beyond that hub's own stock are sent over (a carrier brings them, and they arrive as spares, switched off) and only what is still missing is printed. SOLARES kept printing relays for its stock of 8 while about 95 spare relays sat at FALQUORYX."),
    ("1.286.0", "2026-10-28", "Idle spares are gathered at the nearest receiving point: each goes to the home of the closest fleet set to receiving (FALQUORYX, SOLARES, ISOLITHOR), within the supply range, instead of all going to one spare depot. A spare already in a receiving system stays there. A spare depot set on the Fleets page still takes them all."),
    ("1.285.0", "2026-10-28", "Order more than one from a shop: each trade row has a quantity. Buy buys that many one after another (the game sells one unit per call) and stops at the first refusal; approve × n has logistics deliver n prices in one trip and execute the trade n times (at most the listed stock)."),
    ("1.284.1", "2026-10-28", "Keep-in-stock prints stay at their hub: spare gathering no longer takes a hub's stock print of a type that hub keeps in stock to the spare depot. SOLARES kept printing propulsors, relays and wards to refill its stock while each one was shipped to FALQUORYX (about 140 spare propulsors there for a stock of 10)."),
    ("1.284.0", "2026-10-27", "Fleets sort themselves out. A device of one fleet run by another fleet's controller is released so its own fleet's controller takes it back, and the idle-miner rule only hands a drone to its own fleet's controller (or a fleetless one): Miner_8's drones were mining for Miner_9's controller at SOLARES. Members on their way home that haven't left yet become spares where they wait once the fleet has its count (Delta Force had 4 more miners waiting in ISOLITHOR for 1 open place). Unstationed fleets (survey, logistics, prospector) drop members beyond their loadout as spares the next time they're idle at a hub; blank loadout types stay don't care, and replicants' vessels, matrices, matrix containers, carriers with devices aboard, a fleet's relays, beacons and wards and reserved devices are never dropped."),
    ("1.283.0", "2026-10-27", "Logistics haulers go where the pickup is: a free logistics carrier with a seat (a surge plate, platform or carrier) now fetches a hauler or transport drone from another system, tows it to the stockpile to load, and on to the receiving location. Before, only haulers already in the pickup system were used."),
    ("1.281.1", "2026-10-27", "Rescues leave reserved devices alone: a device tagged for a contract, or tagged to: another system, is no longer brought home when it is out of command range (its deal or its own trip handles it)."),
    ("1.281.0", "2026-10-27", "Bulk profile import on the Reputation page: paste JSON of replicant code (or current name) to name, description, plan, project and pronouns, preview what changes and what is wrong (unknown replicant, too long, duplicate or taken name), then apply. Only changed fields are sent, one profile edit per replicant. Bulk tag edits on the Device list no longer lose a tag when the device refresh after the order lands at the same moment."),
    ("1.280.1", "2026-10-27", "Another player's devices stay out of your device list. Re-fetching a device after an order (the datacenter's replicant interface after a simulation, say) added it even when it was someone else's, and it then lingered 12 hours as unlisted, with a rescue request on the Logistics page. Devices the game names another owner for are dropped on the next sync."),
    ("1.280.0", "2026-10-27", "Logistics page: every logistics job still going has Cancel job (and Confirm trip when it waits for your Confirm), on its request row and under Recent trips. Old jobs are no longer left off (the page showed only the last 15), and a skipped request still shows its running job: C02BCAB4 sat a day at SOLARES waiting for a Confirm the page never offered."),
    ("1.279.2", "2026-10-27", "Surge hops inside a system go as one trip with the spot as a waypoint again (the game takes it from a belt to ISOLITHOR-1-L4); only when the game refuses it as a loop (from the entry point to the belt) does the device hop to the spot first and cruise the rest. Hand-sent trip previews offer the waypoint again."),
    ("1.279.1", "2026-10-27", "Surge hops inside a system are trips of their own: the game refuses a surge spot as a waypoint there (\"Loop detected: already at ISOLITHOR\"), so trips from ISOLITHOR-1-L4 to the belt cruised 499 AU for 57 min. Now the device hops to the Kuiper belt first (30 s), then cruises the rest. Hand-sent trip previews no longer offer the refused waypoint inside a system."),
    ("1.279.0", "2026-10-27", "Server speedups from the sampling profiler (snapshots of 2026-10-27): the device sync's lost-device check looked at every catalog star against every relay (about 3 s each sync; now each relay looks only at the stars in its reach, ~13 ms); the map's relay bridges are kept until your relays or the catalog change, and the star grid is built once per catalog update; which controller runs which drone (asked by many engine passes) folds in only new AMI events instead of replaying them all; the desktop wallpaper's map and dashboard data are kept half an hour."),
    ("1.278.0", "2026-10-27", "Device list: filters switch in the browser, with no trip to the server. The page holds every device and hides the ones outside the filter, so narrowing a filter is near instant (about 50 ms for 1,060 devices instead of half a second plus any server stall); the search box asks the server only which devices match the text. Hidden devices are unticked so a bulk command never reaches one you cannot see, and the command counts follow the filter."),
    ("1.277.1", "2026-10-27", "Uploaded devices leave the device list at once instead of lingering 12 hours as unlisted (the reserved tag kept being re-sent to them and failed with Device not found). A data device deployed at the datacenter that drops out of the game's list counts as uploaded, so chips consumed earlier are cleared on the next sync and added to the Data uploads count; the Lost devices page names the reason."),
    ("1.277.0", "2026-10-27", "System page: under Bodies, each system object of the scan (a space station or another megastructure) folds out with its stage, status, progress, description, requirements and contributors, and Full system scan folds out the whole scan."),
    ("1.276.0", "2026-10-27", "Galaxy map: megastructures from system scans show again. They were filed under their designation (SPACE-STATION-006), which names no star, instead of their STAR-OBJ-n location, so none ever showed. Space stations (megastructure type space_station) get the silver wheel instead of the hexagon."),
    ("1.275.0", "2026-10-27", "Galaxy map: colony space stations get their own mark, a silver wheel lower right of the star, with its own checkbox. The game reports no station object (so they never counted as megastructures); the systems come from the story overview (the twelve colony worlds) and stay marked after the storyline ends."),
    ("1.274.0", "2026-10-26", "FALQUORYX and SOLARES print for each other: a fleet print, a keep-in-stock print or a ward / relay / beacon order goes to the other printing hub's autofactory when the home stockpile can't cover it and the other one can, and a carrier brings the device over. Only hubs within the sector range (1728 ly) share, so Delta stays apart."),
    ("1.273.0", "2026-10-26", "The three heaviest engine passes (salvage, restart idle miners, reopen sites) each run every third minute, on a minute of their own, instead of all three every minute: together they held the server about 13 s of every minute, so pages stalled (the first tick after a start still runs them all). Diagnostics › Performance has a Where the server is busy table: a sampling profiler that names the functions holding the server, inside the engine or not, to find the next thing to move."),
    ("1.272.0", "2026-10-26", "Simulations › Data uploads no longer lists devices already uploaded (the interface consumed them): the app keeps the uploaded codes and shows a count by type instead. Uploads sent before this version are recovered once from the action log."),
    ("1.271.1", "2026-10-26", "Surge hops out of a system to a body elsewhere now end with the destination's star as the last waypoint (the game refused ISOLITHOR-KUIPER straight to KHAZENEN-BELT-1, so the trip fell back to the game's own slower route)."),
    ("1.271.0", "2026-10-26", "Rescues can be paused: Fleets › Logistics › Pause rescues stops all rescues, or just one fleet's, for 6 h, a day, 3 days, a week or until resumed. Paused rescues stay listed (marked paused) but nothing is sent; a timed pause ends by itself, and a rescue already under way goes on."),
    ("1.270.0", "2026-10-26", "Repeat on the Devices page: Repeat on ticked turns the picked command into one repeat per ticked device (every N minutes, optionally stopping at a find), and a device page's Command card has its own Repeat button and lists that device's repeats with Stop / Resume and Delete. A repeat waits, without counting an error, while its device doesn't offer the command (still scanning, say) and sends as soon as it does."),
    ("1.269.0", "2026-10-26", "Console can repeat a command: Repeat sends the POST, PATCH or PUT request now and again every N minutes until you stop it (Console › Repeating: Stop / Resume, Now, Delete), such as a replicant belt scan or a survey drone search. Finds in the command's system (device.discovered, salvage.discovered) are counted on the row and notify each time; the repeat keeps going unless set to stop at the first find. Three failed sends in a row stop it."),
    ("1.268.0", "2026-10-26", "Orders show without a refresh: an order you give (or each step of a chain you started, such as stow or attach before a trip) asks the game for the devices it touched right away and again 20 s later, and for 10 minutes any event about them does too. Arrivals refresh the arriving device (up to 30 per 10 minutes). The device page is live (status, current task, trip, details, carrier rows, events) and follows changes within seconds for a minute after an order. Its trip line counts down, and when it reaches zero the page loads the new location."),
    ("1.267.0", "2026-10-26", "Live pages: 24 pages (Dashboard, Fleets, Logistics, Systems, Stars, Traffic, Contracts, Automations, Notifications, Messages and more) redraw their cards by themselves when devices, jobs or game events change, from the app's cache with no game calls: at most once a minute per tab, only while it is visible, never a card you are typing in or just used. Filters stay applied. The header's ● live switch pauses it. Pages that ask the game when opened (device, replicant, location, Leaderboards, Blog) stay manual."),
    ("1.266.2", "2026-10-25", "A bootstrap hub made before 1.257.0 still asked for a surge carrier (Delta Force · Hub kept printing toward one): its loadout now wants a surge plate instead, switched on the next bootstrap pass."),
    ("1.266.1", "2026-10-25", "Recent on BobNet shows times in the app's time zone (TZ) like the rest of the page, instead of the game's UTC."),
    ("1.266.0", "2026-10-25", "Messages: the event-stream BobNet feed has a search box (text, poster, channel or system, any case) over every message the app has saved, and both BobNet cards have an Older button. The feed pages back 80 at a time; Recent on BobNet follows the game's next_cursor for older pages from the relay (per channel too)."),
    ("1.265.1", "2026-10-25", "Map › Systems always shows the relay, beacon and ward columns; the Wards, beacons & relays and only systems missing one options are gone (the filters cover them)."),
    ("1.265.0", "2026-10-25", "Map › Systems: the relay, beacon and ward add / remove buttons are checkboxes that save in the background without reloading the page, so several can be ticked in a row. New filters show only systems with each checked or unchecked, and by civilization (has one, missing a beacon, all have beacons, none), remembered in the browser."),
    ("1.263.0", "2026-10-25", "Fleet details starts with an index of the fleets: a name opens that fleet alone, without loading the rest, and otherwise each card loads as it scrolls into view (the page is about 56 KB instead of 5 MB). The Tags & controllers check is kept an hour like the plans, with a Refresh button. Pages now read the database through their own connection, so they no longer queue behind the engine's queries. Diagnostics › Performance shows each engine stage's CPU time and a new Server stalls table (times the server could not answer for over a second, by the stage running then) to show which passes to move off the server next."),
    ("1.262.0", "2026-10-25", "Blueprints › Can afford at has a hide vessels checkbox that leaves replicant vessels out of the column (remembered in the browser)."),
    ("1.260.1", "2026-10-25", "Fleet details loads faster: each fleet's Add / remove devices list of devices to add loads when you open the box (it was every device once per fleet, over half of a 5 MB page; now about 0.9 MB), the loadout plan matches to: tags to stars without a pass over the whole star catalog (also speeds up the engine's loadout pass), and the star catalog is read only when a destination list is remade."),
    ("1.260.0", "2026-10-25", "Carrier balancing has a leeway (Fleets › Logistics › Carriers per hub, default 25 % of a hub's share, at least one carrier): a carrier only moves from a hub more than the leeway over its share to one more than the leeway under it, or to a hub with none, so small imbalances even out by themselves instead of sending plates across the 500 ly between the hubs. A stocked contract that a logistics job is already on its way to fulfill gets no second replicant: three replicant carriers had left for Geothermal Surplus at ELUSORYR-3, two after it was done."),
    ("1.259.0", "2026-10-25", "Hub upkeep: the game draws a hub's upkeep from the stockpile at the hub's own location. The Upkeep "
               "page has a Hub upkeep card per active hub: its price per 20 % (upkeep_requirements), the target on site (back "
               "to 100 % plus one step), what is there now, whether a maintenance drone patrols its system, and the delivery. "
               "Logistics tops each hub's spot up to the target (new hub upkeep task category, urgent below 50 %); the pile "
               "there is kept for the hub and never consolidated away. Rule option: Keep maintenance drones patrolling › "
               "Logistics stocks hub upkeep (on)."),
    ("1.258.0", "2026-10-25", "New fleet role: rock chaser, for asteroid mining inside relay coverage, in its home system "
               "(hub) or roaming to covered systems within a radius. Fleet details has its target criteria (size, resource "
               "priority, hours of mining after the trip, when to leave, ship-at amount, drop). It picks and tracks a target, "
               "carries its rigs there and mines it; a fleet hauler ships the pile out as soon as it reaches the ship-at "
               "amount; 80 minutes before the ETA (or once the rock is mined down) everything boards at once and flies to the "
               "next target. A sensor array searches the Kuiper belt and marks objects for launch, launched with a fleet "
               "vector charge from the card or automatically. Unverified live: rig commands, the slots-full error text, "
               "where detect_object must run."),
    ("1.257.0", "2026-10-25", "A bootstrap hub's kit has a surge plate instead of a surge carrier, and the heaven vessel does the mass transport: from the outposts stage on the loadout pass lends the bootstrap's own vessel (hold 10) to its fleets, so it ferries the outposts' kits the hub's autofactory prints, though it hosts the replicant. A bootstrap's devices now always ride apart from other fleets' in a delivery batch."),
    ("1.256.0", "2026-10-25", "Restart idle miners has a Mine one site at a time option (off by default) for drones without an AMI controller: each restarted drone goes to the open site with the least of its resource left (start_mining with a target), so the sites run out one after another and each freed survey drone searches again while the other sites keep the drones busy, instead of every site running out within minutes and the belt going dry for an hour. A refused target falls back to any site. Controllers pick their own sites, so their drones are left alone."),
    ("1.255.0", "2026-10-25", "Diagnostics: checkboxes choose what a snapshot includes (stockpiles, controllers, belts, events, idle drones, raw device pages, fleets, logistics, relays, asteroids, last commands, performance, version history), kept for the next one. The sections that make requests share the 60-request budget, so leaving one out lets the others read more controllers, belts or idle drones; the rest only cost file size."),
    ("1.253.2", "2026-10-25", "Mining controllers get maintain_ratios again: the game now wants the ratios under configuration.ratios (it refused the flat form every pass for the Delta Force hub and the print-tilted schedules). Back to the belt no longer tries to fly drones a controller holds in another system (they cannot surge): they are released for a controller there, so Miner_7's 9EE3E14D no longer stops on three drones at SOLARES. Logistics balancing never sends a carrier back the way another of its class is still flying (plates went SOLARES to FALQUORYX and back two minutes apart)."),
    ("1.253.1", "2026-10-25", "A fleet mission whose members cannot all board (the carriers here are full) now stalls with an alert and retries by itself, instead of waiting silently: Surveyor 2 sat 41 h at FALQUORYX because its heaven vessel also held an FTL slingshot that is not in the fleet, so its eighth survey drone never fit. The note names such extra cargo, and Launch anyway also works on a mission stalled before leaving."),
    ("1.253.0", "2026-10-25", "Pausing the relay rollout (Map › Relays) now stops relay printing: relay print jobs still on their way are canceled, relay prints waiting in an autofactory queue for a system are dequeued, and relay orders waiting on a print are dropped (the rollout orders those systems again on Resume, without skipping them). While paused no relay order prints one, the Systems list and lost-print re-sourcing included; spares are still sent and idle relays switched on. A Pause prints button does the same while the rollout is not running."),
    ("1.252.0", "2026-10-25", "Surge spots follow the game docs: only the Oort cloud, the Kuiper belt and the entry point (a planet's L4/L5 is no longer counted: live dry runs cruised there). The system page lists its surge spots under Bodies."),
    ("1.251.2", "2026-10-25", "Bootstrap drones mine whatever the open sites hold, even a resource the belt's catalog does not list (mining drones mine volatiles too): at ISOLITHOR-BELT-1 six drones sat idle beside five sites of volatiles only."),
    ("1.251.1", "2026-10-24", "Every travel estimate uses the planned route (surge hops, a hub on the way, the off-ramp, the star before a spot in another system), not the game's own: a device's travel command (its preview names the waypoints and the time saved, a refused plan goes the game's way), the sector range check on automated trips (a trip over 24 h by the catalog is previewed by its planned route, so a hub can bring it under), and the asteroid mining trip time. The replicant travel form, Stars and Map previews had it since 1.243.0; fleet moves, missions and logistics go through the job runner, which plans every trip."),
    ("1.251.0", "2026-10-24", "Game v3.6.2. Simulations has a Data uploads card: devices with the data feature (such as a guidance chip) deployed at the replicant interface, with a replicant there, can be uploaded for analysis, by hand only since the upload consumes the device; devices elsewhere or stowed say why they cannot go yet. A scan that discovers a device (device.discovered) raises a notification. Console preset and API reference for the upload endpoint; the repo spec copy is refreshed. Hubs now help arrivals and cost upkeep only while active: the route planner already counted only our active hubs."),
    ("1.250.0", "2026-10-24", "Survey controllers adopt belt surveyors (confirmed live), belt surveyors first: when re-opening sites, in a mining fleet's belt_search, in a survey fleet's survey_system, in AMI surveys on arrival, and in the AMI schedules' and hand-off's adoption of idle drones. Without a controller belt surveyors still search or scan on their own."),
    ("1.249.0", "2026-10-24", "Belt surveyors are their own loadout item again (owner): 1.248.0's stand-in for survey drones is undone, so loadouts, the Fleets roster, prints and a bootstrap hub's wants count belt surveyors and survey drones separately. Everywhere the app works with survey drones it still takes belt surveyors too, belt surveyors first: belt searches, site re-opening, a mining fleet's work, asteroid scans, a bootstrap home's ratio and searches, a supply system's surveyors (belt surveyors picked first), a survey fleet's scans, and the counts."),
    ("1.248.1", "2026-10-24", "Planned routes name a system's star before a surge spot reached from another system, as the game requires. A hub plus the off-ramp (via CAELZAROR, ISOLITHOR-KUIPER) was refused (\"Cannot travel directly from CAELZAROR to ISOLITHOR-KUIPER (different systems)\") and the trip fell back to the game's route; now it goes via CAELZAROR, ISOLITHOR, ISOLITHOR-KUIPER. Applies to the replicant travel form and automated trips; a refused surge hop on an automated trip keeps only its hub waypoint."),
    ("1.248.0", "2026-10-24", "Belt surveyors are printed and shipped in place of survey drones. A fleet loadout's survey drones are filled by belt surveyors first (members, spares, then prints of belt surveyors once their blueprint is known); a loadout that names belt surveyors itself counts them on their own. The Fleets roster counts them in the survey drone row. Carriers attach them, since they can't stow. A bootstrap hub folds its belt surveyors into its survey drone count, and a supply system wants its full share of surveyors even when the vessel can only take survey drones: the loadout pass sends or prints the rest."),
    ("1.247.0", "2026-10-24", "Belt surveyors (said to search belts about 4x faster than survey drones) are used wherever belts get searched, and go first: re-opening sites at a worked-out belt and filling a belt's free site slots, a mining fleet starting work at its belt, and the asteroid scans. They search on their own (no AMI controller takes them). They count as survey drones on the belt viability card and the drone tally, in a bootstrap home's survey-per-miner ratio and its hub loadout, and a stationed fleet's belt surveyors at home fill its survey drone loadout (extra survey drones go spare)."),
    ("1.246.0", "2026-10-24", "Relay rollout: each seed system has its own radius (Map › Relays, one box per seed, or STAR:ly in the seeds list), so SOLARES can stay close while FALQUORYX reaches far. Seeds you don't set use the default radius. A system counts for the nearest seed whose radius it lies in."),
    ("1.244.0", "2026-10-24", "Mining fleets may work a hub home: a stationed fleet's home blocks mining missions (target list, Launch check, Prospects scoring) only when that fleet has miners in its loadout or among its members. Explore missions still keep out of every stationed home."),
    ("1.243.0", "2026-10-24", "A replicant's travel preview and Go plan the route like automated trips: surge hops to a Kuiper or Oort point, a hub on the way when the game's previews say it's faster, and the off-ramp nearest the destination. Before, the form sent only the destination and the game's own route takes none of them (FALQUORYX to ISOLITHOR-BELT-1 surged to ISOLITHOR-1-L4, then cruised 499 AU). The preview names the waypoints and the time saved against the game's direct route; a refused waypoint falls back to the game's route."),
    ("1.242.0", "2026-10-24", "Asteroid impacts (system.object_impacted, diversion.impacted) are warnings now, so they no longer raise the bell's badge; impact warnings stay errors. Messages: unread game messages come first with a blue edge and a dot, read ones (salvage notes the app marks read) are dimmed under a Read divider, the card shows how many are unread, and an unread only box hides the rest."),
    ("1.241.1", "2026-10-24", "Relay orders whose print never happened are retried. Relay (and ward, beacon) orders went to the two SOLARES autofactories, whose print queues hold only 10, so most prints never got queued; the order still said printing and sat until it expired after 3 days (73 relay orders on 2026-10-23). Now a print order with nothing in a queue, nothing tagged for it and no job on it after 20 min sends a spare (16 stock relays sat at the belts meanwhile) or prints again on a factory with a free slot, and its 3 days start over; with every queue full it waits for a slot and doesn't expire."),
    ("1.241.0", "2026-10-23", "Beacons reach the civilization's body. A beacon in a civilization's system but not at its body (most were left at the relay's L4 point by a relay run, with no vessel left in the system to move it) is now pinned to the body, and the loadout pass sends a carrier that picks it up and drops it there; until then no second beacon is printed. Beacons ordered for a system with a civilization are pinned to its body from the start, so the run drops them right there. Contracts then shows the beacon instead of in system."),
    ("1.240.0", "2026-10-23", "Logistics carriers spread over the hubs. One logistics fleet still works anywhere, nearest carrier first, but each hub (FALQUORYX, SOLARES; ISOLITHOR once it is within the sector range) now has a share of every carrier class, weighted by its demand over the last 3 days, at least one each, and a carrier idle 30 min at a hub over its share flies to a hub under it. The Logistics page shows a Carriers per hub card. The loadout supply range now defaults to the sector range (1728 ly) instead of 100 ly, so fleet moves and spares may borrow carriers from the other hub (a saved 100 becomes 1728; set it lower on Fleets settings to limit it again)."),
    ("1.239.1", "2026-10-23", "Fleet moves with one-seat carriers. A borrowed carrier in no fleet (a spare or taxi surge plate) that brings a moving fleet home one device at a time now waits at the drop while more of the batch waits, instead of flying home and back between trips (Miner 5 SADICHBIA to BADU: about 4 min a trip instead of 20). The strays list and the move alert say a member is waiting for a free carrier when the loadout pass has it queued, instead of nothing is bringing it home."),
    ("1.239.0", "2026-10-23", "Idle drones at a dry belt, and a supply system for a dry bootstrap hub. Idle miners now mine what the belt's open sites still hold, first what the factories they feed wait for (their own system's, or the hub their fleet sends to), instead of retrying a resource no open site has (ISOLITHOR-BELT-1: 7 miners idle on conductive while sites held silicates the autofactory needed). Idle survey drones search a belt with fewer than 5 open sites even when it isn't worked out. A bootstrap hub whose autofactory waits 3 h for a resource the hub belt lacks offers a supply system within 7.5 ly whose belt has it (your OK on the Fleets page): part of the hub's miners (supply miners setting, default 4) and survey drones move there, the vessel prints a relay for each system, a surge plate and a transport hauler, and the hauler rides the plate to bring what the hub waits for."),
    ("1.238.0", "2026-10-23", "Logistics task categories. Each request belongs to a category (queued prints, trades & contracts, rescues, shop listings, repair patrols, stockpile returns) and every free logistics carrier is given one category with work, which it keeps until that category runs dry, so rescues, shop visits and repair patrols are no longer starved by a long list of deliveries. Evacuations still go first with every carrier; leftover carriers then help any category, stockpile returns last. The Logistics page shows each request's category and each carrier's. The logistics fleets' repair devices are kept for repair patrols."),
    ("1.237.0", "2026-10-23", "Game v3.6.1. A new blueprint's notification says how it was unlocked (experience, achievement or decommissioning), and the Events feed lists blueprint.unlocked. Decommissioning a device made from components now unlocks the components' blueprints too: the device page and the components warning say so. API reference and game notes updated (the spec itself is unchanged)."),
    ("1.236.0", "2026-10-23", "Cargo rides where the attach points are. Each device type knows its hold and attach points, and a matrix container's one slot is its cradle (never cargo): cargo handed to it goes on the surge platform it is attached to, which flies. Surge plates, platforms and mobile fleets now carry beacons too (the carrier flies to the beacon, attaches it, detaches it at the drop). A platform with a populated matrix container attached counts as a replicant vessel everywhere in deliveries."),
    ("1.235.0", "2026-10-23", "Ordered beacons ride a carrier's multi-drop run with relays, so one vessel with a hold drops several (17 waited for a carrier each). Moving a beacon to a civilization's body no longer picks a vessel whose hold is full or that is itself towed (matrix container 10D7491E failed No storage space every 90 s), and a failed try waits 30 min. The idle-miner restart treats No <resource> remaining at any active site quietly and mines another resource of the belt for 3 h."),
    ("1.234.0", "2026-10-23", "Beacon and relay deliveries move again. A beacon comes out of the printer already monitoring, so the loadout pass took an ordered beacon (tagged to another system) for a placed one and never sent a carrier: 22 sat at the SOLARES and FALQUORYX belts. Relays go first: the logistics pass now leaves the nearest free logistics vessel hosting a replicant to the loadout pass where relays or beacons wait for an escort beyond the network (Logistics page: Kept free to escort)."),
    ("1.233.6", "2026-10-23", "A mining step the game refuses with \"No <resource> remaining at any active site\" is skipped quietly and no longer stops the job with an alert; a bootstrap fleet leaves that resource alone at the belt for 3 h and mines something else (its status says which resources no open site has)."),
    ("1.233.5", "2026-10-23", "Reverted 1.233.3: a bootstrap fleet's home receives materials only when the fleet is set to receiving on the Fleets overview."),
    ("1.233.4", "2026-10-23", "Bootstrap hub and outpost kits include a maintenance drone (tagged to the bootstrap, printed by the hub's autofactory)."),
    ("1.233.3", "2026-10-23", "A bootstrap fleet's home receives materials: stray stockpiles return to it when it's the nearest receiver, supplying fleets and delivered salvage can pick it, and the Logistics backlog counts stock there as received."),
    ("1.233.2", "2026-10-22", "The Prospects page scores stay put again (no stock weighting; it's where wards are chosen); "
     "only a mining fleet picking its next home weighs what waiting prints lack. Drones lean that way too: a gather_evenly "
     "AMI schedule on a mining controller runs maintain_ratios toward the resources waiting prints lack (among what its belt "
     "has), and Restart idle miners starts a drone on the most-needed one. Bootstraps are left out both ways."),
    ("1.233.1", "2026-10-22", "Prospect scores (a mining fleet's next home, bootstrap choices) weigh what waiting prints lack "
     "much more: up to 4× instead of 1.35×, so a belt rich in it beats a richer belt without it."),
    ("1.233.0", "2026-10-22", "System map: working drones orbit the site they work (a survey drone's tracking site, a "
     "miner's last mining event) instead of their location marker; drones with no known site still circle the location."),
    ("1.232.3", "2026-10-22", "README restructured for skimming: a map legend with icons drawn from the map's own "
     "markers (docs/icons, tools/make_legend_icons.py), a pages table and a screenshot grid; the full detail moved to "
     "docs/FEATURES.md in sections, tables and short bullets."),
    ("1.232.2", "2026-10-22", "Dashboard: long replicant names and locations on the Replicants card are cut short with an "
     "ellipsis (hover for the full text) instead of spilling out of the card."),
    ("1.232.1", "2026-10-22", "A mining controller whose salvage ran out ('depleted:recalling') goes back to its belt "
     "with its drones, including drones left recall_waiting (Miner_1 at AEMEROTH sat with 8 idle drones). Repair patrols "
     "go only to systems the relay network covers or a fleet's home, not ones lit by a passing replicant (one was "
     "stranded out of range at ERAJAELAE). A bootstrap fleet's home piles are no longer sent for (Delta Force at ISOLITHOR)."),
    ("1.232.0", "2026-10-22", "Dashboard: cards can be reordered (drag the handle, or the arrows) and folded, remembered per "
     "browser; Achievements moved to the bottom. Logistics backlog adds request age and lead time, carrier use and hold fill, "
     "stalled / auto-retrying missions and expired orders; a new Trends card shows devices by fleet role, mining output per "
     "hour (estimated from pile gains where something mines) and systems you can command / your relay network covers."),
    ("1.231.0", "2026-10-22", "Dashboard: Logistics backlog card, sampled every 10 minutes and kept 30 days (2d / 7d / 30d): "
     "resources not in a receiving stockpile (other piles, logistics holds), devices waiting to be moved (strays, to: / at: "
     "devices not there yet, rescues), and open / unmet logistics requests."),
    ("1.230.0", "2026-10-22", "Galaxy map: another player's hub (a star the catalog flags has_hub with no hub of yours) "
     "shows as the hub node in coral instead of your gold, with its own other hubs checkbox under Icons."),
    ("1.229.0", "2026-10-22", "System map: each working drone orbits its site as a small dot, clockwise; survey drones "
     "orbit farther out, counterclockwise, with a light blue scanning beam aimed at the site (the drones toggle hides them)."),
    ("1.228.1", "2026-10-22", "Hub routing reads the hub list and star positions from the shared catalog lookups instead "
     "of re-reading the whole star catalog for each trip."),
    ("1.228.0", "2026-10-22", "Hub routing uses every hub, not just yours: any star the catalog flags has_hub is tried as "
     "a waypoint on long automated trips (a hub speeds up every ship arriving in its system)."),
    ("1.227.0", "2026-10-22", "Blueprints: each device lists every other field the game sends with a value (relay range, "
     "print queue slots, strength, current hubs, and any field added later), zero and empty ones left out; the full "
     "description opens under the short one"),
    ("1.226.0", "2026-10-22", "Systems: Add a beacon covers every local civilization in the system (one beacon at each "
     "civilization body; the request stays open until all have one, sending a spare or printing the next as needed); "
     "a system with a beacon at one civilization but not another offers Add again"),
    ("1.225.0", "2026-10-22", "Systems and Prospects: remove now deactivates your ward, relay or beacon and marks it spare, "
                              "so a later add elsewhere can send it."),
    ("1.224.0", "2026-10-22", "Galaxy map: relay networks draw only their links, no ring at each relay system "
                              "(rings stay for relays on order, switched off, or in a suggested bridge)."),
    ("1.223.0", "2026-10-22", "Contracts: each contract and quiet civilization shows whether your beacon is at its body "
               "(green), only elsewhere in its system (amber) or missing (red)."),
    ("1.222.0", "2026-10-22", "Console: the release command lists only the devices that controller currently controls."),
    ("1.221.0", "2026-10-22", "Surge off-ramp: a trip to another system arrives at that system's surge spot nearest the "
               "destination (Kuiper, Oort, an L-point) when the game would arrive much further off, instead of cruising "
               "the rest (2D6BFDF1 surged to ISOLITHOR-1-L4, then had 499 AU to cruise to ISOLITHOR-BELT-1)."),
    ("1.220.0", "2026-10-22", "Sector range (the rescue range, renamed): every automated trip longer than a day at the "
               "game's surge pace, or of unknown length, waits for Confirm on the Automations page (one alert). Logistics "
               "carriers only serve stars within the sector range (1728 ly): a far sector gets its own logistics fleet. "
               "Cause: freighter 2D6BFDF1 sent from SOLARES on a 241 h trip to fetch the ISOLITHOR stockpile."),
    ("1.219.0", "2026-10-22", "Repair patrols: logistics carriers take the logistics fleets' own repair devices to the "
               "most worn systems that lack one (one per six worn devices, most missing capacity first), drop them at the "
               "worst device and set them to patrol. Trips over a day wait for Confirm, like rescues."),
    ("1.218.0", "2026-10-22", "Stalled fleet missions retry by themselves: the stalled phase runs again after 15 min, then "
               "30 min, 1 h … up to 6 h apart, eight tries in all, before waiting for Retry. A stall at a new spot starts "
               "over; only the first stall at a spot notifies. The fleet card shows the next try; comms-range stalls keep "
               "resuming once back in range."),
    ("1.217.0", "2026-10-22", "Galaxy map relay networks: each network has its own color (a new palette clear of the "
               "hazard red, resource orange and anomaly magenta), and a hub's links are a darker shade of it."),
    ("1.216.3", "2026-10-22", "Surge hops go in the trip's own `via` (one travel command, as the game takes it) instead of "
               "separate trips: the nearest surge spot when leaving from a belt or a body, then (inside a system) the spot "
               "nearest the destination, before any hub waypoints. A refused `via` takes the game's own route."),
    ("1.216.2", "2026-10-22", "Surge hops on departures too: a surge-capable device at a belt or a body first cruises to the "
               "nearest surge spot (Oort, Kuiper, entry point, a planet's L4/L5) when the game would cruise much further — "
               "leaving a system it cruises to the entry point (ISOLITHOR-BELT-1 → ISOBAREA: 3544 s, by the Kuiper belt "
               "515 s). Inside a system it then hops on from there."),
    ("1.216.1", "2026-10-22", "A bootstrap takes every device of its own aboard before it leaves for a new home (one set on "
               "its card) or the hub: before, a new home left the drones at the old one, and the hub move checked only the "
               "first device it found. A vessel already at its new home doesn't fly back for strays."),
    ("1.216.0", "2026-10-22", "Desktop wallpaper: every Galaxy page layer can be switched on or off (layers=, grouped as "
     "on the Galaxy page in the Octos add-on 1.5.0), and follow= keeps the galaxy centered on a replicant as it travels "
     "(the system view shows the system it's in). The galaxy map data names the device each replicant rides in."),
    ("1.215.0", "2026-10-22", "Logistics batches deals at one location: when several approved trades or contracts wait at "
     "the same place, the stock there pays the first ones and a single delivery brings what all the others lack (as far as "
     "the carriers hold); its replicant then fulfills them all. The others show as delivered together with it."),
    ("1.214.0", "2026-10-22", "Surge hops: a trip inside a system by a surge-capable device standing at a surge spot (Oort, "
               "Kuiper, entry point, a planet's L4/L5) hops first to the spot nearest the destination when that saves at "
               "least 30 AU (and a quarter of the cruise), then cruises the rest. A refused hop cruises as planned."),
    ("1.213.0", "2026-10-22", "A bootstrap's home can be set on its card (until the hub is chosen): it starts over there, "
               "compounding at that system's belt. Choosing the hub moves the home to it. Explore fleets without a survey "
               "controller send a drone parked at a Lagrange point to the belt (or a body) before it scans: the game refuses "
               "a survey from there."),
    ("1.212.1", "2026-10-22", "An explore fleet without a survey controller scans with its own survey drones again: a drone "
               "with no controller was taken for the (missing) controller's, so the work phase found none and every system "
               "was marked surveyed without a drone scan (Delta Force). A bootstrap fleet runs no missions: one left over is stopped "
               "on its next pass, and the mission runner skips bootstraps."),
    ("1.212.0", "2026-10-22", "Galaxy map layers are grouped by how they draw: view, clouds and glows, rings and waves, "
               "lines, icons. Every toggle and its saved setting stay as they were."),
    ("1.211.0", "2026-10-22", "Bootstrap hub and outpost choices weigh asteroid risk: a system with an incoming object "
               "now is never offered; each past asteroid there (up to 3) costs 10 points and hazard system tags 15, and the "
               "reasons say so. The hub's autofactory isn't printed while an object heads for the hub."),
    ("1.210.2", "2026-10-22", "Converting a fleet to a bootstrap from its Save row shows why when it can't (the message "
               "was swallowed), stops a running mission instead of refusing, and finds the heaven vessel when the replicant's "
               "matrix is deployed on its own. A bootstrap first takes a loose matrix of its own aboard, so the vessel never "
               "flies off out of command range. A rescue job is canceled when its device is in command range again (or "
               "joined a bootstrap) before the rescue moved it: the DELTA heaven vessel was still due to fly home to BADU."),
    ("1.210.1", "2026-10-22", "A rescue to a star whose position the app doesn't know (DELTA) waits for your Confirm too: "
               "its distance counted as nothing before, so it went without asking."),
    ("1.210.0", "2026-10-22", "Bootstrap is a fleet role: pick it in a fleet's role select and Save to convert the fleet "
               "(the Convert button is gone); Back to a normal fleet takes the role to return to. The Fleets overview shows a "
               "bootstrap's stage. Logistics never rescues a bootstrap's devices (out of range on purpose: drones left mining "
               "while the vessel surveys; seen at DELTA). The home kit counts what else is aboard (matrix, slingshot) against "
               "the hold, so no drones are left behind at the move."),
    ("1.209.3", "2026-10-22", "The system page shows an object (STAR-OBJ-n) only while it exists: an asteroid still "
               "open on the Defense page, or a structure in the latest scan. Past asteroids stay in Defense › Concluded."),
    ("1.209.2", "2026-10-22", "Beacons with a destination (to: / at:, a fleet or a contract), e.g. printed beacons waiting "
               "at a belt for their civilization's system, are no longer marked redundant and spare. A working fleetless "
               "beacon the civ beacon pass marks spare keeps the tag; before, that pass and Loadouts flipped it every few minutes."),
    ("1.209.1", "2026-10-22", "A stowed slingshot is no longer offered for firing (the game answers \"Slingshot must be "
               "deployed\"): the replicant page says which vessel holds it and to deploy it first."),
    ("1.209.0", "2026-10-22", "A logistics rescue with more than a day of travel waits for your Confirm on Fleets › "
               "Logistics (one alert; the OK holds two days). An asteroid whose name a former rock used is no longer "
               "closed by that rock's old impact event (FALQUORYX-OBJ-2 went unread and unevacuated). Propulsors still "
               "pushing leave 45 minutes before the impact (was 30; a Defense rule setting). A fleet mission stalled "
               "on 'out of comms range' resumes by itself once its members are back in range."),
    ("1.208.1", "2026-10-22", "A slingshot stowed in a vessel counts as being where the vessel is: firing it with the "
               "replicant (or its matrix) in the same hold, and linking it to a matrix in that hold, now work."),
    ("1.208.0", "2026-10-22", "Bill's BobNet move post no longer sends a replicant after him: it stops an ongoing follow, "
               "and following his trail is started by hand on the Trail page only. Get listing on his shop is a "
               "logistics job like any other shop's (after a move, once his shop is seen again)."),
    ("1.207.1", "2026-10-22", "Asteroid mining: of several survey drones searching a belt, the one whose search started last "
               "goes to scan the rock."),
    ("1.207.0", "2026-10-22", "Device scans on arrival (and the traffic poll) keep the equipment lockers they see: the "
     "Lockers and System pages list them with their code (new ones too), and a row's Retrieve uses that code directly."),
    ("1.206.1", "2026-10-22", "Asteroid mining: the scanning drone is an idle survey drone, else one searching a belt for a "
               "new site (called off first); never one tracking a site."),
    ("1.206.0", "2026-10-22", "Console: presets for the rest of the live API (webhook, overview, replicant profile, "
               "scan, mine, print, transfer, teleport, cancel travel, relay posts, permissions, trades, research, simulations, "
               "location events, megastructure contributions, all leaderboards, blog posts), each with a description."),
    ("1.205.2", "2026-10-22", "Asteroid mining: a survey drone stays at a rock being mined and scans it every 30 minutes (a "
               "setting) until the cutoff, instead of a visit every 6 hours."),
    ("1.205.1", "2026-10-22", "Asteroid mining: a rig is sent only when, after the trip (the game's travel preview), an hour of "
               "mining (a setting) is left before the one-hour cutoff."),
    ("1.205.0", "2026-10-22", "Asteroid mining rule (game v3.6.0, off by default, on Map › Defense): fracture rigs mine "
               "asteroids one resource at a time in your priority order, idle rigs in the system go to a rock with a free "
               "slot, rocks you launched keep a share of each resource for their terraform effect, a survey drone scans "
               "rocks for exact amounts, and the stockpile is hauled out while they mine. A diverted or off-target rock "
               "now recalls only the propulsors early; rigs keep mining until the hour before the ETA."),
    ("1.204.0", "2026-10-22", "Console: every preset has a short description, and a new Lockers group finds equipment "
               "lockers and retrieves one (POST /devices/{locker}/retrieve). Duplicate presets removed."),
    ("1.202.0", "2026-10-22", "Game v3.6.0: an off_target asteroid (system.object_off_target) counts as diverted, so no more "
               "propulsors are sent or printed for it, and its devices are evacuated (it is flung out at the ETA; before, "
               "evacuation skipped it). Defense shows the resources left on an asteroid; impact alerts give the "
               "description and say when a mined-out rock had no terraform effect."),
    ("1.201.1", "2026-10-22", "Lockers: the Retrieve-by-code card from 1.200.0 is gone (the row's Retrieve does it)."),
    ("1.201.0", "2026-10-22", "Lockers: once a locker's price is all on site, its row offers Retrieve instead of Deliver; "
     "it scans from your replicant at the locker and retrieves."),
    ("1.200.0", "2026-10-22", "Lockers: retrieve straight from a locker's code (the docs' POST /devices/{code}/retrieve); "
     "the scan for lockers reads every page and falls back to an unfiltered scan."),
    ("1.199.0", "2026-10-21", "A device pinned to a spot (at: tag, e.g. a locker's price) is dropped right at that spot by "
     "its carrier, not at the system's entry point; one that can't fly and sits elsewhere in its pin's system is "
     "fetched by a carrier (3 compute cores for the TWAFFY-OORT locker were left at TWAFFY-11-L4)."),
    ("1.198.0", "2026-10-21", "Galaxy map: each galactic region gets its own background color, a soft glow round "
     "its stars. The region the view is centered on has none; the others fade in with their distance from the view's "
     "center. A region colors checkbox under layers turns it off."),
    ("1.197.0", "2026-10-21", "Extra relays are marked spare before delivery: a relay waiting to go to a system where "
     "one of yours already relays, or a second one bound for the same system, loses its to: tag and becomes spare "
     "where it is. One already aboard a carrier or on a running job keeps its trip and is spared on arrival."),
    ("1.196.0", "2026-10-21", "Relay, ward and beacon orders never print a second one for a system when a print "
     "tagged for it is already printing or waiting in a factory queue. An extra relay idle in a system where one of "
     "yours already relays is marked spare, so the next order sends it on."),
    ("1.195.0", "2026-10-21", "Relay deliveries: a carrier fills its hold with relays for several systems even when "
     "another replicant printed them (they are handed over as they board); before, those went one per trip and many "
     "orders expired. A new relay, ward or beacon order sends one already tagged for that system instead of printing "
     "another. Diagnostics snapshots list every device past 500, plus the relay orders and rollout state."),
    ("1.194.0", "2026-10-21", "Fleet missions: a carrier still outside the target system with the crew aboard "
     "(the trip there did not take it) flies again before unloading or working, instead of stalling on \"the survey "
     "controller is in another system\" at every resume; it stalls with the reason after two more tries."),
    ("1.193.0", "2026-10-21", "Survey missions: a member too far to cruise to its work spot (over the Fleets › settings "
     "limit) rides the fleet's carrier there instead of stalling, and a survey crew is never dropped at the Kuiper belt "
     "or Oort cloud: with nothing known inside the system yet it stays aboard until the work phase takes it in."),
    ("1.192.0", "2026-10-21", "Lighter engine pass: lookups over the star catalog (positions, wards, place tags, "
     "prospecting) are built once per catalog update instead of copying the whole catalog several times a pass, and the "
     "season copy (megabytes) is no longer read every pass just to check its age. A pass on a 42,000-star catalog went "
     "from 1.6 s to 0.4 s in testing, so pages wait less behind it."),
    ("1.191.0", "2026-10-21", "A spare given a destination is no longer spare: pinning one (locker delivery, components, "
     "simulation prep) drops its spare and stock tags, and the loadout pass drops the spare tag from any device with a "
     "to: or at: pin that is not just being gathered at the spare depot."),
    ("1.190.0", "2026-10-21", "Faster Fleets pages: the loadout plan, prospect rankings and destination lists are kept "
     "for an hour instead of recomputed on every load (Fleet details took 31 s live); a fleet or loadout edit or the "
     "Recompute button refreshes them."),
    ("1.189.0", "2026-10-21", "A device's page says what it's doing: a fleet member shows its fleet's status (mission, "
     "phase and latest step, stationed, relocating), a logistics vessel its current task and step, any device the "
     "automation job working it."),
    ("1.188.0", "2026-10-21", "Strays: members away from their stationed fleet's home are listed on the Fleets page; "
     "one riding another fleet's idle carrier is let off for the loadout pass to bring home, one stuck away an hour is "
     "reported once, and a move to a new home stays open (and is reported) until every member is there."),
    ("1.187.0", "2026-10-21", "Fleet members stay with their fleet: an idle logistics carrier takes another fleet's "
     "members home instead of dropping them at a receiving point, and any carrier lets off other fleets' members it "
     "would carry away before it leaves on a trip."),
    ("1.186.2", "2026-10-21", "The logistics drop-off never lets off a replicant: matrices, matrix containers (the "
     "cradle feature), vessels hosting a replicant, and anything with a matrix inside stay aboard."),
    ("1.186.1", "2026-10-21", "Auto survey on arrival leaves a fleet's vessel and its passengers alone away from the "
     "fleet's station (Surveyor 1's heaven vessel, flown into PHEVENIA, had its survey controller and a drone deployed "
     "and sent off while the fleet was boarding them)."),
    ("1.186.0", "2026-10-21", "Fleet missions board anyone not aboard before every leg and before the trip home, not "
     "only before the first departure (a survey fleet left AINELRAMU for PHEVENIA without its survey controller, which "
     "the recall had withdrawn but never boarded)."),
    ("1.185.0", "2026-10-21", "A fleet's new home no longer waits for a complete loadout: it moves now and the loadout "
     "pass fills it there. A survey fleet still collects every member it has first, from other systems too. A new home "
     "stays pending only while nothing could be commanded there."),
    ("1.184.0", "2026-10-21", "Galaxy map: markers from the system scans' tags, an orange gem (lower left) for resource rich "
     "and a magenta spiral (upper right) for an anomaly, each with its own checkbox; a star's info lists all its tags "
     "(hazardous ones in red)."),
    ("1.183.0", "2026-10-21", "Mining fleets moving to a new home no longer assemble first: their own carriers and the "
     "logistics fleets' each load as many members as they hold and leave as they fill. Survey fleets still collect every "
     "member, from other systems too, and move together."),
    ("1.182.0", "2026-10-21", "Systems page: a System tags section lists every tag in the cached system scans with the "
     "systems carrying it, most common first; hazardous tags in red."),
    ("1.181.1", "2026-10-21", "Galaxy map: the hazard haze also marks systems whose scan tags them hazardous (the tags "
     "shown next to a system page's name); the star's info lists those tags."),
    ("1.181.0", "2026-10-20", "Galaxy map: a red haze pulses around systems an incoming object still threatens (the object's "
     "system and its impact target's, from Defence), with its own hazards checkbox; the star's info names the object and "
     "the time to impact."),
    ("1.180.4", "2026-10-20", "Logistics no longer tries to fulfill a contract or trade while part of its price is still on "
     "its way (Construction Boom at PERHALIEL-4 failed every pass with Event criteria not met): it waits for the deliveries. "
     "Loadouts send only a vessel hosting a replicant to pick up devices in a system outside the relay network, also when "
     "the destination is covered (a surge platform flew alone into SOL for a gas separator and was stranded there)."),
    ("1.180.3", "2026-10-20", "Fleets: the Templates grid spans the full page width (Settings below it). Blueprints: the Plan "
     "card sits above a full-width blueprint list instead of beside it, so the list no longer ends up squeezed under it on a phone."),
    ("1.180.2", "2026-10-20", "Logistics no longer rescues a device out of command range when its at: tag names the spot "
     "where it is: it was stocked there on purpose (compute cores at MIRFAKA-OBJ-1 sent a heaven vessel to fetch them)."),
    ("1.180.1", "2026-10-20", "Prints the app queues to fill a locker's price, a simulation's cores or missing components are "
     "spread over the autofactories at the chosen stockpile, evenly by print time, instead of all going on one of them."),
    ("1.180.0", "2026-10-20", "Stars: one route-from dropdown of replicants above the list and a preview button on each star, "
     "instead of a button per replicant on every row. Demo galaxy: systems answer scans (planets, a belt) and another "
     "player's outpost shows up; the System screenshot shows ELNYXOTH."),
    ("1.179.0", "2026-10-20", "Demo galaxy for the mock server (RS_MOCK_DEMO=1): about 2,100 real star positions, 44 systems, "
     "500 devices and 18 replicants shaped like a live account, with made-up names and codes "
     "(tools/make_demo_galaxy.py builds it from a snapshot). The screenshots in docs/screens now show it."),
    ("1.178.0", "2026-10-20", "Screenshots of every page in docs/screens, shown in the README (taken from the mock game "
     "server); tools/screenshots.py takes them again."),
    ("1.177.0", "2026-10-20", "Story card: the season's likely end (the latest end date among storylines and events, "
     "2026-10-31 for season 3) with days left; the game's own season end date wins if it ever sends one."),
    ("1.176.0", "2026-10-20", "Live API spec checked (one new endpoint, GET /overview, already read; repo openapi.json "
     "refreshed). Story card: shows each item's end date, event reward, its list of stars or datacenters and the Ark's stop."),
    ("1.175.0", "2026-10-20", "Shop list: Get listing reads a shop's trades now when a replicant of yours is in its "
     "system (the game answers only then), else logistics sends a device hosting a replicant there at the lowest "
     "priority (new tier 6, shop listing) and the trades are read once it arrives. For Bill's shop it follows his trail "
     "once from SOL and reads the trades when the shop is found."),
    ("1.174.0", "2026-10-20", "Refresh traders says what it found: how many replicants it asked, how many traders the "
     "game listed, and any errors (it used to show nothing when the directory was empty). The traders table shows the "
     "last directory read and what the game's directory covers. A shop whose trades can't be read shows its trade "
     "count and stock, and a directory entry without a location (Bill's) keeps the last known one."),
    ("1.173.0", "2026-10-20", "Device scan on arrival (new rule, on by default): a replicant's vessel arriving in another "
     "system scans its devices at once, not at the next traffic poll. Every device scan now adds other players' trade "
     "controllers to the shops list, with their trades, since system scans don't list shops."),
    ("1.172.0", "2026-10-20", "New page Map › Terraforming candidates: surveyed planets and moons rated for a species "
     "(picker), ranked by the estimated time to habitable with a kit of n devices of each kind, showing each attribute's "
     "gap, the devices it takes, the distance to your nearest autofactory and whether the system is commandable (both "
     "as filters). Gravity out of range, gas giants and intelligent natives are dropped. Read surveyed bodies reads "
     "their details (25 a click, again after a week). README caveats updated."),
    ("1.171.0", "2026-10-20", "Shops stay listed until you remove them (Remove on the traders table, after you checked): "
     "a failed trade, a trader refresh that no longer lists a shop out of reach, or a system scan without it (it said "
     "shops: [] at ROSALIATE with a shop there) no longer drops it. Shops with no trades read are listed too. A BobNet post (X is open "
     "for shoppers! ... Find me in the Oort) moves that owner's shop to the poster's system (STAR-OORT), or lists it "
     "until its controller is seen. Bill is the exception: closed from his BobNet move post until seen again."),
    ("1.170.0", "2026-10-20", "Galaxy map: the dashed routes of ships in transit and of fleets move toward where they are "
     "going (still when the system asks for reduced motion). Fixes from a code review: lookbacks over game events (devices "
     "given away in trades, canceled prints) compare real times, not text in the game's -04:00 offset, which cut 4 hours "
     "off each window; two unused reads dropped and dead code cleaned up."),
    ("1.169.2", "2026-10-20", "Fixes from the 17:08 snapshot alerts: an attach that finds the device on another carrier "
     "has that carrier detach it (a deploy only frees stowed devices); cargo on a carrier a running job holds waits for "
     "it; a delivery that failed twice sits out 6 hours; no return trip for a pile an AMI ferry already moves, and a "
     "return load takes what is left; an unescorted trip does not leave for a system gone out of comms range; ETAs in "
     "the log are real UTC."),
    ("1.169.1", "2026-10-20", "Logistics: an unmet request says where it loads and where it goes, how many cargo devices "
     "are on jobs, moving or taken by higher requests, and why the nearest free one was passed over; a rescue lists each "
     "vessel with a replicant aboard and what keeps it."),
    ("1.169.0", "2026-10-20", "An approved trade holds only the nearest logistics vessel with a replicant aboard (it held "
     "them all, so fleet carriers towing a replicant sat out relay runs and rescues). A device's page offers attach to a "
     "carrier at its location, stowable devices included."),
    ("1.168.0", "2026-10-20", "Blueprints: each device says how it travels between systems (flies itself, stowed, "
     "attached, or compacted then attached) and what it carries (hold and attach slots, cargo)."),
    ("1.167.3", "2026-10-20", "Notes only: travel via a waypoint to a body in another system is confirmed live (the "
     "game flies the final leg once the destination star is the last waypoint)."),
    ("1.167.2", "2026-10-20", "Logistics: a priority you pick shows at once (it used to wait for the next pass, so the "
     "reload looked like it was cleared), the menu keeps it selected, and evacuation and rescue are accepted too."),
    ("1.167.1", "2026-10-20", "A device reserved for a system (relays above all) loses its stock tag along with spare; "
     "the stock pass also drops it from devices already taken for a fleet or a system."),
    ("1.167.0", "2026-10-20", "Travel via a waypoint (by hand or a hub route) adds the destination's star when the "
     "destination is a body in another system, as the game requires. Logistics fly a carrier that tows a replicant's "
     "matrix container by the carrier's own travel (the replicant's travel moved the container, which can't surge). AMI "
     "schedules skip controllers out of comms range. Diagnostics snapshots include the logistics state and jobs."),
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
