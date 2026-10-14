"""MCP server: a thin adapter that exposes gcal.py over MCP.

Primitives (who controls each one):
  - Tools     (model-controlled)       get_available_slots, create_meeting
  - Resource  (application-controlled) scheduler://config
  - Prompt    (user-controlled)        schedule_meeting

Transport: STDIO. The host spawns `python server.py` and speaks JSON-RPC over
stdin/stdout. RULE: never print() to stdout here; it would corrupt the
protocol stream. Logs go to stderr (the SDK's logger already does).
"""

from __future__ import annotations

import json
from typing import Annotated

import os

from dotenv import dotenv_values
from pydantic import BaseModel, Field

import gcal
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

# The host passes only a minimal environment (PATH, HOME, ...) to the subprocess,
# so read the scheduler settings from .env here. Least privilege: take ONLY the
# keys this server needs; the Anthropic API key belongs to the host, not the server.
_CONFIG_KEYS = ("TIMEZONE", "WORK_START_HOUR", "WORK_END_HOUR", "SLOT_STEP_MINUTES")
for _k, _v in dotenv_values(gcal.ROOT / ".env").items():
    if _k in _CONFIG_KEYS and _v and _k not in os.environ:
        os.environ[_k] = _v
CFG = gcal.load_config()

mcp = MCPServer(
    name="meet-scheduler",
    version="0.1.0",
    instructions=(
        "Books Google Meet calls on the owner's Google Calendar. "
        "Always call get_available_slots first and only book a slot it returned."
    ),
)


# ------------------------------------------------- structured output models --
# Return types become each tool's `outputSchema`; results carry `structuredContent`.


class Slot(BaseModel):
    start: str = Field(description="ISO-8601 start with UTC offset")
    end: str = Field(description="ISO-8601 end with UTC offset")


class SlotsResult(BaseModel):
    date: str
    timezone: str
    duration_minutes: int
    slots: list[Slot]
    note: str | None = None


class MeetingResult(BaseModel):
    status: str = Field(description="'created' or 'already_existed' (idempotent retry)")
    event_id: str
    meet_link: str | None
    html_link: str | None
    start: str | None
    end: str | None
    attendees: list[str]


# ----------------------------------------------------------------- tools ----
# The docstring becomes the tool `description`; the type hints + Field()
# metadata become the `inputSchema`. Both are what the LLM reads to decide
# WHEN to call a tool and HOW to fill its arguments.


@mcp.tool(
    annotations=ToolAnnotations(title="Find free slots", read_only_hint=True, open_world_hint=True),
)
def get_available_slots(
    date: Annotated[str, Field(description="Day to check, format YYYY-MM-DD", pattern=r"^\d{4}-\d{2}-\d{2}$")],
    duration_minutes: Annotated[int, Field(description="Meeting length in minutes", ge=15, le=120)] = 30,
) -> SlotsResult:
    """List free meeting slots on the calendar owner's Google Calendar for one day.

    Only returns slots inside working hours that do not overlap existing events.
    Call this before create_meeting, and offer the user only slots returned here.
    """
    try:
        return SlotsResult(**gcal.available_slots(date, duration_minutes, CFG))
    except gcal.SchedulerError as e:
        # ToolError -> result with isError=true; the message is shown to the LLM.
        raise ToolError(str(e)) from e


@mcp.tool(
    annotations=ToolAnnotations(
        title="Book Google Meet",
        read_only_hint=False,
        destructive_hint=False,  # adds an event; never modifies/deletes existing ones
        idempotent_hint=True,  # same args -> same event (deterministic event id)
        open_world_hint=True,  # emails an external attendee
    ),
)
def create_meeting(
    title: Annotated[str, Field(description="Meeting title, e.g. 'Intro call: <topic>'", min_length=1, max_length=200)],
    attendee_name: Annotated[str, Field(description="Attendee's full name", min_length=1, max_length=100)],
    attendee_email: Annotated[str, Field(description="Attendee's email address; the invite is sent here")],
    start_iso: Annotated[
        str, Field(description="Slot start exactly as returned by get_available_slots (ISO-8601 with offset)")
    ],
    duration_minutes: Annotated[int, Field(description="Meeting length in minutes", ge=15, le=120)] = 30,
    description: Annotated[str | None, Field(description="Optional agenda / topic details")] = None,
) -> MeetingResult:
    """Book a Google Calendar event with a Google Meet link and email the invite to the attendee.

    Side effect: creates a real calendar event and sends an email. Only call after
    the user has chosen a slot from get_available_slots and confirmed name, email and topic.
    Safe to retry: an identical request returns the existing event (status 'already_existed').
    """
    try:
        return MeetingResult(
            **gcal.create_meeting(
                title, attendee_name, attendee_email, start_iso, duration_minutes, description, CFG
            )
        )
    except gcal.SchedulerError as e:
        raise ToolError(str(e)) from e


# -------------------------------------------------------------- resource ----


@mcp.resource(
    "scheduler://config",
    name="scheduler-config",
    description="Timezone, working hours and slot rules used by this scheduler.",
    mime_type="application/json",
)
def scheduler_config() -> str:
    return json.dumps(CFG.as_dict())


# ---------------------------------------------------------------- prompt ----


@mcp.prompt(title="Schedule a meeting")
def schedule_meeting(topic: str) -> str:
    """Start a scheduling conversation about a given topic."""
    return (
        f"I'd like to schedule a Google Meet call about: {topic}. "
        "Please collect my name and email, show me available slots, and book the one I pick."
    )


if __name__ == "__main__":
    mcp.run("stdio")
