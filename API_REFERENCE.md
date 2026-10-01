# Replicant Space API — Developer Reference

Compiled 2026-09-29 from https://replicant.space/docs/ via WebFetch (page text is summarised by a
fetch model, so JSON blocks labelled "verbatim" were explicitly requested character-for-character;
everything else is as-reported). Anything not seen in the docs is marked **(INFERRED)**.

---

## 0. Global conventions

| Item | Value |
|---|---|
| Base URL | `https://api.replicant.space/v1` |
| Auth | `Authorization: Bearer <api_token>` on every call |
| Content type | `Content-Type: application/json` for bodies |
| Timestamps | ISO-8601 with tz offset in the account's timezone (e.g. `2026-05-17T13:55:41+01:00`). Some fields come back without offset (e.g. achievements `last_achieved_at: "2026-07-04T18:22:10"`, webhook `verified_at: "2026-05-27T06:24:19.917217"`) and a few in `Z` (event stream, prospect `completes_at`) — parse leniently. |
| Durations | numeric seconds, field name suffixed `_seconds` (may be float, e.g. `41.1`) |
| Distances | AU in-system (`_au`), light-years interstellar (`_ly`) |
| Device / replicant codes | 8-char uppercase hex (e.g. `B58FCC78`), never reused |
| Versioning | `v1`; new fields added without version bump — **ignore unknown keys** |
| Unread header | every response carries `X-Replicant-Space-Unread-Count` |

### Response envelope
**No `{"data": ...}` wrapper.** Resources are returned as top-level objects; lists are top-level
keys named after the collection (`"devices": [...]`, `"events": [...]`, `"replicants": [...]`,
`"blueprints": [...]`, `"locations": ...`, `"trades": [...]`, `"traders": [...]`, `"audit": [...]`,
`"messages": [...]`, `"achievements": [...]`).
Actions typically return `{"status": "<verb_state>", ...}` (e.g. `travel_initiated`, `enqueued`,
`mining`, `stowed`, `decommissioning`, `teleporting`, `transferred`, `replicated`, `launched`).

### Pagination (three styles coexist)
1. **Cursor (most lists)** — query `cursor`, `limit`; response `next_cursor` (`null` when done).
   - Integer device-ID cursors: `/devices`, `/replicants/{code}/devices`, `/scan/devices`, `/devices/tags/{tag}`, `/messages`, `/replicants` directory, legacy replicant events, device logs, beacon audit, BobNet messages.
   - String cursors: `/v1/events` (stream-style id `"1784292762940-0"`), `/v1/inventory` (location code, e.g. `"TARAZEDAR-BELT-1"`).
   - `latest=true` (newest first) is **incompatible with `cursor`** on endpoints that support it.
   - Default `limit` 20, max 50 for device lists; `/v1/events` default & max 100; `/devices/tags/{tag}` default 10.
2. **Page-based** — `/replicants/{code}/stars`: `page` (default 1), `per_page` (1–50, default 10); response echoes `page`, `per_page`.
3. **Unpaginated** — `/v1/stars` (full catalogue), `/v1/blueprints`, `/v1/locations`, `/v1/achievements`.

### Error envelope
```json
{ "error": "Insufficient conductive resource to print this device" }
```
Single `error` string; category from HTTP status. Some errors add a `detail` object (seen on observatory prospect):
```json
{
  "error": "No new stars visible from this location",
  "detail": { "neighbours": 22, "outward_neighbours": 14, "expected": 16.8, "ratio": 1.31, "outward_ratio": 1.667 }
}
```

| Status | Meaning |
|---|---|
| 400 | validation / game-state errors: missing fields, invalid commands, invalid destination, criteria not met |
| 401 | missing or invalid bearer token |
| 403 | not your replicant/device, email not verified, gated resources |
| 404 | missing replicant, device, star, location, event, trade, or token |
| 409 | account still provisioning, duplicate email, replicant offline |
| 410 | expired verification link |
| 413 | oversized request body |
| 429 | rate limited |
| 500 | server error |
| 503 | maintenance mode, galaxy not seeded, etc. |

### Rate limits
- Global per token: **GET 120/min**, **actions (POST/PATCH/DELETE) 60/min**.
- Per-endpoint: registration 10/h, verification 30/h, webhook changes 12/h, feedback 10/h, **star catalogue 1/min**.
- 429 headers: `Retry-After` (s), `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset` (unix ts).
- 429 body is different from the normal error envelope:
```json
{ "code": 429, "status": "Too Many Requests" }
```

### Location codes
`STAR` · planet `STAR-2` · moon `STAR-2-1` · belt `STAR-BELT-1` · resource site `STAR-BELT-1-SITE-2` ·
salvage `STAR-1-3-SAL-1` · system object/asteroid/megastructure `STAR-OBJ-1` · Lagrange `STAR-4-L1..L5` ·
outer `STAR-KUIPER`, `STAR-OORT`.
Galactic coords: `position: {x, y, z}` in **light-years offset from Sol (0,0,0)**; +x toward Galactic Centre, +y direction of rotation, +z Galactic north.

### Resources
`carbon`, `conductive`, `rares`, `silicates`, `structural`, `volatiles`.
Abundance strings: `scarce`, `low`, `moderate`, `high`, `rich`. Belt density: `sparse`, `moderate`, `dense`.

---

## 1. Accounts

### POST /v1/accounts — register (no auth)
```json
{ "email": "bob@replicant.space", "name": "Bob", "timezone": "Europe/London" }
```
201: `{"message": "Verification email sent. Click the link in the email to activate your account."}`
Verification link returns:
```json
{
  "api_token": "OsiJIqbw_8tj4SLgeo_...",
  "message": "Email verified successfully",
  "replicant": { "name": "bob-1", "replicant_code": "C2AF4A82" }
}
```

### POST /v1/accounts/recover — new token (old invalidated on verify)
Body `{"email": "..."}` → 200 `{"message": "If that email exists, a verification link has been sent"}`

### GET /v1/accounts/me  (verbatim)
```json
{
  "name": "bob",
  "email": "bob@example.com",
  "email_verified": true,
  "created_at": "2026-05-17T20:53:25+01:00",
  "experience_points_total": 87340,
  "status": "active",
  "timezone": "Europe/London",
  "unread_message_count": 1,
  "replicants": [
    {
      "created_at": "2026-05-17T20:53:43+01:00",
      "current_location": "REGULUZ-KUIPER",
      "current_star": "REGULUZ",
      "device_count": 5,
      "experience_points": 0,
      "hosted_device_code": "11ADA230",
      "name": "bob-1",
      "replicant_code": "77F75255"
    }
  ],
  "bobnet_channels": ["#general", "#trade"],
  "message_notify": {
    "email": false,
    "webhook": true,
    "preferences": {
      "ami": true, "devices": true, "location_events": true, "mining": true,
      "multiplayer": true, "printing": true, "progression": true,
      "scanning": true, "trade": true, "travel": true
    }
  }
}
```
**This is how to enumerate your replicants** — `replicants[]` summary here. (There is no documented "list my replicants" endpoint; `GET /v1/replicants` is the public directory of *all* players.)

### PATCH /v1/accounts/me — settings
Fields: `name`, `email` (re-verifies), `timezone` (IANA), `replicant_cooperation` (`"individual"`|`"shared"`),
`events.ami_digest_interval` (1–30 multiplier), `events.muted` (event-name patterns, wildcards),
`message_notify` (deprecated), `messages.email` (bool), `messages.subscribed` (subset of `"alert"`,`"social"`,`"simulation"`,`"progression"`), `bobnet_channels`.
Example response: `{"replicant_cooperation": "shared", "name": "Bob", "email": "bob@example.com"}`
(Nesting of `events.*` / `messages.*` as JSON objects, e.g. `{"events": {"ami_digest_interval": 3}}` — **(INFERRED)** from dotted notation.)

### DELETE /v1/accounts/me — wipe (email-confirmed)
202 `{"message": "Confirmation email sent. Click the link in the email to wipe your account."}`

### Messages
- `GET /v1/messages` — query `cursor` (int), `limit` (default 20), `latest` (bool), `unread_only` (bool).
  Response shape **not shown in docs**. (INFERRED: `{"messages":[{id, message_type, title, body, created_at, read...}], "next_cursor": ...}` — `message_id`, `message_type`, `title`, `body` are the fields of the `message.new` event payload.)
- `POST /v1/messages/read` — `{"ids": [1]}` **or** `{"mark_all": true}` (mutually exclusive).

### Achievements
- `GET /v1/achievements[?category=]` (public, verbatim):
```json
{
  "achievements": [
    {
      "achievement_key": "first_scan",
      "title": "First Scan",
      "description": "Completed your first system scan.",
      "category": "exploration",
      "xp_reward": 100,
      "player_count": 42,
      "last_achieved_at": "2026-07-04T18:22:10"
    }
  ]
}
```
- `GET /v1/achievements/{achievement_key}` — same fields + `players: [{account_name, achieved_at}]`.
- `GET /v1/accounts/achievements` — yours (auth). **Response not shown** (INFERRED: same `achievements[]` items, probably with `achieved_at`).

### Other account endpoints
- `GET /v1/accounts/reputation` — species standing; response shape not documented. (Docs' curl has typo `/v1/account/reputation`.)
- `GET /v1/replicants/{code}/reputation` and `GET /v1/species` — appear only in the Postman collection; shapes undocumented.
- `POST /v1/feedback` — `{"type": "typo"|"bug"|"idea", "body": "..."}` → `{"status": "feedback_received"}`

### Webhook
- `POST /v1/accounts/webhook` `{"url": "https://..."}` → `{"status": "webhook_registered", "verified_at": "...", "webhook_secret": "whsec_..."}`
- `GET /v1/accounts/webhook` → `{"url": "...", "verified_at": "..."}`
- Handshake: server POSTs `{"type": "webhook_verification", "challenge": "chl_..."}`; reply `{"challenge": "chl_..."}`.
- Signature header `X-Replicant-Space-Signature` = lowercase hex HMAC-SHA256(key=`webhook_secret`, msg=raw body).
- Top-level `type`: `webhook_verification` | `message` | `bobnet` | `event`. Event payload (verbatim):
```json
{
  "type": "event",
  "event_type": "device_cruise_arrived",
  "device_code": "F54FA154",
  "device_type": "heaven_vessel",
  "replicant_code": "57F0F6C8",
  "payload": { "location": "SOL-5-L5", "from_location": "PORRAMA-KUIPER" },
  "timestamp": "2026-05-10T08:55:27+01:00"
}
```
(Note: webhook uses legacy `event_type` names, not the dotted `event` names of `/v1/events`.)

---

## 2. Replicants

### GET /v1/replicants/{code}  (your replicant, verbatim)
```json
{
  "name": "mercutio-1",
  "replicant_code": "8AFE4482",
  "hosted_device_code": "37C51F74",
  "location": "CHAMAKUY-BELT-1",
  "position": { "x": 34.1906, "y": -8.9593, "z": -42.9832 },
  "stowed_devices": [
    { "device_code": "3CA5D7E4", "device_type": "replicant_matrix" }
  ],
  "status": "stationary",
  "experience_points": 1245
}
```
- `hosted_device_code` = the vessel (usually `heaven_vessel`) carrying the matrix. `status` = host device activity (`stationary`, `printing`, `mining`, `travelling`...).
- **No ETA field** on the replicant. Arrival time comes from the travel response `arrives_at`, `travel.departed` event payload `arrives_at`, or `travel.arrived` event. (INFERRED: poll host device via `GET /v1/devices/{hosted_device_code}` for status.)
- For *another* player's code the same path returns the public profile: `name, replicant_code, description, plan, project, pronouns, is_npc`.

### GET /v1/replicants — public directory
Query `cursor`, `limit` (20), `latest`, `name` (case-insensitive search).
```json
{ "replicants": [ { "name": "Sylphrena", "replicant_code": "30B93F2F", "last_location": "IMPOLLA", "is_npc": true } ], "next_cursor": 10 }
```

### PATCH /v1/replicants/{code} — profile
`name` (unique), `is_npc`, `pronouns` (≤50), `description` (≤500), `plan` (≤500), `project` (≤2000). 200.
(`cohort_permission`: `"private"`|`"public"` is a per-replicant setting per the concepts page — presumably also PATCHed here **(INFERRED)**.)

### GET /v1/replicants/{code}/devices — this replicant's devices
Query `location`, `device_type`, `cursor` (int device ID), `limit` (20, max 50).
```json
{
  "devices": [
    { "device_code": "2AC61214", "device_type": "mining_drone", "location": "SOL-BELT-1", "status": "mining", "operational_capacity": 0.92 },
    { "device_code": "7FE981A0", "device_type": "mining_drone", "location": "SOL-BELT-1", "status": "mining (structural)", "operational_capacity": 0.99 }
  ],
  "next_cursor": 4821
}
```
Note: this example shows `operational_capacity` as 0–1 fraction while `/v1/devices` shows 0–100 (`67.0`). Treat as percentage 0–100 and normalise if ≤1 **(INFERRED — docs inconsistent)**.

### GET /v1/replicants/{code}/stars — nearest stars
Query `page`, `per_page` (1–50). Response: `page`, `per_page`, `replicant_position{x,y,z}`, `stars[]` with
`designation, color, distance_from_replicant (ly), entry_point, estimated_planets, estimated_travel_time (s), has_hub (optional), position{x,y,z}, region`.
### GET /v1/replicants/{code}/stars/{star}
```json
{
  "replicant_position": { "x": 0.0, "y": 0.0, "z": 0.0 },
  "star": {
    "color": "Yellow", "designation": "MENKENTAR", "distance_from_replicant": 6.37,
    "entry_point": "MENKENTAR-5-L4", "estimated_planets": 9, "estimated_travel_time": 3,
    "explored": false, "has_life": null,
    "position": { "x": -4.659, "y": -0.1315, "z": 4.3386 }, "spectral_type": "G4"
  }
}
```

### POST /v1/replicants/{code}/travel
Body `{"destination": "<location code>", "dry_run": false}` (`dry_run` optional).
Response — docs show **two shapes for `route`** (object on API page, array on quickstart). Handle both.
API page (verbatim):
```json
{
  "departed_at": "2026-05-10T08:23:45+01:00",
  "arrives_at": "2026-05-10T08:55:27+01:00",
  "destination": "SOL",
  "route": { "distance_ly": 42.2915, "from": "PORRAMA-KUIPER", "to": "SOL-5-L4", "time_seconds": 1902, "type": "surge" },
  "status": "travel_initiated"
}
```
Quickstart (verbatim):
```json
{
  "origin": "SOL-OORT",
  "destination": "SOL-BELT-1",
  "departed_at": "2026-05-17T13:55:41+01:00",
  "arrives_at": "2026-05-17T13:56:22+01:00",
  "total_time_seconds": 41.1,
  "route": [
    { "leg": 1, "from": "SOL-OORT", "to": "SOL-4-L4", "type": "surge_hop", "time_seconds": 30 },
    { "leg": 2, "from": "SOL-4-L4", "to": "SOL-BELT-1", "type": "cruise", "time_seconds": 11.1 }
  ],
  "status": "travel_initiated"
}
```
Dry run: `status: "preview"`, `destination_type`, `final_destination`, `final_destination_name`, `total_distance_ly`, `total_time_seconds`, `route[]` of `{leg, from, to, from_name, to_name, distance_ly, time_seconds, type}`.
Route `type`s: `cruise`, `surge`, `surge_hop`. Surge only from Oort, Kuiper, entry points.
**DELETE /v1/replicants/{code}/travel** — cancel (Postman only; body `{}`).

### POST /v1/replicants/{code}/scan — system scan (body `{}` or none), 200 (verbatim, trimmed planets)
```json
{
  "asteroid_belt": {
    "belts": [
      {
        "designation": "CHAMAKUY-BELT-1", "density": "dense",
        "inner_radius_au": 0.52, "outer_radius_au": 0.78,
        "resources": { "carbon": "scarce", "conductive": "high", "rares": "low", "silicates": "scarce", "structural": "high", "volatiles": "low" }
      }
    ],
    "present": true
  },
  "entry_point": "CHAMAKUY-5-L4",
  "outer_system": {
    "kuiper": { "designation": "CHAMAKUY-KUIPER", "distance_au": 19.21 },
    "oort":   { "designation": "CHAMAKUY-OORT",   "distance_au": 2326.29 }
  },
  "planets": [
    { "designation": "CHAMAKUY-1", "in_habitable_zone": false, "moon_count": 1, "orbital_distance_au": 0.141, "type": "Barren" },
    { "designation": "CHAMAKUY-2", "in_habitable_zone": true,  "moon_count": 3, "orbital_distance_au": 0.205, "type": "Ocean World" },
    { "designation": "CHAMAKUY-5", "in_habitable_zone": false, "moon_count": 43, "orbital_distance_au": 1.241, "type": "Ice Giant" }
  ],
  "replicants": {
    "Bob": { "last_active": "2026-05-10T23:01:19+01:00", "location": "CHAMAKUY-BELT-1", "replicant_code": "8AFE4482" }
  },
  "star": {
    "age_my": 5988.11, "color": "Red", "designation": "CHAMAKUY",
    "habitable_zone": { "inner_au": 0.18, "outer_au": 0.32 },
    "luminosity_solar": 0.035611, "mass_solar": 0.2444,
    "position": { "x": 34.1906, "y": -8.9593, "z": -42.9832 },
    "spectral_type": "M5", "temperature_k": 2977
  },
  "system_tags": ["binary_system"]
}
```
- `replicants` is an **object keyed by replicant name**, not an array.
- Planet `type` values seen: `Barren`, `Ocean World`, `Terrestrial`, `Frozen`, `Ice Giant`.
- No moons/resource sites/other devices — those need survey drones. No per-planet coordinates (only orbital distance AU).

### GET /v1/replicants/{code}/scan/devices — other devices in current system
Query `device_type`, `owner_replicant_code`, `cursor`, `limit` (20/50).
```json
{
  "star": "CHAMAKUY", "device_count": 2,
  "devices": [ { "device_code": "D8C2A140", "device_type": "survey_drone", "location": "CHAMAKUY-2", "owner_replicant_code": "4A1F0B22", "owner_name": "helga-3" } ],
  "next_cursor": 7122
}
```

### POST /v1/replicants/{code}/mine — onboard mining (blocks travel/scan/print)
Body `{"resource_type": "rares"}` → 202 `{"status": "mining", "resource_type": "rares"}`.
**DELETE /v1/replicants/{code}/mine** — stop (Postman only).

### POST /v1/replicants/{code}/print — vessel printer
Body `{"device_type": "mining_drone"}` → 202 `{"status": "enqueued"}` (no ETA in response; use `print.started` event `completes_at`).
`{"command": "clear_queue"}` → 200 `{"queue": [], "queue_length": 0, "status": "queue_cleared"}`
`{"command": "cancel"}` → 200 `{"device_type": "survey_drone", "resources_refunded": true, "status": "af_print_cancelled"}`

### GET /v1/replicants/{code}/inventory
Used in quickstart only (not in API nav). Verbatim response:
```json
{ "star": "SOL", "locations": [ { "location": "SOL-BELT-1", "items": { "carbon": 25, "silicates": 28, "structural": 123 } } ] }
```
Preferred documented endpoint: `GET /v1/inventory` (§4).

### POST /v1/replicants/{code}/teleport — via FTL relay network (or slingshot)
Body `{"target": "<empty matrix device code | slingshot code>"}` → 202
```json
{
  "status": "teleporting", "source_star": "SOL", "destination_star": "POLIBUS",
  "started_at": "2026-05-10T14:30:00+01:00", "completes_at": "2026-05-10T14:30:30+01:00",
  "offline_seconds": 30, "target_matrix_code": "0799A49D"
}
```
### POST /v1/replicants/{code}/transfer — same-location host swap
`{"target": "B22A1198"}` → `{"status": "transferred", "old_host": "B22A1198", "new_host": "0799A49D"}`

### Replicate (clone) — device command on your matrix
`POST /v1/devices/{matrix_code}`:
1. `{"command": "stow", "target": "24C8355C"}` — stow empty matrix into cradle device (200).
2. `{"command": "replicate", "target": "7A303E2A"}` → 201
```json
{ "status": "replicated", "new_replicant_code": "A1B2C3D4", "new_replicant_name": "Bob-2", "host_device_code": "BCF35044", "matrix_code": "3CA5D7E4" }
```
Target must be `empty_replicant_matrix`, stowed in a cradle device at the source matrix's location. Clone vessels: `matrix_container`, `heaven_vessel`, `system_hub`.

### BobNet
- `POST /v1/replicants/{code}/message` `{"channel": "#general", "text": "hey bob"}`
- `POST /v1/devices/{relay_code}` `{"command": "message", "channel": "#general", "text": "..."}`
- `GET /v1/devices/{relay_code}/channels` → `{"channels": [{"last_active", "name"}]}`
- `GET /v1/devices/{relay_code}/messages?cursor&limit&latest&include_npcs` → `{"messages": [{id, channel, current_star, message, replicant_code, replicant_name, time}], "next_cursor", "total", "total_messages"}`

---

## 3. Devices

### GET /v1/devices — all your devices (account-wide)
Query: `replicant_code`, `device_type`, `tags` (comma list, `*` wildcard), `exclude_tags`, `untagged` (bool), `tag` (deprecated), `location` (star code or location), `cursor` (int device ID), `limit` (20, max 50).
```json
{
  "devices": [
    {
      "device_code": "B58FCC78",
      "device_type": "mining_drone",
      "replicant_code": "4BBA7CBE",
      "location": "SOL-BELT-1",
      "features": ["cruise", "mine", "stow"],
      "available_commands": ["change_owner", "deactivate", "decommission", "deploy", "recall", "retarget", "start_mining", "stow", "travel"],
      "operational_capacity": 67.0,
      "status": "mining (rares)"
    }
  ],
  "next_cursor": 4821
}
```

### GET /v1/devices/{code}  (verbatim)
```json
{
  "device_code": "B58FCC78",
  "device_type": "mining_drone",
  "replicant_code": "4BBA7CBE",
  "location": "TARAZEDAR-BELT-1",
  "features": ["cruise", "mine", "stow"],
  "available_commands": ["change_owner", "deactivate", "decommission", "deploy", "recall", "retarget", "start_mining", "stow", "travel"],
  "operational_capacity": 67.0,
  "status": "idle"
}
```
Documented but not shown in example: cargo, stowage, attachments, print queue (autofactory docs say "view queue at /devices/<code>"). Likely keys **(INFERRED)**: `stowed_devices` (seen on heaven_vessel concept example + replicant), `stow_capacity` (seen), `cargo`/`cargo_capacity`, `attached_devices`, `queue`/`queue_length` (seen on print responses), `tags`, `directive`/`configuration` for AMI.
Heaven vessel concept example:
```json
{ "device_type": "heaven_vessel", "stow_capacity": 10,
  "features": ["surge", "cruise", "system_scan", "mine", "cradle", "print", "census"],
  "stowed_devices": [ {"device_type": "replicant_matrix"}, {"device_type": "mining_drone"} ] }
```
**No busy-until/ETA field is documented on devices** — ETAs arrive via events (`travel.departed.arrives_at`, `print.started.completes_at`, `scan.started.eta_seconds`, `device.compacting.completes_at`, etc.).

#### Device status values
`stowed`, `idle`, `travelling`, `cruising`, `surging`, `recalling`, `recall_waiting`, `decommissioning`, `collecting`, `depositing`, `waiting_for_surge_plate`, `mining (<resource>)`, `prospecting`, `tracking`, `scanning`, `monitoring`, `printing (<device_type>)`, `waiting_for_resources`, `repairing`, `diverting`, `patrolling`, `coordinating`, `relaying`, `inactive`, `compacting`, `compacted`, `unfurling`.
(Also seen: `mining` bare, replicant `stationary`.) Parse `name (arg)` pattern.

### POST /v1/devices/{code} — command model
Body `{"command": "<name>", ...args}`. Example `{"command": "stow"}` → `{"device_code": "40ED8A3E", "status": "stowed"}`.
Check `available_commands` on the device before issuing.

| Feature | Commands |
|---|---|
| (universal) | `change_owner`, `deactivate` |
| `cruise` | `travel`, `recall`, `decommission` |
| `surge` | `travel` |
| `stow` | `deploy`, `stow` |
| `attach` | `attach`, `detach` |
| `system_scan` | `system_scan` |
| `survey` | `scan`, `search` |
| `census` | `stellar_census` |
| `prospect` | `prospect` |
| `mine` | `start_mining`, `retarget` |
| `transport` | `collect_resources`, `deposit_resources` |
| `print` | `enqueue_print`, `dequeue_print`, `clear_queue` |
| `repair` | `repair` |
| `cradle` | `replicate` |
| `ami` | `adopt`, `release`, `set_directive`, `clear_directive`, `launch`, `withdraw`, `activate` (+ `assemble`) |
| `modular` | `compact`, `unfurl` |
| `relay` | `activate` |

Command bodies seen:
```jsonc
{"command": "travel", "destination": "ZHANGA-1"}
{"command": "deploy"}
{"command": "stow", "target": "24C8355C"}            // stow a device into target
{"command": "start_mining", "resource_type": "carbon"}
{"command": "retarget", "resource_type": "silicates"}
{"command": "scan"}                                  // survey drone: body at current location, 202, result via scan.completed
{"command": "search"}                                // survey drone: belts only, finds resource site, 202
{"command": "collect_resources", "resources": {"carbon": 50, "structural": 30}}
{"command": "deposit_resources"}                     // omit resources = empty hold
{"command": "attach", "device": "7FE981A0"}
{"command": "detach"}
{"command": "decommission"}                          // 202 {"device_code": "...", "status": "decommissioning"}; ~60% recovery, may learn blueprint
{"command": "activate"}                              // relay / hub / ward / AMI
{"command": "compact"} / {"command": "unfurl"}      // 30% of print time each
{"command": "prospect", "direction": [0.0, -1.0, 0.0]}  // observatory; → {"status": "prospecting", "completes_at": "..."}
{"command": "set_welcome_message", "message": "..."} // system hub
{"command": "message", "channel": "#general", "text": "..."} // relay
{"command": "enqueue_print", "device_type": "mining_drone", "quantity": 1, "tags": ["fleet-713"],
 "controller": "MC91FF22", "oncomplete": {"command": "travel", "destination": "LERNA-BELT-1"}, "flatpack": false}
   // → {"queue": [], "queue_length": 0, "status": "enqueued"}; oncomplete supports travel, start_mining
{"command": "dequeue_print", "index": 1}
{"command": "clear_queue"}
```
Transport cargo capacities: transport drone 20, transport hauler 80, cargo freighter 500. Carriers: surge plate 1, surge platform 4, surge carrier 9, mobile fleet 36 devices.
Vessels: heaven (stow 10), cargo vessel (stow 50, cargo 200, attach 3, 40% slower), racing vessel (stow 5, 35% faster).

### PATCH /v1/devices/{code} — configuration / tags
Body wraps in `configuration` (per examples): `{"configuration": {"tags": [...]}}`, `{"configuration": {"add_tags": ["taxi"]}}`, `remove_tags`; slingshot `{"configuration": {"linked_device": "<matrix_code>"}}`.
Response: `{"device_code": "B58FCC78", "tags": ["autofactories", "group-alpha"]}`. Tags ≤32 chars `[a-z0-9-_:.]`.
### GET /v1/devices/tags/{tag}?cursor&limit (default 10, max 50) → `{devices: [{device_code, device_type, replicant_code, location, status, operational_capacity}], next_cursor}`

### GET /v1/devices/{code}/logs — per-device events (legacy format)
Query `cursor`, `limit` (20), `latest`.
```json
{
  "events": [
    {
      "created_at": "2026-06-06T22:00:03+01:00",
      "device_code": "8F748260",
      "device_type": "heaven_vessel",
      "event_type": "device_cruise_departed",
      "id": 125835,
      "message": "Cruising to object MUROPE-OBJ-1",
      "payload": { "attached_devices": [], "destination": "MUROPE-OBJ-1", "distance_au": 0.328, "origin": "MUROPE-5-L4", "recalling": false, "travel_time_seconds": 1.1 }
    }
  ]
}
```

### Other device sub-resources
- `GET /v1/devices/{beacon_code}/audit?cursor&limit&latest&device_type&replicant_code` → `{"audit": [{id, device_code, device_type, replicant_code, travel_type: "arrival"|"departure", location, logged_at, vector: "-0.37,0.14,-0.92"|null}]}`
- `GET /v1/devices/{relay_code}/network` → `{"connections": [{"device_code", "distance_ly", "star"}], "range_ly": 7.5, "status": "relaying"}`
- `POST /v1/devices/{interface_code}/simulate` `{"scenario", "replicant_code"}`; `GET .../simulate/active`.

---

## 4. Locations

### GET /v1/locations — overview of everywhere you have presence
```json
{ "locations": { "SOL-2": { "devices": 1, "replicants": 1, "resource_sites": 4, "resources": 1023 },
                 "TARAZEDAR-BELT-1": { "devices": 23, "replicants": 1, "resource_sites": 10, "resources": 23012 } } }
```
(`locations` is an **object keyed by location code**.)

### GET /v1/locations/{code}
Shape depends on `location_type`:
- **Star** code → same body as system scan.
- **Belt**:
```json
{
  "location_type": "belt", "location": "TARAZEDAR-BELT-1",
  "belt": { "density": "sparse", "designation": "TARAZEDAR-BELT-1", "inner_radius_au": 0.6, "outer_radius_au": 0.9,
            "resources": { "carbon": "rich", "conductive": "scarce", "rares": "low", "silicates": "low", "structural": "moderate", "volatiles": "high" } },
  "devices": [], "inventory": [], "resource_sites": []
}
```
- **Planet / moon** (key `planet` or `moon` — `planet` key **(INFERRED)** by symmetry):
```json
{ "moon": { "atmo_o2_pct": null, "atmo_pressure_atm": null, "atmo_toxicity": null, "atmosphere": false, "biosphere_index": null,
            "category": "frozen", "density_gcc": 2.61, "designation": "DELTA-3-1", "has_subsurface_ocean": false, "hydrosphere_pct": null,
            "life_stage": "none", "location_type": "rocky", "mass_earth": 0.005969, "name": null, "orbital_distance_km": 27567.1,
            "orbital_period_hours": 48.31, "radius_earth": 0.2328, "scanned": true, "surface_gravity": 0.1101,
            "surface_temp_c": -111.0, "surface_temp_k": 162.0, "tags": ["cratered", "rocky"], "tectonic_index": null,
            "tidally_locked": true, "type": "rocky" } }
```
All non-star locations include `devices`, `inventory`, `resource_sites`. Item shapes of those arrays not shown.
- **Asteroid / system object** (`STAR-OBJ-n`):
```json
{ "location": "DELTA-OBJ-3", "location_type": "object",
  "object": { "active_propulsors": 1, "approach_angle": 8.7, "approach_speed": 0.79, "composition": "carbonaceous",
              "current_thrust_per_hour": 4.0, "designation": "DELTA-OBJ-3", "discovered_at": "2026-09-01T22:18:55+01:00",
              "impact_eta": "2026-09-02T22:03:55+01:00", "impact_likelihood": 100.0, "impact_target": "DELTA-3",
              "mass_class": "large", "object_type": "incoming_asteroid", "orbital_distance_au": 35.71,
              "progress_pct": 0.0, "required_strength": 168.0, "status": "active" } }
```
- **Megastructure**: `{"code", "location_type": "megastructure", "system", "megastructure": {"name", "progress", "requirements": {"<device_type>": {"needed", "contributed"}}}, "devices", "inventory"}`.
  `POST /v1/locations/{code}/contribute` `{"devices": [...]}` → `{location, accepted, rejected, progress, status: "contribution_recorded"}`; `GET /v1/leaderboards/megastructure`.

### GET /v1/inventory — stockpiles by location
Query `location` (star or location code), `cursor` (location code string), `limit` (20, max 50). Sorted by location code.
```json
{ "locations": [ { "location": "TARAZEDAR-2-3", "items": { "rares": 38, "silicates": 412 } },
                 { "location": "TARAZEDAR-BELT-1", "items": { "carbon": 348, "structural": 9234 } } ],
  "next_cursor": "TARAZEDAR-BELT-1" }
```
(`items` omits zero resources.)

### GET /v1/stars — full star catalogue
- Unpaginated JSON, **1 request/minute**, regenerated every 5 min (`generated_at`). Catalogue covers ~70 ly; observatories discover beyond.
- Size: example `total: 24`; real size not stated (INFERRED: hundreds–thousands; cache locally).
```json
{
  "total": 24,
  "generated_at": "2026-07-12T14:30:00.123456+00:00",
  "stars": [
    { "designation": "CYGNUS", "name": "Cygnus Prime", "spectral_type": "M5", "color": "yellow",
      "position": { "x": 41.2, "y": -11.4, "z": 30.9 }, "estimated_planets": 6,
      "entry_point": "CYGNUS-5-L4", "has_hub": true, "region": "solzone" }
  ]
}
```
Optional booleans `has_hub`, `has_ward` appear only when true (INFERRED from "optional"). Coordinates in ly from Sol. Color casing varies (`"yellow"` vs `"Yellow"`).

---

## 5. Blueprints

### GET /v1/blueprints — account-scoped known blueprints
```json
{
  "blueprints": [
    {
      "device_type": "maintenance_drone",
      "short_description": "Repairs your devices once they lose enough operational capacity.",
      "description": "...",
      "directives": ["patrol"],
      "features": ["cruise", "repair", "ami", "stow"],
      "print_time": 270,
      "resources": { "carbon": 35, "conductive": 80, "rares": 20, "silicates": 40, "structural": 150, "volatiles": 10 },
      "attach_capacity": 0,
      "cargo_capacity": 0,
      "stow_capacity": 0
    }
  ]
}
```
- `print_time` in **seconds** (no `_seconds` suffix). `resources` = input cost. Output = one `device_type` (no separate outputs field).
- Quickstart example: `mining_drone` print_time 180, `{carbon 25, conductive 50, silicates 25, structural 100}`.
- Starter device types: `ftl_beacon` (100 s), `mining_drone`, `survey_drone`, `compute_core`, `autofactory`, `empty_replicant_matrix`, `matrix_container`, `heaven_vessel` (8 h), `maintenance_drone`.
- Other device_types seen elsewhere: `replicant_matrix`, `ami_mining_controller`, `ami_trade_controller`, `point_defence_array`, `surge_plate`, `system_hub`. Snake-case names for transport drone/hauler, cargo freighter, relay, slingshot, ward, observatory, other AMI controllers not shown (INFERRED: `transport_drone`, `ftl_relay`, `ami_survey_controller`, `ami_transport_controller`, ...).

---

## 6. AMI controllers

Devices with the `ami` feature. Commands on `POST /v1/devices/{controller_code}`:
```jsonc
{"command": "adopt", "devices": ["A1B2C3D4", "E5F60718"]}
{"command": "release", "devices": ["..."]}
{"command": "set_directive", "directive": "<name>", "configuration": { ... }}
{"command": "clear_directive"}
{"command": "launch"}      // → {"device_code": "175B5AD3", "status": "launched", "assigned_devices": {"deployed": ["E811348D", ...]}}
{"command": "withdraw"}
{"command": "assemble"}
{"command": "activate"} / {"command": "deactivate"}
```
Directives:
- **Survey** — `survey_system` `{"planets": "all", "moons": "all"|"none", "recall": true}`; `belt_search` (no config).
- **Mining** (`ami_mining_controller`) — `gather_resources` `{carbon, conductive, rares, silicates, structural: <int>}`; `gather_evenly`; `maintain_ratios` `{structural, conductive, silicates, rares: <decimal>}`; `deplete_smallest`; `gather_salvage` `{"location": "...", "recall": bool}`.
- **Transport** — `delivery` `{"route": {"collect": "SOL-BELT-1", "deliver": "SOL-3-L4"}, "requirement": {"carbon": 50, "silicates": 100}}`; `shuttle` `{"collect", "deliver", "priority": ["carbon","rares"]}`; `ferry` (interstellar, same fields); `consolidate` `{"deliver", "priority": []}`.
- **Maintenance drone** — `patrol`.
- **Trade** (`ami_trade_controller`) — `trade` `{"name", "description", "announcement"}`.
- **Fleet controller** — no directives; relays `travel` to adopted surge-capable devices (cascades).
All return 200.

### AMI digests (events `ami.mining.digest`, `ami.survey.digest`, `ami.transport.digest`)
Emitted each evaluation tick (~10 s) × `events.ami_digest_interval` (1–30), only while a directive is active; never muted. Payload:
```json
{
  "directive": "string",
  "report": {},
  "activity": { "event_count": 0, "counts": {}, "window": ["<ISO start>", "<ISO end>"] },
  "devices": [ { "device_code": "string", "status": "string", "events": 0, "last_event": "string" } ]
}
```
`report` contents per controller not documented. (INFERRED: `counts` keyed by event name.) There is no separate digest endpoint — read them from `/v1/events?category=ami` or the stream.

---

## 7. Events

### GET /v1/events — account-wide event log (current format)
Query: `cursor` (string id), `limit` (default/max 100), `filtered` (bool, apply mute patterns; default false), `device_code`, `category`, `event` (dotted name), `after`, `before` (ISO-8601).
Verbatim:
```json
{
  "events": [
    {
      "id": "1784292742869-0",
      "version": 1,
      "category": "mining",
      "event": "mining.started",
      "replicant_code": "BC6F680D",
      "device_code": "32763CDF",
      "device_type": "mining_drone",
      "star": "ABOTEIN",
      "location": "ABOTEIN-BELT-1",
      "payload": {
        "availability": "high", "belt": "ABOTEIN-BELT-1", "cycle_time_seconds": 52, "density": "moderate",
        "designation": "ABOTEIN-BELT-1-SITE-0", "location": "ABOTEIN-BELT-1", "location_type": "belt",
        "resource_type": "structural", "site": "ABOTEIN-BELT-1-SITE-0"
      },
      "created_at": "2026-07-17T13:52:22+01:00"
    }
  ],
  "next_cursor": "1784292762940-0"
}
```
Nullable: `device_code`, `device_type`, `star`, `location`. Order is ascending (reading "after" cursor) (INFERRED).

### GET /v1/events/stream — SSE
`cursor` query or `Last-Event-ID` header to resume; ~10,000 event retention (older cursors restart at earliest). `: keepalive` comments. Mute patterns applied automatically.
```
id: 1752681600000-0
event: mining.started
data: {"version":1,"category":"mining","event":"mining.started","device_code":"2AC61214","payload":{"resource_type":"structural","site":"SOL-BELT-1-SITE-3"},"created_at":"2026-07-16T10:00:00Z"}
```
(Needs `Authorization` header → use fetch-based SSE client, not `EventSource`, in browsers (INFERRED).)

### Legacy per-replicant log: GET /v1/replicants/{code}/events
Query `cursor` (int), `limit` (20), `latest`, `event_type`, `device_type`, `device`. Items: `created_at, device_code, device_type, event_type (snake legacy e.g. device_deployed, print_complete, device_cruise_arrived), message (human text), payload`. Same format as `/v1/devices/{code}/logs` (which also has integer `id`).

### Event catalogue (dotted `event` names; category = prefix)
| Category | Event | Payload fields |
|---|---|---|
| ami | `ami.adopted` | `devices[{device_code, device_type}]` |
| ami | `ami.assembled` | `destination`, `assembled_count` |
| ami | `ami.launched` | `directive_status`, `evaluated`, `devices_deployed` |
| ami | `ami.released` | `devices[{device_code, device_type}]` |
| ami | `ami.withdrawn` | `directive_paused`, `devices_recalled` |
| ami | `ami.mining.digest` / `ami.survey.digest` / `ami.transport.digest` | see §6 |
| bobnet | `bobnet.new` | `id`, `replicant_name`, `replicant_code`, `current_star`, `channel`, `message` |
| device | `device.attached` | `target_code`, `target_type` |
| device | `device.changed_owner` | `from_replicant`, `to_replicant`, `direction` |
| device | `device.compacted` | — |
| device | `device.compacting` | `completes_at` |
| device | `device.decommissioned` | `resources_recovered`, `blueprint_discovered` |
| device | `device.deployed` | `deployed_from_device_code` |
| device | `device.detached` | `target_code`, `target_type` |
| device | `device.unfurled` | — |
| device | `device.unfurling` | `completes_at` |
| device | `device.stowed` | `stowed_in_device_code` |
| directive | `directive.cleared` | `previous_directive` |
| directive | `directive.completed` | `directive` |
| directive | `directive.paused` | `directive` |
| directive | `directive.resumed` | `directive` |
| directive | `directive.set` | `directive`, `configuration` |
| diversion | `diversion.activated` | `object_designation`, `size_class` |
| diversion | `diversion.deactivated` | `device_code` |
| diversion | `diversion.diverted` | `object_designation`, `outcome` |
| diversion | `diversion.impacted` | `object_designation` |
| diversion | `diversion.partial` | `object_designation`, `outcome` |
| event | `event.completed` | `designation`, `location`, `event_type`, `tier`, `rewards`, `consumed` |
| event | `event.discovered` | `designation`, `location`, `event_type`, `tier`, `title`, `description`, `criteria` |
| experience | `experience.gained` | `source`, `amount` |
| hub | `hub.activated` | `star`, `location` |
| hub | `hub.destroyed` | `star`, `location` |
| hub | `hub.warning` | `capacity`, `warning_type` |
| hub | `hub.maintained` | `resources_consumed`, `capacity` |
| megastructure | `megastructure.contributed` | `megastructure_designation`, `accepted_count`, `contributed_devices` |
| message | `message.new` | `message_id`, `message_type`, `title`, `body` |
| mining | `mining.relocated` | `location`, `resource_type`, `old_site`, `new_site` |
| mining | `mining.retargeted` | `location_type`, `location`, `site`, `old_resource`, `new_resource`, `availability`, `density`, `cycle_time_seconds` |
| mining | `mining.started` | `location_type`, `location`, `site`, `resource_type`, `availability`, `density`, `cycle_time_seconds` |
| mining | `mining.stopped` | `location`, `resource_type`, `quantity_mined` |
| multiplayer | `multiplayer.replicant_entered` | `replicant_code`, `replicant_name` |
| multiplayer | `multiplayer.replicant_left` | `replicant_code`, `replicant_name` |
| print | `print.started` | `device_type`, `print_mode`, `completes_at`, `tags` |
| print | `print.completed` | `device_type`, `new_device_code`, `print_mode`, `compacted`, `consumed_device_codes`, `tags` |
| prospect | `prospect.completed` | `origin`, `stars_generated`, `stars` |
| relay | `relay.activated` | `star`, `location` |
| replicant | `replicant.transferred` | `old_host`, `new_host` |
| salvage | `salvage.depleted` | `site` |
| salvage | `salvage.discovered` | `designation`, `location`, `salvage_type`, `name`, `resources` |
| scan | `scan.started` | `scan_target`, `scan_type`, `eta_seconds` |
| scan | `scan.completed` | `scan_target`, `scan_type`, `report` |
| search | `search.started` | `search_target`, `search_type`, `eta_seconds` |
| search | `search.completed` | `search_target`, `search_type`, `report` |
| simulation | `simulation.started` | `simulation_id`, `scenario_code`, `starting_star` |
| simulation | `simulation.completed` | `simulation_id`, `scenario_code`, `score_seconds`, `resources_mined`, `devices_printed` |
| simulation | `simulation.abandoned` | `simulation_id`, `scenario_code` |
| simulation | `simulation.expired` | `simulation_id`, `scenario_code` |
| site | `site.depleted` | `site` |
| story | `story.awakened` | `new_replicant_code`, `new_replicant_name`, `host_device_code` |
| story | `story.hint` | `hint`, `planet`, `designation` |
| system | `system.body_renamed` | `body_type`, `designation`, `new_name` |
| system | `system.devices_halted` | `star`, `devices_halted` |
| system | `system.entry_point_set` | `star`, `entry_point` |
| system | `system.object_detected` | `object_designation`, `size_class`, `impact_target`, `impact_eta`, `discovery_source` |
| teleport | `teleport.started` | `source_star`, `destination_star`, `target_matrix_code` |
| teleport | `teleport.completed` | `destination_star`, `new_host_code` |
| teleport | `teleport.failed` | `reason`, `target_matrix_code` |
| terraform | `terraform.activated` / `terraform.deactivated` / `terraform.device_activated` / `terraform.device_deactivated` | — |
| terraform | `terraform.status` | `body`, `attributes`, `projections`, `effects`, `warnings`, `anomaly`, `asteroids` |
| trade | `trade.created` | `trade_code`, `name`, `stock`, `escrowed` |
| trade | `trade.deleted` | `trade_code`, `name`, `remaining_stock`, `released` |
| trade | `trade.completed` | buyer: `trade_code`, `trade_name`, `role`, `remaining_stock`, `criteria_paid`, `rewards_received`; seller: `trade_code`, `trade_name`, `role`, `remaining_stock`, `criteria_received` |
| transport | `transport.collected` | `resources`, `total`, `cargo_after`, `cargo_capacity` |
| transport | `transport.delivered` | `resources`, `total`, `cargo_after`, `cargo_capacity` |
| travel | `travel.departed` | `travel_type`, `origin`, `destination`, `distance_au` (cruise) / `distance_ly` (surge), `travel_time_seconds`, `arrives_at`, `attached_devices`, `legs` (multi-leg) |
| travel | `travel.arrived` | `attached_devices`, `destination`, `origin`, `recalling`, `travel_type` |
| travel | `travel.cancelled` | `travel_type`, `origin`, `destination`, `return_time_seconds`, `attached_devices` |
| triangulation | `triangulation.started` | `signature`, `target`, `completes_at` |
| triangulation | `triangulation.complete` | `signature`, `target`, `direction` |
| triangulation | `triangulation.failed` | `signature`, `target`, `reason` |
| ward | `ward.activated` / `ward.deactivated` | — |

Note the real `mining.started` example also carries `belt` and `designation` beyond the catalogue list — payloads may be supersets.
Legacy `event_type` names seen (webhook / replicant events / device logs): `device_deployed`, `print_complete`, `device_cruise_arrived`, `device_cruise_departed`.

---

## 8. Trading
- `GET /v1/replicants/{code}/traders` → `{"traders": [{controller_code, description, is_local, location, owner_name, owner_replicant_code, shop_name, star, total_stock, trade_count}]}`
- `GET /v1/devices/{controller_code}/trades` → `{"trades": [{name, trade_code: "TRD-E400B1", current_stock, initial_stock, criteria: {resources:{}, devices:{}}, rewards: {resources:{}, devices:{}}, created_at}]}`
- `POST /v1/devices/{controller_code}/trades/{trade_code}` — execute (no body) → 200
- `POST /v1/devices/{code}/trades` — create (201) `{"name", "stock", "criteria": {"resources": {}, "devices": {}}, "rewards": {"resources": {}, "devices": {"point_defence_array": 5}}}`; rewards escrowed.
- `DELETE /v1/devices/{code}/trades/{trade_id}` — 204.
- Shop setup: `ami_trade_controller` + `set_directive` `trade`. Directory listing requires an active FTL relay in-system.

## 9. FTL infrastructure (summary)
- **FTL beacon** — cheap, deploy anywhere; monitoring + traffic audit (`/devices/{code}/audit`); no remote command.
- **FTL relay** — must sit at L4/L5; `activate`; 7.5 ly range, auto-chains; enables remote command + BobNet; `/devices/{code}/network`.
- **System hub** — `activate`, `set_welcome_message`, `compact`/`unfurl`, travel; 15 ly relay, mining lock, up to 25% travel speed-up, rename bodies/set entry point; 7-day shield then −10%/day without maintenance.
- **System ward** — `activate` (response may include `evicted_miners`) / `deactivate`; max 25 per account; incompatible with hubs.
- **FTL slingshot** — PATCH `configuration.linked_device`, then teleport with slingshot code as `target`; drops to 5% capacity per use, needs ≥80%.
- **Galactic observatory** — `prospect` (optional `direction` vector) → `prospect.completed`.
- **Autofactory** — queued printing (`enqueue_print` etc.), decommission recycling, blueprint discovery; modular.

---

## Confirmed from live data (2026-10-01)

**Device list fields** (`GET /devices`): `controller_device_code` (AMI controller running it, or null), `stowed_in_device_code`,
`attached_to_device_code`, `attached_devices` (carrier side), `attach_capacity`, `stow_capacity`/`stow_used`/`stowed_devices`,
`cargo` (list of `{quantity, resource_type}`), `cargo_capacity`/`cargo_used`, `in_control_range` (bool), `hosting_replicant`,
`location` = null while stowed, `taxi_mode` ("taxi") on surge plates, `scan` `{target, started_at, completes_at, progress_percent}`
on surveying drones, `tracking_site_id`.
AMI controllers: `ami_directive` `{name, config, _eval_state}`, `ami_directive_status` ("active" / null), `available_directives`.
`_eval_state` values seen: `exhausted:[resources]:<place>`, `idle`, `idle:no_sources`, `no_targets:recalling`,
`active:1l:0d`, `searching:4:0`. Survey controllers also offer `belt_search`.
Autofactory: `print_queue` items `{device_type, notify: {device}, tags}` (tags **are** kept), `printing`
`{device_type, started_at, completes_at, eta_seconds, progress_percent, tags}`, queue capacity = blueprint `queue_size` (10);
the error is `Not enough queue space (N available, M requested)` and the running print takes a slot.

**Surge plates:** `available_commands` include `attach`/`detach`; the **carrier** attaches the cargo
(`POST /devices/<plate> {"command": "attach", "device": <cargo>}`); telling the cargo to attach fails with
"Device does not have attach capability". `device.detached` comes from the plate with `{target_code, target_type}`.
Transport drones/haulers have no `stow`/`deploy` ("Cannot deploy a transport drone") — they can only ride attach carriers.
Plates in `taxi_mode: "taxi"` under a transport controller carry its drones between systems for a `ferry`
(the ferry digest shows drones `surging`, then `device.detached`).

**travel.arrived:** `{destination, origin, travel_type, attached_devices, recalling}`; a surge arrival has star codes
(`origin: "AEMEROTH"`, `destination: "FALQUORYX"`) and lands at an entry point (`location: FALQUORYX-1-L4`).
Travel to where it already is fails with "Already at destination".

**Mining:** `start_mining` on an exhausted belt → "Belt exhausted - no active resource sites". Belt `resource_sites` was `[]`
for an exhausted belt. A moon with salvage stock showed it as `inventory` (`AEMEROTH-6-7`: 277 structural);
`GET /locations/<...-SAL-n>` → "Planet not found" (use the body code).

**Events:** `GET /events?event_type=…` does **not** filter (returns everything); filter client-side.
`ami.transport.digest` payload: `{directive, devices[{device_code, status, last_event}], report{collect, deliver,
cargo_capacity, cargo_carried, fleet{delivering, loading, waiting}, resources{device: {res: qty}}}}`. `ami.released` `{devices: [{device_code, device_type}]}`.

**Blueprints:** cargo_freighter cargo 500 (print 1200 s); transport_hauler 80; transport_drone 20; cargo_vessel stow 50 / cargo 200 / attach 3;
surge_plate attach 1; surge_platform 4; surge_carrier 9; mobile_fleet 36; autofactory queue 10.
