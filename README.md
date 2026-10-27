# Replicant Space web client (self-hosted)

A personal web client and automation engine for [Replicant Space](https://replicant.space/), the API-first
Bobiverse-inspired game. It runs as one Portainer stack, reached through a Cloudflare Tunnel with Google sign-in, and
can host other players too ([multi-user mode](#multi-user-mode-a-server-for-each-player)).

**Contents:** [Screenshots](#screenshots) · [Pages](#pages) · [Map legend](#map-legend) · [Automation](#automation) ·
[Setup](#setup-about-20-minutes) · [Multi-user mode](#multi-user-mode-a-server-for-each-player) ·
[Development](#local-development) · [Configuration](#configuration-app) · [Roadmap](#roadmap) ·
[Full feature guide](docs/FEATURES.md)

```
Internet ─► Cloudflare ─► cloudflared ─► nginx ─(auth_request)─► oauth2-proxy ─► Google sign-in
                                           │
                                           └─► app  (FastAPI + htmx, SQLite, holds the game token)
```

| | |
|---|---|
| 🔒 **No host ports** | The tunnel is the only way in; nginx checks every request (the live-update stream too) with oauth2-proxy. |
| 🔑 **Token stays on the server** | The game API token is an environment variable of the `app` container only and never reaches the browser. |
| ⏱️ **One process talks to the game** | One event-stream connection, scheduled polls, and every call paced under the game's limits (120 reads + 60 actions a minute), with budget kept for your clicks. |
| 🗄️ **Full event history** | The game keeps about the last 10,000 events; the app stores everything it sees in SQLite on a named volume. |

## Screenshots

Every page is in [docs/screens](docs/screens/README.md), taken from the mock game server's demo galaxy (made-up names
and codes).

| Dashboard | Galaxy map | Device tree |
|---|---|---|
| [![Dashboard](docs/screens/dashboard.jpg)](docs/screens/README.md#dashboard) | [![Galaxy map](docs/screens/galaxy-map.jpg)](docs/screens/README.md#galaxy-map) | [![Device tree](docs/screens/tree.jpg)](docs/screens/README.md#tree) |
| **System** | **Fleets** | **Logistics** |
| [![System](docs/screens/system.jpg)](docs/screens/README.md#system) | [![Fleets](docs/screens/fleets.jpg)](docs/screens/README.md#fleets) | [![Logistics](docs/screens/logistics.jpg)](docs/screens/README.md#logistics) |
| **Relays** | **Defense** | **Terraforming** |
| [![Relays](docs/screens/relays.jpg)](docs/screens/README.md#relays) | [![Defense](docs/screens/defense.jpg)](docs/screens/README.md#defense) | [![Terraforming](docs/screens/terraforming.jpg)](docs/screens/README.md#terraforming) |

## Pages

Six tabs, each with sub-tabs; every page keeps its own URL. Account, Diagnostics and Console are in the menu under your
name. Each rule's settings are on the page it works on, in a collapsed *Rules* panel. Everything below is described in
full in the **[feature guide](docs/FEATURES.md)**.
Most pages update live: when devices, jobs or game events change, their cards redraw from the app's cache (no game
calls) at most once a minute while the tab is visible, but not while you type in them; *● live* in the header pauses it. Your own orders and arrivals re-fetch the devices involved, so the device page shows the result within
seconds, and a trip's countdown reaching zero loads the new location.

| Tab | Page | In short |
|---|---|---|
| **Dashboard** | [Dashboard](docs/FEATURES.md#dashboard) | while-you-were-away digest, alerts, season story, logistics backlog, trends, stockpiles; cards drag, fold and remember |
| **Devices** | [Tree](docs/FEATURES.md#tree) | system → type → device, cargo nested in its carrier |
| | [Device list](docs/FEATURES.md#device-list-fleet) | filter, then one command or tag change for many devices |
| | [Device](docs/FEATURES.md#device-page) · [Replicant](docs/FEATURES.md#replicant-page) | every command, print queue, carrier card, permissions; travel, scan, profile, slingshot |
| | [AMI](docs/FEATURES.md#ami) | controllers, directives, schedules |
| | [Lost](docs/FEATURES.md#lost) | devices out of sight and devices gone, with the reason |
| **Map** | [Galaxy](docs/FEATURES.md#galaxy) | live 3D map: markers, fleets, traffic, relay networks ([legend](#map-legend)) |
| | [Stars](docs/FEATURES.md#stars) · [Systems](docs/FEATURES.md#systems-list) · [System](docs/FEATURES.md#system-page) | nearest unexplored stars; systems by distance with ward / relay / beacon checkboxes and filters; one system's map, resources, belts, bodies |
| | [Prospects](docs/FEATURES.md#prospects) | every scanned system scored out of 100, wards |
| | [Relays](docs/FEATURES.md#relays) | live links, networks, bridges, relay rollout |
| | [Traffic](docs/FEATURES.md#traffic) · [Trail](docs/FEATURES.md#trail) | beacon logs and civilization contact; follow a replicant (Bill) through the beacons |
| | [Wards & hubs](docs/FEATURES.md#wards--hubs) · [Upkeep](docs/FEATURES.md#upkeep) | ward cap, hub shield and upkeep; wear and maintenance |
| | [Defense](docs/FEATURES.md#defense) | asteroids: defend, evacuate, mine them with rigs or a rock chaser fleet |
| | [Terraforming](docs/FEATURES.md#terraforming) · [candidates](docs/FEATURES.md#terraforming-candidates) | live console per world, dropping rocks; bodies rated as targets |
| **Fleets** | [Fleets](docs/FEATURES.md#fleets) · [details](docs/FEATURES.md#pages) | status against loadout, missions, strays, roles, bootstrap |
| | [Logistics](docs/FEATURES.md#logistics) | deliveries on demand by task category, carriers spread over the hubs, rescues, repair patrols |
| | Reset & reform | rebuild messy tags |
| **Economy** | [Blueprints](docs/FEATURES.md#blueprints) | travel, cargo, costs, production planner, [component prints](docs/FEATURES.md#component-prints) |
| | [Contracts](docs/FEATURES.md#contracts) · [Reputation](docs/FEATURES.md#reputation) · [Shop](docs/FEATURES.md#shop) | contracts and trades, deliveries, shops and traders |
| | [Simulations](docs/FEATURES.md#simulations) | scenario runs with build orders, terraforming training, research and data uploads |
| | [Lockers](docs/FEATURES.md#lockers) | equipment lockers: deliver the price, retrieve |
| **Activity** | [Automations, Events, Messages, Notifications, Leaderboards, Blog](docs/FEATURES.md#activity-and-tools) | rules, jobs and log; history; BobNet; alerts; boards; the game's blog |
| **Menu** | Account · [Diagnostics · Console](docs/FEATURES.md#activity-and-tools) | server runs, [desktop wallpaper](docs/FEATURES.md#desktop-wallpaper-optional); snapshots and performance; any API call |

## Map legend

Every marker and layer has its own checkbox under *layers* on the Galaxy page; the map's own *legend* panel says the
same. Markers sit at a fixed spot around the star, so you learn where to look.

**Star markers**

| | Marker | Means |
|:-:|---|---|
| <img src="docs/icons/devices.svg" width="22"> | blue square, left | systems with your devices |
| <img src="docs/icons/replicant.svg" width="22"> | pink diamond, above | one of your replicants is there |
| <img src="docs/icons/hub.svg" width="22"> | gold node, upper left | your hub |
| <img src="docs/icons/other-hub.svg" width="22"> | coral node, upper left | another player's hub (arriving there is 25 % faster) |
| <img src="docs/icons/ward.svg" width="22"> | blue shield | your ward |
| <img src="docs/icons/other-ward.svg" width="22"> | coral shield | another player's ward or hub |
| <img src="docs/icons/scanned.svg" width="22"> | green dotted ring | scanned |
| <img src="docs/icons/found.svg" width="22"> | yellow starburst, below | found by your observatories, not scanned yet |
| <img src="docs/icons/prospected.svg" width="22"> | cyan corner brackets | an observatory prospected from here (once per star) |
| <img src="docs/icons/megastructure.svg" width="22"> | violet hexagon, below | megastructure |
| <img src="docs/icons/station.svg" width="22"> | silver wheel, lower right | colony space station (the game's story overview names the systems) |
| <img src="docs/icons/rich.svg" width="22"> | orange gem, lower left | the scan tags it resource rich |
| <img src="docs/icons/anomaly.svg" width="22"> | magenta spiral, upper right | the scan tags an anomaly |
| <img src="docs/icons/hazard.svg" width="22"> | pulsing red haze | the scan tags it hazardous, or an incoming object threatens it |
| <img src="docs/icons/surveying.svg" width="22"> | green wave | being surveyed |
| <img src="docs/icons/mining.svg" width="22"> | sparkles | drones mining now, by resource: structural (gray), conductive (orange), silicates (sand), carbon (brown), volatiles (cyan), rares (magenta) |
| <img src="docs/icons/cover.svg" width="22"> | range sphere | relay / hub range |
| <img src="docs/icons/region.svg" width="22"> | background tint | one color per galactic region; the region at the view's center has none, the others fade in with distance |

**Lines and movement**

| | Layer | Means |
|:-:|---|---|
| <img src="docs/icons/fleet.svg" width="22"> | teal dashes | a fleet's route (dashes run toward its target) |
| <img src="docs/icons/transit.svg" width="22"> | violet arrow | a ship in transit (points where it's going) |
| <img src="docs/icons/supply.svg" width="22"> | amber / blue arc | supply line: materials / trade |
| <img src="docs/icons/prospecting.svg" width="22"> | cyan cone | an observatory prospecting (a wave running out to its reach) |
| <img src="docs/icons/relay-network.svg" width="22"> | colored curves | a relay network, one color each, active (a replicant within reach); a hub's links in a darker shade |
| <img src="docs/icons/relay-inactive.svg" width="22"> | dashed curves | a relay network without a replicant |
| <img src="docs/icons/relay-order.svg" width="22"> | white ring | a relay on order |
| <img src="docs/icons/relay-off.svg" width="22"> | red ring | a relay of yours switched off, in a system no network reaches |
| <img src="docs/icons/relay-bridge.svg" width="22"> | dashed gray | a suggested bridge between networks |
| <img src="docs/icons/other-players.svg" width="22"> | coral ring and arrow | other players' fixed devices; arrivals and departures at your beacons in the last hour |
| <img src="docs/icons/trail.svg" width="22"> | gold line | the trail of the replicant you follow |

**System map**

| | Marker | Means |
|:-:|---|---|
| <img src="docs/icons/drone-work.svg" width="22"> | green dot circling clockwise | a mining or other working drone, around the site it mines (else its location) |
| <img src="docs/icons/drone-survey.svg" width="22"> | light blue dot circling counterclockwise, with a beam | a survey drone tracking a site, beam on the site |

Markers, sparkles and names shrink and fade with distance from the view's center (gone by 30 ly); right-click (on
touch: long-press) two stars to measure. The icons are drawn by `tools/make_legend_icons.py` from the map's own shapes.

## Automation

Rules run in the app container, so they keep going with the browser closed. The details are in
[How automation works](docs/FEATURES.md#how-automation-works).

- **Jobs**: each rule creates ordered game commands, some waiting for an event (e.g. `travel.arrived`). Jobs live in
  SQLite and survive redeploys; a failing *critical* step stops the job with an alert.
- **Safe defaults**: watch-and-alert rules start on, every other rule starts off. *Dry run* logs what rules would do.
  All commands share the rate limiter and show in the audit log as user `automation`.
- **Fleets** keep their loadouts, run missions, follow a path forward, chase asteroids and bootstrap new hubs
  ([Fleets](docs/FEATURES.md#fleets)); **logistics** delivers materials and devices on demand
  ([Logistics](docs/FEATURES.md#logistics)).
- **Travel** waits on the game's ETA, routes via hubs and surge hops when that's faster, and asks for **Confirm** on
  trips over a day ([Sector range](docs/FEATURES.md#activity-and-tools)).
- **Season changes** pause every automation until you resume; the engine watchdog flags a stalled engine, and late
  replayed events trigger nothing.

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

- **Version** (`rsweb/version.py`: `VERSION` plus a `CHANGES` list) is shown in the header, with a short **code
  fingerprint** (a hash of the app's code), so a code change is visible even if the version wasn't bumped. Set
  `APP_BUILD` (e.g. a git commit) in the stack to show that too.
- **Server started** lines: every start adds a run record and logs the version, the run id and the previous run, and
  whether that run stopped cleanly. A run with no clean stop was killed, crashed or hard-redeployed; the log shows when
  it was last seen (a heartbeat updates that every minute). After an upgrade, the log also lists what changed.
- **Tagged history**: every log entry and job carries the version and run that wrote it. The Automations page greys out
  and tags anything from an **earlier run**, an **older version**, or from before versioning (**unversioned**).
- The Account page's **Server** card has the last 20 runs and the version history; Diagnostics snapshots include the
  same history, and tag their alerts and failed jobs the same way.
- When releasing, bump `VERSION` and add a `CHANGES` line.

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

**Demo galaxy:** `RS_MOCK_DEMO=1 uvicorn rsweb.mock:app --port 9000` serves `rsweb/demo_galaxy.json` instead of the small Sol world: about
2,100 real star positions with 44 systems, 500 devices and 18 replicants, every name and code made up. Rebuild it from a Diagnostics snapshot
and a saved `/api/map.json?part=core` (zipped is fine) with `python tools/make_demo_galaxy.py <snapshot.json> --stars map.zip`.
Tests keep using the Sol world.

**Screenshots:** with both servers running (the mock in demo mode) and synced (about a minute), `python tools/screenshots.py --system ELNYXOTH` retakes every page into
`docs/screens` (needs `pip install playwright` and a Chromium; point `PW_CHROMIUM` at it if Playwright has none).

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
tools/screenshots.py       retake docs/screens; make_demo_galaxy.py builds the mock's demo galaxy
tools/make_legend_icons.py draw docs/icons (the README's map legend) from the map's marker shapes
docs/FEATURES.md           every page and automation in detail; docs/screens has a screenshot of each page
tools/fetch_docs.py        save the game's docs pages and changelog as text (to check the app against them)
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
  bootstrap.py, boot_engine.py   bootstrap fleets: stages and decisions; the engine pass that runs them
  lost.py                  lost equipment: last known locations of devices out of sight
  ward_orders.py           Prospects › add ward: activate / send a spare / print, then switch it on
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
2. **GUPPI theming**: an optional theme for the web client in the style of GUPPI (the Bobiverse replicant's interface).

<details><summary><b>Fleet controller: how the app would use it</b></summary>

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

</details>

## Caveats

The API reference was compiled from the public docs and checked against the live API and the OpenAPI spec. Messages and achievements are confirmed live; reputation and some device detail fields are still unconfirmed. The UI copes with missing or unknown fields and shows raw JSON where it can't lay a response out. If something looks off, check the response in the **Console** and adjust the matching template.

Features added from the OpenAPI spec (1.117.0: leaderboards, blog, API health, permissions, keep in stock …) are described
with their pages in the [feature guide](docs/FEATURES.md).
