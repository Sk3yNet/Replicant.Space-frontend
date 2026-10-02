# Replicant Space web client (self-hosted)

A personal web client for [Replicant Space](https://replicant.space/), the API-first Bobiverse-inspired game.
It runs as one Portainer stack. You reach it through a Cloudflare Tunnel and sign in with Google through oauth2-proxy.

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

| Page | What it does |
|---|---|
| **Dashboard** | *While you were away* digest, open alerts, replicants, live countdowns (travel, prints, scans), fleet summary, stockpiles with 48 h trend, live event feed |
| **System** | The page also has a **Resources available** card: per resource, the quantity at mining sites, in salvage and stockpiled, plus the belt level. It lists each mining site and salvage with its quantity; Used-up salvage is hidden everywhere, since it never comes back: lists, map, location drop-downs. So are depleted or closed sites, where a belt's latest detail no longer lists them. Belts always stay, because new sites can be opened by searching. **Refresh quantities** re-reads the system's belts and the bodies its salvage sits on, one request each and at most 15. It reads the body because salvage codes like `X-1-SAL-2` aren't locations to the game; `X-1` is. If there's no stored system scan, `GET /locations/<STAR>` supplies the belt list, and any belt your devices or events have visited is added. A body's detail lists its salvage with the percentage of each resource left. The app multiplies that by the amounts from the salvage's discovery, shows both, and hides salvage that its body no longer lists. It reports how many belts it read and how many open mining sites they list, then reloads. Mining sites only exist while they're open: a survey drone's `search` at the belt, or an AMI survey controller on `belt_search`, opens one, and it stays open while the drone tracks it. A **Locations** card lists every known location in the system. The map draws all of them: ◆ resource sites, ▲ salvage, ■ Lagrange points, ● objects and the outer system. Each is labelled with the quantity available and opens its detail when clicked, and a checkbox turns the labels on or off. The Systems list adds Mineable, Salvage and Most-available columns. |
| **Tree** | System → device type → device, each level indented under its parent. Each type branch shows its count, how many are working, idle or stowed, and how many are below 50% capacity. Stowed devices are nested inside the carrier holding them. A carrier holding 4+ devices of different kinds groups them by type as well. Each device opens to its own command panel (same fields and drop-downs as the device page). The tree always opens fully collapsed. The page has Expand all, Types, Systems only and Collapse all buttons, plus a filter box that opens the branches that match |
| **Fleet** | Every device, filterable by type, status (working / moving / idle), system, tag or text; capacity warnings |
| **Device** | Autofactories get a **Print queue** card. It shows what's printing now, with a countdown taken from `print.started` (the game leaves the current item out of `print_queue`) or *waiting for resources*. Below that is the waiting list in order, each item with **Remove** (`dequeue_print` with its 1-based index). The card also has Add to queue, Clear queue, a time estimate for when everything is done, and a refresh every minute. Carrier vessels get a **Carrier** card listing carryable devices in the same system with Launch / Stow / Recall checkboxes. Picked devices elsewhere fly to the vessel if they can travel. If they can't, the vessel goes to fetch them and can return afterwards. Also on this page: live detail, run any available command (JSON args pre-filled per command, confirmation for destructive ones), tags, local event history + game log |
| **Replicant** | Route preview (`dry_run`) → go (optionally chained with "then, on arrival…" follow-ups), nearest stars with ETAs, system scan, mining, vessel printing (one at a time: heaven vessels have no queue), BobNet |
| **Systems** | Log-scaled top-down map of a system (planets, belts, habitable zone, Kuiper) with your devices and stockpiles; click to load location detail |
| **Galaxy** | 3D map of the star catalogue: your presence, replicants, scanned systems, hubs, relay/hub range spheres, search, straight-line distance, travel estimate & route preview, shift-click to measure |
| **Blueprints** | **Print queues** card with a live summary of every autofactory's current print and queue. Production planner: pick a printer (autofactories first), enter quantities, see need/have/short at the printer's location. **Queue** sends `enqueue_print` for every line. If stock is short and the system has an AMI mining controller, it can also set `gather_resources` for exactly the shortfall and launch it. If the mining happens elsewhere in the system, a transport controller gets a `delivery` directive to the printer. Also: |
| **Blueprints (cont.)** | Cost/print time, how many each printer can afford from local stock, a production planner (quantities → total cost and shortfall), print / enqueue |
| **Loadouts** | You define **phases**, the stages of a system's development such as *1 Survey* or *2 Mining outpost*. Each phase sets how many of each device type a system at that stage should have, and every system gets a phase. Each pass, the app does the following: (1) devices above a system's loadout get tagged `spare`; (2) a system that is short gets `spare` devices from elsewhere, nearest first, which are retagged `to:<star>` with `spare` removed; (3) whatever is still missing is printed on an autofactory whose stock covers the cost, the system's own factory first, and the print is tagged `to:<star>`; (4) a device bound for another system flies there itself if it can surge. Otherwise a carrier in its system takes it there: surge plates, platforms, carriers and fleets use `attach` and `detach`, sent to the carrier and naming the cargo. Vessels use `stow` and `deploy`. Transport drones and haulers can't be stowed, so only attach carriers take them. Plates in taxi mode, or run by a controller, are left to their ferry. Boarding and unloading are critical steps, so a carrier never leaves without its load, never brings it back, and never re-homes a device that didn't arrive. A print is only queued where the autofactory's queue has room, and the running print takes a slot. "Already at destination" counts as done. Devices run by a transport controller (ferry fleets and taxi plates) are never released for being in another system. A ferry that's already running from a pick-up in the source to the destination keeps its route while that pick-up still has stock. A device that's itself moving is never used as a carrier in the same pass. AMI schedules skip a controller that's salvaging, or one that reports `exhausted` on the same directive. A device away from its `home:` system that is idle, not run by a controller and not spare is sent home the same way. That covers prints made on another system's autofactory. Surge plates and platforms in taxi mode count as free carriers unless a controller runs them. A carrier whose delivery failed, for example out of comms range or mid-surge, sits out for 30 minutes. A printed device that comes out without its `to:` tag gets one added. A ferry the app has sent isn't re-sent until the controller reports it finished. After each delivery the carrier returns. On arrival the `to:` tag is removed. Every device counted for a system gets a `home:<star>` tag. A device marked `spare` loses its `home:` tag: it belongs to no system until it's sent somewhere, or its own system needs it back, and then it's re-homed. It counts for that system wherever it is, so a carrier or transport out on a delivery isn't counted as missing at home or made spare where it's visiting. The page lists these devices under **Away from home**. A delivered device is re-homed when it arrives. Devices with an **ignored tag** are invisible to all of this, and so is the replicant's own vessel unless you allow it. Types a phase leaves blank are "don't care". The page shows each system's want/have/incoming/short counts and what the next pass would do. It has Apply buttons, and the **Keep systems at their loadout** rule on the Automations page runs passes on a timer (Dry run applies). The Tree shows each system's phase. **Materials:** mark a system as a *source* or a *destination*. The source's biggest stockpile goes to the nearest destination by star distance. Cargo freighters (`cargo_freighter`, with a surge drive) do the interstellar hauling. Which controller runs a device is read from its `controller_device_code`. A controller's state comes from `ami_directive` and `_eval_state`; `exhausted` there also triggers the salvage switch. Stowage comes from `stowed_in_device_code`, and prints from `printing`. Devices with `in_control_range: false` are left alone. The ferry controller is never one that also runs drones or haulers: freighters under such a controller are released to the ferry controller. A device leaving for another system is released from its controller first. A device tagged `to:` another system is never put to work where it is. AMI schedules, auto-survey, re-open sites, salvage, idle-miner restarts and drone adoption all skip it. An AMI controller that's leaving releases the drones it runs and clears its directive before it boards or surges. **Arrivals:** each pass also looks for idle drones in the system they belong to that no controller runs. That covers devices you deliver, which land at the entry point, nowhere near the controller. Each one flies to that system's controller of the right kind (mining, survey or transport, never the ferry controller), fewest drones first, and is adopted. If the controller is already running a directive it launches, so the drone gets to work. Spare drones, drones passing through and drones bound elsewhere are left alone. Maintenance drones that reach their home system are given the `patrol` directive (`set_directive` `patrol`), which is all they need; no activation. That includes one already sitting idle at home without patrol. AMI controllers that arrive home `inactive` are activated. Each happens once per arrival, so a device you switch off or re-task yourself is left alone. Both have switches on the Automations page. A bound device that can't move this pass is listed on the Loadouts page with the reason: a running job, out of comms range, tracking a site, position unknown, or no carrier. A device run from another system is released. A device adopted by a controller in the system it's in is re-homed there, and extra `home:` tags are removed. They run under a dedicated AMI transport controller in the source, tagged `ferry` and called the ferry controller. The app picks a controller already tagged `ferry`, or one that already runs freighters, or one that manages nothing else, and adds the tag. Each pass, it flies over and adopts any idle freighters in the source, then sets the `ferry` directive. A ferry already running is only re-sent if the route changes. Transport drones and haulers stay on in-system work under a second transport controller. The ferry controller is never given in-system jobs: the planner's delivery, AMI schedules and drone hand-offs all skip it. Materials land at the destination's autofactory, else its biggest stockpile, else its entry point. |
| **Contracts** | In-game events (`event.discovered` / `event.completed`). For each open event the page shows its criteria and rewards, how much of each requirement is at the event's location and in its system, and whether a replicant is there. A replicant must be at the location to fulfil one. Status is one of: *gather* (not enough in the system), *deliver* (enough in the system), *needs replicant*, or *ready*. Actions: **Send replicant** (travel to the location), **Deliver materials** (the system's in-system AMI transport controller gets a `delivery` directive from the biggest piles, for exactly the shortfall), and **Fulfil**. Fulfil sends `POST /locations/{location}/events/{designation}`; you can override that under *How to fulfil*. The **Work on contracts** rule on the Automations page fulfils events as soon as they're ready. It can also deliver the shortfall from other stockpiles and send a replicant that's already in the system; both are optional and off by default. Completed events are listed with what they used and what they paid |
| **Diagnostics** | **Capture live snapshot** makes read-only GETs: every device, stockpiles, mining controllers (full detail), the belts your miners and controllers are at, recent mining and AMI events, and the detail and latest logs of up to 6 idle drones. It's capped at 60 requests on the background budget and can be limited to chosen systems. The result is bundled with the app's own state: rule settings, dry run, AMI schedules, recent jobs and their errors, the automation log, restart cooldowns, exhausted belts and which controller manages what. The page shows a **mining diagnosis**. For each controller it lists the directive, `_eval_state`, drones run and drones mining, and the open sites at its belt. For each drone that isn't mining it gives the reasons, such as no open sites at the belt, the controller has no directive or reports exhausted, the drone isn't at a belt, it's out of range, it's in a restart cooldown or a rule is off, and the fix for each. **Download snapshot (JSON)** saves every request path, time and response, which work as test fixtures. The API token is never included. |
| **Fleets** | Mobile fleet command. A fleet is a named group of devices tagged `fleet:<id>`, with a role (mining / explore / trade), a home system and its own loadout. Loadouts, idle-miner restarts, salvage, re-open sites, auto-survey and AMI schedules never recruit fleet devices, even in a busy system. The loadout editor is a list of lines. Picking a type in the blank **+ add a type…** line sets its quantity to 1 and opens a new blank line below it. Every change saves the whole list, and lines with no type or a quantity of 0 are ignored. Setting an existing type to 0 or clicking **✕** removes it completely. If that type has member devices, the app asks first, and those devices leave the fleet. **Save** applies name, role and home changes. Under the loadout, the **carrying budget** shows two lines, each giving needed / available. The first covers the current members, plus how many are stowed or attached right now. The second covers the full loadout, with each type counted at the larger of want and have. Carriers offer **hold slots** and **attach points**: surge plate 1, platform 4, carrier 9 and mobile fleet 36 attach points; a cargo vessel has 50 hold slots and 3 attach points. A heaven vessel has 10 hold slots and a racing vessel 5, or whatever the device or blueprint reports. Every device that can't surge needs a seat. A device that has the `stow` feature goes in a hold first, so the attach points stay free for devices that don't. In live data, drones, AMI controllers, maintenance drones, beacons, relays and wards have it, and transport drones and haulers don't. Hold overflow also uses attach points. Freighters and other surge-capable devices fly themselves. A shortfall is shown in red, with how many surge carriers, platforms or cargo vessels would cover it. When boarding, a device stows itself into a hold (`stow` with `target`), or the carrier attaches it; unloading deploys or detaches it. The full-loadout line and the per-type **Seats** column update live as you edit. In that column, ▣50 +3 is a carrier's hold and attach points, −1 ▣ is a rider that can go in a hold, −1 is one that needs an attach point, and ✈ is a device that surges itself. **Print what's missing** queues the shortfall with the fleet tag, so new devices join the fleet as they come off the line. Adding a device swaps its `home:`/`spare` tags for the fleet tag. Carriers are any surge plates, platforms, carriers or mobile fleets in the fleet; the others attach to them, and freighters surge on their own. Missions run phase by phase, one job each, and the page shows the current phase, the job step and a log:
Every mission has a **gather** phase after assembling. The carrier with the most room tours, nearest first, the systems where the fleet has stranded members (no fleet carrier there). It also recruits *spare* devices that fill gaps in the fleet's loadout, nearest first: they get the fleet tag and lose `spare`/`home:`. They fly to the carrier and attach. Self-surging members simply join at the target. Anything that doesn't fit is logged.
• **Mining:** assemble → gather → travel → deploy → work → watch → recall → return → unload. Work means everyone goes to the richest belt; the survey controller runs `belt_search` and the mining controller `gather_evenly`. In *ferry* mode the transport controller also ferries to the nearest destination system; otherwise freighters fill up on recall and haul the load home. Watch recalls the fleet once the mining controller has reported `exhausted` for N minutes with no drone searching.
• **Explore:** for each target in order: travel → deploy → `survey_system` → wait for `no_targets` → recall; then home.
• **Trade:** a freighter loads the trade's price at home, the fleet travels to the trader's system, the freighter goes to the trader's location, then the trade runs (`POST /devices/<trader>/trades/<code>`); then home. Use **Refresh traders** to load the directory.
Buttons: **Recall now**, **Retry** after a stall, and **Stop**. Boarding and movement steps are critical: a device that can't board or travel stops the mission with the reason, instead of being left behind. |
| **AMI** | Per-controller targets from the latest scan reports of the controller's own system (drop-downs grouped by belts, resource sites, salvage, stockpiles, Lagrange points… with resource levels and stock), **Refresh targets**, controllers with latest digest, directive picker with config templates, adopt/release, launch/withdraw |
| **Automations** | **Re-open resource sites:** belts never run out, but only open sites can be mined. A survey drone opens a site by searching, then stays there tracking it. When a mining controller reports `exhausted`, or a drone is told "Belt exhausted", the system's AMI survey controller flies to the belt, adopts idle survey drones up to the number per belt, and runs `belt_search`. With no survey controller, idle survey drones fly there and `search`. Drones that are tracking or searching are never marked spare or moved, and while sites are being re-opened the mining controller keeps mining instead of switching to salvage. **Salvage when mining sites run out:** when every known site at a belt is depleted and the system has salvage, the system's AMI mining controller gets `gather_salvage` for the biggest salvage. The location sent is the body the salvage orbits, e.g. `AEMEROTH-6-7` for `AEMEROTH-6-7-SAL-1`. It adopts idle drones there first and is then launched. With no mining controller in the system, idle drones at the worked-out belt fly to the salvage and mine its main resource, a set number per salvage. When a salvage is used up, the next one is picked. Salvage **recall** defaults on, so drones return to their controller when a salvage is used up. **Back to the belt**: a mining controller can get stuck while a belt in its system has open sites again (re-read at most every 10 minutes). That happens when its directive is exhausted at a body (its drones left at used-up salvage), stale-exhausted at a belt that has re-opened, paused, or done with salvage. Its drones are then released, flown back and re-adopted, and the directive is re-set and launched. The directive is the one its AMI schedule uses, else `gather_evenly`. Each controller waits 30 minutes before it's retried, and AMI schedules leave these controllers to this rule. Restart idle miners skips drones at worked-out belts. Other server-side rules with on/off checkboxes and a global **dry run**: system scan on arrival, auto-survey new systems (deploy carried survey drones, visit each planet/belt in turn, scan/search, return and stow), deploy a carried FTL beacon in new systems, restart idle mining drones (or hand them to a mining AMI), periodic **AMI schedules** that re-issue directives to idle controllers. Active jobs step-by-step with Cancel, recent jobs, log |
| **Events** | Full local history with filters and payloads, backfill from the game |
| **Notifications** | In-app alerts (hub warnings, incoming objects, failed teleports, depleted sites…) and accomplishments (prints, completed directives, trades, discoveries…); toasts + bell badge live |
| **Messages** | Game messages (mark read) and BobNet chat |
| **Console** | Call any API endpoint through the server-side client (account wipe/token recovery are blocked) |
| **Account** | Game account, achievements, sync health, audit log of every command issued from the UI |

**"Since last login"**: a gap of more than 30 minutes (`VISIT_GAP_MINUTES`) between page loads starts a new visit. The dashboard then summarises everything since you were last here: devices printed, resources mined and delivered, net stockpile change, arrivals, scans, completed directives, XP, alerts and AMI digests. **Got it** hides the summary until your next visit. `/digest?hours=N` shows any window.

### Automations

Any job stops (with an alert) after 3 steps in a row fail with the same error, or 6 in a row with any error. Rules run inside the app container, so they keep going with the browser closed. Each rule, when its trigger fires, creates a **job**, which is an ordered list of game commands. Some steps wait for an event before moving on, e.g. `travel.arrived` for the right destination or `scan.completed` for the right body. If a step errors, or times out (1 h for travel/scans, 2 min for deploy/stow), it is logged and skipped, so one bad target doesn't strand a drone. If a critical step fails, such as the initial deploy, the job stops and you get an alert.

Jobs are saved in SQLite, so they survive redeploys. Their commands go through the same rate limiter using the background budget, and appear in the Account audit log as user `automation`.

Arrival rules (scan, beacon, survey) fire only when a device arrives from another system, by surge or from an origin in a different star; trips inside a system never trigger them. Bodies count as surveyed once a `scan.completed` or `search.completed` event names them. When an AMI survey controller finishes `survey_system`, every body in that system counts as surveyed. Repeat arrivals only visit what's left, and a fully surveyed system is skipped. The AMI path also only runs when the controller has drones to work with. **Survey this system now** runs the rule for a chosen vessel where it currently is.

**Command chains:** every travel command (on a device's Command box, or the replicant's route preview → Go) has an optional **Then, on arrival…** section. You can add up to three follow-ups, each one a device and a command: deploy a carried device, start mining, survey scan, search a belt, system scan, travel on, stow, attach, AMI launch, and so on. They run as a job on the Automations page. Each waits for the previous one to finish (arrival, `device.deployed`, `scan.completed` …). Arriving "at a star" counts when the vessel lands at any of that star's locations. The wait lasts until the game's own ETA plus 30 minutes. The Dry run switch doesn't apply to chains you start yourself.

**AMI schedules:** these keep AMI controllers busy without the app repeating work the controllers already do. Each schedule names one controller, or "all mining/survey/transport controllers" (optionally only in one system). It also sets a directive with its configuration, built with the same picker as the AMI page, and an interval. When a schedule is due, it checks each controller. If **Only when idle** is set, a controller is skipped while its directive is still running; it counts as idle once its last directive event was completed, cleared or paused. A controller that is due can first **adopt** idle drones of the right kind at its location that no other controller manages. It then gets `set_directive`, and is launched if that box is ticked. The **Run AMI schedules** switch turns them all on or off. **Run now** runs one schedule straight away, ignoring Dry run. The last run time and result appear in the table.

How the other rules work with AMI:
- **Auto-survey** (**Use an AMI survey controller**, on by default): if the vessel carries a survey controller, the rule deploys it and the survey drones, lets the controller adopt them, and sets `survey_system` to cover every body. Otherwise it falls back to driving the drones step by step.
- **Restart idle miners** (**Prefer AMI**): drones that a controller already manages are left alone. When a mining controller is at the same location, idle drones are handed to it (adopt, plus launch if the controller is idle) rather than being given `start_mining` directly.

All rules start **off**. Turn on **Dry run** first: rules then log exactly the jobs they would run, and send nothing.

## Setup (about 20 minutes)

### 1. Google OAuth client
1. Go to <https://console.cloud.google.com/> and create a project, e.g. "replicant".
2. Open **APIs & Services ▸ OAuth consent screen**. Choose External, fill in the app name and your email, and add yourself as a test user. It can stay in *Testing* mode.
3. Open **Credentials ▸ Create credentials ▸ OAuth client ID ▸ Web application**.
   - Authorised redirect URI: `https://<PUBLIC_HOST>/oauth2/callback`
4. Copy the client ID and secret.

### 2. Cloudflare Tunnel
1. Go to **Zero Trust ▸ Networks ▸ Tunnels ▸ Create a tunnel** (Cloudflared) and copy the token (`eyJ…`).
2. Add a **Public hostname** such as `replicant.yourdomain.com` with service `HTTP` → `nginx:80`.
   Cloudflare Access is not needed; oauth2-proxy handles sign-in.

### 3. Deploy in Portainer
1. Push this folder to a Git repo (a private GitHub repo is fine). No secrets live in it.
2. In Portainer: **Stacks ▸ Add stack ▸ Repository**. Enter the repo URL (plus credentials if it's private), and use compose path `docker-compose.yml`.
3. Under **Environment variables**, add every variable from `.env.example`:

   | Variable | Value |
   |---|---|
   | `PUBLIC_HOST` | `replicant.yourdomain.com` |
   | `CF_TUNNEL_TOKEN` | tunnel token |
   | `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | from step 1 |
   | `OAUTH2_COOKIE_SECRET` | output of `python3 -c "import secrets,base64;print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"` |
   | `ALLOWED_EMAIL` | your Google address |
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

Never set `DEV_USER` in the stack: it makes the app trust requests with no identity header.

## Configuration (app)

| Env | Default | |
|---|---|---|
| `RS_API_TOKEN` / `RS_API_TOKEN_FILE` | – | game token (env var, or a file path if you prefer a mounted secret) |
| `RS_API_BASE` | `https://api.replicant.space/v1` | |
| `ALLOWED_EMAILS` | – | comma list; empty = anyone oauth2-proxy lets through |
| `RS_GET_PER_MIN` / `RS_ACT_PER_MIN` | 110 / 55 | client-side budget (game: 120 / 60) |
| `RS_GET_RESERVE` / `RS_ACT_RESERVE` | 30 / 20 | budget background polling may not touch |
| `POLL_ACCOUNT`, `POLL_DEVICES`, `POLL_INVENTORY`, `POLL_MESSAGES`, `POLL_BLUEPRINTS`, `POLL_CATALOGUE` | 60, 60, 120, 300, 300, 1800 s | blueprints also refresh after likely-unlock events and when the Blueprints page opens |
| `VISIT_GAP_MINUTES` | 30 | idle gap that starts a new visit for the digest |

## Layout

```
docker-compose.yml     the Portainer stack
nginx/                 nginx image with the auth_request config
app/rsweb/api.py       rate-limited API client + SSE parser
app/rsweb/ingest.py    event stream ingester, timers, pollers
app/rsweb/notify.py    event text, notifications, visit tracking, digest
app/rsweb/web.py       pages, htmx partials, actions, live stream to the browser
app/rsweb/mock.py      fake game API for dev/tests
API_REFERENCE.md       API notes compiled from the official docs (field names to verify against live responses)
```

## Caveats

The API reference was compiled from the public docs, not from the live API. A few response shapes are undocumented: messages, achievements, reputation, and some device detail fields. The UI copes with missing or unknown fields and shows raw JSON where it can't lay a response out. If something looks off, check the response in the **Console** and adjust the matching template.
