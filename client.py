"""MCP HOST: terminal chat (REPL) + LLM tool-calling loop + one MCP client.

Roles in this one process:
  - Host:   owns the UI, the LLM, the conversation history and the
            human-in-the-loop confirmation gate.
  - Client: the MCP protocol connection to ONE server (mcp.Client).
The LLM never talks to the server; the host relays every tool call.
LLM-specific code lives in llm.py (OpenAI by default; LLM_PROVIDER=anthropic also works).

Usage:
  uv run python client.py            # mode="auto": server/discover (2026-07-28 era)
  uv run python client.py --legacy   # mode="legacy": initialize handshake (2025-11-25 era)
  uv run python client.py --trace    # print every JSON-RPC message on the wire

Chat commands: /tools  /schedule <topic>  /quit
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import anyio
from dotenv import load_dotenv

import llm
from llm import ToolCall, ToolResult

from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent
SERVER_LOG = ROOT / "server.log"
MAX_TOOL_ROUNDS = 8  # safety cap on LLM <-> tool iterations per user turn

DIM, CYAN, YELLOW, GREEN, RED, RESET = "\033[2m", "\033[36m", "\033[33m", "\033[32m", "\033[31m", "\033[0m"


# ------------------------------------------------------------- tracing ------
# A Transport is just an async context manager yielding (read_stream, write_stream).
# We wrap stdio_client's streams to print every JSON-RPC message in both directions.


def _fmt(session_message, limit: int = 400) -> str:
    msg = session_message.message.model_dump(by_alias=True, exclude_none=True, mode="json")
    text = json.dumps(msg, separators=(",", ":"))
    return text if len(text) <= limit else text[:limit] + f"...(+{len(text) - limit} chars)"


class _TracedRead:
    def __init__(self, inner):
        self._inner = inner

    async def receive(self):
        item = await self._inner.receive()
        if not isinstance(item, Exception):
            print(f"{DIM}  ← {_fmt(item)}{RESET}")
        return item

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return await self.receive()
        except (anyio.EndOfStream, anyio.ClosedResourceError):
            raise StopAsyncIteration

    async def aclose(self):
        await self._inner.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()

    def __getattr__(self, name):  # e.g. last_context
        return getattr(self._inner, name)


class _TracedWrite:
    def __init__(self, inner):
        self._inner = inner

    async def send(self, item, /):
        print(f"{DIM}  → {_fmt(item)}{RESET}")
        await self._inner.send(item)

    async def aclose(self):
        await self._inner.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()

    def __getattr__(self, name):
        return getattr(self._inner, name)


@asynccontextmanager
async def stdio_transport(trace: bool):
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "server.py")], cwd=str(ROOT))
    # Server stderr (its logs) goes to server.log so it doesn't mix with the chat.
    with open(SERVER_LOG, "a") as errlog:
        async with stdio_client(params, errlog=errlog) as (read, write):
            yield (_TracedRead(read), _TracedWrite(write)) if trace else (read, write)




# --------------------------------------------------------- MCP <-> LLM glue --


def result_text(result) -> str:
    """MCP CallToolResult -> plain text for the LLM (structured output preferred)."""
    if result.structured_content is not None:
        return json.dumps(result.structured_content)
    return "\n".join(getattr(c, "text", f"[{c.type} content]") for c in result.content)


def needs_confirmation(tool) -> bool:
    """Gate every tool that isn't declared read-only.

    Annotations are HINTS and must be treated as untrusted unless you trust the
    server. We trust our own server here; with a third-party server, gate by an
    allowlist you control instead.
    """
    return not (tool.annotations and tool.annotations.read_only_hint)


def build_system_prompt(config: dict, server_instructions: str | None) -> str:
    tz = ZoneInfo(config["timezone"])
    now = datetime.now(tz)
    return f"""You are a friendly scheduling assistant that books Google Meet calls.

Current date/time: {now.strftime('%A %Y-%m-%d %H:%M')} ({config['timezone']}).
Scheduler config (from MCP resource scheduler://config): {json.dumps(config)}
Server instructions: {server_instructions or '-'}

Process:
1. Collect the attendee's full name, email address and meeting topic. Ask for anything missing, one short question at a time.
2. Ask which day they prefer (resolve words like "tomorrow" using the current date above), then call get_available_slots.
3. Show the returned slots as a short numbered list in local time (e.g. "1. 10:30-11:00"). Never invent slots.
4. When the user picks one, call create_meeting with start_iso copied exactly from the chosen slot and a title like "<topic> - <name>".
5. On success, share the Google Meet link and say the invite was emailed. If a tool returns an error, explain it briefly and recover (e.g. fetch slots again).
Keep replies short."""


# -------------------------------------------------------------- the host ----


class Host:
    def __init__(self, client: Client, llm_cls, model: str):
        self.client = client
        self.llm_cls, self.model = llm_cls, model
        self.llm = None  # created in setup(), once tools + system prompt are known
        self.tools = []

    async def setup(self) -> None:
        c = self.client
        print(f"{GREEN}Connected:{RESET} protocol {c.protocol_version}, server {c.server_info.name if c.server_info else '?'}")

        # tools/list: discover what the server can do; the adapter converts the schemas for the LLM.
        self.tools = (await c.list_tools()).tools
        print(f"{GREEN}Tools:{RESET} {', '.join(t.name for t in self.tools)}")

        # resources/read: application-controlled context, injected by the HOST (model never calls it).
        res = await c.read_resource("scheduler://config")
        config = json.loads(res.contents[0].text)
        system = build_system_prompt(config, c.instructions)

        prompts = (await c.list_prompts()).prompts
        print(f"{GREEN}Prompts:{RESET} {', '.join(p.name for p in prompts)} (user-invoked via /schedule <topic>)")

        # The LLM adapter owns ALL conversation state (the server is stateless).
        self.llm = self.llm_cls(self.model, system, self.tools)

    async def run_prompt(self, topic: str) -> str:
        """prompts/get: a user-chosen template the server provides; we use its text as the user's message."""
        result = await self.client.get_prompt("schedule_meeting", {"topic": topic})
        return "\n".join(m.content.text for m in result.messages if getattr(m.content, "text", None))

    async def call_tool(self, call: ToolCall) -> ToolResult:
        tool = next((t for t in self.tools if t.name == call.name), None)
        print(f"{CYAN}[tool] {call.name}({json.dumps(call.args)}){RESET}")

        if tool is not None and needs_confirmation(tool):
            answer = (await anyio.to_thread.run_sync(input, f"{YELLOW}Allow '{call.name}'? [y/N] {RESET}")).strip().lower()
            if answer not in ("y", "yes"):
                return ToolResult(call, "The user declined this action. Ask what they would like to change.", True)

        try:
            # tools/call: the host relays the LLM's request to the MCP server.
            result = await self.client.call_tool(call.name, call.args)
        except Exception as e:  # transport/protocol failure (not a tool error)
            return ToolResult(call, f"Tool call failed at protocol level: {e}", True)

        out = ToolResult(call, result_text(result), bool(result.is_error))
        print(f"{RED if out.is_error else DIM}[result] {out.text[:300]}{RESET}")
        return out

    async def turn(self, user_text: str) -> None:
        mark = self.llm.checkpoint()
        self.llm.add_user(user_text)
        for _ in range(MAX_TOOL_ROUNDS):
            try:
                text, calls = await anyio.to_thread.run_sync(self.llm.complete)
            except Exception as e:  # API/network error: roll back this turn so history stays valid
                self.llm.restore(mark)
                print(f"{RED}LLM error: {e}{RESET}")
                return

            if text.strip():
                print(f"{GREEN}Bot:{RESET} {text.strip()}")
            if not calls:
                return

            # Run every requested tool call, then send all results back together.
            results = [await self.call_tool(c) for c in calls]
            self.llm.add_tool_results(results)

        print(f"{RED}Stopped after {MAX_TOOL_ROUNDS} tool rounds.{RESET}")

    async def repl(self) -> None:
        print(f"\nType a message. Commands: /tools  /schedule <topic>  /quit\n")
        while True:
            try:
                line = (await anyio.to_thread.run_sync(input, "You: ")).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if not line:
                continue
            if line in ("/quit", "/exit", "exit", "quit"):
                return
            if line == "/tools":  # exactly what the LLM receives
                print(json.dumps(self.llm.tools, indent=2))
                continue
            if line.startswith("/schedule"):
                topic = line[len("/schedule"):].strip() or "a quick call"
                line = await self.run_prompt(topic)
                print(f"{DIM}(prompt expanded) {line}{RESET}")
            await self.turn(line)


async def main() -> None:
    parser = argparse.ArgumentParser(description="MCP Google Meet scheduler chatbot (host)")
    parser.add_argument("--legacy", action="store_true", help="use the initialize handshake (pre-2026 era)")
    parser.add_argument("--trace", action="store_true", help="print JSON-RPC messages")
    parser.add_argument("--provider", default=None, help="openai | anthropic (default: LLM_PROVIDER or openai)")
    parser.add_argument("--model", default=None, help="model id (default: OPENAI_MODEL / ANTHROPIC_MODEL)")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    provider = args.provider or os.getenv("LLM_PROVIDER", "openai")
    llm_cls, model = llm.resolve(provider, args.model)  # fails fast if the API key is missing
    mode = "legacy" if args.legacy else "auto"
    print(f"{DIM}Spawning server over STDIO, mode={mode}, llm={provider}:{model}, server logs -> {SERVER_LOG.name}{RESET}")

    # Entering the Client performs version negotiation:
    #   auto   -> server/discover (falls back to initialize for old servers)
    #   legacy -> initialize + notifications/initialized
    async with Client(stdio_transport(args.trace), mode=mode) as client:
        host = Host(client, llm_cls, model)
        await host.setup()
        await host.repl()


if __name__ == "__main__":
    anyio.run(main)
