# Mapping API

POST your account → link routing straight into PostgreSQL. No dashboard, no UI —
just JSON endpoints, protected by a bearer token and published on **localhost
only**.

Base URL: `http://127.0.0.1:8088`
Auth: `Authorization: Bearer $ADMIN_API_TOKEN` (generated on first `./start.sh`)

```bash
TOKEN=$(grep '^ADMIN_API_TOKEN=' .env | cut -d= -f2)
```

---

## The routing model

An account draws **only** from the sources mapped to it:

```
youtube_1    →  link1
youtube_2    →  link2
instagram_1  →  link2      (same link, different account — allowed)
instagram_2  →  link3
```

A source is either:

| kind | `location` | meaning |
| --- | --- | --- |
| `local` | folder under `VIDEO_ROOT` | e.g. `channel_one` → `/videos/to_publish/channel_one` |
| `remote_url` | https base URL | Instagram fetches from it directly; no local hosting needed |

An account with **no** sources mapped draws from the whole library, so existing
single-library setups keep working.

---

## Wire everything in one POST

```bash
curl -X POST http://127.0.0.1:8088/bulk \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "sources": [
      {"name": "link1", "location": "channel_one",  "kind": "local"},
      {"name": "link2", "location": "channel_two",  "kind": "local"},
      {"name": "link3", "location": "https://cdn.example.com/reels",
       "kind": "remote_url"}
    ],
    "accounts": [
      {"platform": "youtube",   "username": "youtube_1",   "account_name": "YT One"},
      {"platform": "youtube",   "username": "youtube_2",   "account_name": "YT Two"},
      {"platform": "instagram", "username": "instagram_1", "account_name": "IG One"},
      {"platform": "instagram", "username": "instagram_2", "account_name": "IG Two"}
    ],
    "mappings": [
      {"account": "youtube_1",   "sources": ["link1"], "themes": {"Motivation": 10},
       "schedules": [{"publish_time": "10:00", "videos_per_day": 1}]},
      {"account": "youtube_2",   "sources": ["link2"], "themes": {"Motivation": 10},
       "schedules": [{"publish_time": "18:00", "videos_per_day": 1}]},
      {"account": "instagram_1", "sources": ["link2"], "themes": {"Gym": 10},
       "schedules": [{"publish_time": "12:00", "videos_per_day": 2}]},
      {"account": "instagram_2", "sources": ["link3"], "themes": {"Supercars": 10},
       "schedules": [{"publish_time": "19:00", "videos_per_day": 1}]}
    ]
  }'
```

Then ingest each link's videos:

```bash
./start.sh scan     # walks every enabled local source separately
```

Verify:

```bash
curl -s http://127.0.0.1:8088/mapping -H "Authorization: Bearer $TOKEN" | jq
```

---

## Per-account OAuth clients

Channels under different Google accounts each have their own Cloud project and
client id. Store them per account:

```bash
curl -X POST http://127.0.0.1:8088/accounts/youtube_1/oauth-client \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"client_id": "aaa.apps.googleusercontent.com",
       "client_secret": "GOCSPX-aaa"}'
```

Or inside `/bulk`:

```json
{
  "oauth_clients": {
    "youtube_1": {"client_id": "aaa.apps...", "client_secret": "GOCSPX-aaa"},
    "youtube_2": {"client_id": "bbb.apps...", "client_secret": "GOCSPX-bbb"}
  }
}
```

Secrets are **write-only**: encrypted at rest, never returned by any endpoint.
`GET /mapping` reports presence only:

```json
{"username": "youtube_1", "oauth_client_id": "aaa.apps...",
 "has_client_secret": true, "has_refresh_token": false}
```

Access and refresh tokens still come from the browser OAuth flow
(`scripts/oauth_youtube.py`) — the API cannot set them, so a leaked admin token
cannot mint platform access.

## Add a Drive folder to an account (one call)

```bash
curl -X POST http://127.0.0.1:8088/ingest \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{
    "account":   ["aestheticFinds256", "ig_account_1"],
    "theme":     "Gym Motivation",
    "drive_url": "https://drive.google.com/drive/folders/FOLDER_ID?usp=sharing"
  }'
```

`account` takes one value or a list, by id or name. The folder is indexed once
regardless. Unknown account anywhere in the list → 404, nothing wired.

The theme **must already be registered** (`POST /themes`); an unknown one is a
400 listing the valid values. Maps both theme and source to the account and
queues indexing. Optional: `source_name`, `priority`,
`replace_sources`. See **SETUP.md §8b**.

## Individual endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | liveness, no auth |
| `GET` | `/mapping` | full account → sources/themes picture |
| `POST` | `/sources` | create or update a source |
| `GET` | `/sources` | list sources |
| `POST` | `/accounts` | create an account |
| `POST` | `/accounts/{account}/sources` | **replace** the account's links |
| `POST` | `/accounts/{account}/themes` | replace themes `{"Motivation": 10}` |
| `POST` | `/accounts/{account}/schedules` | replace publishing times |
| `POST` | `/videos/{id}/accounts` | pin one video to specific accounts |
| `GET` | `/themes` | the valid theme set |
| `PATCH` | `/themes/{name}?coordinated=false` | opt out of same-video-everywhere |
| `POST` | `/themes` | register a new theme (409 if it exists) |
| `POST` | `/ingest` | Drive folder + theme + account, in one call |
| `POST` | `/accounts/{account}/oauth-client` | per-account OAuth app id/secret |
| `POST` | `/bulk` | everything above in one call |

```bash
# Point one account at a different link
curl -X POST http://127.0.0.1:8088/accounts/youtube_1/sources \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '["link2"]'
```

`/accounts/{account}/sources`, `/themes` and `/schedules` **replace** rather than
append, so re-posting a corrected mapping cannot leave a stale link behind.

---

## Notes

- Tokens and credentials are never accepted or returned by this API. OAuth stays
  in `SETUP.md` / `manage.py`, so a leaked admin token cannot exfiltrate a
  platform token.
- `ADMIN_API_HOST` defaults to binding inside the container; compose publishes it
  as `127.0.0.1:8088`. Change that only if you genuinely need remote access, and
  put TLS in front of it if you do.
