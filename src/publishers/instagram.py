"""Instagram Reels publishing via the official Content Publishing API.

Flow, per Meta's documentation:
  1. POST /{ig-user-id}/media          -> container id
  2. GET  /{container-id}?fields=status_code  until FINISHED
  3. POST /{ig-user-id}/media_publish  -> published media id

No browser automation, no password login, no scraping - the source video is
handed to Meta as a public URL and Meta fetches it.
"""

from __future__ import annotations

import logging
import time
from urllib.parse import quote

import httpx

from ..config import get_settings
from ..crypto import decrypt
from ..models import Account, Publication, Video
from .base import PublishError, PublishResult, RetryablePublishError

log = logging.getLogger(__name__)

# Meta's guidance: poll roughly once per minute, give up after ~5 minutes.
POLL_INTERVAL_SECONDS = 10
POLL_TIMEOUT_SECONDS = 300


class InstagramPublisher:
    def __init__(self, client: httpx.Client | None = None):
        self.settings = get_settings()
        self._client = client

    def app_credentials(self, account: Account) -> tuple[str, str]:
        """Meta app id/secret for this account, per-account first.

        Accounts can live under different Meta apps, so token refresh must use
        the app the token was actually issued by.
        """
        stored = account.credentials
        app_id = (stored.client_id if stored else None) or self.settings.meta_app_id
        secret = None
        if stored and stored.client_secret_encrypted:
            secret = decrypt(stored.client_secret_encrypted)
        return app_id, (secret or self.settings.meta_app_secret)

    def api_base(self, account: Account) -> str:
        """Meta serves the two login flows from different hosts.

        "Instagram API with Instagram Login" lives on graph.instagram.com and
        needs no Facebook Page; the Facebook Login flow lives on
        graph.facebook.com. Calling the wrong host fails with a confusing
        permissions error rather than a clear 404, so it is chosen per account.
        """
        host = self.settings.instagram_graph_host
        credentials = account.credentials
        if credentials and isinstance(credentials.extra, dict):
            host = credentials.extra.get("graph_host") or host
        return f"https://{host}/{self.settings.meta_graph_version}"

    def _http(self) -> httpx.Client:
        return self._client or httpx.Client(timeout=60.0)

    def public_url_for(self, video: Video, source=None) -> str:
        """Prefer the video's own source link; fall back to the global base URL.

        A remote source already is a public URL, so no separate hosting is
        needed for accounts fed that way.
        """
        if source is not None and getattr(source, "kind", None) is not None:
            if source.kind.value == "remote_url":
                return f"{source.location.rstrip('/')}/{quote(video.filepath)}"
        base = self.settings.public_media_base_url.rstrip("/")
        if not base:
            raise PublishError(
                "No public URL for this video. Either set PUBLIC_MEDIA_BASE_URL "
                "or give the account a remote_url source.")
        return f"{base}/{quote(video.filepath)}"

    def remaining_quota(self, account: Account, token: str) -> int | None:  # noqa: D401
        """Meta exposes the account's own usage; checking beats discovering the
        limit through a failed publish."""
        try:
            with self._http() as client:
                response = client.get(
                    f"{self.api_base(account)}/{account.platform_account_id}"
                    f"/content_publishing_limit",
                    params={"fields": "config,quota_usage", "access_token": token})
            if response.status_code != 200:
                return None
            data = (response.json().get("data") or [{}])[0]
            usage = data.get("quota_usage")
            config = (data.get("config") or {}).get("quota_total",
                                                    self.settings.instagram_daily_limit)
            if usage is None:
                return None
            return max(int(config) - int(usage), 0)
        except Exception:
            return None

    def publish(self, account: Account, video: Video, publication: Publication,
                file_path: str) -> PublishResult:
        credentials = account.credentials
        if not credentials or not credentials.access_token_encrypted:
            raise PublishError(f"no OAuth token stored for @{account.username}")
        token = decrypt(credentials.access_token_encrypted)
        if not account.platform_account_id:
            raise PublishError(
                f"@{account.username} has no platform_account_id (IG user id)")

        remaining = self.remaining_quota(account, token)
        if remaining is not None and remaining <= 0:
            raise RetryablePublishError(
                "Instagram 24h publishing quota exhausted for this account")

        caption = "\n\n".join(part for part in
                             [publication.caption, publication.hashtags] if part)

        base = self.api_base(account)
        with self._http() as client:
            source = video.source if video.source_id else None
            container = client.post(
                f"{base}/{account.platform_account_id}/media",
                data={"media_type": "REELS",
                      "video_url": self.public_url_for(video, source),
                      "caption": caption or "",
                      "access_token": token},
            )
            payload = _json(container)
            if container.status_code >= 400:
                raise _classify(container.status_code, payload, "container creation")
            container_id = payload.get("id")
            if not container_id:
                raise PublishError(f"no container id returned: {payload}")

            status = self._await_container(client, base, container_id, token)
            if status != "FINISHED":
                raise PublishError(
                    f"container {container_id} ended in state {status}")

            published = client.post(
                f"{base}/{account.platform_account_id}/media_publish",
                data={"creation_id": container_id, "access_token": token},
            )
            published_payload = _json(published)
            if published.status_code >= 400:
                raise _classify(published.status_code, published_payload, "publish")

            media_id = published_payload.get("id")
            if not media_id:
                raise PublishError(f"no media id returned: {published_payload}")

            permalink = self._permalink(client, base, media_id, token)
            return PublishResult(platform_post_id=str(media_id), platform_url=permalink)

    def _await_container(self, client: httpx.Client, base: str,
                         container_id: str, token: str) -> str:
        deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
        status = "IN_PROGRESS"
        while time.monotonic() < deadline:
            time.sleep(POLL_INTERVAL_SECONDS)
            response = client.get(f"{base}/{container_id}",
                                  params={"fields": "status_code",
                                          "access_token": token})
            if response.status_code >= 400:
                raise _classify(response.status_code, _json(response), "status poll")
            status = _json(response).get("status_code", "IN_PROGRESS")
            if status in {"FINISHED", "ERROR", "EXPIRED", "PUBLISHED"}:
                return status
        raise RetryablePublishError(
            f"container {container_id} still {status} after {POLL_TIMEOUT_SECONDS}s")

    def _permalink(self, client: httpx.Client, base: str, media_id: str,
                   token: str) -> str | None:
        try:
            response = client.get(f"{base}/{media_id}",
                                  params={"fields": "permalink",
                                          "access_token": token})
            if response.status_code == 200:
                return _json(response).get("permalink")
        except Exception:
            pass
        return None


def _json(response: httpx.Response) -> dict:
    try:
        return response.json()
    except Exception:
        return {"raw": response.text[:500]}


def _classify(status_code: int, payload: dict, stage: str) -> PublishError:
    """Meta's error codes decide retryability. Guessing from the HTTP status
    alone would retry a permanently malformed request forever."""
    error = payload.get("error", {}) if isinstance(payload, dict) else {}
    code = error.get("code")
    message = error.get("message", str(payload)[:300])
    transient = {1, 2, 4, 17, 32, 341, 613}  # unknown, service, throttling
    if status_code >= 500 or code in transient or status_code == 429:
        return RetryablePublishError(f"{stage} failed (transient, code {code}): {message}")
    return PublishError(f"{stage} failed (code {code}): {message}")
