"""YouTube uploads via the official Data API v3 (videos.insert, resumable).

videos.insert is capped at 100 calls/day per project in the Video Uploads quota
bucket, which is the real ceiling on how many channels this can feed - the
scheduler checks it before queueing rather than burning the quota on failures.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from ..config import get_settings
from ..crypto import decrypt, encrypt
from ..models import Account, Publication, Video
from .base import PublishError, PublishResult, RetryablePublishError

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
# 22 = People & Blogs. Safe default for short-form talking/motivational content.
DEFAULT_CATEGORY_ID = "22"
_RETRYABLE_REASONS = {
    "backendError", "internalError", "rateLimitExceeded",
    "userRateLimitExceeded", "quotaExceeded", "uploadLimitExceeded",
}


class YouTubePublisher:
    def __init__(self):
        self.settings = get_settings()

    def oauth_client(self, account: Account) -> tuple[str, str]:
        """The Google OAuth client for this account.

        Per-account first: each Google account is its own Cloud project with its
        own client id, and refreshing a token against the wrong client fails
        with an opaque invalid_client error. The .env values remain as a
        single-project fallback.
        """
        stored = account.credentials
        client_id = (stored.client_id if stored else None) or self.settings.youtube_client_id
        secret = None
        if stored and stored.client_secret_encrypted:
            secret = decrypt(stored.client_secret_encrypted)
        secret = secret or self.settings.youtube_client_secret
        if not client_id or not secret:
            raise PublishError(
                f"{account.account_name} has no OAuth client. Set one with: "
                f"manage.py set-oauth-client {account.id} --client-id ... "
                f"--client-secret ...")
        return client_id, secret

    def _credentials(self, account: Account, session=None) -> Credentials:
        stored = account.credentials
        if not stored or not stored.refresh_token_encrypted:
            raise PublishError(
                f"{account.account_name} has no refresh token - run "
                f"scripts/oauth_youtube.py for this account")

        client_id, client_secret = self.oauth_client(account)
        credentials = Credentials(
            token=decrypt(stored.access_token_encrypted),
            refresh_token=decrypt(stored.refresh_token_encrypted),
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret,
            scopes=(stored.scopes or " ".join(SCOPES)).split(),
        )
        if not credentials.valid or credentials.expired:
            credentials.refresh(Request())
            # Persist the rotated access token so the next run starts valid.
            stored.access_token_encrypted = encrypt(credentials.token)
            if credentials.expiry:
                stored.token_expires_at = credentials.expiry.replace(
                    tzinfo=timezone.utc)
        return credentials

    def publish(self, account: Account, video: Video, publication: Publication,
                file_path: str) -> PublishResult:
        credentials = self._credentials(account)
        youtube = build("youtube", "v3", credentials=credentials,
                        cache_discovery=False)

        tags = [tag.lstrip("#") for tag in (publication.hashtags or "").split()
                if tag.strip()][:15]
        description = "\n\n".join(
            part for part in [publication.description or publication.caption,
                              publication.hashtags] if part)

        body = {
            "snippet": {
                "title": (publication.title or video.filename)[:100],
                "description": description[:5000],
                "tags": tags,
                "categoryId": DEFAULT_CATEGORY_ID,
            },
            "status": {
                "privacyStatus": "public",
                "selfDeclaredMadeForKids": False,
            },
        }

        media = MediaFileUpload(file_path, chunksize=8 * 1024 * 1024,
                                resumable=True, mimetype="video/*")
        try:
            request = youtube.videos().insert(
                part="snippet,status", body=body, media_body=media,
                notifySubscribers=True)
            response = None
            while response is None:
                # Resumable upload: a dropped connection resumes from the last
                # acknowledged chunk instead of restarting a large file.
                status, response = request.next_chunk()
                if status:
                    log.info("upload %s%% for publication %s",
                             int(status.progress() * 100), publication.id)
        except HttpError as exc:
            raise _classify(exc) from exc
        except (TimeoutError, ConnectionError) as exc:
            raise RetryablePublishError(f"network failure during upload: {exc}") from exc

        video_id = response.get("id")
        if not video_id:
            raise PublishError(f"upload returned no video id: {response}")
        return PublishResult(platform_post_id=video_id,
                             platform_url=f"https://www.youtube.com/watch?v={video_id}")


def _classify(exc: HttpError) -> PublishError:
    status = getattr(exc.resp, "status", None)
    reason = ""
    try:
        details = exc.error_details or []
        if details and isinstance(details, list):
            reason = details[0].get("reason", "")
    except Exception:
        pass
    message = str(exc)[:300]
    if status in (500, 502, 503, 504) or reason in _RETRYABLE_REASONS:
        return RetryablePublishError(f"YouTube transient error ({reason}): {message}")
    return PublishError(f"YouTube rejected the upload ({reason}): {message}")
