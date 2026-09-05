"""Publisher contract shared by every platform."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..models import Account, Publication, Video


class PublishError(Exception):
    """Permanent failure. Retrying the same input will not help."""


class RetryablePublishError(PublishError):
    """Transient failure - rate limit, timeout, 5xx."""


@dataclass
class PublishResult:
    platform_post_id: str
    platform_url: str | None = None


class Publisher(Protocol):
    def publish(self, account: Account, video: Video,
                publication: Publication, file_path: str) -> PublishResult: ...


def get_publisher(platform: str):
    if platform == "instagram":
        from .instagram import InstagramPublisher
        return InstagramPublisher()
    if platform == "youtube":
        from .youtube import YouTubePublisher
        return YouTubePublisher()
    raise PublishError(f"unknown platform: {platform}")
