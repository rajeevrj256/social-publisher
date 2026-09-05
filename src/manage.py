"""Admin CLI: everything you need to run the system without a dashboard."""

from __future__ import annotations

import argparse
import json
from datetime import time as dtime

from sqlalchemy import select

from .crypto import encrypt
from .db import session_scope
from .logging_setup import setup_logging
from .models import (
    Account, AccountAIConfig, AccountCredential, AccountSchedule, AccountTheme,
    FolderThemeMap, HashtagSet, Platform, Theme, Video, VideoAccountMapping,
)
from .scanner import scan


def cmd_scan(args) -> None:
    with session_scope() as session:
        stats = scan(session, args.root)
    print(json.dumps(stats, indent=2))


def cmd_add_account(args) -> None:
    with session_scope() as session:
        account = Account(
            platform=Platform(args.platform),
            account_name=args.name,
            username=args.username,
            platform_account_id=args.platform_id,
            timezone=args.timezone,
        )
        session.add(account)
        session.flush()
        print(f"created account #{account.id} {args.platform}:@{args.username}")


def cmd_set_token(args) -> None:
    """Tokens arrive here and are encrypted before they touch the database."""
    with session_scope() as session:
        account = session.get(Account, args.account_id)
        if not account:
            raise SystemExit(f"no account #{args.account_id}")
        credential = account.credentials or AccountCredential(account_id=account.id)
        if args.access_token:
            credential.access_token_encrypted = encrypt(args.access_token)
        if args.refresh_token:
            credential.refresh_token_encrypted = encrypt(args.refresh_token)
        if args.scopes:
            credential.scopes = args.scopes
        session.add(credential)
        print(f"stored credentials for account #{account.id} (encrypted)")


def cmd_set_oauth_client(args) -> None:
    """Store the OAuth app credentials for one account (secret encrypted)."""
    with session_scope() as session:
        account = session.get(Account, args.account_id)
        if not account:
            raise SystemExit(f"no account #{args.account_id}")
        credential = account.credentials or AccountCredential(account_id=account.id)
        credential.client_id = args.client_id
        if args.client_secret:
            credential.client_secret_encrypted = encrypt(args.client_secret)
        session.add(credential)
        print(f"OAuth client stored for @{account.username} (secret encrypted)")


def cmd_add_theme(args) -> None:
    with session_scope() as session:
        theme = Theme(name=args.name, description=args.description)
        session.add(theme)
        session.flush()
        print(f"created theme #{theme.id} {theme.name}")


def cmd_map_theme(args) -> None:
    with session_scope() as session:
        theme = session.scalar(select(Theme).where(Theme.name == args.theme))
        if not theme:
            raise SystemExit(f"no theme named {args.theme!r}")
        session.add(AccountTheme(account_id=args.account_id, theme_id=theme.id,
                                 priority=args.priority))
        print(f"mapped account #{args.account_id} -> {theme.name} "
              f"(priority {args.priority})")


def cmd_add_schedule(args) -> None:
    hour, _, minute = args.at.partition(":")
    with session_scope() as session:
        session.add(AccountSchedule(
            account_id=args.account_id,
            day_of_week=args.day,
            publish_time=dtime(int(hour), int(minute or 0)),
            videos_per_day=args.per_day,
            timezone=args.timezone,
        ))
        print(f"schedule added for account #{args.account_id} at {args.at}")


def cmd_map_folder(args) -> None:
    with session_scope() as session:
        theme = session.scalar(select(Theme).where(Theme.name == args.theme))
        if not theme:
            theme = Theme(name=args.theme)
            session.add(theme)
            session.flush()
        session.add(FolderThemeMap(folder=args.folder, theme_id=theme.id))
        print(f"folder {args.folder}/ -> {theme.name}")


def cmd_hashtags(args) -> None:
    with session_scope() as session:
        theme_id = None
        if args.theme:
            theme = session.scalar(select(Theme).where(Theme.name == args.theme))
            if not theme:
                raise SystemExit(f"no theme named {args.theme!r}")
            theme_id = theme.id
        session.add(HashtagSet(account_id=args.account_id, theme_id=theme_id,
                               hashtags=args.tags))
        print("hashtags saved")


def cmd_ai(args) -> None:
    with session_scope() as session:
        account = session.get(Account, args.account_id)
        if not account:
            raise SystemExit(f"no account #{args.account_id}")
        config = account.ai_config or AccountAIConfig(account_id=account.id)
        config.ai_enabled = args.enabled
        if args.prompt:
            config.system_prompt = args.prompt
        session.add(config)
        print(f"AI config updated for @{account.username}")


def cmd_map_video(args) -> None:
    with session_scope() as session:
        session.add(VideoAccountMapping(video_id=args.video_id,
                                        account_id=args.account_id,
                                        priority=args.priority))
        print(f"video #{args.video_id} -> account #{args.account_id}")


def cmd_list(args) -> None:
    with session_scope() as session:
        print("ACCOUNTS")
        for account in session.scalars(select(Account).order_by(Account.id)):
            print(f"  #{account.id} {account.platform.value:9s} @{account.username}"
                  f"{'' if account.enabled else '  [paused]'}")
        print("\nTHEMES")
        for theme in session.scalars(select(Theme).order_by(Theme.id)):
            count = session.scalar(
                select(__import__('sqlalchemy').func.count()).select_from(Video)
                .where(Video.theme_id == theme.id))
            print(f"  #{theme.id} {theme.name} ({count} videos)")


def cmd_tick(args) -> None:
    from .scheduler import tick
    print(json.dumps(tick(), indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="manage")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("scan", help="ingest videos from disk")
    p.add_argument("--root")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("add-account")
    p.add_argument("--platform", required=True, choices=["instagram", "youtube"])
    p.add_argument("--name", required=True)
    p.add_argument("--username", required=True)
    p.add_argument("--platform-id")
    p.add_argument("--timezone", default="Asia/Kolkata")
    p.set_defaults(func=cmd_add_account)

    p = sub.add_parser("set-token")
    p.add_argument("account_id", type=int)
    p.add_argument("--access-token")
    p.add_argument("--refresh-token")
    p.add_argument("--scopes")
    p.set_defaults(func=cmd_set_token)

    p = sub.add_parser("set-oauth-client",
                       help="per-account OAuth app id/secret")
    p.add_argument("account_id", type=int)
    p.add_argument("--client-id", required=True)
    p.add_argument("--client-secret", required=True)
    p.set_defaults(func=cmd_set_oauth_client)

    p = sub.add_parser("add-theme")
    p.add_argument("name")
    p.add_argument("--description")
    p.set_defaults(func=cmd_add_theme)

    p = sub.add_parser("map-theme")
    p.add_argument("account_id", type=int)
    p.add_argument("theme")
    p.add_argument("--priority", type=int, default=10)
    p.set_defaults(func=cmd_map_theme)

    p = sub.add_parser("add-schedule")
    p.add_argument("account_id", type=int)
    p.add_argument("--at", required=True, help="HH:MM local to the account")
    p.add_argument("--day", type=int, help="0=Mon .. 6=Sun; omit for daily")
    p.add_argument("--per-day", type=int, default=1)
    p.add_argument("--timezone")
    p.set_defaults(func=cmd_add_schedule)

    p = sub.add_parser("map-folder")
    p.add_argument("folder")
    p.add_argument("theme")
    p.set_defaults(func=cmd_map_folder)

    p = sub.add_parser("hashtags")
    p.add_argument("account_id", type=int)
    p.add_argument("tags")
    p.add_argument("--theme")
    p.set_defaults(func=cmd_hashtags)

    p = sub.add_parser("ai")
    p.add_argument("account_id", type=int)
    p.add_argument("--enabled", action="store_true")
    p.add_argument("--prompt")
    p.set_defaults(func=cmd_ai)

    p = sub.add_parser("map-video")
    p.add_argument("video_id", type=int)
    p.add_argument("account_id", type=int)
    p.add_argument("--priority", type=int, default=0)
    p.set_defaults(func=cmd_map_video)

    p = sub.add_parser("list")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("tick", help="run one scheduler pass now")
    p.set_defaults(func=cmd_tick)
    return parser


def main() -> None:
    setup_logging("manage")
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
