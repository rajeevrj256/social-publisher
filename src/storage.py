"""Temporary public hosting for videos that a platform fetches by URL.

YouTube accepts the bytes directly, so it never comes here. Instagram's
Content Publishing API does the opposite: it is handed a URL and Meta's own
servers download the file, which means the bytes must be reachable from the
public internet for the minute or two a publish takes.

The object is uploaded before the publish and deleted after it, so the store
holds one video at a time rather than a second copy of the library. Any
S3-compatible endpoint works (Cloudflare R2, Backblaze B2, S3, MinIO) - the
differences between them are entirely in the endpoint and the public base URL.
"""

from __future__ import annotations

import logging
import mimetypes
import uuid
from pathlib import Path

from .config import get_settings

log = logging.getLogger(__name__)


class StorageError(RuntimeError):
    """Upload or delete failed. Callers treat this as retryable."""


def is_configured() -> bool:
    """Whether a bucket is set up. When false the caller falls back to
    PUBLIC_MEDIA_BASE_URL, so existing deployments keep working untouched."""
    settings = get_settings()
    return bool(settings.media_bucket and settings.media_s3_endpoint
                and settings.media_s3_access_key
                and settings.media_s3_secret_key)


def _client():
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise StorageError(
            "boto3 is required for media uploads: pip install boto3") from exc

    settings = get_settings()
    options = {"signature_version": "s3v4"}
    # boto3 >= 1.36 sends CRC32 checksum headers by default, which R2 and B2
    # reject. Older releases have neither the problem nor these knobs, and
    # passing an unknown one to Config is a TypeError - so they are added only
    # when the installed botocore actually understands them.
    for name in ("request_checksum_calculation", "response_checksum_validation"):
        try:
            Config(**{name: "when_required"})
        except TypeError:
            continue
        options[name] = "when_required"

    return boto3.client(
        "s3",
        endpoint_url=settings.media_s3_endpoint,
        aws_access_key_id=settings.media_s3_access_key,
        aws_secret_access_key=settings.media_s3_secret_key,
        region_name=settings.media_s3_region or "auto",
        config=Config(**options),
    )


def upload(file_path: str, filename: str) -> tuple[str, str]:
    """Put the file in the bucket and return (public_url, object_key).

    The key is prefixed with a random id so two accounts publishing the same
    video at the same time cannot delete each other's object mid-fetch.
    """
    settings = get_settings()
    suffix = Path(filename).suffix or ".mp4"
    key = f"{settings.media_key_prefix.strip('/')}/{uuid.uuid4().hex}{suffix}".lstrip("/")
    content_type = mimetypes.guess_type(filename)[0] or "video/mp4"

    try:
        with open(file_path, "rb") as handle:
            _client().put_object(Bucket=settings.media_bucket, Key=key,
                                 Body=handle, ContentType=content_type)
    except Exception as exc:  # noqa: BLE001 - boto raises many shapes
        raise StorageError(f"upload to {settings.media_bucket} failed: {exc}") from exc

    base = settings.media_public_base_url.rstrip("/")
    if not base:
        raise StorageError(
            "MEDIA_PUBLIC_BASE_URL is not set - the bucket's public hostname "
            "is what Meta fetches from, and cannot be guessed from the endpoint")
    log.info("uploaded %s to %s", filename, key)
    return f"{base}/{key}", key


def delete(key: str) -> None:
    """Best effort cleanup. A leftover object costs pennies; a failed publish
    because cleanup raised would cost a video, so this never propagates."""
    try:
        _client().delete_object(Bucket=get_settings().media_bucket, Key=key)
        log.info("removed %s from bucket", key)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not delete %s from bucket: %s", key, exc)
