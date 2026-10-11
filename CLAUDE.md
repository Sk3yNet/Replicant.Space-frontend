# Working on Replicant.Space-frontend

Self-hosted web client and automation engine for the game Replicant Space (FastAPI + htmx + Jinja2, aiosqlite kv store).
Code is in `app/rsweb`, tests in `app/tests`, docs in `README.md` and `API_REFERENCE.md`. The owner deploys with
Portainer, which follows `main`: **a push to `main` is a deploy.**

## Standing rules from the owner

- **Plan or build: your call.** Build straight away when a request is concrete and its behavior is clear. Plan first,
  briefly, when the result depends on game facts or preferences you can't know. Lead the plan with your assumptions and
  the questions only the owner can answer, all in one batch. Don't propose and then wait on small, clear changes.
- **Never deploy unless told.** Build, test, then hold the change uncommitted until the owner says "deploy" (or
  "build and deploy" in the request). When the stop hook asks you to commit and push, decline in one line: held for deploy.
- **"Deploy" means everything outstanding.** Ship every uncommitted change in the repo (code, docs, CLAUDE.md), not
  just the latest one, following the checklist below.
- **Save tokens.** Read only the code you need, don't repeat work already done, and keep replies short and concrete.
  The owner is rationing usage across the week.
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
- An FTL relay reaches 7.5 ly and a system hub 15 ly. Devices out of range of a relay, with no replicant in their system,
  can't be commanded. The game allows 25 wards per account, and wards and hubs don't share a system.
- The home sector (FALQUORYX, OTHILETH …) is about 500 ly from the Sol zone (SOLARES, FRANZAN …). A surge across that
  gap takes hours, and borrowed carriers must not fly it empty (loadout supply range: 100 ly).
- Fleets never leave with a partial loadout. Long trips wait on the game's ETA, and a canceled or misrouted trip stops the
  job.

## Writing character texts (replicant profiles)

Name ≤ 60 characters with underscores (no spaces or dashes). Description, Plan and Project ≤ 500 characters each. The
text is rendered as HTML, so use no quotes, apostrophes, ampersands or angle brackets: spell contractions out and
introduce speech with "Says:".
