"""FFprobe metadata extraction and per-platform compatibility rules."""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class VideoInfo:
    duration: float | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    codec: str | None = None
    file_size: int | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.duration is not None


def probe(path: str | Path) -> VideoInfo:
    path = Path(path)
    if not path.exists():
        return VideoInfo(error="file not found")
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True, timeout=120, check=False,
        )
    except FileNotFoundError:
        return VideoInfo(error="ffprobe not installed")
    except subprocess.TimeoutExpired:
        return VideoInfo(error="ffprobe timed out")

    if result.returncode != 0:
        return VideoInfo(error=f"ffprobe failed: {result.stderr.strip()[:300]}")

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return VideoInfo(error=f"unparseable ffprobe output: {exc}")

    video_stream = next(
        (s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if video_stream is None:
        return VideoInfo(error="no video stream")

    fps = None
    rate = video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate")
    if rate and "/" in rate:
        num, _, den = rate.partition("/")
        try:
            fps = float(num) / float(den) if float(den) else None
        except (ValueError, ZeroDivisionError):
            fps = None

    fmt = data.get("format", {})
    duration = fmt.get("duration") or video_stream.get("duration")
    return VideoInfo(
        duration=float(duration) if duration else None,
        width=video_stream.get("width"),
        height=video_stream.get("height"),
        fps=round(fps, 3) if fps else None,
        codec=video_stream.get("codec_name"),
        file_size=int(fmt["size"]) if fmt.get("size") else path.stat().st_size,
    )


# Reels tab eligibility: 9:16, 3-90s, H.264/HEVC.
INSTAGRAM_RULES = {"min_duration": 3.0, "max_duration": 90.0,
                   "codecs": {"h264", "hevc"}, "max_size": 1_000_000_000}
YOUTUBE_RULES = {"min_duration": 1.0, "max_duration": 12 * 3600,
                 "codecs": None, "max_size": 256 * 1024**3}


def check_compatibility(info: VideoInfo, platform: str) -> tuple[bool, str | None]:
    """Cheap pre-flight so an incompatible file fails here rather than after a
    multi-minute upload that the platform then rejects."""
    rules = INSTAGRAM_RULES if platform == "instagram" else YOUTUBE_RULES
    if not info.ok:
        return False, info.error or "no metadata"
    if info.duration is not None:
        if info.duration < rules["min_duration"]:
            return False, f"too short ({info.duration:.1f}s)"
        if info.duration > rules["max_duration"]:
            return False, f"too long ({info.duration:.1f}s)"
    if rules["codecs"] and info.codec and info.codec.lower() not in rules["codecs"]:
        return False, f"codec {info.codec} not accepted by {platform}"
    if info.file_size and info.file_size > rules["max_size"]:
        return False, f"file too large ({info.file_size} bytes)"
    return True, None
