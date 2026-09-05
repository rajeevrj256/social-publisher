#!/usr/bin/env bash
# One-command bootstrap for macOS and Linux.
#   ./start.sh          start everything (creates .env and keys on first run)
#   ./start.sh stop     stop
#   ./start.sh logs     follow logs
#   ./start.sh scan     ingest videos
#   ./start.sh seed     load the example accounts/themes
set -euo pipefail
cd "$(dirname "$0")"

compose() {
  if docker compose version >/dev/null 2>&1; then docker compose "$@"
  elif command -v docker-compose >/dev/null 2>&1; then docker-compose "$@"
  else echo "Docker Compose not found. Install Docker Desktop." >&2; exit 1; fi
}

random_key() {
  # Fernet needs url-safe base64 of 32 bytes; openssl emits standard base64.
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -base64 32 | tr '+/' '-_'
  else
    docker run --rm python:3.12-slim python -c \
      "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())" \
      2>/dev/null || head -c 32 /dev/urandom | base64 | tr '+/' '-_'
  fi
}

random_password() {
  if command -v openssl >/dev/null 2>&1; then openssl rand -hex 24
  else head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n'; fi
}

set_env() { # key value - fill in only if the value is currently empty
  local key="$1" value="$2"
  if grep -qE "^${key}=.+" .env 2>/dev/null; then return; fi
  if grep -qE "^${key}=" .env 2>/dev/null; then
    # BSD and GNU sed disagree on -i, so rewrite through a temp file instead.
    awk -v k="$key" -v v="$value" -F= 'BEGIN{OFS="="}
      $1==k{print k, v; next} {print}' .env > .env.tmp && mv .env.tmp .env
  else
    printf '%s=%s\n' "$key" "$value" >> .env
  fi
}

if ! docker info >/dev/null 2>&1; then
  echo "Docker is not running. Start Docker Desktop and try again." >&2
  exit 1
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from the template."
fi
set_env APP_ENCRYPTION_KEY "$(random_key)"
set_env POSTGRES_PASSWORD "$(random_password)"
set_env ADMIN_API_TOKEN "$(random_password)"
mkdir -p videos/to_publish

case "${1:-up}" in
  stop|down) compose down ;;
  logs)      compose logs -f scheduler worker bot ;;
  scan)      compose run --rm worker python -m src.manage scan ;;
  seed)      compose run --rm worker python -m scripts.seed_example ;;
  tick)      compose run --rm worker python -m src.manage tick ;;
  list)      compose run --rm worker python -m src.manage list ;;
  reset)
    if grep -qE '^DATABASE_URL=.+' .env; then
      echo "DATABASE_URL points at a managed database." >&2
      echo "Refusing to wipe it from here - drop the tables in that provider's" >&2
      echo "console if you really mean to." >&2
      exit 1
    fi
    read -r -p "This deletes ALL publication history. Type YES: " confirm
    [ "$confirm" = "YES" ] || { echo "Cancelled."; exit 1; }
    compose down -v && compose run --rm migrate ;;
  up|"")
    compose build
    # A managed database (Neon, RDS, ...) means no local Postgres container.
    if grep -qE '^DATABASE_URL=.+' .env; then
      echo "Using the managed database from DATABASE_URL."
      compose up -d redis
    else
      compose up -d postgres redis
    fi
    compose run --rm migrate
    compose up -d scheduler worker bot api
    echo
    echo "Running. Next:"
    echo "  1. Fill TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env (see SETUP.md)"
    echo "  2. ./start.sh seed        # example accounts and themes"
    echo "  3. ./start.sh scan        # ingest videos/to_publish"
    echo "  4. Send /status to your Telegram bot"
    echo
    echo "Logs: ./start.sh logs"
    ;;
  *) echo "Usage: ./start.sh [up|stop|logs|scan|seed|tick|list|reset]"; exit 1 ;;
esac
