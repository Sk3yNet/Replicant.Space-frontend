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
- An FTL relay reaches 7.5 ly and a system hub 15 ly (blueprints report the relay range since 2026-10-12). Devices out of range of a relay, with no replicant in their system,
  can't be commanded. The game allows 25 wards per account, and wards and hubs don't share a system.
- The home sector (FALQUORYX, OTHILETH …) is about 500 ly from the Sol zone (SOLARES, FRANZAN …). A surge across that
  gap takes hours, and borrowed carriers must not fly it empty (loadout supply range: 100 ly).
- Trades and contracts paid in devices need them deployed at the exact location, with a replicant there. Bill
  (A8F48B26) runs The Skunkworks shop; it moves, probably about daily (NOSOXA-OORT on 2026-10-11), his route always
  passes through SOL (leaving his shop he can't be followed: he resets to SOL), and he announces moves on BobNet #general ("NOSOXA is in the rear-view mirror"). His shop is his AMI trade controller
  (3BF0ED09 on 2026-10-13, codes may change); a racing vessel and an FTL relay travel with it (all at ALGENIBUS-OORT then).
- Devices travel stowed (in a hold), attached (towed: the same thing), or compacted and then attached. Cargo freighters
  can't be: "Cannot attach a large device" (live 2026-10-12), so they fly themselves. Among replicant vessels only the cargo vessel can attach devices; the others fly alongside. A typical
  fleet carrier holds a matrix container with the replicant's matrix inside, so the carrier brings that replicant along.
- Contracts are approved one by one, or pre-approved for a whole civilization. Shop listings are never fulfilled unless
  you approve that one trade.
- Since v3.5.0 (2026-10-13) anything left at an asteroid's location is destroyed on impact or lost on diversion; the
  game warns an hour ahead (`system.impact_warning`). Evacuate before the ETA. The warning names no asteroid: its
  location is where your devices are (KELMORNEA-4-L4 on 2026-10-15); asteroids are named by their STAR-OBJ-n location.
  Diversions never reach 0 % likelihood (1 % counts as diverted enough). An event is over once its ETA passes.
- Relays never fold (hubs do, and only work at L4/L5). A relay left switched off out of everyone's reach can't be
  commanded: switch it on before the carrier leaves. Out of reach the game lists a relay `out_of_range` whether it's on
  or off. A hub links to relays only within the relay's own 7.5 ly (live list from the SOLARES hub, 2026-10-15).
- Simulations run at the datacenter (MIRFAKA-OBJ-1) through its replicant interface, a device owned by Bill (F4791EDF on
  2026-10-15): scan for devices from a replicant there and use that code in `/v1/devices/:interface_code/simulate`.
  The replicant entering a run becomes the interface to a virtual galaxy: every simulated system is named VIRT… (region
  virt6), and its devices (ordinary hex codes) show in the account's `/devices`. The app keeps all of it apart
  (`sim:devices`, `sim:inventory`, `sim:stars`, `sim:systems`, events with category `simulation`).
- Replicant vessels accept print orders: never queue prints on a vessel unless asked.
- Game messages announcing salvage say "Salvage discovered:"; their field names vary (`id`/`message_id`, `read`/`is_read`).
- Fleets never leave with a partial loadout. Long trips wait on the game's ETA, and a canceled or misrouted trip stops the
  job.

## Writing character texts (replicant profiles)

Name ≤ 60 characters with underscores (no spaces or dashes). Description and Plan ≤ 500 characters each, Project ≤ 2000. The
text is rendered as HTML, so use no quotes, apostrophes, ampersands or angle brackets: spell contractions out and
introduce speech with "Says:".
