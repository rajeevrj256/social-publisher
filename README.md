# Social Publisher

Automated, multi-account social publishing driven by PostgreSQL and controlled
entirely from Telegram. No web dashboard.

**Nothing is uploaded without your explicit approval on Telegram.**

---

## One command

**macOS / Linux**

```bash
./start.sh
```

**Windows (PowerShell)**

```powershell
.\start.ps1
```

First run creates `.env`, generates the encryption key and database password,
builds the images, runs the migrations and starts the scheduler, worker and bot.
Everything runs in Docker, so the three platforms behave identically.

Then add your Telegram token and chat id to `.env` (see **SETUP.md**), and:

```bash
./start.sh seed      # example accounts, themes, schedules
./start.sh scan      # ingest videos/to_publish
./start.sh logs
```

| Command | macOS/Linux | Windows |
| --- | --- | --- |
| start | `./start.sh` | `.\start.ps1` |
| stop | `./start.sh stop` | `.\start.ps1 stop` |
| logs | `./start.sh logs` | `.\start.ps1 logs` |
| ingest videos | `./start.sh scan` | `.\start.ps1 scan` |
| run one pass now | `./start.sh tick` | `.\start.ps1 tick` |

---

## Per-account source links

Each account draws from its **own** link, and a link can feed more than one
account:

```
youtube_1    →  link1
youtube_2    →  link2
instagram_1  →  link2      (shared, still independent history)
instagram_2  →  link3
```

Wire it in one POST — see **MAPPING_API.md**:

```bash
curl -X POST http://127.0.0.1:8088/bulk -H "Authorization: Bearer $TOKEN" ...
```

A source is a folder under `VIDEO_ROOT` (`local`) or a public https base URL
(`remote_url`). An account with no sources mapped draws from the whole library.

## The model

```
ACCOUNT  ->  THEME  ->  VIDEO  ->  PUBLICATION  ->  post id / url / metadata
```

Publication state is **per (video, account, platform)** — never a boolean on the
video. One clip can serve twenty accounts and keep separate history for each.

```
video001.mp4
  ├── @motivation_daily  (instagram)  published   https://instagram.com/...
  ├── @cars_world        (instagram)  pending
  └── Motivation Channel (youtube)    published   https://youtube.com/...
```

The duplicate rule is a database constraint, not application logic:

```sql
UNIQUE (video_id, account_id, platform)
```

Same video + same account + same platform cannot happen twice. The same video
across different accounts is unconstrained.

---

## No AI key? The bot asks you

`ANTHROPIC_API_KEY` is optional. Without it nothing is invented and no filename
is ever used as a caption — the bot asks you for the wording first:

```
✍️ DETAILS NEEDED

Publication:  #41
Account:      @motivation_daily (instagram)
Video:        motivation001.mp4  (28.4s, 1080x1920)
Missing:      title, caption

Reply with everything at once:

/meta 41
Title: Never Give Up
Caption: Keep moving forward.
Tags: #motivation #success
```

Send that back and it moves straight to the approval card. One field at a time
works too: `/title 41 ...`, `/caption 41 ...`, `/tags 41 ...`. `/needs` lists
everything waiting on your wording.

`/approve` refuses while a title or caption is missing, so a half-written post
cannot slip through.

Three ways to get captions, in order of precedence:

1. **You, on Telegram** — always wins.
2. **A caption template** on the account (`manage.py ai --prompt`), if set.
3. **Claude**, only when `ANTHROPIC_API_KEY` is set and the account has
   `ai_enabled`.

## How a post happens

1. The scheduler finds a due slot for **one account** (no global queue).
2. It picks a theme by weight, then the next eligible unpublished video.
3. It generates the title, caption and hashtags **for that account**.
4. It sends you an approval request on Telegram with every detail.
5. You tap ✅ or send `/approve <id>`. **Only then** does anything upload.
6. Reject and it proposes a different video — up to 5 per slot, then it stops.
7. The worker uploads and records post id, URL and the exact metadata used.

---

## Telegram

| Command | Purpose |
| --- | --- |
| `/needs` | posts waiting on your wording |
| `/meta <id>` | set Title/Caption/Tags in one message |
| `/title` `/caption` `/tags` | set one field |
| `/pending` | posts waiting on your approval |
| `/approve <id>` / `/reject <id>` | decide |
| `/status` | accounts, state, pending counts |
| `/accounts` | connected accounts |
| `/queue` | upcoming publications |
| `/recent` | recently published, with links |
| `/failed` | failures and anything needing review |
| `/retry [id]` | retry failed publications |
| `/pause <account>` / `/resume <account>` | control one account |
| `/publish <account>` | publish the next eligible video now |
| `/theme` | account → theme mappings |
| `/report` | today's summary |

Only the chat id in `TELEGRAM_CHAT_ID` is obeyed. Everyone else is ignored.

---

## Layout

```
social-publisher/
├── start.sh / start.ps1      one-command bootstrap
├── docker-compose.yml        postgres, redis, migrate, scheduler, worker, bot
├── SETUP.md                  API credentials + database, step by step
├── src/
│   ├── models.py             11 tables, the source of truth
│   ├── scanner.py            SHA-256 ingest, folder -> theme
│   ├── selection.py          theme weighting, eligibility, reservation
│   ├── scheduler.py          per-account slots, limits, retries
│   ├── jobs.py               publish job + crash recovery
│   ├── telegram_bot.py       the entire interface
│   ├── metadata_ai.py        per-account captions
│   ├── manage.py             admin CLI
│   └── publishers/           instagram.py, youtube.py (official APIs only)
└── tests/                    29 tests, no database server needed
```

---

## Platform limits worth knowing

- **Instagram:** 100 API publishes per rolling 24h per account. The scheduler
  checks the account's real usage via `content_publishing_limit` first.
- **Instagram:** the source file must be reachable over https —
  `PUBLIC_MEDIA_BASE_URL`.
- **YouTube:** `videos.insert` is capped at 100 uploads/day per Google Cloud
  project, shared across every channel in that project.

---

## Tests

```bash
python -m venv venv && ./venv/bin/pip install -r requirements-dev.txt
./venv/bin/python -m pytest tests/ -q
```

They run on SQLite, so no database server is required.

---

## Safety

- Only official APIs. No Selenium, no scraping, no password login.
- OAuth tokens are encrypted at rest; logs are redacted.
- Original video files are never modified or deleted.
- Postgres and Redis are not published to the host.
- Containers run as a non-root user; the video volume is read-only.
