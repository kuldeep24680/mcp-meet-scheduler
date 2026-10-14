"""One-time Google OAuth (Desktop app flow). Run this yourself, NOT via the MCP host.

Why separate from server.py:
  1. This flow opens a browser and prints to stdout; on STDIO, stdout is the
     MCP protocol channel, so printing there would corrupt JSON-RPC.
  2. The host spawns the server headless; an interactive flow inside a tool
     call would just hang.

"Desktop app" clients need no registered redirect URI, but run_local_server()
still starts a temporary loopback HTTP server on localhost to receive the code.

Usage:
  uv run python auth.py           # browser consent -> writes token.json
  uv run python auth.py --check   # verify token.json with a free/busy call
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta

from dotenv import load_dotenv
from google_auth_oauthlib.flow import InstalledAppFlow

import gcal


def authorize() -> None:
    if not gcal.CREDENTIALS_FILE.exists():
        sys.exit(f"Missing {gcal.CREDENTIALS_FILE.name}. Download the Desktop-app OAuth client JSON from GCP.")
    flow = InstalledAppFlow.from_client_secrets_file(str(gcal.CREDENTIALS_FILE), gcal.SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
    gcal.TOKEN_FILE.write_text(creds.to_json())
    print(f"Saved {gcal.TOKEN_FILE.name}. Scopes granted: {sorted(creds.scopes or [])}")


def check() -> None:
    load_dotenv(gcal.ROOT / ".env")
    cfg = gcal.load_config()
    service = gcal.get_service()
    now = datetime.now(cfg.tz)
    resp = (
        service.freebusy()
        .query(body={"timeMin": now.isoformat(), "timeMax": (now + timedelta(days=1)).isoformat(),
                     "timeZone": cfg.timezone, "items": [{"id": gcal.CALENDAR_ID}]})
        .execute()
    )
    busy = resp["calendars"][gcal.CALENDAR_ID].get("busy", [])
    print(f"OK: token works. {len(busy)} busy block(s) in the next 24h ({cfg.timezone}).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="verify existing token.json")
    args = parser.parse_args()
    try:
        check() if args.check else authorize()
    except gcal.SchedulerError as e:
        sys.exit(f"ERROR: {e}")
