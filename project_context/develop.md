# develop.md — Design & Implementation Plan

> **Status:** APPROVED 2026-10-14 (defaults in §10 accepted). Implemented — validate with `testing.md`.
> **Last updated:** 2026-10-14
> **Goal:** Learn MCP internals end-to-end (not a production app). Minimal and legible.

---

## 1. What we're building

- A terminal chatbot that schedules a Google Meet call.
- Flow: user chats → bot collects name / email / topic → shows free calendar slots → user picks one → bot books a Google Calendar event with a Meet link → Calendar emails the invite.
- Split of work:
  - **Claude** writes all code and explains it.
  - **Kul** validates the architecture, runs everything locally (OAuth + live Google API calls must run on the laptop), and pastes back results or errors.

---

## 2. MCP landscape (verified 2026-10-14)

- **The spec is now stateless (revision 2026-07-28):**
  - The `initialize` / `notifications/initialized` handshake is **removed**.
  - Every request carries the protocol version and client capabilities in `_meta`.
  - Servers MUST implement a new **`server/discover`** RPC (versions, capabilities, identity).
  - Protocol-level sessions are removed (no `Mcp-Session-Id`).
  - Roots, Sampling and Logging are **deprecated**.
  - Server→client requests are replaced by **Multi Round-Trip Requests**: the server returns `InputRequiredResult` and the client retries with `inputResponses`.
  - All results carry `resultType` (`"complete"` or `"input_required"`).
- **The Python SDK is now v2:**
  - `pip install mcp` → 2.x. Requires Python 3.11+.
  - Server class: `from mcp.server import MCPServer` (the FastMCP lineage, renamed).
  - The client `Client(...)` takes a `mode=` argument. This project uses two values:
    - `"auto"` (default): sends a `server/discover` probe and falls back to `initialize` against old servers.
    - `"legacy"` (`--legacy` flag): always runs the `initialize` handshake.
- **Interview implication:** know **both eras**.
  - Legacy: `initialize → initialized → tools/list → tools/call`.
  - Modern: `server/discover → tools/list → tools/call`.
  - This project runs the same client in both modes and compares the traces.

---

## 3. Design decisions (confirmed / changed)

| Original direction | Verdict | Notes |
|---|---|---|
| Python + FastMCP | ✅ adjusted | Official SDK v2 `MCPServer`. One package for server + client, and it tracks the spec. |
| STDIO transport | ✅ | Single local user. Streamable HTTP is a stretch phase, for contrast. |
| Desktop OAuth, no redirect URIs | ✅ + correction | You register no redirect URI, but `InstalledAppFlow.run_local_server()` still spins up a temporary **loopback** server on localhost. |
| Separate one-time `auth.py` | ✅ | (1) The browser flow prints to stdout, which corrupts JSON-RPC on STDIO. (2) The host spawns the server headless, so an interactive flow inside a tool call would hang. |
| `freebusy` + `events.insert` with `hangoutsMeet`, `conferenceDataVersion=1` | ✅ | Add **`sendUpdates="all"`** so Calendar sends the invite email itself. No Gmail API needed. |
| Idempotency via deterministic `requestId` | ❌ **wrong layer** | See §3.1. |
| `.gitignore` | ✅ + additions | `.env`, `.venv`, `__pycache__` are already covered. **`credentials.json` and `token.json` are NOT yet in `.gitignore`** — add both in Phase 0 before anything else. |

### 3.1 Idempotency (correction)

- `conferenceData.createRequest.requestId` deduplicates **only Meet conference creation**. A retried `events.insert` still creates a **second event**.
- **Fix:** set the event's own **`id`** deterministically.
  - `id = base32hex(sha256(attendee_email | start_iso | duration | title)).lower()`
  - Allowed characters are `a-v`, `0-9`; length 5–1024.
- On retry, Google returns **409 Conflict**. The server catches it, calls `events.get`, and returns the existing event with `status: "already_existed"`.
- **Edge case:** IDs of deleted events stay reserved.
  - Rebooking an identical, previously deleted meeting → 409, and the existing event shows `status=cancelled`.
  - The server reports this explicitly and does **not** pretend success.

---

## 4. Architecture

```
┌──────────────── HOST: client.py (one process) ────────────────┐
│  Terminal REPL ── conversation state ── LLM tool loop (llm.py)│
│                        │                                       │
│                MCP Client (1:1 with server)                    │
└────────────────────────┬───────────────────────────────────────┘
                         │ stdin/stdout: JSON-RPC (stderr = logs)
┌────────────────────────▼───── SERVER: server.py (subprocess) ──┐
│  MCPServer: 2 tools · 1 resource · 1 prompt                    │
│        │  thin adapter layer only                              │
│  gcal.py: pure Google Calendar logic (zero MCP code)           │
│        │  token.json (never leaves this process)               │
└────────┼───────────────────────────────────────────────────────┘
         ▼  Google Calendar API (freebusy, events.insert/get)
```

- **Host** = owns the UI (REPL), the LLM and the conversation.
- **Client** = the protocol connection, one per server.
- **Server** = owns capabilities and credentials.
- The **LLM never talks to the server.** The host relays every tool call.
- **Credential isolation:** the Google token lives only in the server process. Neither the LLM nor the client ever sees it.
- **`gcal.py` is separate from `server.py`** to show that MCP is a thin adapter over ordinary business logic.
- **REPL** = Read–Eval–Print Loop: a terminal chat loop. It means no frontend code, and `--trace` output appears inline.

### 4.1 Repo layout

```
mcp-meet-scheduler/
├── project_context/
│   └── develop.md     # this file
├── README.md          # run steps + "concept → line of code" interview map
├── pyproject.toml     # pinned deps (uv)
├── .gitignore         # + credentials.json, token.json
├── .env.example       # LLM_PROVIDER, OPENAI_API_KEY, TIMEZONE, WORK_* hours
├── auth.py            # one-time browser OAuth → token.json
├── gcal.py            # freebusy → free slots; insert/get event
├── server.py          # MCPServer: tools, resource, prompt
└── client.py          # host: REPL + Claude loop + MCP Client (--trace, --legacy)
```

---

## 5. MCP surface

### Tools (model-controlled)

- **`get_available_slots(date: "YYYY-MM-DD", duration_minutes: int = 30)`**
  - Runs freebusy over working hours and subtracts busy blocks.
  - Steps in 30-minute increments, skips past times, returns at most 8 slots.
  - Returns ISO-8601 times with explicit offsets.
  - Annotation: `readOnlyHint`.
- **`create_meeting(title, attendee_name, attendee_email, start_iso, duration_minutes, description?)`**
  - Validates, re-checks freebusy, then inserts with the deterministic event `id`.
  - Returns `{event_id, meet_link, html_link, status}`.
  - Annotations: `idempotentHint`, not read-only.
- Both use typed signatures with field descriptions, so the SDK generates the JSON Schema. Both return **structured output**.
- **Schema demo:** deliberately weaken one tool description in a run and observe how the LLM's tool choice degrades.

### Resource (application-controlled)

- **`scheduler://config`** holds timezone, working hours and slot step.
- The **host** reads it at startup and injects it into the system prompt. The model never calls it.

### Prompt (user-controlled)

- **`schedule_meeting(topic)`** is exposed by the host as a `/schedule` slash command.
- It shows that prompts are user-selected templates, not model-invoked.

---

## 6. Runtime chat flow

1. The host spawns the server over STDIO and connects.
   - `auto` mode sends `server/discover`; `--legacy` mode runs `initialize`.
2. The host calls `tools/list`; `llm.py` maps each MCP `inputSchema` 1:1 to OpenAI `parameters` (or Claude `input_schema`).
3. The system prompt contains today's date, the timezone (Asia/Kolkata) and the config resource. Without the date, the LLM can't resolve "tomorrow".
4. The LLM collects name, email and topic, then calls `get_available_slots`. The host relays it as `tools/call`.
5. The user picks a slot, and the LLM calls `create_meeting`.
6. **Confirmation gate:**
   - The host prints the arguments and asks y/n before relaying.
   - On "no", it returns "user declined" as the tool result.
   - This is human-in-the-loop for a side-effecting tool, and a host responsibility.
7. The event is created, `sendUpdates=all` emails the invite, and the bot prints the Meet link.

- **State** lives only in the host's message list. The server is stateless, matching the 2026 spec.

---

## 7. Error handling & security

### Two error classes

- **Protocol errors** (JSON-RPC errors: unknown tool, malformed params) → the LLM never sees them.
- **Tool execution errors** → a result with `isError: true` and a readable message, returned to the LLM so it can self-correct (e.g. "slot taken → re-fetch slots").

### Google API failure mapping

| Failure | Message / behaviour |
|---|---|
| 401 / invalid token | "Run auth.py" |
| 409 | Idempotent hit → return the existing event |
| 403 / 429 | Permission or rate-limit message |
| Meet link `pending` | Re-fetch the event once |

### Validation (all LLM arguments are untrusted)

- Email format.
- Start time in the future and inside working hours.
- Duration between 15 and 120 minutes.
- Slot still free at insert time.

### Security notes

- **OAuth scopes:** `calendar.events` + `calendar.freebusy` (least privilege).
  - Confirm on the first run. If a scope is wrong, delete `token.json` and re-run `auth.py`.
- The free-slot re-check is **check-then-act, not atomic**. That's acceptable for one user; state it as a known limit.
- **Tool annotations are hints.** Clients must treat them as untrusted unless they trust the server.
- **STDIO trust boundary** = the local OS user, with no transport auth.
  - Over HTTP, the server becomes an OAuth 2.1 resource server. This is discussed, not built.

---

## 8. Build phases (~2.5 h) and checkpoints

| # | Phase | Time | Kul verifies |
|---|---|---|---|
| 0 | Repo setup, `.gitignore` fix, deps, `credentials.json`, run `auth.py` | 20m | `token.json` exists and is git-ignored |
| 1 | `gcal.py` + server; slots tool in **MCP Inspector** | 35m | Wire-level `server/discover` → `tools/list` → `tools/call` |
| 2 | `create_meeting` in Inspector | 20m | Invite email arrives, Meet link works, 2nd identical call → `already_existed` |
| 3 | `client.py`: Claude loop + confirmation gate + `--trace` | 40m | Full chat books a meeting end-to-end |
| 4 | Failure drills + era comparison | 20m | Bad email, past date, deleted token; `--legacy` vs `auto` traces |
| 5 | *Stretch:* same server over Streamable HTTP | 15m | Can explain what changes: transport, auth, statelessness |

- **Prerequisites on the laptop:** Python 3.11+, uv, Node.js/`npx` (for the Inspector), an OpenAI API key, and `credentials.json` (Desktop app OAuth client).
- `--trace` prints every MCP request/response. If SDK v2 exposes no raw message hook, Claude adds a small stream wrapper.

---

## 9. Risks

- **SDK v2 is recent** and Claude's knowledge is mostly v1, so expect 1–2 API-mismatch errors on the first run (budget ~15 min).
- **Testing-mode OAuth refresh tokens expire after ~7 days.** Re-run `auth.py` before any demo after that.
- **Timezones** are the top bug source. Use IANA tz and explicit offsets everywhere; no naive datetimes.
- **Every test booking sends a real invite.** Use your own secondary address as the attendee.
- **`.gitignore` gap:** `credentials.json` / `token.json` are not ignored yet, so committing before Phase 0 would leak secrets.

---

## 10. Decisions (approved — defaults in bold)

1. **LLM:** ~~Anthropic~~ → **OpenAI Responses API, `gpt-5.4-mini`** (changed 2026-10-14: Kul has an OpenAI account). Anthropic kept behind `LLM_PROVIDER=anthropic`. Manual tool loop, no SDK agent helper.
2. **SDK:** **official `mcp` v2 (`MCPServer`)** vs standalone FastMCP (PrefectHQ).
3. **Hours:** **Asia/Kolkata, Mon–Fri 10:00–18:00, 30-min step**.
4. **Include resource + prompt:** **Yes** (covers all three primitives).
5. **Confirmation gate before `create_meeting`:** **Yes**.
6. **Env tooling:** **uv** vs venv + pip.
7. **Stretch phase 5 (HTTP):** **Yes, if time allows**.

---

## 11. Implementation notes (SDK `mcp` 2.3.0, verified in sandbox)

- **Pinned:** `mcp[cli]>=2.3,<3`, `anthropic>=1.13`; `uv.lock` committed for reproducibility.
- **Errors:**
  - `ToolError` → result with `isError: true` and the message.
  - Unexpected exceptions → `isError` with details **masked**.
  - Unknown tool / schema-validation failures → also `isError` results in this SDK, not JSON-RPC errors.
- **The STDIO host passes only a minimal env** to the server.
  - `server.py` reads just the 4 scheduler keys from `.env`.
  - It never loads the LLM API keys (credential boundary).
- **`--trace`** is implemented as a `Transport` wrapper around `stdio_client` streams (no SDK hook needed).
- **Idempotency order:** `events.get(id)` → free/busy re-check → `events.insert` → on 409, re-get.
  - The get comes first so a retry doesn't see its own event as "busy".
- **`--legacy` traces** show `initialize` + `notifications/initialized` with no `_meta` afterwards.
- **`auto` traces** show `server/discover`, with `_meta` (version, clientInfo, capabilities) on every request.
- **Offline test:** `tests/test_offline.py` uses a fake Calendar + in-process client and covers both eras.
- **LLM switch (2026-10-14):** OpenAI Responses API is default (OpenAI's changelog says its newest models require Responses for tool calling).
  - Tools: `{"type":"function","name","description","parameters": inputSchema, "strict": false}`. `strict` must be false: strict mode needs `additionalProperties:false` and all fields required, which our optional/default args don't meet.
  - History: the host appends `resp.output` (incl. `reasoning` + `function_call` items), then `function_call_output` items keyed by `call_id`.
  - OpenAI has no `is_error` flag: failures are sent as text prefixed `ERROR:`. Anthropic keeps `is_error: true`.
  - Verified with the real `openai` 3.31 / `anthropic` 1.13 SDKs over mocked HTTP + real STDIO server.
- **Verified live 2026-10-14** (from the linked VM): OAuth scopes incl. `calendar.freebusy`, real Meet link, invite, idempotent retry, slot-taken, both eras, real OpenAI loop. See `testing.md`.
- **Lesson from the VM run (good interview point):** the STDIO client spawns the server with a *minimal* env (PATH, HOME…), so proxy vars (`HTTPS_PROXY`) are **not inherited**. Behind a corporate proxy the server can't reach Google unless the host passes them via `StdioServerParameters(env=...)`. Also `httplib2` (Google client) needs `PySocks` to tunnel through a proxy. Not needed on a normal Mac network, so the repo code is unchanged.

## 12. Interview concept map

| Concept | Where it shows up |
|---|---|
| Host vs client vs server | `client.py` (host + client) / `server.py` |
| Tools vs resources vs prompts | §5; `server.py` decorators |
| STDIO vs Streamable HTTP | Phase 1 vs Phase 5 |
| Lifecycle (both eras) | `--trace` output, `auto` vs `--legacy` |
| Schemas drive tool choice | `tools/list` → `llm.py` (OpenAI `parameters` / Claude `input_schema`); description demo |
| Auth / security boundaries | `auth.py`, token isolation, confirmation gate |
| Error handling | `isError` results vs JSON-RPC errors |
| Idempotency | deterministic event `id` + 409 handling in `gcal.py` |

---

## References

- [MCP 2026-07-28 changelog](https://modelcontextprotocol.io/specification/2026-07-28/changelog)
- [MCP Python SDK v2 docs](https://py.sdk.modelcontextprotocol.io/)
- [Python SDK — Protocol versions](https://py.sdk.modelcontextprotocol.io/protocol-versions/)
