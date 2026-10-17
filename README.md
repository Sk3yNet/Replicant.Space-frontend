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
| Devices | Tree · Device list · AMI · Lost |
| Map | Galaxy · Stars · Systems · Prospects · Traffic · Trail · Wards & hubs · Defense · Upkeep |
| Fleets | Fleets · Reset & reform |
| Economy | Blueprints · Contracts · Reputation · Shop |
| Activity | Automations · Events · Messages · Notifications |

**Rule settings live on the page each rule works on**, in a collapsed *Rules* panel at the top. The Automations page lists every
rule with an on/off switch, plus the jobs and the log.

### Dashboard and devices
- **Dashboard**: a *while you were away* digest (a gap of 30 min starts a new visit; `/digest?hours=N` for any window), open
  alerts, replicants, tutorial progress (the current tutorial's steps and the next step's hint), achievements (earned, with XP and when, and the ones still to earn), game news (the official changelog and blog, read every 6 hours; new versions and posts ring the bell), live countdowns, fleet summary, stockpiles with a 48 h trend, live event feed.
- **Tree**: system → device type → device, with stowed devices nested in their carrier; each device opens its command panel.
- **Device list** (`/fleet`): every device, filterable by type, status, system, tag or text. Tick devices (or all listed) to send
  them one command, or to **add tags** / **remove tags**; the ones that can't take it — command not available, in use by an
  automation, a deploy/detach while the carrier is between systems — are listed and skipped.
- **Device page**: live detail, any available command with its fields (destructive ones ask first), tags, history and game log,
  cancel travel. Autofactories get a **print queue** card (add with an optional *deliver to*, remove, clear); carrier vessels
  a **carrier** card on every carrier (launch / stow / recall for a hold, detach / attach for attach points); stow, deploy, attach and detach work from either side (the carrier's
  page lists its hold / attachments, the cargo's page lists carriers here — only the side the device fits); *Decommission at an autofactory* takes a device there so the autofactory learns
  its blueprint. `enqueue_print` on a replicant's vessel is sent as the replicant's own print (one at a time).
- **Replicant page**: route preview and travel (with *then, on arrival…* follow-ups), nearest stars, scan, mining, vessel
  printing, profile editing, FTL slingshot.
- **AMI**: controllers with their latest digest, a directive picker with targets from the controller's system, adopt / release /
  launch / withdraw, and **AMI schedules** that re-issue directives to idle controllers.

- **Lost** (Devices › Lost): devices out of sight — surging, left out of the device list, out of relay range with no replicant
  in their system, or aboard a carrier that is — with their last known location and where they're heading, until they're seen
  again (then listed under *Seen again*). Write one off to stop tracking it. *Gone, with the reason* lists devices that
  left for a known reason (destroyed by an asteroid, lost to a diversion, decommissioned, sent to another replicant, paid
  in a trade or to a contract, consumed by a print, contributed to a megastructure, held in a trade's escrow).

### Maps
- **Galaxy**: (every layer and each kind of star marker — mine, scanned, hubs, replicants, new finds, prospected from, my wards, megastructures (violet hexagon), surveying — has its own checkbox; your choices are remembered in this browser) 3D map of the star catalog (plus stars from censuses and your observatories; your systems load first, the
  rest of the catalog after), live: ships in transit, fleets,
  supply lines, mining sparkles and **prospecting cones** (each observatory scan: a wave running out to its reach) redraw as events
  arrive. Relay/hub range, search, distances, route preview; right-click (on touch: long-press) two stars to measure. **Other players** (coral):
  their fixed devices from the latest scans, a shield where someone else's ward or hub is, and an arrow for each arrival or
  departure your beacons logged in the last hour (with the likely other end of the trip), all fading with distance from the
  point the view is centered on. The **trail** of the replicant you follow is drawn in gold.
- **Relays** (Map › Relays): *Live links* asks any relay or hub which neighbors it has really linked with (and flags links expected but missing); your relay networks — working relays (7.5 ly, or the range a relay reports) and active hubs (15 ly, by distance like relays — checked live 2026-10-15; on the galaxy map a hub's links are gold, and two relays both linked to the same hub don't show their own link); the range sphere per system is its working relay or hub that reaches furthest; a delivered relay is switched on before its carrier leaves; a relay out of range but on is a network of its own (whether it's on: the last activate/deactivate, relay.activated, or the last time it was seen relaying or inactive), and a switched-off relay gets a red ring unless its system has a working relay or hub
  (15 ly) that reach each other — each in its own color, **active** when one of your replicants is within reach (only then
  can what it covers be commanded), with suggested bridges (the fewest new relays, at most 10, joining two networks) and
  *Add route* to order them. On the galaxy map (*relay network* option, off by default) links are curves, solid for active
  networks and dashed for ones without a replicant; relays on order are white rings, suggested bridges dashed gray.
- **Moving a replicant**: the Replicant page lists empty replicant matrices with *Transfer* (same location, instant) or *Teleport* (another system, over the FTL network), each with a confirmation.
- **Megastructures**: a megastructure's location page shows its progress and what it still needs, with a Contribute button for your devices there; `/megastructures` has the leaderboard.
- **Lockers** (Economy › Lockers): equipment lockers (NPC devices that hand out a set loadout once per account, e.g. SOL-OORT, TWAFFY-OORT): scan where a replicant is, then retrieve.
- **Simulations** (Economy › Simulations): queue a run of a scenario at the datacenter's replicant interface
  The page also lists the interface's **research** topics (each unlocks blueprints, once per account; a replicant must be there), the all-player **leaderboards** (per scenario: top 10), and the game's view of a run in progress.
  (MIRFAKA-OBJ-1): spare compute cores are pinned there (else printed at an autofactory, logistics bringing the
  materials, the loadout pass the cores), then a free replicant is sent. When cores, replicant and interface are all
  there, the bell nags (every 2 h) until you press *Start*. The bootstrap's compounding logic plays the run on the
  simulation's own devices (no other automation touches them; everything from the virtual galaxy, the VIRT… systems, is kept apart: its devices, stockpiles, stars, surveys and events never reach your real numbers, map or alerts, and the page shows them in a Virtual galaxy card), recording when each build-order step was reached. When
  it ends, everything from inside it (devices, events, scans, jobs) is purged; the report stays (score, resources
  mined, devices printed, the step times), and the next run follows the build order you choose: a Build orders card holds named orders, one step per line
  (`scan system`, `deploy all`, `mine: structural, conductive`, `print 2 mining_drone`, `wait: 500 structural`,
  `mine until objective`); the run card crosses steps off as they're done and the timeline records when.
  Runs in the game lists every run the game has going: End early on any of yours, and devices / galaxy links that open the
  normal Devices page and Galaxy map on that run's own data (`?sim=<run>`: its devices, stock and the virtual stars
  around its replicant, read live through it and cached for a minute). A run's clock runs faster than real time, so when one of its
  jobs finishes the next step starts at once rather than on the next engine tick. On a run's galaxy map the star rings
  shrink to fit its tighter star spacing.
- **Stars**: unexplored stars nearest a system, with ETA and route; stellar census per vessel.
- **Systems list**: per system, the **Fleets** homed there (⌂) or on a mission there (→), and (with *Wards, beacons &
  relays* ticked) your ward, FTL relay and FTL beacon with *add* (activate an idle one there, else send a spare, else print
  one in range; a relay is placed at an L4/L5 point) and *remove* (deactivate; it stays deployed) or *cancel* an order.
  ✦ marks a local civilization (green: your beacon is at its body). A beacon requested for such a system goes on to the
  civilization's body, carried by a vessel in the system.
- **Star sizes**: the Galaxy and System maps draw each star by its radius (the scan's `radius_solar`, else the typical
  radius of its spectral class, giants ten times that); the star's details say which.
- **System page**: a top-down map with your devices, stockpiles, sites and salvage. Three cards: **Resources** (per
  resource: richness, open sites, % left, ≈ units left — learned from what past sites gave — salvage, stockpiled),
  **Belts** (richness, a viability line, open sites) and **Bodies** (planets, moons, L-points with their salvage and stock).
  **Prospects** (Map tab) scores every scanned system out of 100 (each resource's yield, scarce 1 to rich 10, × belt density,
  weighted by rarity: rares 3, volatiles 2.5 … structural 1; a stationed fleet's home is scored for review only, marked
  *home*, and ⚑ marks systems where a fleet of yours is homed), with a **Ward** column — ⛨ yours / ⛨ theirs — and *add ward*
  (activates an idle ward of yours there, else sends a spare, else prints one in range on the autofactory with the shortest
  queue, then switches it on once deployed; a queued print gives way to a spare that turns up; 25-ward cap) / *remove*
  (deactivates yours, it stays deployed, and drops a print not yet started). Surveyed checkboxes per body. *Other players here*: their
  fixed devices (beacons, relays, wards, hubs, factories, observatories, controllers) from the latest scan, also drawn on the
  map; a replicant arriving in a system scans it, or use *Scan for other devices*. Snapshots older than 7 days are dropped.
- **Traffic**: civilization contact (a beacon must sit at the body of the civilization's event), visitors, each beacon's audit log,
  redundant beacons. How often beacons are read (default 10 minutes).
- **Trail**: follow a replicant through the audit logs of public FTL beacons; candidate systems with travel time and a travel
  button; *follow to the end* keeps going until the target stops moving (for Bill, until it finds his AMI trade controller). *Reset trail* clears the beacons and logs when the
  target relocates. Bill (A8F48B26) always passes through SOL: with nothing to follow, or a cold trail, the follow picks
  him up there, and *restart from SOL* clears the trail and starts over. Leaving his shop he can't be followed (he resets to SOL), so a departure
  from his shop's system sends the follow to SOL. His BobNet posts about moving ("NOSOXA is in the rear-view mirror")
  clear the beacon logs, restart the follow at SOL and set his old shop location aside.
- **Wards & hubs**: your wards (against the 25 cap), hub shield and upkeep with warnings, `evicted_miners` log. Other players'
  warded systems are left alone by every mining rule and mission.
- **Terraforming** (Map › Terraforming): each world with a terraform monitor: its latest `terraform.status` (attributes, change per tick, projections +6/+12/+24, what drives them, warnings, anomalies), coupling rules active or close, bio seeder preconditions; your terraforming devices with strength (0–1), direction (reversible ones) and on/off. **Dropping rocks**: detect Kuiper objects, launch one with a vector charge at a planet or moon, set propulsor / trajectory deflector directions; rocks you launch are left alone by asteroid defense.
- **Defense**: incoming asteroids with a propulsor estimate and **Defend now**. **Evacuation** (game v3.5.0: what's left at an
  asteroid is destroyed on impact or lost on diversion): within the hour before impact, or at the game's
  `system.impact_warning`, your devices there leave for the system's entry point (propulsors still pushing stay until 30
  minutes out, and are recalled as soon as the asteroid is diverted enough: impact likelihood at or below the Asteroid defense rule's setting, default 1 %, as it never reaches 0; an asteroid with propulsors on it is read every 5 minutes, also a setting; one that can't fly boards a carrier there), their `at:` pin to the asteroid is dropped, and a stockpile there
  is logistics' top priority. Nothing more is sent or printed for an asteroid due within the hour. An event is over once its impact time passes: it's no longer read, alerted or evacuated, and moves to the Concluded list; the card title shows the warning's destination. An asteroid is named by its location designation (STAR-OBJ-n): the game's impact warning names none and comes from where your devices are, so it's filed under the asteroid tracked in that system (else the system's scan names it at the next read), and the warned place is evacuated too. **Upkeep**: wear per system and maintenance
  coverage.

### Fleets
A fleet is a named group of devices tagged `fleet:<id>`, with a role (mining / explore / logistics), a home and a loadout (its own
lines or a template).
- **Fleets** (the landing page) shows each fleet's status against its loadout, loaded just after the page, and the templates
  and loadout settings. **Fleet details** has the slower parts: each fleet's card (members, loadout, missions), what the next
  loadout pass would do, the tags & controllers check, and what's spare or on the way.
- **Stationed** fleets are kept at their loadout in their home system by the **loadout pass**: extras become `spare`, spares
  are sent where they're short (within the supply range, default 100 ly), what's still missing is printed and carried there
  (large devices are compacted first). Placed beacons and relays are never made spare or moved. A borrowed carrier only comes
  from within the supply range of the pick-up (the trip itself can be any length, e.g. a new home across the gap); a fleet's
  own carriers go anywhere. Spares are only gathered to a depot within range. **Supplied from** (Overview): a hub whose
  autofactories print the fleet's shortfalls and whose carriers bring them, at any distance — e.g. a Sol-sector fleet
  supplied from a home-sector printing hub.
- **Nothing leaves short:** a fleet only sets off with its whole loadout in its home system (aboard, there, or arriving).
  A launch with gaps is refused (tick *Launch anyway* to send it short); a mission whose devices didn't all make it waits
  at gather, refilling from spares, until complete or *Launch anyway*; auto-scout and auto-deals wait; a **new home** stays
  *pending* (the fleet keeps its current home, where the loadout pass fills it) and the fleet moves once complete, or on
  *Move anyway*. Surge-capable members don't hold a move up unless the fleet needs them as carriers: each flies straight
  to the new home from wherever it is, once it has finished its current task.
- **Missions** run phase by phase (assemble → gather → travel → deploy → work → … → return → unload) with a log, *Retry*,
  *Stop*, *End mission & board* and *Recall*. Mining works the richest belt (or salvage); explore surveys each target (it no
  longer leaves relays or beacons: ask for those on the Systems list; an already-surveyed target is skipped, and the
  crew stays aboard at home). Trade fleets are retired: the logistics service does their work, and each one becomes a logistics
  fleet (same members and loadout) once its last run ends. A recall withdraws a launched AMI controller (its
  drones come back) before it boards. No automation sends a drone or AMI controller on an in-system cruise longer than the
  max cruise (Fleets › settings); a manual Travel that long asks first (surge-capable devices excepted). Every
  automated trip logs its expected arrival; a step refused while something is still on its way waits for that ETA.
- **Path forward** (stationed mining fleets): a heading; when the home runs dry the fleet picks the best system ahead and moves.
- **Observatories** prospect wherever they're deployed — the heading (else outward from Sol), then 13 other directions — and
  compact when every direction is used up or the fleet is leaving.
- **Auto-scout** (explore fleets): surveys the nearest unsurveyed systems within 100 ly of home, ring by ring (only inside relay
  coverage without a replicant aboard); sets out at 85 % capacity, comes home for repairs at 50 % and deploys its worn members so they can be repaired. Any mining or survey fleet does the same: a member at or below 50 % while it works sends it home
  (it recalls, returns and unloads as usual), and back home members below 85 % are deployed from their vessel to be repaired.
- **Prospector** fleets (role *prospector*: a galactic observatory and a carrier with an attach point): a tour of the
  stars within 30 ly of the fleet's home. Each star is prospected from once (the game allows one prospect per star),
  aimed at the emptiest hemisphere; then the observatory compacts and the carrier takes it to the next star — the
  emptiest neighborhood not prospected from yet, nearer first, commandable, and not another prospector's. A tour that
  hasn't moved on or prospected for 12 hours, or has no star left in range, raises a warning in the bell.
- **Logistics** fleets (Fleets › Logistics): no missions; their devices deliver materials and devices on demand. A print delivery to another system leaves with its hold at least a quarter full (or carrying all that's wanted); a smaller load waits for more to be spared, also when a print is stalled if the trip is longer than 15 ly. The page's **In transit** card lists every hold with materials in it (what, where, heading for, which run), with totals; the dashboard's stockpiles show "+ N in transit".
  Requests: queued prints (their cost minus what's stocked), contracts you approved or whose civilization you pre-approved
  (Contracts), trades you approved (Shop: *approve*, used once; plain shop listings are never fulfilled), a home system's
  piles consolidated at its drop point (its autofactory, else its biggest pile; systems the Consolidate rule covers with an
  AMI transport controller are left to it), and stray stockpiles brought back to a gather point (a fleet that takes
  materials in) — or, where such a fleet moved away, to its new home; with no gather point, the page says so. Deliveries draw on
  stockpiles beyond the network too while a replicant is free to go along. Priority: queued prints (waiting ones first), local, long-range, returns; bump or skip a request on the Logistics
  page. Home systems keep what their own prints need, but hubs share that reserve so each one's print queue is paid for
  about as many hours ahead (print time over its autofactories): the one with the fewest hours gets what its next print
  lacks from the one with the most, while the giver still has at least as many hours left. Inside one system a free AMI transport controller (drones / haulers)
  gets the delivery as a `delivery` directive; logistics covers what it can't. Ships are picked nearest first, then the
  smallest that takes the rest in one go (else the biggest), and as many as the load needs go at once, as one convoy:
  freighters and cargo vessels fly themselves (a freighter is too large to attach); haulers and drones stay in their
  system unless they ride (attached) on a free logistics carrier (plate, platform, carrier, fleet carrier). A ship still
  holding what's needed (say, from a trip that failed) is used first, and delivers without picking up again. Outside an
  active relay network one device hosting a replicant goes with the whole convoy — a carrier with a replicant's matrix
  aboard (a fleet carrier holding a matrix container) counts — or nothing goes; when a delivery completes a contract or
  trade, that replicant fulfills it, and the convoy loads the payout there and brings it to the nearest gather point
  (devices paid out are sent there as deliveries). Afterwards everyone waits inside the network. Device deliveries (and fleet moves done
  piecemeal) take logistics carriers first. **Rescues** (last priority): a device out of command range (placed relays and
  beacons, wards, hubs and fleets on a mission excepted) is brought home — its fleet's home, else the nearest covered home.
  One that can surge is reached by a replicant in its system or anywhere its dark relay network covers, and commanded
  home; one that can't is picked up from its last known location by a carrier with a free seat, a replicant along.
- **Bootstrap** fleets: from a bare heaven vessel (three mining drones and a replicant aboard) to an autofactory hub with three
  mining outposts. Home: the drones mine the best belt, the vessel mines onboard what they get little of (usually rares and
  volatiles), and the replicant prints survey drones and miners until the hold-sized kit is built (a home another player has
  warded, like Sol, is skipped). Then a survey of every system within the radius (10 ly), **your OK on the hub**, the move
  there, compounding to an autofactory printed on site, and the hub as a stationed fleet that takes materials in. Outposts
  (**your OK on each**) must keep the relay chain (each within 7.5 ly of the hub or an outpost; else a relay waypoint, also
  your OK), and send their materials to the hub by cargo freighter. The hub and every outpost get an AMI mining controller
  (`maintain_ratios`: the mix the bootstrap's missing devices cost) and an AMI survey controller (`belt_search`); while the
  salvage rule has one on salvage or resting at a dry belt, the bootstrap leaves it alone. Start one with *New bootstrap*
  (a replicant's vessel and what's aboard), or *Convert to bootstrap* on an existing fleet that has a replicant's vessel;
  *Back to a normal fleet* makes it a stationed fleet again, homed where most of its devices are. A ward goes up where it costs under 6 hours of the
  system's output, or at once when another player's drones mine there. Everything it prints is tagged `boot:<id>`: the
  loadout pass never lends it others' spares, factories or carriers, nor lends its own.
- Also: owner hand-over (`change_owner`), materials (*send to* / *takes materials in*), *fill from spares*, carrying budget,
  *Reset & reform* for messy tags. Hand tags: `to:<system>` sends a device there (its carrier is found for it),
  `at:<location>` pins it to a spot; `to:<location>` (e.g. `to:sol-3`) is rewritten as `to:sol` + `at:sol-3`.

### Economy
- **Blueprints**: costs and print times, what each printer can afford, a production planner that queues prints (spread over
  several autofactories) and can mine or deliver the shortfall.
- **Contracts**: in-game events with criteria (any one option completes it), rewards and progress; *send replicant*, *deliver
  materials*, *fulfill*. **Deliver devices** sends spare devices to a contract's location; printing what no spare covers waits
  for **Authorize printing**.
- **Reputation**: account and per-replicant reputation, known species. **Shop** (and a Trades section on **Contracts**): your trade controllers, trades, other traders —
  every replicant's trader directory plus every shop a system scan lists, the newest sighting of each winning (shops can
  move). A trade run paid in devices gets spare ones delivered and deployed at the shop; a failed trade sets that shop
  aside until it's seen again.

### Activity and tools
- **Automations**: every rule, jobs step by step with Cancel, and the log. **Events**: full local history with filters.
  **Messages**: game messages and BobNet channels. **Notifications**: alerts and accomplishments, with toasts and a badge.
- **Diagnostics**: a live snapshot (read-only GETs plus the app's state) with a mining diagnosis, downloadable as JSON;
  a **Performance** panel (pages slowest on average, the slowest requests with their queries, game API calls, engine
  lock waits and saved values decoded; engine stage timings and lock holds; table sizes and the largest saved values,
  also in the snapshot). Every page answer carries a Server-Timing header, and requests over 1 s are logged;
  feedback to the game's developers. **Console**: call any API endpoint through the server-side client; presets for every documented query, grouped, with fill-in blanks (replicant, device, location … with suggestions) that build the path and body, which stay editable before sending.
- **Desktop wallpaper** (optional): the Galaxy and System maps as a live Windows wallpaper with
  [Octos](https://github.com/underpig1/octos) and [sk3ynet/replicant-space-octos](https://github.com/sk3ynet/replicant-space-octos).
  *Account › Desktop wallpaper* creates a read-only link (`/wallpaper/<id>/#key=…`); options such as `view=galaxy|system|cycle`,
  `cover=0`, `fleets=0`, `supply=0`, `production=0`, `hud=right|left|off`.

### How automation works

**Long trips** (e.g. ~500 ly from the home sector to the Sol zone): a wait for an arrival lasts until the game's own ETA
(+30 min) and is extended while the device is still moving (up to 48 h); a command the game refuses because something
is still travelling is retried every 5 min (up to 3 h) while it's still heading where the job sent it. A canceled trip,
an arrival somewhere else, or a carrier that turned back stops the job (a fleet mission stalls; *Retry* flies again), so
nothing is deployed or unloaded anywhere but the destination.
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

### From the OpenAPI spec (1.117.0)

- **Leaderboards** (Activity › Leaderboards): every board the game keeps (XP, fleet, trades, distance, reputation,
  colonies …), top 10 each, your replicants in bold.
- **Blog** (Activity › Blog): read and search the game's blog, write a post as one of your replicants, edit or delete
  your own.
- Dashboard: the game API's health (API, database, Redis), checked every 5 minutes.
- System pages: the location events you've discovered there, the game's own stockpile figures, and the stars nearby
  (new ones join the map, like a census).
- Replicant pages: the game's event log for that replicant (filter by event type).
- Device pages: permissions (other accounts allowed to use the device), with grant and revoke.
- **Materials** (Fleets overview): a fleet is *receiving* (takes materials in) or *supplying the nearest receiver*: its
  materials go to the receiving fleet nearest its home (recomputed whenever fleets change), and a mission that ferries
  while mining delivers to the receiver nearest the belt it mines. A fleet set earlier to send to one named fleet keeps it.

