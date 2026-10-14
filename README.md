# mcp-meet-scheduler

Learning project: a chatbot that books Google Meet calls through an **MCP server**.
You chat, the bot collects name / email / topic, shows free Google Calendar slots,
and books the one you pick (Meet link + emailed invite).

- Design and plan: [`project_context/develop.md`](project_context/develop.md)
- Step-by-step validation: [`project_context/testing.md`](project_context/testing.md)

## Architecture

```
client.py  (HOST: REPL + LLM tool loop + MCP Client)  ← llm.py (OpenAI | Anthropic adapters)
   │  STDIO: JSON-RPC on stdin/stdout, server logs → server.log
server.py  (MCP SERVER: 2 tools · 1 resource · 1 prompt — thin adapter)
   │
gcal.py    (plain Google Calendar logic, no MCP)  ──►  Google Calendar API
auth.py    (one-time OAuth, run by you → token.json)
```

## Quick start

Needs Python 3.11+, [uv](https://docs.astral.sh/uv/) and an OpenAI API key (or Anthropic). Node.js is optional (for MCP Inspector).

```bash
uv sync
cp .env.example .env                 # add OPENAI_API_KEY (or LLM_PROVIDER=anthropic + key)
# put credentials.json (GCP OAuth client, type "Desktop app") in the repo root
uv run python tests/test_offline.py  # no Google/LLM needed
uv run python auth.py                # browser consent → token.json
uv run python auth.py --check
uv run python client.py --trace      # chat; --legacy for the initialize handshake
```

## Files

| File | Role |
|---|---|
| `gcal.py` | Free/busy → slots; booking with a deterministic event id (idempotency); error mapping |
| `server.py` | `MCPServer`: tools `get_available_slots`, `create_meeting`; resource `scheduler://config`; prompt `schedule_meeting` |
| `client.py` | Host: spawns the server, negotiates (`auto` / `legacy`), confirmation gate, `--trace`, `--provider` |
| `llm.py` | LLM adapters: MCP tool schema → OpenAI Responses `function` tools / Claude tools; results back. No MCP code |
| `auth.py` | Desktop OAuth flow (loopback redirect) → `token.json`; `--check` verifies it |
| `tests/test_offline.py` | Fake Google + in-process MCP client; covers both protocol eras |

## Interview concept map

| Concept | Where to look |
|---|---|
| Host vs client vs server | `client.py` `Host` (host) + `mcp.Client` (client) / `server.py` |
| Tools vs resources vs prompts | `server.py` decorators; the host reads the resource, the user triggers the prompt (`/schedule`), the model calls tools |
| Transport (STDIO) | `client.py` `stdio_transport()`; never `print()` in `server.py` |
| Lifecycle, both eras | `--trace`: `server/discover` (auto) vs `initialize` + `notifications/initialized` (`--legacy`) |
| Schemas drive tool choice | type hints + `Field(description=...)` + docstrings → `inputSchema` → `llm.py` adapters (same schema, two LLMs) |
| Auth / security | `auth.py`; token stays in the server process; `needs_confirmation()` gate; annotations are hints |
| Errors | `ToolError` → `isError: true` result the LLM sees; unexpected exceptions are masked by the SDK |
| Idempotency | `gcal.deterministic_event_id()` + `events.get` before insert + 409 handling |
