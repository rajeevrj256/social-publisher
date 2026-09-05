"""Seed the exact example from the brief: three IG accounts + one YT channel."""

from __future__ import annotations

import sys
from datetime import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from src.db import session_scope  # noqa: E402
from src.models import (  # noqa: E402
    Account, AccountAIConfig, AccountSchedule, AccountTheme, FolderThemeMap,
    HashtagSet, Platform, Theme,
)

THEMES = ["Motivation", "Success", "Mindset", "Gym", "Workout", "Fitness",
          "Supercars", "Luxury Cars", "Car Facts"]

ACCOUNTS = [
    ("instagram", "motivation_daily", "Motivation Daily",
     [("Motivation", 10), ("Success", 5)], [("10:00", None)],
     "#motivation #success #mindset",
     "Write short, energetic motivational Instagram captions. No clickbait."),
    ("instagram", "fitness_clips", "Fitness Clips",
     [("Gym", 10), ("Fitness", 8)], [("12:00", None), ("20:00", None)],
     "#gym #fitness #workout",
     "Write fitness captions using gym language. 3-5 relevant hashtags."),
    ("instagram", "cars_world", "Cars World",
     [("Supercars", 10), ("Luxury Cars", 6)], [("19:00", None)],
     "#supercars #luxurycars",
     "Write luxury car captions. Avoid generic hashtags."),
    ("youtube", "motivation_channel", "Motivation Channel",
     [("Motivation", 10)], [("18:00", None)],
     "#motivation #shorts",
     "Write YouTube Shorts titles under 60 characters. Direct, no emoji spam."),
]

FOLDERS = {"motivation": "Motivation", "success": "Success", "gym": "Gym",
           "workout": "Workout", "fitness": "Fitness", "cars": "Supercars",
           "luxury": "Luxury Cars", "carfacts": "Car Facts"}


def main() -> None:
    with session_scope() as session:
        themes = {}
        for name in THEMES:
            theme = session.scalar(select(Theme).where(Theme.name == name))
            if not theme:
                theme = Theme(name=name)
                session.add(theme)
                session.flush()
            themes[name] = theme

        for folder, theme_name in FOLDERS.items():
            exists = session.scalar(
                select(FolderThemeMap).where(FolderThemeMap.folder == folder))
            if not exists:
                session.add(FolderThemeMap(folder=folder,
                                           theme_id=themes[theme_name].id))

        for platform, username, name, theme_specs, slots, tags, prompt in ACCOUNTS:
            account = session.scalar(select(Account).where(
                Account.platform == Platform(platform),
                Account.username == username))
            if account:
                continue
            account = Account(platform=Platform(platform), username=username,
                              account_name=name, timezone="Asia/Kolkata")
            session.add(account)
            session.flush()

            for theme_name, priority in theme_specs:
                session.add(AccountTheme(account_id=account.id,
                                         theme_id=themes[theme_name].id,
                                         priority=priority))
            for at, day in slots:
                hour, minute = at.split(":")
                session.add(AccountSchedule(
                    account_id=account.id, publish_time=time(int(hour), int(minute)),
                    day_of_week=day, videos_per_day=1))
            session.add(HashtagSet(account_id=account.id, hashtags=tags))
            session.add(AccountAIConfig(account_id=account.id, ai_enabled=False,
                                        system_prompt=prompt))
            print(f"seeded {platform}:@{username}")

    print("\nDone. Add OAuth tokens next - see SETUP.md.")


if __name__ == "__main__":
    main()
