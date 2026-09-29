# Replicant Space web client (self-hosted)

A personal web client for [Replicant Space](https://replicant.space/), the API-first Bobiverse-inspired game.
It runs as one Portainer stack. You reach it through a Cloudflare Tunnel and sign in with Google through oauth2-proxy.

```
Internet ─► Cloudflare ─► cloudflared ─► nginx ─(auth_request)─► oauth2-proxy ─► Google sign-in
                                           │
                                           └─► app  (FastAPI + htmx, SQLite, holds the game token)
```

- **No host ports are published.** The tunnel is the only way in, and nginx checks every request (including the live-update stream) with oauth2-proxy.
- **The game API token stays on the server.** It is a Docker secret inside the `app` container and never reaches the browser.
- **One process talks to the game.** It keeps one connection to the event stream, polls on a schedule, and paces every call under the game's limits (120 reads + 60 actions per minute), keeping some budget in reserve for your clicks.
- **Full event history.** The game only keeps about the last 10,000 events. The app stores everything it sees in SQLite on a named volume.

## Features

| Page | What it does |
|---|---|
| **Dashboard** | *While you were away* digest, open alerts, replicants, live countdowns (travel, prints, scans), fleet summary, stockpiles with 48 h trend, live event feed |
| **Fleet** | Every device, filterable by type, status (working / moving / idle), system, tag or text; capacity warnings |
| **Device** | Live detail, run any available command (JSON args pre-filled per command, confirmation for destructive ones), tags, local event history + game log |
| **Replicant** | Route preview (`dry_run`) → go, nearest stars with ETAs, system scan, mining, vessel printing, BobNet |
| **Systems** | Log-scaled top-down map of a system (planets, belts, habitable zone, Kuiper) with your devices and stockpiles; click to load location detail |
| **Galaxy** | 3D map of the star catalogue: your presence, replicants, scanned systems, hubs, relay/hub range spheres, search, straight-line distance, travel estimate & route preview, shift-click to measure |
| **Blueprints** | Cost/print time, how many each printer can afford from local stock, a production planner (quantities → total cost and shortfall), print / enqueue |
| **AMI** | Controllers with latest digest, directive picker with config templates, adopt/release, launch/withdraw |
| **Events** | Full local history with filters and payloads, backfill from the game |
| **Notifications** | In-app alerts (hub warnings, incoming objects, failed teleports, depleted sites…) and accomplishments (prints, completed directives, trades, discoveries…); toasts + bell badge live |
| **Messages** | Game messages (mark read) and BobNet chat |
| **Console** | Call any API endpoint through the server-side client (account wipe/token recovery are blocked) |
| **Account** | Game account, achievements, sync health, audit log of every command issued from the UI |

**"Since last login"**: a gap of more than 30 minutes (`VISIT_GAP_MINUTES`) between page loads starts a new visit. The dashboard then summarises everything since you were last here: devices printed, resources mined and delivered, net stockpile change, arrivals, scans, completed directives, XP, alerts and AMI digests. **Got it** hides the summary until your next visit. `/digest?hours=N` shows any window.

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

**Updating:** push to the repo, then in Portainer open the stack and choose **Pull and redeploy**. You can also turn on automatic updates / GitOps polling. The SQLite volume `rsweb-data` survives redeploys.

**Rotating the game token:** use account recovery on replicant.space, paste the new token into the stack's `RS_API_TOKEN`, and choose **Update the stack**.

<details><summary>Notes and alternatives</summary>

- Deploying from Git with inline `configs: content:` and `secrets: environment:` needs Docker Compose ≥ 2.23, which ships with Portainer 2.20+.
- If you'd rather use Portainer's **Web editor** (no Git), it can't build images. Build and push them somewhere first (e.g. GHCR with the included GitHub Actions workflow). Then replace `build: ./app` / `build: ./nginx` with `image: ghcr.io/<you>/replicant-web-app:latest` / `…-nginx:latest`.
- For LAN debugging, uncomment `ports: ["8080:80"]` on nginx. Google sign-in will still send you back to `PUBLIC_HOST`.
- `ALLOWED_EMAIL` holds one address. It is enforced twice: by oauth2-proxy's allow-list and by the app itself.
</details>

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
| `RS_API_TOKEN` / `RS_API_TOKEN_FILE` | – | game token (the stack uses the secret file) |
| `RS_API_BASE` | `https://api.replicant.space/v1` | |
| `ALLOWED_EMAILS` | – | comma list; empty = anyone oauth2-proxy lets through |
| `RS_GET_PER_MIN` / `RS_ACT_PER_MIN` | 110 / 55 | client-side budget (game: 120 / 60) |
| `RS_GET_RESERVE` / `RS_ACT_RESERVE` | 30 / 20 | budget background polling may not touch |
| `POLL_ACCOUNT`, `POLL_DEVICES`, `POLL_INVENTORY`, `POLL_MESSAGES`, `POLL_CATALOGUE` | 60, 60, 120, 300, 1800 s | |
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
