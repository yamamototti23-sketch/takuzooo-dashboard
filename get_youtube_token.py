#!/usr/bin/env python3
"""Run the OAuth installed-app flow once to mint a refresh token.

Reads `client_secret.json` (Desktop or Web client) and writes `secrets.env`
with YT_CLIENT_ID / YT_CLIENT_SECRET / YT_REFRESH_TOKEN.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/yt-analytics.readonly",
    "https://www.googleapis.com/auth/youtube.readonly",
]

ROOT = Path(__file__).resolve().parent
CLIENT_SECRET = ROOT / "client_secret.json"
OUT = ROOT / "secrets.env"


def load_client() -> tuple[str, str]:
    cfg = json.loads(CLIENT_SECRET.read_text(encoding="utf-8"))
    for key in ("installed", "web"):
        if key in cfg:
            c = cfg[key]
            return c["client_id"], c["client_secret"]
    raise SystemExit("client_secret.json: neither 'installed' nor 'web' section found")


def main() -> int:
    if not CLIENT_SECRET.exists():
        raise SystemExit(f"missing {CLIENT_SECRET}")
    client_id, client_secret = load_client()

    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)
    creds = flow.run_local_server(
        port=0,
        prompt="consent",
        access_type="offline",
        open_browser=True,
    )

    if not creds.refresh_token:
        raise SystemExit("no refresh_token returned; re-run with a fresh consent")

    OUT.write_text(
        "YT_CLIENT_ID={cid}\n"
        "YT_CLIENT_SECRET={csec}\n"
        "YT_REFRESH_TOKEN={rt}\n".format(
            cid=client_id, csec=client_secret, rt=creds.refresh_token
        ),
        encoding="utf-8",
    )
    OUT.chmod(0o600)
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
