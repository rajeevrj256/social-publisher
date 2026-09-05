"""Pull a shared Google Drive folder into a local source directory.

Drive share links are HTML pages, not media URLs, so they cannot be used as a
`remote_url` source. YouTube's resumable upload reads from disk anyway, so the
files have to be local either way. This downloads them once into the folder a
`local` source points at, after which the normal scanner takes over.

    python -m scripts.fetch_gdrive --url "https://drive.google.com/drive/folders/..." \
        --source yt_link_1
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from src.config import get_settings  # noqa: E402
from src.db import session_scope  # noqa: E402
from src.models import Source, SourceKind  # noqa: E402

VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}


def resolve_target(source_name: str | None, folder: str | None) -> Path:
    settings = get_settings()
    root = Path(settings.video_root)
    if folder:
        return root / folder
    if not source_name:
        raise SystemExit("give --source NAME or --folder PATH")
    with session_scope() as session:
        source = session.scalar(select(Source).where(Source.name == source_name))
        if not source:
            raise SystemExit(f"no source named {source_name!r}")
        if source.kind != SourceKind.local:
            raise SystemExit(
                f"source {source_name!r} is {source.kind.value}; only a local "
                f"source has a folder to download into")
        return root / source.location


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True, help="Drive folder share link")
    parser.add_argument("--source", help="local source name to fill")
    parser.add_argument("--folder", help="explicit folder under VIDEO_ROOT")
    args = parser.parse_args()

    target = resolve_target(args.source, args.folder)
    target.mkdir(parents=True, exist_ok=True)
    print(f"downloading into {target}")

    # Invoke through this interpreter rather than a bare `gdown`: inside a venv
    # or a container the console script is often not on PATH even though the
    # package is installed.
    def count_videos() -> int:
        return sum(1 for p in target.rglob("*")
                   if p.suffix.lower() in VIDEO_SUFFIXES)

    before = count_videos()
    command = [sys.executable, "-m", "gdown", "--folder", args.url,
               "-O", str(target)]
    try:
        result = subprocess.run(command, capture_output=True, text=True)
        code, stderr = result.returncode, (result.stderr or "")
    except FileNotFoundError:
        raise SystemExit(
            f"gdown is not installed. Install it with:\n"
            f"  {sys.executable} -m pip install gdown")

    # Judge by what landed on disk, not by the exit code: gdown returns
    # non-zero when any single file in a large folder is skipped, even though
    # the other few hundred downloaded perfectly well.
    downloaded = count_videos() - before
    if downloaded <= 0 and count_videos() == 0:
        print(stderr.strip()[:600], file=sys.stderr)
        raise SystemExit(
            "\nNothing downloaded. The folder must be shared as 'Anyone with "
            "the link'. For a private folder use rclone with a Drive remote.")
    if code != 0:
        print(f"note: gdown reported errors on some files (exit {code}); "
              f"{downloaded} new file(s) still downloaded.")

    videos = [p for p in target.rglob("*") if p.suffix.lower() in VIDEO_SUFFIXES]
    print(f"\n{len(videos)} video file(s) now in {target}")
    for path in videos[:10]:
        print(f"  {path.relative_to(target)}")
    if len(videos) > 10:
        print(f"  ... and {len(videos) - 10} more")
    print("\nNext:  python -m src.manage scan")


if __name__ == "__main__":
    main()
