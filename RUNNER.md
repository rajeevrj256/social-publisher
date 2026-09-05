# Runner — every API call, and what it does

The admin API is the only way to configure this system. There is no dashboard.
It listens on **localhost only** and every call needs a bearer token.

```bash
cd /Users/user/Documents/imp/social-publisher
TOKEN=$(grep -m1 '^ADMIN_API_TOKEN=' .env | cut -d= -f2)
API=http://127.0.0.1:8088
```

> **`-m1` matters.** Without it, a duplicate key in `.env` puts a newline inside
> `$TOKEN`, which splits the `Authorization` header and every request fails with
> a misleading `Invalid HTTP request received`.

---

## The model in one picture

```
SCHEDULE ──owns──> ACCOUNTS          one slot, fired once, same video for all
   │
THEME ────────────> which videos an account may post
   │
SOURCE ───────────> where those videos come from (Drive folder / local folder)
   │
VIDEO ────────────> indexed as a link; bytes fetched only at publish time
   │
PUBLICATION ──────> one row per (video, account, platform): status, post id,
                    URL, and the exact title/caption/hashtags used
```

---

## 0. Is it alive?

```bash
curl -sS $API/health
```
```json
{"status": "ok"}
```
The only endpoint that needs no token. Use it to check the container is up.

---

## 1. Themes — the allowed set

A theme is a content category. **Videos and accounts are matched through it.**
It must exist before anything can reference it; `/ingest` refuses unknown names
rather than inventing them, because a typo would otherwise park videos under a
theme no account publishes from.

### List them

```bash
curl -sS $API/themes -H "Authorization: Bearer $TOKEN"
```
```json
[{"id": 1, "name": "Sticky Faceless", "active": true,
  "coordinated": true, "videos": 635, "description": "..."}]
```

### Create one

```bash
curl -sS -X POST $API/themes \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name": "Luxury_theme", "description": "Luxury lifestyle reels"}'
```

Returns **409** if the name already exists. `coordinated` defaults to **true**.

### Turn coordination off

```bash
curl -sS -X PATCH "$API/themes/Luxury_theme?coordinated=false" \
  -H "Authorization: Bearer $TOKEN"
```

**Coordinated (default):** every account on the theme publishes the *same* video
each cycle. **Off:** each account picks independently and they drift apart as
soon as one is paused or rejects something. Approvals are per account either way.

---

## 2. Accounts

### Create

```bash
curl -sS -X POST $API/accounts \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{
    "platform": "youtube",
    "username": "aestheticFinds256",
    "account_name": "Aesthetic Finds",
    "platform_account_id": "UCRfFQzzcuaCb91191iSM8GQ",
    "timezone": "Asia/Kolkata",
    "require_approval": true
  }'
```

`platform` is `youtube` or `instagram`. `platform_account_id` is the YouTube
channel id or Instagram user id. `require_approval: true` means nothing uploads
without your explicit yes on Telegram.

Everywhere an account is referenced you may pass its **id** (`"1"`) or its
username. **Prefer the id** — usernames can be renamed, ids cannot.

### OAuth client (per account)

```bash
curl -sS -X POST $API/accounts/1/oauth-client \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"client_id": "xxx.apps.googleusercontent.com",
       "client_secret": "GOCSPX-..."}'
```

Different Google accounts are different Cloud projects with different client ids;
refreshing a token against the wrong one fails with an opaque `invalid_client`.
The secret is **encrypted at rest and never returned** by any endpoint.

Access and refresh tokens do **not** come through this API — they come from the
browser flow (`scripts/oauth_youtube.py`), so a leaked admin token cannot mint
platform access.

---

## 3. Ingest — the main one

Point an account (or several) at a Drive folder for a theme. **This is the call
you will use most.**

```bash
curl -sS -X POST $API/ingest \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{
    "account":   ["aestheticFinds256", "ig_account_1"],
    "theme":     "Luxury_theme",
    "drive_url": "https://drive.google.com/drive/folders/FOLDER_ID?usp=sharing"
  }'
```

| Field | Required | Meaning |
| --- | --- | --- |
| `account` | yes | one account **or a list**; ids or usernames, mixed freely |
| `theme` | yes | **must already exist** — 400 listing valid values otherwise |
| `drive_url` | yes | folder shared as "Anyone with the link" |
| `source_name` | no | defaults to `drive_<theme slug>` |
| `priority` | no | weight against the account's other themes (default 10) |
| `replace_sources` | no | `true` drops the account's other sources; default adds |

What it does, in order:

1. Resolves **every** account first — a typo in the third entry fails the whole
   call with 404 rather than half-wiring the first two.
2. Creates the source (or repoints an existing one of the same name).
3. Maps the theme and the source to each account.
4. **Queues** indexing and returns immediately.

```json
{"accounts": [{"id": 1, "username": "aestheticFinds256",
               "theme_mapped": true, "source_mapped": true}],
 "theme": "Luxury_theme", "coordinated": true,
 "source": "drive_luxury_theme", "indexing": "queued", "job_id": "f967..."}
```

Indexing runs in the background because listing a few hundred Drive files takes
minutes — no HTTP client should wait through that. A Telegram message reports
the result. **The folder is indexed once**, not per account.

**No video is downloaded here.** Only Drive file ids are stored; each clip is
fetched at publish time and deleted straight after, so a 20 GB library costs
nothing on disk.

---

## 4. Schedules — when it publishes

### Shared schedule (recommended)

**One slot, many accounts, fired once.**

```bash
curl -sS -X POST $API/schedules \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{
    "name": "evening_1805",
    "publish_time": "18:05",
    "accounts": ["aestheticFinds256", "ig_account_1"],
    "videos_per_day": 1,
    "timezone": "Asia/Kolkata"
  }'
```

| Field | Required | Meaning |
| --- | --- | --- |
| `name` | yes | unique; re-posting the same name updates it |
| `publish_time` | yes | `HH:MM` in this schedule's timezone |
| `accounts` | yes | ids or usernames |
| `videos_per_day` | no | slots per day (default 1) |
| `day_of_week` | no | `0`=Mon … `6`=Sun; omit for daily |
| `timezone` | no | default `Asia/Kolkata` |
| `theme` | no | restrict this slot to one theme |

**Why this exists.** Giving each account its own schedule row is broken for
shared content: the schedules fire at their own times and each fan-out reaches
the others, so every account ends up publishing *twice*. A shared schedule picks
the video **once** and hands it to every member. Creating one therefore deletes
the per-account rows for those accounts (`per_account_schedules_removed`).

### List / delete

```bash
curl -sS $API/schedules -H "Authorization: Bearer $TOKEN"
curl -sS -X DELETE $API/schedules/evening_1805 -H "Authorization: Bearer $TOKEN"
```

### Per-account schedule (independent accounts only)

```bash
curl -sS -X POST $API/accounts/1/schedules \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '[{"publish_time": "10:00", "videos_per_day": 1},
       {"publish_time": "20:00", "videos_per_day": 1}]'
```

**Replaces** that account's schedule rows. Use only for accounts that should run
their own queue, not for accounts sharing content.

---

## 5. Sources — where videos come from

```bash
curl -sS $API/sources -H "Authorization: Bearer $TOKEN"
```
```json
[{"id": 2, "name": "drive_motivation", "kind": "gdrive",
  "theme": "Sticky Faceless", "videos": 628, "enabled": true,
  "location": "https://drive.google.com/drive/folders/..."}]
```

Use this to watch indexing progress — `videos` climbs as the background job runs.

### Create directly (usually `/ingest` does this for you)

```bash
curl -sS -X POST $API/sources \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name": "drive_gym", "kind": "gdrive",
       "location": "https://drive.google.com/drive/folders/ID"}'
```

| `kind` | `location` | behaviour |
| --- | --- | --- |
| `gdrive` | Drive folder URL | indexed as links; fetched per video at publish time |
| `local` | folder under `VIDEO_ROOT` | files already on disk |
| `remote_url` | https base URL | Instagram fetches straight from it |

An account draws **only** from the sources mapped to it. One source can feed
several accounts.

---

## 6. Mapping — read and adjust

### See everything

```bash
curl -sS $API/mapping -H "Authorization: Bearer $TOKEN"
```
```json
[{"id": 1, "username": "aestheticFinds256", "platform": "youtube",
  "enabled": true, "require_approval": true,
  "sources": ["drive_motivation"], "themes": {"Sticky Faceless": 10},
  "oauth_client_id": "2834...", "has_client_secret": true,
  "has_access_token": true, "has_refresh_token": true}]
```

Credentials show as **presence only** — secrets never leave the database.
`has_refresh_token: false` means that account cannot publish yet.

### Replace an account's sources / themes

```bash
curl -sS -X POST $API/accounts/1/sources \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '["drive_motivation"]'

curl -sS -X POST $API/accounts/1/themes \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"Sticky Faceless": 10, "Luxury_theme": 5}'
```

Both **replace** rather than append, so re-posting a corrected list cannot leave
a stale entry behind. Theme values are priority weights: `10` vs `5` posts about
twice as often.

### Pin one video to specific accounts

```bash
curl -sS -X POST $API/videos/395/accounts \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '["aestheticFinds256"]'
```

A video with **no** pin is governed by theme and source. The moment any pin
exists it becomes authoritative and only listed accounts may use it — so this
narrows eligibility, never widens it. That table being empty is normal.

---

## 7. Bulk — wire everything at once

```bash
curl -sS -X POST $API/bulk \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{
    "sources": [
      {"name": "link1", "location": "https://drive.google.com/drive/folders/A",
       "kind": "gdrive"}
    ],
    "accounts": [
      {"platform": "youtube", "username": "yt_1", "account_name": "YT One"}
    ],
    "mappings": [
      {"account": "yt_1", "sources": ["link1"], "themes": {"Sticky Faceless": 10},
       "schedules": [{"publish_time": "18:05", "videos_per_day": 1}]}
    ],
    "oauth_clients": {
      "yt_1": {"client_id": "aaa.apps...", "client_secret": "GOCSPX-aaa"}
    }
  }'
```

Rebuilds a whole configuration in one call. Does **not** index — run `/ingest`
or `./start.sh scan` afterwards.

---

## 8. Typical setups

### One new niche on one account

```bash
curl -sS -X POST $API/themes -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"name":"Gym"}'

curl -sS -X POST $API/ingest -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"account":"1","theme":"Gym",
       "drive_url":"https://drive.google.com/drive/folders/ID"}'

curl -sS -X POST $API/schedules -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"gym_daily","publish_time":"19:00","accounts":["1"]}'
```

### Same content across three accounts

```bash
curl -sS -X POST $API/themes -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"name":"Luxury_theme"}'

curl -sS -X POST $API/ingest -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"account":["1","ig_a","ig_b"],"theme":"Luxury_theme",
       "drive_url":"https://drive.google.com/drive/folders/ID"}'

curl -sS -X POST $API/schedules -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"luxury_evening","publish_time":"19:00",
       "accounts":["1","ig_a","ig_b"]}'
```

All three get the **same video** each evening, each approving its own caption.

### Check before the slot fires

```bash
curl -sS $API/mapping   -H "Authorization: Bearer $TOKEN"   # tokens present?
curl -sS $API/sources   -H "Authorization: Bearer $TOKEN"   # videos indexed?
curl -sS $API/schedules -H "Authorization: Bearer $TOKEN"   # slot correct?
```

---

## 9. After the API: Telegram

The API configures; Telegram operates. At each slot you receive the video with
its details and approve there.

| Command | Purpose |
| --- | --- |
| `/needs` | posts waiting on your wording |
| `/meta <id>` | set Title / Caption / Tags in one message |
| `/pending` | posts waiting on your approval |
| `/approve <id>` · `/reject <id>` · `/defer <id>` | decide |
| `/status` · `/queue` · `/recent` · `/failed` · `/retry <id>` | operate |
| `/pause <account>` · `/resume <account>` | control one account |

### The three decisions

Each approval message carries three buttons. They differ in what happens to
the **video**, not just to the slot:

| Button | Command | Effect on the video |
| --- | --- | --- |
| ✅ Approve | `/approve <id>` | uploads now |
| ❌ Reject | `/reject <id>` | **permanent** — never offered to this account again |
| 🕒 Not today | `/defer <id>` | back in the pool from midnight, account-local |

Reject is permanent because the unique constraint on
`(video, account, platform)` allows exactly one publication row per pair, and a
rejected row keeps occupying it. "Not today" writes `available_after` instead;
once that passes, selection stops excluding the video and the scheduler revives
that same row rather than inserting a second one.

Both a reject and a defer immediately propose a different video for the same
slot, up to `REJECTION_LIMIT` proposals.

The scope is per account. Deferring or rejecting on `yt1` says nothing about
`ig1` — the same video can still go there tomorrow.

### If you do not answer

An unanswered request is **not** treated as a rejection. At
`APPROVAL_CUTOFF_HOUR` (default 23:00, account-local) the scheduler defers it
automatically and tells you so on Telegram. The slot is lost; the video returns
the next day.

The deadline is recomputed on every 60s tick rather than fired by a cron at
23:00, so a container that is down at the cutoff still expires the request when
it comes back up.

To see what would expire and when:

```bash
docker compose exec -T scheduler python -c "
from datetime import timezone
from zoneinfo import ZoneInfo
from sqlalchemy import select
from src.db import session_scope
from src.models import Account, Publication, PublicationStatus
from src.scheduler import approval_deadline
with session_scope() as s:
    for p in s.scalars(select(Publication).where(
            Publication.status == PublicationStatus.awaiting_approval)):
        a = s.get(Account, p.account_id)
        req = p.approval_requested_at or p.scheduled_at or p.created_at
        if req.tzinfo is None: req = req.replace(tzinfo=timezone.utc)
        d = approval_deadline(a, req).astimezone(ZoneInfo(a.timezone))
        print(f'pub {p.id} -> auto-defer at {d:%d %b %H:%M} {a.timezone}')
"
```

---

## 10. Errors worth recognising

| Response | Cause |
| --- | --- |
| `Invalid HTTP request received` | newline in `$TOKEN` — use `grep -m1` |
| `401 invalid token` | wrong token, or run from another directory |
| `400 unknown theme` | register it first with `POST /themes` |
| `404 account not found` | typo, or use the numeric id |
| `409 theme already exists` | it is already registered; nothing to do |
| `500` after a model change | containers running stale code — `docker compose up -d --build` |



#shift auth user:

gh auth switch --user rajeevrj256