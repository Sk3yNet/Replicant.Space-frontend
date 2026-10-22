# Working on Replicant.Space-frontend

Self-hosted web client and automation engine for the game Replicant Space (FastAPI + htmx + Jinja2, aiosqlite kv store).
Code is in `app/rsweb`, tests in `app/tests`, docs in `README.md` and `API_REFERENCE.md`. The owner deploys with
Portainer, which follows `main`: **a push to `main` is a deploy.**

## Standing rules from the owner

- **Plan or build: your call.** Build straight away when a request is concrete and its behavior is clear. Plan first,
  briefly, when the result depends on game facts or preferences you can't know. Lead the plan with your assumptions and
  the questions only the owner can answer, all in one batch. Don't propose and then wait on small, clear changes.
- **Ship to `main` when done.** There is no test server: once a change is built and the full suite passes, run the
  deploy checklist below and push to `main` without waiting to be told.
- **"Deploy" means everything outstanding.** Ship every uncommitted change in the repo (code, docs, CLAUDE.md), not
  just the latest one, following the checklist below.
- **Save tokens.** Read only the code you need, don't repeat work already done, and keep replies short and concrete.
  The owner is rationing usage across the week. Each step re-sends the whole conversation, so: suggest a fresh session
  after a deploy; ask one short question before building on an unconfirmed game fact; search (`grep -n`, short `sed -n`
  ranges) rather than read big files; read snapshots with scripts that print only what's needed.
- **American spelling** in all text (UI, logs, comments, docs). The game's own names stay as the API spells them:
  the `travel.cancelled` event, the `travelling` status, the `/defence` URL, identifiers.

## Deploy checklist (on "deploy")

1. Bump `VERSION` in `app/rsweb/version.py` and add a `CHANGES` entry (newest first, today's date, a short summary).
2. Run the full suite and require it to pass:
   `cd app && python -m pytest -q > /tmp/claude-0/all.log 2>&1; rc=$?` (rc must be 0).
3. Update `README.md` when behavior or pages changed.
4. Commit, then `git push -q origin main`. End the commit message with:

   ```
   Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
   Claude-Session: https://claude.ai/code/session_01QkRgNdiSgaWDkDuW8ndNLw
   ```

## Testing

- `cd app && python -m pytest -q` (the full suite takes about a minute). Tests use the mock game server in `rsweb/mock.py`.
- Run just the tests you touched while working (`-k name`), and the full suite before saying something is done.
- To look at a page: start `uvicorn rsweb.mock:app --port 9000` and the app with
  `DB_PATH=<scratch>/dev.sqlite RS_API_BASE=http://127.0.0.1:9000/v1 RS_API_TOKEN=dev DEV_USER=you@example.com uvicorn rsweb.main:app --port 8000`.
  Use Playwright with `executable_path="/opt/pw-browsers/chromium"`. Stop the servers by PID, never `pkill -f`,
  which matches your own shell.

## Reading a diagnostics snapshot

Read uploads with `python3 -I` from a scratch script. Use `app.devices` for the device list (every page merged).
`calls` holds the raw GETs, and `/devices` is split over several pages there, so never judge from one page. Under
`app` you also find jobs, log, actions (last 60 commands), fleets, loadouts, loadouts_last and ward_orders. A snapshot
from before 1.69.0 has no `app.devices`: merge every `/devices` call yourself.

## Game facts learned live (keep them in mind)

- A new start, or a teleport to Sol, gives a heaven vessel with three mining drones and a replicant matrix. There's no
  autofactory and no stock. Sol is warded.
- Mining feeds the location's stockpile. Only transport-feature devices move materials (haulers in a system, cargo
  freighters between systems); a heaven vessel carries devices only.
- An FTL relay links to other relays within 7.5 ly and a system hub within 15 ly (blueprints report the relay range since
  2026-10-12). That range is only relay-to-relay: a relay or hub delivers commands to its own system alone. A system is
  commandable only with a relay or hub of a network that has a replicant in one of its systems, or with a replicant in it.
  The game allows 25 wards per account, and wards and hubs don't share a system.
- The home sector (FALQUORYX, OTHILETH …) is about 500 ly from the Sol zone (SOLARES, FRANZAN …). A surge across that
  gap takes hours, and borrowed carriers must not fly it empty (loadout supply range: 100 ly).
- Trades and contracts paid in devices need them deployed at the exact location, with a replicant there. Bill
  (A8F48B26) runs The Skunkworks shop; it moves, probably about daily (NOSOXA-OORT on 2026-10-11), his route always
  passes through SOL (leaving his shop he can't be followed: he resets to SOL), and he announces moves on BobNet #general ("NOSOXA is in the rear-view mirror"). His shop is his AMI trade controller
  (3BF0ED09 on 2026-10-13, codes may change); a racing vessel and an FTL relay travel with it (all at ALGENIBUS-OORT then).
- Devices travel stowed (in a hold), attached (towed: the same thing), or compacted and then attached. Cargo freighters
  can't be: "Cannot attach a large device" (live 2026-10-12), so they fly themselves. Stowable devices attach too (a mobile fleet
  attached an FTL relay, live 2026-10-20); the carrier sends `attach`, the cargo's own commands don't list it. Among replicant vessels only the cargo vessel can attach devices; the others fly alongside. A typical
  fleet carrier holds a matrix container with the replicant's matrix inside, so the carrier brings that replicant along.
- Contracts are approved one by one, or pre-approved for a whole civilization. Shop listings are never fulfilled unless
  you approve that one trade.
- Since v3.5.0 (2026-10-13) anything left at an asteroid's location is destroyed on impact or lost on diversion; the
  game warns an hour ahead (`system.impact_warning`). Evacuate before the ETA. The warning names no asteroid: its
  location is where your devices are (KELMORNEA-4-L4 on 2026-10-15); asteroids are named by their STAR-OBJ-n location.
  Diversions never reach 0 % likelihood (1 % counts as diverted enough). An event is over once its ETA passes.
- Devices' `in_control_range` is the game's word on comms: `relaynet.dark_stars` overrides the model with it
  (RHYBRENEUS, 2026-10-18: 18 logistics devices stranded `out_of_range` when the app still counted relay reach as coverage).
- Relays never fold (hubs do, and only work at L4/L5). A relay left switched off out of everyone's reach can't be
  commanded: switch it on before the carrier leaves. Out of reach the game lists a relay `out_of_range` whether it's on
  or off. A hub links to relays only within the relay's own 7.5 ly (live list from the SOLARES hub, 2026-10-15).
- Simulations run at the datacenter (MIRFAKA-OBJ-1) through its replicant interface, a device owned by Bill (F4791EDF on
  2026-10-15): scan for devices from a replicant there and use that code in `/v1/devices/:interface_code/simulate`.
  The replicant entering a run becomes the interface to a virtual galaxy: every simulated system is named VIRT… (region
  virt6), and its devices (ordinary hex codes) show in the account's `/devices`. The app keeps all of it apart
  (`sim:devices`, `sim:inventory`, `sim:stars`, `sim:systems`, events with category `simulation`).
- The terraforming training (live 2026-10-17, VIRTCELRENETH) gives a racing vessel (a survey drone stowed), one
  terraform monitor, 6 orbital mirrors, 6 orbital shades, 3 each of the other terraform devices and one atmospheric
  regulator, all idle at the planet (VIRTCELRENETH-1), commanded by device code (activate / deactivate). The regulator
  (status `regulating`) can be switched off again; while on it holds temperature, pressure and hydrosphere still, while
  device-driven oxygen and biosphere keep moving. A tick is 30 s; an active device's status is `terraforming`, the monitor's
  `monitoring`, and the monitor carries its latest report in the device list (`terraform_status`). Effects are reported
  as words (negligible, weak, moderate). Settings work: PATCH `{"configuration": {"settings": {"strength": 1.0,
  "direction": "decrease"}}}` answers with the device's `settings`. The monitor's warnings name the habitable limits
  (temperature > 350, oxygen < 10, toxicity > 20 suppress the biosphere). `GET /species` gives each species' habitable ranges in
  `environment` (temperature, pressure, oxygen, toxicity, hydrosphere, biosphere, tectonic, gravity); the training is for
  Humans (270–300 K, 0.8–1.2 atm, O2 18–25, toxicity 0–5, hydrosphere 40–70, biosphere 30–70, tectonic 5–30). The
  docs' 200–350 K etc. are only the bio seeder preconditions. Ideas not built yet go in TODO.md.
- Arriving in a system with your own hub is 25 % faster (37.5 s/ly instead of 50 on surges, live 2026-10-18); leaving one
  gains nothing, and the game's routing never picks a hub: `travel` with `via: ["HUB_STAR"]` does (`hubroute.py`). A `via` ending in
  another system than a body destination needs that system's star last (`via: ["SOLARES", "SOL"]` to SOL-3-1; the game
  then flies the final leg to the body, live 2026-10-20); the app adds it (`hubroute.waypoints`).
- Surge hop (game docs, owner 2026-10-22): inside a system a surge-capable device hops in seconds between surge-capable
  places (Oort, Kuiper, the entry point, a planet's L4/L5), then cruises the rest. A plain `travel` from an L-point to a
  belt cruises all the way (499 AU, 57 min at ISOLITHOR); the job runner puts the hop in first (`_hop_first`).
  Leaving a system, the game cruises to the entry point before surging; a surge from the Kuiper belt works too
  (ISOLITHOR-BELT-1 → ISOBAREA: 3544 s by the entry point, 515 s by ISOLITHOR-KUIPER), so the runner cruises there first.
- An active propulsor must be deactivated before it can travel (recall after a diversion: deactivate, then travel).
- Some blueprints need component devices (blueprint `components`, e.g. biosphere cultivator: 2 hydroponic bays, 1 nutrient
  synthesizer, 1 orbital farm). Without them the factory sits `waiting_for_resources` with
  `waiting_for.components: {type: {have, need}}` (live 2026-10-18, 9E13498F at SOLARES-BELT-1).
  A climate processor needs a terraform monitor. A locked blueprint ("Blueprint not yet unlocked") is unlocked by
  decommissioning a device of that type (the terraform monitor, 2026-10-19).
- v3.6.0 (2026-10-21): asteroids (STAR-OBJ-n) are resource sites, mined only by `fracture` devices (mining rigs) with
  `start_mining`; slots small 1 / medium 2 / large 5, shared by all players. The location lists `resource_sites` with
  `resources_remaining_pct`; a survey drone's scan there gives exact amounts. A mined-out rock has no terraform effect.
  A diverted rock turns `off_target` (`system.object_off_target`) and stays minable until it's flung out at the ETA.
- Seasons (`GET /overview`, Dashboard › Story) end in a time skip: devices may move, location events end, asteroids
  drift away, AMI directives pause. Season 3 (from v3.5.3) is Alteration Protocol.
- Replicant vessels accept print orders: never queue prints on a vessel unless asked.
- Game messages announcing salvage say "Salvage discovered:"; their field names vary (`id`/`message_id`, `read`/`is_read`).
- Owner's plan (2026-10-19): relays go first (Relay rollout; any logistics vessel with a replicant may escort relays
  beyond the network); mining stays inside the network, and no automatic assignment goes outside it; survey fleets
  work at the edge. The owner reassigns replicants and attaches matrices by hand: don't automate those.
- Following Bill's trail starts only by hand on the Trail page (owner 2026-10-22): no automation (his BobNet move post,
  Get listing) pulls a replicant off its job to follow him; his shop's listing or trades go to a logistics fleet member.
- Fleets never leave on a mission with a partial loadout, but a new home never waits for one (owner 2026-10-21): mining
  fleets trickle over as carriers fill, survey fleets collect every member first and move together. Long trips wait on the game's ETA, and a canceled or misrouted trip stops the
  job.

## Writing character texts (replicant profiles)

Name ≤ 60 characters with underscores (no spaces or dashes). Description and Plan ≤ 500 characters each, Project ≤ 2000. The
text is rendered as HTML, so use no quotes, apostrophes, ampersands or angle brackets: spell contractions out and
introduce speech with "Says:".
