# Replicant Space web client (self-hosted)

A personal web client for [Replicant Space](https://replicant.space/), the API-first Bobiverse-inspired game.
It runs as one Portainer stack. You reach it through a Cloudflare Tunnel and sign in with Google through oauth2-proxy.
The same stack can also host other players, each with their own server and game key: see [Multi-user mode](#multi-user-mode-a-server-for-each-player).

```
Internet ─► Cloudflare ─► cloudflared ─► nginx ─(auth_request)─► oauth2-proxy ─► Google sign-in
                                           │
                                           └─► app  (FastAPI + htmx, SQLite, holds the game token)
```

- **No host ports are published.** The tunnel is the only way in, and nginx checks every request (including the live-update stream) with oauth2-proxy.
- **The game API token stays on the server.** It is an environment variable of the `app` container only and never reaches the browser.
- **One process talks to the game.** It keeps one connection to the event stream, polls on a schedule, and paces every call under the game's limits (120 reads + 60 actions per minute), keeping some budget in reserve for your clicks.
- **Full event history.** The game only keeps about the last 10,000 events. The app stores everything it sees in SQLite on a named volume.

## Features

Six tabs, each with sub-tabs; every page keeps its own URL. Account, Diagnostics and Console are in the menu under your name.
Per-release detail is in the app's version history (Account › Server, or `app/rsweb/version.py`).

| Tab | Pages |
|---|---|
| Dashboard | — |
| Devices | Tree · List · AMI |
| Map | Galaxy · Stars · Systems · Traffic · Trail · Wards & hubs · Defense · Upkeep |
| Fleets | Fleets · Reset & reform |
| Economy | Blueprints · Contracts · Reputation · Shop |
| Activity | Automations · Events · Messages · Notifications |

**Rule settings live on the page each rule works on**, in a collapsed *Rules* panel at the top. The Automations page lists every
rule with an on/off switch, plus the jobs and the log.

### Dashboard and devices
- **Dashboard**: a *while you were away* digest (a gap of 30 min starts a new visit; `/digest?hours=N` for any window), open
  alerts, replicants, live countdowns, fleet summary, stockpiles with a 48 h trend, live event feed.
- **Tree**: system → device type → device, with stowed devices nested in their carrier; each device opens its command panel.
- **List** (`/fleet`): every device, filterable by type, status, system, tag or text. Tick devices (or all listed) to send them
  one command; the ones that can't take it — command not available, in use by an automation, a deploy/detach while the carrier
  is between systems — are listed and skipped.
- **Device page**: live detail, any available command with its fields (destructive ones ask first), tags, history and game log,
  cancel travel. Autofactories get a **print queue** card (add with an optional *deliver to*, remove, clear); carrier vessels
  a **carrier** card (launch / stow / recall); *Decommission at an autofactory* takes a device there so the autofactory learns
  its blueprint. `enqueue_print` on a replicant's vessel is sent as the replicant's own print (one at a time).
- **Replicant page**: route preview and travel (with *then, on arrival…* follow-ups), nearest stars, scan, mining, vessel
  printing, profile editing, FTL slingshot.
- **AMI**: controllers with their latest digest, a directive picker with targets from the controller's system, adopt / release /
  launch / withdraw, and **AMI schedules** that re-issue directives to idle controllers.

### Maps
- **Galaxy**: 3D map of the star catalog (plus stars from censuses and your observatories), live: ships in transit, fleets,
  supply lines, mining sparkles and **prospecting cones** (each observatory scan, darker as it progresses) redraw as events
  arrive. Relay/hub range, search, distances, route preview; right-click two stars to measure. **Other players** (coral):
  their fixed devices from the latest scans, a shield where someone else's ward or hub is, and an arrow for each arrival or
  departure your beacons logged in the last hour (with the likely other end of the trip), all fading with distance from the
  point the view is centered on. The **trail** of the replicant you follow is drawn in gold.
- **Stars**: unexplored stars nearest a system, with ETA and route; stellar census per vessel.
- **Systems / System page**: a top-down map with your devices, stockpiles, sites and salvage. Three cards: **Resources** (per
  resource: richness, open sites, % left, ≈ units left — learned from what past sites gave — salvage, stockpiled),
  **Belts** (richness, a viability line, open sites) and **Bodies** (planets, moons, L-points with their salvage and stock).
  *Mining prospects* scores every scanned system out of 100. Surveyed checkboxes per body. *Other players here*: their
  fixed devices (beacons, relays, wards, hubs, factories, observatories, controllers) from the latest scan, also drawn on the
  map; a replicant arriving in a system scans it, or use *Scan for other devices*. Snapshots older than 7 days are dropped.
- **Traffic**: civilization contact (a beacon must sit at the body of the civilization's event), visitors, each beacon's audit log,
  redundant beacons. How often beacons are read (default 10 minutes).
- **Trail**: follow a replicant through the audit logs of public FTL beacons; candidate systems with travel time and a travel
  button; *follow to the end* keeps going until the target stops moving. *Reset trail* clears the beacons and logs when the
  target relocates.
- **Wards & hubs**: your wards (against the 25 cap), hub shield and upkeep with warnings, `evicted_miners` log. Other players'
  warded systems are left alone by every mining rule and mission.
- **Defense**: incoming asteroids with a propulsor estimate and **Defend now**. **Upkeep**: wear per system and maintenance
  coverage.

### Fleets
A fleet is a named group of devices tagged `fleet:<id>`, with a role (mining / explore / trade), a home and a loadout (its own
lines or a template).
- **Stationed** fleets are kept at their loadout in their home system by the **loadout pass**: extras become `spare`, spares
  are sent where they're short (within the supply range, default 100 ly), what's still missing is printed and carried there
  (large devices are compacted first). Placed beacons and relays are never made spare or moved.
- **Missions** run phase by phase (assemble → gather → travel → deploy → work → … → return → unload) with a log, *Retry*,
  *Stop*, *End mission & board* and *Recall*. Mining works the richest belt (or salvage); explore surveys each target and
  leaves a relay and a beacon; trade runs a contract or trade end to end.
- **Path forward** (stationed mining fleets): a heading; when the home runs dry the fleet picks the best system ahead and moves.
- **Observatories** prospect wherever they're deployed — the heading (else outward from Sol), then 13 other directions — and
  compact when every direction is used up or the fleet is leaving.
- **Auto-scout** (explore fleets): surveys the nearest unsurveyed systems within 100 ly of home, ring by ring (only inside relay
  coverage without a replicant aboard); sets out at 85 % capacity, comes home for repairs at 50 %.
- **Auto-fulfil** (trade fleets): contracts and trades your stockpiles can pay for, nearest first.
- Also: owner hand-over (`change_owner`), materials (*send to* / *takes materials in*), *fill from spares*, carrying budget,
  *Reset & reform* for messy tags.

### Economy
- **Blueprints**: costs and print times, what each printer can afford, a production planner that queues prints (spread over
  several autofactories) and can mine or deliver the shortfall.
- **Contracts**: in-game events with criteria (any one option completes it), rewards and progress; *send replicant*, *deliver
  materials*, *fulfill*. **Deliver devices** sends spare devices to a contract's location; printing what no spare covers waits
  for **Authorize printing**.
- **Reputation**: account and per-replicant reputation, known species. **Shop**: your trade controllers, trades, other traders.

### Activity and tools
- **Automations**: every rule, jobs step by step with Cancel, and the log. **Events**: full local history with filters.
  **Messages**: game messages and BobNet channels. **Notifications**: alerts and accomplishments, with toasts and a badge.
- **Diagnostics**: a live snapshot (read-only GETs plus the app's state) with a mining diagnosis, downloadable as JSON;
  feedback to the game's developers. **Console**: call any API endpoint through the server-side client.
- **Desktop wallpaper** (optional): the Galaxy and System maps as a live Windows wallpaper with
  [Octos](https://github.com/underpig1/octos) and [sk3ynet/replicant-space-octos](https://github.com/sk3ynet/replicant-space-octos).
  *Account › Desktop wallpaper* creates a read-only link (`/wallpaper/<id>/#key=…`); options such as `view=galaxy|system|cycle`,
  `cover=0`, `fleets=0`, `supply=0`, `production=0`, `hud=right|left|off`.

### How automation works
- Rules run in the app container, so they keep going with the browser closed. Each rule creates **jobs**: ordered game commands,
  some waiting for an event (e.g. `travel.arrived`) before the next. A failing step is logged and skipped; a failing *critical*
  step stops the job with an alert. Jobs are stored in SQLite and survive redeploys.
- All commands share the rate limiter (background budget) and appear in the Account audit log as user `automation`.
- **Dry run** makes rules log what they would do without sending anything. Watch-and-alert rules start on; every other rule
  starts off.
- **Engine watchdog**: if the engine lock is held for more than 10 minutes, the log and Diagnostics say **automations stalled**.
- Events that arrive more than 30 min late (a replay after downtime) go into the history only; they trigger no rules.

## Setup (about 20 minutes)

### 1. Google OAuth client
1. Go to <https://console.cloud.google.com/> and create a project, e.g. "replicant".
2. Open **APIs & Services ▸ OAuth consent screen**. Choose External, fill in the app name and your email, and add yourself as a test user. It can stay in *Testing* mode.
3. Open **Credentials ▸ Create credentials ▸ OAuth client ID ▸ Web application**.
   - Authorized redirect URI: `https://<PUBLIC_HOST>/oauth2/callback`
4. Copy the client ID and secret.

### 2. Cloudflare Tunnel
1. Go to **Zero Trust ▸ Networks ▸ Tunnels ▸ Create a tunnel** (Cloudflared) and copy the token (`eyJ…`).
2. Add a **Public hostname** such as `replicant.yourdomain.com` with service `HTTP` → `nginx:80`.
   Cloudflare Access is not needed; oauth2-proxy handles sign-in.

### 3. Deploy in Portainer
1. Push this folder to a Git repo (a private GitHub repo is fine). No secrets live in it.
2. In Portainer: **Stacks ▸ Add stack ▸ Repository**. Enter the repo URL (plus credentials if it's private), and use compose path `docker-compose.yml`. That's the multi-user stack; with only your own address in `ALLOWED_EMAIL` it serves just you. For the original single-user stack, use `docker-compose.single.yml`.
3. Under **Environment variables**, add every variable from `.env.example`:

   | Variable | Value |
   |---|---|
   | `PUBLIC_HOST` | `replicant.yourdomain.com` |
   | `CF_TUNNEL_TOKEN` | tunnel token |
   | `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | from step 1 |
   | `OAUTH2_COOKIE_SECRET` | output of `python3 -c "import secrets,base64;print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"` |
   | `ALLOWED_EMAIL` | your Google address first, then anyone you're hosting (comma-separated, no spaces) |
   | `RS_API_TOKEN` | your Replicant Space API token |
   | `TZ` | `America/New_York` |

4. **Deploy the stack.** Portainer builds the `app` and `nginx` images from the repo.
5. Open `https://<PUBLIC_HOST>`, sign in with Google, and the dashboard fills in within a minute.

**Updating:** push to the repo, then in Portainer open the stack and choose **Pull and redeploy**. The `app` and `nginx` images are never pulled from a registry (`pull_policy: build`); they're rebuilt from the repo on every redeploy. You can also turn on automatic updates / GitOps polling. The SQLite volume `rsweb-data` survives redeploys.

**Rotating the game token:** use account recovery on replicant.space, paste the new token into the stack's `RS_API_TOKEN`, and choose **Update the stack**.

<details><summary>Notes and alternatives</summary>

- A one-shot `auth-config` container writes `ALLOWED_EMAIL` into oauth2-proxy's allow-list on every deploy, then exits (it shows as *exited (0)* in Portainer — that's expected). Several addresses can be comma-separated.
- If you'd rather use Portainer's **Web editor** (no Git), it can't build images. Build and push them somewhere first (e.g. GHCR with the included GitHub Actions workflow). Then replace `build: ./app` / `build: ./nginx` with `image: ghcr.io/<you>/replicant-web-app:latest` / `…-nginx:latest`.
- For LAN debugging, uncomment `ports: ["8080:80"]` on nginx. Google sign-in will still send you back to `PUBLIC_HOST`.
- `ALLOWED_EMAIL` is enforced twice: by oauth2-proxy's allow-list and by the app itself.
</details>

## Multi-user mode: a server for each player

The same stack can host friends too. Each person signs in with their own Google account and gets their **own copy of
the app**: their own game API key, database, event history, rules and automations, and their own share of the game's
rate limit (the limit is per key). Nobody can see anyone else's server.

```
Internet ─► cloudflared ─► nginx ─(auth_request)─► oauth2-proxy      Google: who are you?
                             ├──(auth_request)───► manager (app:8000)  which server is yours?
                             ├─► /_tenant/* ─────► manager             sign-up walkthrough, API key, status
                             └─► app:<your port>                       your own server (8100, 8101 …)
```

The `app` container runs a small **manager** (`rsweb/tenants.py`). It starts one app server per registered person,
restarts it if it dies, and stops them all cleanly when the stack stops. Someone who signs in without a server is sent
to the **walkthrough** at `/_tenant/`, which gets them a game API key and starts their server. Each key is checked with the
game before it's stored, and is kept on the volume, readable only by the app (`/data/tenants/<name>/token`, mode 600).
It never reaches a browser.

### Switching the stack to multi-user mode (owner)

1. **Google sign-in for other people.** In the Google Cloud console, open **APIs & Services ▸ OAuth consent screen**.
   While the app is in *Testing*, only the listed **test users** can sign in, so add each person's Google address
   there (up to 100). Or choose **Publish app**: with only the basic email/profile scopes this needs no Google review.
   The OAuth client and redirect URI stay as they are.
2. **Use `docker-compose.yml`.** Since 1.17.1 it *is* the multi-user stack, so a stack on that path switches over on
   its next **Pull and redeploy**, keeping its name and its `rsweb-data` volume. With `OWNER_EMAIL` unset, the first
   address in `ALLOWED_EMAIL` is the owner and keeps `RS_API_TOKEN` and the existing database.
3. **Set who may sign up** (environment variables):

   | Variable | Value |
   |---|---|
   | `OWNER_EMAIL` | your Google address (default: the first in `ALLOWED_EMAIL`). You keep `RS_API_TOKEN` and your existing database and history. |
   | `RS_API_TOKEN` | your game key, as before. Optional: leave it empty to add yours through the walkthrough. |
   | `ALLOWED_EMAIL` | comma-separated Google addresses of the people you're hosting, no spaces |
   | `ALLOWED_DOMAINS` | optional: everyone at these domains, e.g. `example.com` |
   | `OPEN_SIGNUP` | `1` lets **any** Google account sign up. Leave it empty unless you mean it. |
   | `MAX_TENANTS` | most servers this host will run (default 10; each takes roughly 100 MB of RAM) |
   | `ADMIN_EMAILS` | optional: others who may see **All servers** (the owner always can) |

   Everything else (`PUBLIC_HOST`, `CF_TUNNEL_TOKEN`, `GOOGLE_CLIENT_*`, `OAUTH2_COOKIE_SECRET`, `TZ`) stays the same.
4. **Pull and redeploy.** Then send your players the address and the walkthrough below.

In multi-user mode, oauth2-proxy accepts any Google account and the manager does the gatekeeping. Someone who isn't on
the list sees a *Not on the list* page and gets nothing else. To remove someone, take them off the list. To also stop
their server and delete their key, open **All servers** (`/_tenant/admin`, in the menu under your name) ▸ **Disconnect**.
Their history is kept.
**All servers** also shows each server's state, its restarts and its last log lines, with Restart and Stop.

To go back to single-user mode, set the compose path to `docker-compose.single.yml`. Your own data was never moved.

### Walkthrough for players: register and apply your API key

1. **Sign in.** Open the address the owner gave you and sign in with the Google account they added. The first time,
   you land on **Set up your Replicant Space server**.
2. **Create a game account** (step 1 on the page). Enter the email for your game account (it doesn't have to be your
   Google address), your in-game name and your time zone (filled in from your browser), then choose **Register**.
   The game emails you a verification link. Registration is rate-limited by the game to a few per hour; if it refuses,
   wait and try again.
   *Already play Replicant Space?* Skip to step 4 if you have your key, or use **Email me a new key** at the bottom if
   you've lost it.
3. **Open the email and copy your API key.** Click the link in the email from Replicant Space (check spam). The page
   that opens shows something like:
   ```json
   { "api_token": "OsiJIqbw_8tj4SLgeo_…", "message": "Email verified successfully",
     "replicant": { "name": "bob-1", "replicant_code": "C2AF4A82" } }
   ```
   The text between the quotes after `api_token` is your **API key**. Copy it without the quotes and keep a copy in a
   password manager: it's the only key to your game account. The link only works once.
4. **Paste the key** into step 3 on the page and choose **Check and start my server**. The key is checked with the game
   (it shows your replicant's name), stored on the server, and your server starts. The dashboard opens by itself a few
   seconds later and fills in over the first minute.

**Later:** *Your server & API key*, in the menu under your name, shows your server's state and lets you restart it.
- **Replacing the key.** Account recovery (**Email me a new key**) issues a new key and stops the old one working.
  When that happens, paste the new key under **Replace your API key**. Your history and settings stay.
- **Disconnecting.** **Disconnect** stops your server and deletes your key from it. Your game account isn't touched,
  and pasting a key again picks up where you left off.

Each game account can be connected only once per host. Two servers on one key would split its rate limit.

## Versions and server runs

The app version (`rsweb/version.py`: `VERSION` plus a `CHANGES` list) is shown in the header. A short **code fingerprint**, a hash of the app's code, is shown with it, so a code change is visible even if the version wasn't bumped. You can set `APP_BUILD` (e.g. a git commit) in the stack to show that too.

Every start adds a run record and writes **server started** lines to the automation log. Those lines give the version, the run id and the previous run, and whether that run stopped cleanly or not. A run with no clean stop was killed, crashed or hard-redeployed, and the log shows when it was last seen; a heartbeat updates that time every minute. After an upgrade, the log also lists what changed since the previous version.

Every log entry and job carries the version and run that wrote it. The Automations page greys out and tags anything from an **earlier run**, an **older version**, or from before versioning (**unversioned**). The Account page has a **Server** card with the last 20 runs and the version history. Diagnostics snapshots include the same history, and tag their alerts and failed jobs the same way.

When releasing, bump `VERSION` and add a `CHANGES` line.

## Local development

```bash
cd app
pip install -r requirements-dev.txt
# fake game server that emits events every few seconds
uvicorn rsweb.mock:app --port 9000 &
# the client, with auth bypassed for localhost
RS_API_BASE=http://127.0.0.1:9000/v1 RS_API_TOKEN=dev DEV_USER=you@example.com uvicorn rsweb.main:app --port 8000
pytest -q
```

**Conventions:** American spelling in all text (UI, logs, comments, docs). The game's own names stay as the API spells them
(e.g. the `travel.cancelled` event, the `travelling` status).

Never set `DEV_USER` in the stack: it makes the app trust requests with no identity header.

Multi-user mode runs locally too: `OWNER_EMAIL=you@example.com DATA_DIR=./data DB_PATH=./data/rsweb.sqlite RS_API_BASE=http://127.0.0.1:9000/v1 DEV_USER=you@example.com python -m rsweb.tenants`
serves the walkthrough at <http://127.0.0.1:8000/_tenant/> (the mock accepts any key). The per-user servers listen on 8100 and up;
open them directly with `DEV_USER` unset and an `X-Auth-Request-Email` header, or put `nginx/multi.conf` in front.

**Replaying a snapshot:** `python tools/replay_snapshot.py <snapshot.json>` runs one automation tick against a Diagnostics snapshot with a stub API (GETs answered from the snapshot, commands recorded, nothing sent) and prints the new log lines, the loadout pass and the commands it would have sent. It is approximate: snapshots don't carry the star catalog, blueprints or replicants.

## Configuration (app)

| Env | Default | |
|---|---|---|
| `RS_API_TOKEN` / `RS_API_TOKEN_FILE` | – | game token (env var, or a file path if you prefer a mounted secret) |
| `RS_API_BASE` | `https://api.replicant.space/v1` | |
| `ALLOWED_EMAILS` | – | comma list; empty = anyone oauth2-proxy lets through |
| `RS_GET_PER_MIN` / `RS_ACT_PER_MIN` | 110 / 55 | client-side budget (game: 120 / 60) |
| `RS_GET_RESERVE` / `RS_ACT_RESERVE` | 30 / 20 | budget background polling may not touch |
| `POLL_ACCOUNT`, `POLL_DEVICES`, `POLL_INVENTORY`, `POLL_MESSAGES`, `POLL_BLUEPRINTS`, `POLL_CATALOGUE` | 60, 60, 120, 300, 300, 1800 s | blueprints also refresh after likely-unlock events and when the Blueprints page opens |
| `POLL_TRAFFIC`, `POLL_OBJECTS` | 600, 900 s | beacon audit logs (one GET per deployed beacon) and incoming asteroids (one GET per tracked object; none when there are none) |
| `VISIT_GAP_MINUTES` | 30 | idle gap that starts a new visit for the digest |

## Layout

```
docker-compose.yml         the Portainer stack (multi-user: one server per Google account)
docker-compose.single.yml  the original single-user stack
nginx/                     nginx image with the auth_request config (default.conf; multi.conf for multi-user mode)
tools/replay_snapshot.py   run one automation tick against a Diagnostics snapshot
API_REFERENCE.md           API notes compiled from the official docs
app/rsweb/
  main.py, config.py       app start-up and settings
  api.py                   rate-limited API client + SSE parser
  ingest.py                event stream, pollers, timers, device sync
  automations.py           the automation engine: jobs, rules, fleet missions, tick stages
  ops_rules.py             the Traffic / Defense / Upkeep / Shop rules (mixed into the engine)
  loadouts.py              the loadout pass: spares, prints, deliveries, placement
  fleets.py                fleets, missions, deals; reform.py rebuilds tags
  pathing.py, prospects.py, prospecting.py, observatory.py   path forward, system scores, observatory prospecting
  others.py                other players' fixed devices, beacon traffic arrows and the trail for the maps
  gameevents.py, contractsupply.py   contracts and their device delivery
  targets.py, sites.py, salvage.py, viability.py   locations, sites, salvage, belt viability
  traffic.py, trail.py, census.py, transit.py      beacons, trails, star census, things in transit
  wards.py, wardhub.py, defence.py, upkeep.py, shop.py, decommission.py, modular.py, printqueue.py …
  web.py and web_*.py      pages, htmx partials and actions; templates/ and static/ (map.js is the galaxy map)
  tenants.py               multi-user manager
  mock.py                  fake game API for dev and tests
```

## Roadmap

What's still open, from comparing the app with the game's API reference and docs.

**Next**
1. **Fleet controller** (once unlocked, see below): one travel command per fleet.
2. **FTL network tools**: relay network view (`/devices/<relay>/network`) with coverage gaps, and teleport / transfer to an empty matrix with a confirmation. (The slingshot is done: see the Replicant page.)
3. **Megastructures**: a Contribute button (`POST /locations/<code>/contribute`) and the leaderboard.
4. **Simulations.**
5. **Housekeeping**: add comments explaining the code blocks throughout the app.

### Fleet controller — how the app would use it

What the game says: the fleet controller is an NPC reward near SOL-3, "in exchange for helping them out with a little project". It adopts **surge-capable** devices only: surge plates and platforms, carriers, mobile fleets, cargo freighters and vessels. One `travel` to the controller is relayed to every adopted device, and each device plans its own route. It cascades: a controller can adopt other fleet controllers. No co-location is needed as long as every device is reachable through the FTL relay network.

How it fits the app:
1. **Flagship per mobile fleet.** A fleet with a fleet controller member adopts its carriers when the fleet is formed or changes. Boarding is unchanged: riders stow or attach to carriers. Departure then becomes a single `travel` to the controller instead of one per carrier, so the carriers leave together and it costs one action. Arrival still waits for each carrier's `travel.arrived` before unloading.
2. **Network controller.** A top-level controller adopts each fleet's controller, which gives "recall every fleet home" or "everything to X" in one command. Because it cascades to every level, the app would only offer it with an explicit confirmation listing every device it moves.
3. **Not for taxis or loadout deliveries.** Those go to different places, so surge-plate taxis stay individually commanded.
4. **Relay check first.** Before relying on the controller, the app reads `/devices/<relay>/network` to confirm every member is reachable. If any isn't, it falls back to per-carrier travel and says why.
5. **To confirm live once you have one:**
   - its `device_type` name;
   - whether the controller itself moves;
   - the `travel` response (per-device `arrives_at`?);
   - whether adopted carriers still take individual commands;
   - adopt and release limits.

   A Diagnostics snapshot after unlocking would settle these.

## Caveats

The API reference was compiled from the public docs, not from the live API. A few response shapes are undocumented: messages, achievements, reputation, and some device detail fields. The UI copes with missing or unknown fields and shows raw JSON where it can't lay a response out. If something looks off, check the response in the **Console** and adjust the matching template.
