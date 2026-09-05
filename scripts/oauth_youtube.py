"""One-time OAuth for a YouTube channel.

Run once per channel. Each channel is a separate account row with its own
refresh token, which is what makes multiple channels independent.

    python -m scripts.oauth_youtube --account-id 4
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: E402

from src.config import get_settings  # noqa: E402
from src.crypto import decrypt, encrypt  # noqa: E402
from src.db import session_scope  # noqa: E402
from src.models import Account, AccountCredential  # noqa: E402

SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.readonly"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--account-id", type=int, required=True)
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--client-id", help="overrides the stored/.env client")
    parser.add_argument("--client-secret")
    args = parser.parse_args()

    settings = get_settings()

    # Resolve the OAuth client for THIS account: each Google account is its own
    # Cloud project, so authorising against the wrong client silently produces a
    # refresh token the publisher cannot later use.
    with session_scope() as session:
        account = session.get(Account, args.account_id)
        if not account:
            raise SystemExit(f"no account #{args.account_id}")
        stored = account.credentials
        client_id = (args.client_id or (stored.client_id if stored else None)
                     or settings.youtube_client_id)
        client_secret = args.client_secret
        if not client_secret and stored and stored.client_secret_encrypted:
            client_secret = decrypt(stored.client_secret_encrypted)
        client_secret = client_secret or settings.youtube_client_secret
        account_label = account.account_name

    if not client_id or not client_secret:
        raise SystemExit(
            f"No OAuth client for account #{args.account_id}. Provide "
            f"--client-id/--client-secret, or store them first with:\n"
            f"  python -m src.manage set-oauth-client {args.account_id} "
            f"--client-id ... --client-secret ...")
    print(f"Authorising {account_label} with client "
          f"{client_id.split('-')[0]}...")

    flow = InstalledAppFlow.from_client_config(
        {"installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }},
        scopes=SCOPES,
    )
    # access_type=offline + prompt=consent is what actually returns a refresh
    # token; without it a re-authorised account silently gets none.
    credentials = flow.run_local_server(port=args.port, access_type="offline",
                                        prompt="consent")

    with session_scope() as session:
        account = session.get(Account, args.account_id)
        if not account:
            raise SystemExit(f"no account #{args.account_id}")
        stored = account.credentials or AccountCredential(account_id=account.id)
        # Persist the client that issued this token, so refresh always matches.
        stored.client_id = client_id
        stored.client_secret_encrypted = encrypt(client_secret)
        stored.access_token_encrypted = encrypt(credentials.token)
        stored.refresh_token_encrypted = encrypt(credentials.refresh_token)
        stored.scopes = " ".join(SCOPES)
        stored.token_expires_at = credentials.expiry
        session.add(stored)

        try:
            from googleapiclient.discovery import build
            youtube = build("youtube", "v3", credentials=credentials,
                            cache_discovery=False)
            response = youtube.channels().list(part="id,snippet", mine=True).execute()
            items = response.get("items") or []
            if items:
                account.platform_account_id = items[0]["id"]
                print(f"linked channel: {items[0]['snippet']['title']} "
                      f"({items[0]['id']})")
        except Exception as exc:
            print(f"could not read channel id automatically: {exc}")

    print("✅ credentials stored (encrypted). Never share this database.")


if __name__ == "__main__":
    main()
