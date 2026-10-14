"""Offline tests: no Google account, no API key, no network.

A fake Calendar service replaces Google, and the MCP Client connects to the
server IN-PROCESS (no subprocess). Run: uv run python tests/test_offline.py

Proves: tool schemas, structured output, isError handling, idempotency,
and that both protocol eras (auto / legacy) work.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import anyio
import httplib2
from googleapiclient.errors import HttpError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os  # noqa: E402

# Pin config so the test is independent of your .env (load_dotenv won't override these).
os.environ.update(TIMEZONE="Asia/Kolkata", WORK_START_HOUR="10", WORK_END_HOUR="18", SLOT_STEP_MINUTES="30")

import gcal  # noqa: E402
import server  # noqa: E402
from mcp import Client  # noqa: E402


# ------------------------------------------------------------ fake Google ---


class _Call:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


def _http_error(status: int) -> HttpError:
    return HttpError(httplib2.Response({"status": status}), b"{}")


class FakeCalendar:
    """Implements just the calls gcal.py makes: freebusy.query, events.get, events.insert."""

    def __init__(self):
        self.store: dict[str, dict] = {}
        self.busy: list[dict] = []
        self.insert_calls = 0

    def freebusy(self):
        return self

    def query(self, body):
        t0, t1 = datetime.fromisoformat(body["timeMin"]), datetime.fromisoformat(body["timeMax"])
        busy = [b for b in self.busy if datetime.fromisoformat(b["start"]) < t1 and t0 < datetime.fromisoformat(b["end"])]
        busy += [
            {"start": e["start"]["dateTime"], "end": e["end"]["dateTime"]}
            for e in self.store.values()
            if e["status"] == "confirmed"
            and datetime.fromisoformat(e["start"]["dateTime"]) < t1
            and t0 < datetime.fromisoformat(e["end"]["dateTime"])
        ]
        return _Call(lambda: {"calendars": {"primary": {"busy": busy}}})

    def events(self):
        return self

    def get(self, calendarId, eventId):
        def run():
            if eventId not in self.store:
                raise _http_error(404)
            return self.store[eventId]
        return _Call(run)

    def insert(self, calendarId, body, conferenceDataVersion, sendUpdates):
        assert conferenceDataVersion == 1 and sendUpdates == "all"

        def run():
            self.insert_calls += 1
            if body["id"] in self.store:
                raise _http_error(409)
            ev = {**body, "status": "confirmed", "hangoutLink": "https://meet.google.com/abc-defg-hij",
                  "htmlLink": f"https://calendar.google.com/event?eid={body['id']}"}
            self.store[body["id"]] = ev
            return ev
        return _Call(run)


# ------------------------------------------------------------------ tests ---


def next_weekday(cfg: gcal.Config) -> str:
    d = datetime.now(cfg.tz).date() + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.isoformat()


async def run(mode: str) -> None:
    fake = FakeCalendar()
    gcal.get_service = lambda: fake  # monkeypatch Google out
    cfg = server.CFG
    day = next_weekday(cfg)
    fake.busy = [{"start": f"{day}T11:00:00+05:30", "end": f"{day}T12:00:00+05:30"}]

    async with Client(server.mcp, mode=mode) as client:
        expected = "2025-11-25" if mode == "legacy" else "2026-07-28"
        assert client.protocol_version == expected, client.protocol_version

        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert set(tools) == {"get_available_slots", "create_meeting"}
        assert tools["get_available_slots"].annotations.read_only_hint is True
        assert tools["create_meeting"].annotations.idempotent_hint is True
        assert "date" in tools["get_available_slots"].input_schema["required"]

        # 1. slots: busy 11:00-12:00 must be excluded
        r = await client.call_tool("get_available_slots", {"date": day, "duration_minutes": 30})
        assert not r.is_error, r
        starts = [s["start"][11:16] for s in r.structured_content["slots"]]
        assert "10:00" in starts and "11:00" not in starts and "11:30" not in starts and "12:00" in starts, starts

        # 2. book first slot
        args = {"title": "Intro - Test", "attendee_name": "Test User", "attendee_email": "test@example.com",
                "start_iso": r.structured_content["slots"][0]["start"], "duration_minutes": 30}
        r1 = await client.call_tool("create_meeting", args)
        assert not r1.is_error and r1.structured_content["status"] == "created", r1
        assert r1.structured_content["meet_link"].startswith("https://meet.google.com/")

        # 3. idempotent retry: same args -> same event, no second insert
        r2 = await client.call_tool("create_meeting", args)
        assert r2.structured_content["status"] == "already_existed", r2
        assert r2.structured_content["event_id"] == r1.structured_content["event_id"]
        assert fake.insert_calls == 1 and len(fake.store) == 1

        # 4. tool errors come back as isError results (not exceptions)
        bad = [
            ("create_meeting", {**args, "attendee_email": "not-an-email"}, "Invalid attendee email"),
            ("create_meeting", {**args, "start_iso": "2020-01-01T10:00:00+05:30"}, "past"),
            ("create_meeting", {**args, "title": "Other", "start_iso": f"{day}T11:00:00+05:30"}, "no longer free"),
            ("create_meeting", {**args, "start_iso": f"{day}T20:00:00+05:30"}, "Outside working hours"),
            ("get_available_slots", {"date": "2020-01-01"}, "in the past"),
            ("get_available_slots", {"date": "tomorrow"}, "pattern"),
        ]
        for name, a, expect in bad:
            r = await client.call_tool(name, a)
            text = r.content[0].text
            assert r.is_error and expect in text, (name, a, text)

        # 5. deleted event id stays reserved -> explicit error, not fake success
        fake.store[r1.structured_content["event_id"]]["status"] = "cancelled"
        r3 = await client.call_tool("create_meeting", args)
        assert r3.is_error and "deleted" in r3.content[0].text, r3

        # 6. resource + prompt
        res = await client.read_resource("scheduler://config")
        assert '"timezone"' in res.contents[0].text
        p = await client.get_prompt("schedule_meeting", {"topic": "pricing"})
        assert "pricing" in p.messages[0].content.text

    print(f"PASS  mode={mode:<6} protocol={expected}")


async def main() -> None:
    for mode in ("auto", "legacy"):
        await run(mode)
    print("All offline tests passed.")


if __name__ == "__main__":
    anyio.run(main)
