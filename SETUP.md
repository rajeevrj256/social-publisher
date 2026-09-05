# Setup — API credentials and database, step by step

Work through these in order. Nothing here needs a web dashboard; everything is
CLI plus Telegram.

---

## 1. Prerequisites

- Docker + Docker Compose
- A domain serving your videos over **https** (Instagram fetches the file itself)
- A Telegram account
- A Meta developer account
- A Google account for each YouTube channel

---

## 2. Create the `.env`

```bash
cd social-publisher
cp .env.example .env
```

Generate the encryption key that protects OAuth tokens at rest:

```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Paste it into `APP_ENCRYPTION_KEY`. **Back it up.** If it is lost, every stored
token becomes undecryptable and each account must be re-authorised.

Set a strong `POSTGRES_PASSWORD` as well. Compose refuses to start without one.

---

## 3. Database

The schema is created by migrations — you never write SQL by hand.

### Option A — managed Postgres (Neon, Supabase, RDS)

Put the connection string in `DATABASE_URL`. The local Postgres container is
then never started, and `POSTGRES_*` is ignored.

Neon gives you a JDBC string; convert it:

```
jdbc:postgresql://ep-xxx.aws.neon.tech/neondb?user=U&password=P&sslmode=require
                              ↓
postgresql+psycopg://U:P@ep-xxx.aws.neon.tech/neondb?sslmode=require&channel_binding=require
```

```bash
docker compose run --rm migrate
```

> Use the **pooled** endpoint (`-pooler` in the host) — the worker, scheduler,
> bot and API each hold connections, and Neon's direct endpoint has a low limit.

### Option B — local Postgres in Docker

```bash
docker compose --profile localdb up -d postgres redis
docker compose run --rm migrate
```

Verify (managed database):

```bash
docker compose run --rm migrate python -c "
from sqlalchemy import create_engine, text
from src.config import get_settings
with create_engine(get_settings().sqlalchemy_url).connect() as c:
    print(c.execute(text(\"select table_name from information_schema.tables \"
                         \"where table_schema='public' order by 1\")).scalars().all())"
```

Or for the local container:

```bash
docker compose exec postgres psql -U publisher -d social_publisher -c '\dt'
```

You should see: `accounts`, `account_credentials`, `account_schedules`,
`account_themes`, `account_ai_config`, `folder_theme_map`, `hashtag_sets`,
`themes`, `videos`, `video_account_mapping`, `publications`.

To reset during setup (**destroys all history**, local container only):

```bash
docker compose --profile localdb down -v
docker compose run --rm migrate
```

`./start.sh reset` refuses to run against a managed database on purpose — wiping
a hosted production branch should be a deliberate act in that provider's console.

---

## 4. Telegram bot

1. Open Telegram, message **@BotFather**, send `/newbot`.
2. Choose a name and a username ending in `bot`.
3. Copy the token into `TELEGRAM_BOT_TOKEN`.
4. Send any message to your new bot (it cannot message you first).
5. Get your chat id:

```bash
curl -s "https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates" | grep -o '"id":[0-9-]*' | head -1
```

6. Put that number in `TELEGRAM_CHAT_ID`.

Only this chat can command the bot. Every other sender is ignored silently.

---

## 5. Instagram (Meta) — per account

> **The page won't load?** `developers.facebook.com/apps` returns 200 but
> redirects to a Facebook **login** page. Sign in to a Facebook account first,
> then reopen it. It is not a network block.

> **You do NOT need App Review.** Review is required only for apps serving other
> people's accounts. Publishing to accounts you own runs on **Standard Access**,
> which works immediately.

### 5.1 Which login flow

Meta offers two, on **different API hosts**:

| | Instagram Login | Facebook Login |
| --- | --- | --- |
| Facebook Page required | **No** | Yes, linked to the IG account |
| API host | `graph.instagram.com` | `graph.facebook.com` |
| Publish permission | `instagram_business_content_publish` | `instagram_content_publish` |

**Use Instagram Login** unless your accounts are already tied to Facebook Pages.
It is the default (`INSTAGRAM_GRAPH_HOST=graph.instagram.com`).

Calling the wrong host fails with a confusing permissions error rather than a
clear 404, so set it correctly per account if you mix the two:

```bash
# Only needed for a Facebook-Login account
docker compose run --rm worker python -c "
from src.db import session_scope
from src.models import Account, AccountCredential
with session_scope() as s:
    a = s.get(Account, 1)
    c = a.credentials or AccountCredential(account_id=a.id)
    c.extra = {'graph_host': 'graph.facebook.com'}
    s.add(c)"
```

### 5.2 Prepare the Instagram account

Each account must be **Professional** (Business or Creator):
Instagram app → Settings → Account type and tools → *Switch to professional
account*. Personal accounts cannot publish via the API.

### 5.3 Create the Meta app (once, covers all accounts)

1. Sign in at <https://www.facebook.com>, then open
   <https://developers.facebook.com/apps>.
2. If prompted, register as a developer (free, instant).
3. **Create app**.
4. Use case: **Other** → app type **Business** → name it → create.
5. In the left sidebar: **Add product** → **Instagram** → *Set up*.
6. Choose **API setup with Instagram login**.

### 5.4 Connect each Instagram account

Still in the Instagram product page, under **API setup with Instagram login**:

1. Step 1 *Generate access tokens* → **Add account** → log in to that Instagram
   account → authorise.
2. The dashboard then shows that account's **Instagram user ID** and a
   **Generate token** button.
3. Click **Generate token** and copy it. This token already carries
   `instagram_business_basic` and `instagram_business_content_publish`.

Repeat *Add account* for every Instagram account. One app serves all of them.

### 5.5 Copy the app credentials

**App settings → Basic** → copy **App ID** and **App secret** into `.env`
(`META_APP_ID`, `META_APP_SECRET`). These are used for refreshing tokens.

### 5.6 Register the account and store the token

```bash
docker compose run --rm worker python -m src.manage add-account \
    --platform instagram --username motivation_daily \
    --name "Motivation Daily" --platform-id 17841400000000000

docker compose run --rm worker python -m src.manage set-token 1 \
    --access-token "IGAAxxxxx..."
```

The token is encrypted before it reaches the database.

### 5.7 Token lifetime

Tokens from the dashboard are long-lived (**60 days**). Refresh before expiry:

```bash
curl -s "https://graph.instagram.com/refresh_access_token\
?grant_type=ig_refresh_token&access_token=CURRENT_TOKEN"
```

Then `set-token` again with the new value. `/accounts` in Telegram shows which
accounts have a token stored.

> **Rate limit:** 100 API publishes per rolling 24h **per account**. The
> scheduler checks each account's real usage before queueing.

> **Reels specs:** 9:16, 3–90 seconds, H.264/HEVC. The pre-flight check rejects
> anything else before uploading rather than after.

## 6. YouTube — per channel

1. Go to <https://console.cloud.google.com> → create a project.
2. **APIs & Services → Library** → enable **YouTube Data API v3**.
3. **OAuth consent screen** → External → add yourself as a **Test user**.
4. **Credentials → Create credentials → OAuth client ID → Desktop app**.
5. Store the client id/secret **on that account**, not in `.env`:

```bash
docker compose run --rm worker python -m src.manage set-oauth-client 4 \
    --client-id "xxx.apps.googleusercontent.com" --client-secret "GOCSPX-..."
```

> **Different Google accounts = different projects = different client ids.**
> Repeat steps 1–5 per Google account. The `.env` values are only a fallback for
> a single shared project. Refreshing a token against the wrong client fails with
> an opaque `invalid_client`, which is why this is stored per account.
>
> Separate projects also means separate quota: 100 uploads/day **each**, rather
> than all channels sharing one allowance.

6. Create the account row, then authorise it:

```bash
docker compose run --rm worker python -m src.manage add-account \
    --platform youtube --username motivation_channel --name "Motivation Channel"

# Run this on your laptop (it opens a browser), not inside Docker:
python -m scripts.oauth_youtube --account-id 4
```

The script stores the refresh token encrypted and fills in the channel id
automatically. Repeat per channel — each is an independent account.

> **Quota:** `videos.insert` is capped at **100 uploads/day per project**. All
> your channels share that project quota unless you create separate projects.

---

## 7. Videos

Organise by theme folder — the folder name becomes the theme:

```
videos/to_publish/
├── motivation/
│   ├── motivation001.mp4
│   └── motivation002.mp4
├── gym/
│   └── gym001.mp4
└── cars/
    └── car001.mp4
```

Point `HOST_VIDEO_ROOT` at that directory, then scan:

```bash
docker compose run --rm worker python -m src.manage scan
```

Files are never modified or deleted. Identity is the SHA-256 of the contents, so
re-scanning is safe and moving a file updates it in place.

Instagram needs the file over https. Serve `videos/to_publish/` from your web
server and set `PUBLIC_MEDIA_BASE_URL` to its base URL, so
`motivation/motivation001.mp4` resolves to
`https://media.example.com/motivation/motivation001.mp4`.

---

## 8. Themes, mappings and schedules

Seed the worked example from the brief:

```bash
docker compose run --rm worker python -m scripts.seed_example
```

Or configure by hand:

```bash
# Themes an account may post, with relative weight
docker compose run --rm worker python -m src.manage map-theme 1 Motivation --priority 10
docker compose run --rm worker python -m src.manage map-theme 1 Success --priority 5

# When it posts (local to the account's timezone)
docker compose run --rm worker python -m src.manage add-schedule 1 --at 10:00
docker compose run --rm worker python -m src.manage add-schedule 2 --at 12:00
docker compose run --rm worker python -m src.manage add-schedule 2 --at 20:00

# Default hashtags
docker compose run --rm worker python -m src.manage hashtags 1 "#motivation #success #mindset"

# Per-account AI voice (optional; needs ANTHROPIC_API_KEY)
docker compose run --rm worker python -m src.manage ai 1 --enabled \
    --prompt "Write short, energetic motivational captions."

# Restrict one video to specific accounts (optional)
docker compose run --rm worker python -m src.manage map-video 42 1
```

Check it:

```bash
docker compose run --rm worker python -m src.manage list
```

---

## 8a. Themes are a registered set

A theme has to exist before videos can be ingested against it. `/ingest` will
**not** invent one — an unrecognised name is almost always a typo, and creating
it silently would park those videos under a theme no account publishes from.

```bash
TOKEN=$(grep '^ADMIN_API_TOKEN=' .env | cut -d= -f2)

# register a theme
curl -X POST http://127.0.0.1:8088/themes \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name": "Gym Motivation", "description": "Faceless gym reels"}'

# see the valid set
curl -s http://127.0.0.1:8088/themes -H "Authorization: Bearer $TOKEN" | jq
```

Re-registering an existing name returns **409 Conflict** rather than creating a
duplicate.

## 8b. Adding more videos later — one call

`POST /ingest` is the single endpoint for "here is a Drive folder, this theme,
this account". It creates the theme if it is new, reuses it if it already
exists, wires both the source and the theme to the account, and queues the
indexing.

```bash
# -m1 matters: a duplicate key in .env would otherwise put a newline in the
# token and every request fails with "Invalid HTTP request".
TOKEN=$(grep -m1 '^ADMIN_API_TOKEN=' .env | cut -d= -f2)

# one account
curl -X POST http://127.0.0.1:8088/ingest \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "account":   "aestheticFinds256",
    "theme":     "Gym Motivation",
    "drive_url": "https://drive.google.com/drive/folders/YOUR_FOLDER_ID?usp=sharing"
  }'

# SEVERAL accounts sharing one theme and one folder
curl -X POST http://127.0.0.1:8088/ingest \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "account":   ["aestheticFinds256", "ig_account_1", "ig_account_2"],
    "theme":     "Gym Motivation",
    "drive_url": "https://drive.google.com/drive/folders/YOUR_FOLDER_ID?usp=sharing"
  }'
```

The folder is indexed **once**, not per account. Accounts may be ids or names,
mixed freely: `["1", "ig_account_1"]`.

If any account in the list is unknown the whole call fails with 404 and nothing
is wired — a typo in the third entry cannot half-configure the first two.

Response:

```json
{
  "account": "aestheticFinds256",
  "theme": "Gym Motivation",
  "source": "drive_gym_motivation",
  "source_created": true,
  "mapped": {"theme": true, "source": true},
  "indexing": "queued",
  "job_id": "f3a1..."
}
```

### Payload fields

| Field | Required | Meaning |
| --- | --- | --- |
| `account` | yes | username or account name; must already exist |
| `theme` | yes | **must already exist** — register via `POST /themes` first |
| `drive_url` | yes | shared Drive folder link ("Anyone with the link") |
| `source_name` | no | defaults to `drive_<theme slug>` |
| `priority` | no | weight against the account's other themes (default 10) |
| `replace_sources` | no | `true` drops the account's other sources; default adds |

### Behaviour

- **Existing theme** → reused, mapping refreshed, priority updated. No duplicate.
- **Unknown theme** → **400 error**, listing the valid themes. Nothing is
  indexed, nothing is created:

```json
{"detail": {
  "error": "unknown theme 'Gymm'",
  "valid_themes": ["Gym Motivation", "Sticky Faceless", "Success"],
  "hint": "register it first: POST /themes {\"name\": \"Gymm\"}"
}}
```
- **Same `source_name` again** → repointed at the new URL rather than duplicated,
  so re-running with a corrected link is safe.
- **Indexing is queued**, not inline: listing several hundred Drive files takes
  minutes. A Telegram message reports the result when it finishes.

Check progress:

```bash
curl -s http://127.0.0.1:8088/sources -H "Authorization: Bearer $TOKEN" | jq
```

```json
[{"name": "drive_gym_motivation", "kind": "gdrive",
  "theme": "Gym Motivation", "videos": 628, "enabled": true}]
```

Videos are **never downloaded** by this call — only their Drive file ids are
stored. Each clip is fetched at publish time and deleted straight after.

## 8c. Same video everywhere (default)

Themes are **coordinated by default** — nothing to switch on. One scheduler run
picks a video **once** and reserves that same video for every enabled account on
the theme, so accounts can never drift onto different clips.

With a single account it changes nothing. With several it is the guarantee that
sharing a theme actually shares the content.

To opt out and let each account run its own independent queue:

```bash
curl -X PATCH "http://127.0.0.1:8088/themes/Gym%20Motivation?coordinated=false" \
  -H "Authorization: Bearer $TOKEN"
```

Or at creation: `{"name": "Gym Motivation", "coordinated": false}`.

**Approvals stay separate.** Each account gets its own Telegram card and its own
title/caption/hashtags, so the same clip can carry different wording per account.
Approving one never publishes the others.

An account that cannot take the video — already published it, incompatible with
its platform, source not mapped — is skipped with a logged reason rather than
failing the group.

## 9. Start everything

```bash
docker compose up -d
docker compose logs -f scheduler worker bot
```

In Telegram send `/status`. You should see every account, its state and its
pending count.

---

## 9b. Captions: AI optional

`ANTHROPIC_API_KEY` is optional and blank by default.

**Without it**, the bot asks you for the wording on Telegram before anything is
published — it never invents a caption or posts a filename as a title:

```
/meta 41
Title: Never Give Up
Caption: Keep moving forward.
Tags: #motivation #success
```

**With a caption template** (no API key needed), captions are generated locally:

```bash
docker compose run --rm worker python -m src.manage ai 1 \
    --prompt "Write short, energetic motivational captions."
```

**With `ANTHROPIC_API_KEY`**, enable per account with `manage.py ai <id> --enabled`.
If the model call fails the post falls back to templates, and if there is still
nothing it asks you — a model outage never blocks or corrupts a post.

## 10. How publishing actually runs

1. The scheduler notices a due slot for an account.
2. It picks a theme by weight, then the next eligible unpublished video.
3. It generates the title, caption and hashtags for **that account**.
4. It sends you an approval request on Telegram with every detail.
5. **Nothing uploads until you approve.** Tap ✅, or send `/approve <id>`.
6. Reject instead and it proposes a different video — up to 5 per slot, then it
   stops for that slot.
7. On approval the worker uploads and saves the post id, URL and exact metadata.

Dry-run one pass without waiting for the clock:

```bash
docker compose run --rm worker python -m src.manage tick
```

Force a single post for one account:

```
/publish motivation_daily
```

---

## 11. Daily operation

| Command | Purpose |
| --- | --- |
| `/needs` | posts waiting on your wording |
| `/meta <id>` | supply Title/Caption/Tags in one message |
| `/pending` | posts waiting on you |
| `/approve <id>` | approve and upload |
| `/reject <id>` | reject; proposes another video |
| `/status` | accounts, state, pending counts |
| `/queue` | upcoming publications |
| `/recent` | recently published, with links |
| `/failed` | failures and anything needing review |
| `/retry [id]` | retry failed publications |
| `/pause <account>` / `/resume <account>` | stop or start one account |
| `/theme` | account → theme mappings |
| `/report` | today's summary |

A daily summary is sent automatically at 23:55.

---

## 12. Security notes

- Tokens are encrypted with `APP_ENCRYPTION_KEY` before they reach the database.
- Logs pass through a redaction filter that strips tokens from any message.
- Credentials are never sent over Telegram.
- Postgres and Redis are not published to the host — only the app network.
- Containers run as a non-root user; the video volume is mounted read-only.
