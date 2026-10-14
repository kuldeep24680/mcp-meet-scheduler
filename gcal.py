"""Google Calendar logic. Pure Python: NO MCP code in this file.

server.py is a thin MCP adapter over these functions. Keeping them separate
shows that MCP is just a protocol layer over ordinary business logic.

Errors raised here are SchedulerError (expected, user-facing). server.py turns
them into MCP tool errors (isError: true) so the LLM can read them and adapt.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ---------------------------------------------------------------- config ----

ROOT = Path(__file__).resolve().parent
CREDENTIALS_FILE = ROOT / "credentials.json"  # OAuth client (Desktop app), from GCP console
TOKEN_FILE = ROOT / "token.json"  # user token, written by auth.py

# Least privilege: create/read events + read free/busy. No full-calendar scope, no Gmail.
SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.freebusy",
]

CALENDAR_ID = "primary"
MAX_SLOTS = 8


@dataclass(frozen=True)
class Config:
    timezone: str
    work_start_hour: int
    work_end_hour: int
    slot_step_minutes: int
    work_days: tuple[int, ...] = (0, 1, 2, 3, 4)  # Mon..Fri (datetime.weekday())

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def as_dict(self) -> dict:
        return {
            "timezone": self.timezone,
            "working_hours": f"{self.work_start_hour:02d}:00-{self.work_end_hour:02d}:00",
            "working_days": "Mon-Fri",
            "slot_step_minutes": self.slot_step_minutes,
            "min_duration_minutes": 15,
            "max_duration_minutes": 120,
        }


def load_config() -> Config:
    return Config(
        timezone=os.getenv("TIMEZONE", "Asia/Kolkata"),
        work_start_hour=int(os.getenv("WORK_START_HOUR", "10")),
        work_end_hour=int(os.getenv("WORK_END_HOUR", "18")),
        slot_step_minutes=int(os.getenv("SLOT_STEP_MINUTES", "30")),
    )


class SchedulerError(Exception):
    """Expected failure with a message that is safe to show the LLM/user."""


# ------------------------------------------------------------------ auth ----


def get_service():
    """Build a Calendar client from token.json.

    Never starts a browser flow: the server runs headless under the host, and
    stdout belongs to the MCP protocol. If the token is missing or dead, tell
    the user to run auth.py instead.
    """
    if not TOKEN_FILE.exists():
        raise SchedulerError("Not authorized: token.json missing. Run `uv run python auth.py` once.")
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError as e:
                raise SchedulerError(
                    "Google token expired or revoked (Testing-mode tokens last ~7 days). "
                    "Delete token.json and run `uv run python auth.py`."
                ) from e
            TOKEN_FILE.write_text(creds.to_json())
        else:
            raise SchedulerError("Invalid token.json. Delete it and run `uv run python auth.py`.")
    # static_discovery=True (default) uses the bundled API spec: no network fetch.
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _http_error(e: HttpError, action: str) -> SchedulerError:
    status = e.resp.status
    if status == 401:
        return SchedulerError(f"{action} failed: unauthorized. Delete token.json and run auth.py.")
    if status == 403:
        return SchedulerError(f"{action} failed: permission denied or rate limited (403). Check scopes/quota.")
    if status == 429:
        return SchedulerError(f"{action} failed: rate limited (429). Retry shortly.")
    return SchedulerError(f"{action} failed: Google API error {status}.")


# ----------------------------------------------------------------- slots ----


def _busy_intervals(service, start: datetime, end: datetime, tz: str) -> list[tuple[datetime, datetime]]:
    body = {
        "timeMin": start.isoformat(),
        "timeMax": end.isoformat(),
        "timeZone": tz,
        "items": [{"id": CALENDAR_ID}],
    }
    try:
        resp = service.freebusy().query(body=body).execute()
    except HttpError as e:
        raise _http_error(e, "Free/busy lookup") from e
    cal = resp.get("calendars", {}).get(CALENDAR_ID, {})
    if cal.get("errors"):
        raise SchedulerError(f"Free/busy lookup returned errors: {cal['errors']}")
    return [
        (datetime.fromisoformat(b["start"]), datetime.fromisoformat(b["end"]))
        for b in cal.get("busy", [])
    ]


def _ceil_to_step(dt: datetime, step_min: int) -> datetime:
    dt = dt.replace(second=0, microsecond=0)
    extra = (dt.hour * 60 + dt.minute) % step_min
    return dt if extra == 0 else dt + timedelta(minutes=step_min - extra)


def available_slots(day: str, duration_minutes: int, cfg: Config, service=None, now: datetime | None = None) -> dict:
    """Free slots on `day` (YYYY-MM-DD) within working hours, in cfg.timezone."""
    tz = cfg.tz
    try:
        d = date.fromisoformat(day)
    except ValueError as e:
        raise SchedulerError(f"Invalid date {day!r}; expected YYYY-MM-DD.") from e

    now = now or datetime.now(tz)
    if d < now.date():
        raise SchedulerError(f"{day} is in the past (today is {now.date().isoformat()} in {cfg.timezone}).")

    result = {"date": day, "timezone": cfg.timezone, "duration_minutes": duration_minutes, "slots": [], "note": None}
    if d.weekday() not in cfg.work_days:
        result["note"] = f"{day} is a {d.strftime('%A')}; meetings are only booked Mon-Fri."
        return result

    win_start = datetime.combine(d, dtime(cfg.work_start_hour), tz)
    win_end = datetime.combine(d, dtime(cfg.work_end_hour), tz)
    cursor = max(win_start, _ceil_to_step(now, cfg.slot_step_minutes))
    if cursor >= win_end:
        result["note"] = "Working hours for this day are over. Try another date."
        return result

    service = service or get_service()
    busy = _busy_intervals(service, win_start, win_end, cfg.timezone)

    dur = timedelta(minutes=duration_minutes)
    step = timedelta(minutes=cfg.slot_step_minutes)
    slots = []
    while cursor + dur <= win_end:
        end = cursor + dur
        if not any(b_start < end and cursor < b_end for b_start, b_end in busy):  # overlap test
            slots.append({"start": cursor.isoformat(), "end": end.isoformat()})
        cursor += step

    result["slots"] = slots[:MAX_SLOTS]
    if not slots:
        result["note"] = "No free slots that day. Try another date."
    elif len(slots) > MAX_SLOTS:
        result["note"] = f"Showing first {MAX_SLOTS} of {len(slots)} free slots."
    return result


# --------------------------------------------------------------- booking ----

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def deterministic_event_id(attendee_email: str, start: datetime, duration_minutes: int, title: str) -> str:
    """Idempotency key, used as the Calendar event's own `id`.

    Same logical booking -> same id -> a retried insert gets 409 instead of a
    duplicate event. Google requires base32hex chars (0-9, a-v), length 5-1024.
    """
    key = f"{attendee_email.strip().lower()}|{start.isoformat()}|{duration_minutes}|{title.strip()}"
    digest = hashlib.sha256(key.encode()).digest()
    return base64.b32hexencode(digest).decode().rstrip("=").lower()


def _parse_start(start_iso: str, cfg: Config) -> datetime:
    try:
        start = datetime.fromisoformat(start_iso)
    except ValueError as e:
        raise SchedulerError(f"Invalid start time {start_iso!r}; use ISO-8601, e.g. 2026-10-15T11:00:00+05:30.") from e
    if start.tzinfo is None:  # be explicit: naive times are interpreted in the configured timezone
        start = start.replace(tzinfo=cfg.tz)
    return start.astimezone(cfg.tz)


def _validate_booking(start: datetime, duration_minutes: int, attendee_email: str, cfg: Config, now: datetime) -> None:
    if not EMAIL_RE.match(attendee_email):
        raise SchedulerError(f"Invalid attendee email {attendee_email!r}.")
    if not 15 <= duration_minutes <= 120:
        raise SchedulerError("Duration must be between 15 and 120 minutes.")
    if start <= now:
        raise SchedulerError("Start time is in the past.")
    end = start + timedelta(minutes=duration_minutes)
    if (
        start.weekday() not in cfg.work_days
        or start.time() < dtime(cfg.work_start_hour)
        or end.date() != start.date()
        or end.time() > dtime(cfg.work_end_hour)
    ):
        raise SchedulerError(
            f"Outside working hours ({cfg.as_dict()['working_hours']} Mon-Fri, {cfg.timezone})."
        )


def _summarize(event: dict, status: str) -> dict:
    return {
        "status": status,
        "event_id": event["id"],
        "meet_link": event.get("hangoutLink"),
        "html_link": event.get("htmlLink"),
        "start": event.get("start", {}).get("dateTime"),
        "end": event.get("end", {}).get("dateTime"),
        "attendees": [a.get("email") for a in event.get("attendees", [])],
    }


def _get_event(service, event_id: str) -> dict | None:
    try:
        return service.events().get(calendarId=CALENDAR_ID, eventId=event_id).execute()
    except HttpError as e:
        if e.resp.status in (404, 410):
            return None
        raise _http_error(e, "Event lookup") from e


def _existing_result(event: dict) -> dict:
    if event.get("status") == "cancelled":
        # Deleted event ids stay reserved by Google; we cannot silently reuse them.
        raise SchedulerError(
            "An identical meeting was booked earlier and then deleted; Google keeps that event id reserved. "
            "Change the title or time to book again."
        )
    return _summarize(event, "already_existed")


def create_meeting(
    title: str,
    attendee_name: str,
    attendee_email: str,
    start_iso: str,
    duration_minutes: int,
    description: str | None,
    cfg: Config,
    service=None,
    now: datetime | None = None,
) -> dict:
    now = now or datetime.now(cfg.tz)
    start = _parse_start(start_iso, cfg)
    _validate_booking(start, duration_minutes, attendee_email, cfg, now)
    end = start + timedelta(minutes=duration_minutes)
    event_id = deterministic_event_id(attendee_email, start, duration_minutes, title)

    service = service or get_service()

    # 1) Idempotency fast path: this exact booking already exists -> return it.
    #    (Must run BEFORE the free/busy check, or a retry would see its own event as "busy".)
    existing = _get_event(service, event_id)
    if existing is not None:
        return _existing_result(existing)

    # 2) Re-check availability. Check-then-act, NOT atomic: fine for one user, a known limit.
    if _busy_intervals(service, start, end, cfg.timezone):
        raise SchedulerError("That slot is no longer free. Fetch available slots again.")

    body = {
        "id": event_id,
        "summary": title,
        "description": description or "",
        "start": {"dateTime": start.isoformat(), "timeZone": cfg.timezone},
        "end": {"dateTime": end.isoformat(), "timeZone": cfg.timezone},
        "attendees": [{"email": attendee_email, "displayName": attendee_name}],
        "conferenceData": {
            # requestId only de-duplicates the *Meet conference*, not the event.
            "createRequest": {"requestId": event_id, "conferenceSolutionKey": {"type": "hangoutsMeet"}}
        },
    }

    # 3) Insert. conferenceDataVersion=1 is required or conferenceData is ignored.
    #    sendUpdates="all" makes Google Calendar email the invite (no Gmail API needed).
    try:
        event = (
            service.events()
            .insert(calendarId=CALENDAR_ID, body=body, conferenceDataVersion=1, sendUpdates="all")
            .execute()
        )
    except HttpError as e:
        if e.resp.status == 409:  # lost a race with an identical concurrent/retried insert
            existing = _get_event(service, event_id)
            if existing is not None:
                return _existing_result(existing)
        raise _http_error(e, "Event creation") from e

    # 4) Meet link can be "pending" for a moment; re-fetch once.
    if not event.get("hangoutLink"):
        time.sleep(1.5)
        event = _get_event(service, event_id) or event

    return _summarize(event, "created")
