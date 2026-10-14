# testing.md — Validation guide

> Run every step **on your laptop**, from the repo root. Tick each box as you go.
> If a step fails, paste back: the command, the full terminal output, and the last ~20 lines of `server.log` (if it exists).

## Already verified by Claude (2026-10-14, sandbox, files checksum-identical to this repo)

| Check | Result |
|---|---|
| T1 `.gitignore` covers credentials.json, token.json, .env, server.log, token.bak | ✅ (run on your copy) |
| T2 clean `uv sync --frozen` + offline tests, both eras | ✅ |
| **Real** `googleapiclient` vs canned HTTP: freebusy body, insert has `conferenceDataVersion=1` + `sendUpdates=all`, base32hex event id, `hangoutsMeet` request, pending-Meet re-fetch, 409 → `already_existed`, 401/403/429 messages, UTC busy vs IST slots | ✅ |
| Real `client.py` Host over real STDIO with a scripted LLM, both eras: gate only on `create_meeting`; `y` books; `n` sends **no** `tools/call`; idempotent rebook; parallel tool_use → one ordered result message; invalid email → `isError`; `/schedule` → `prompts/get`; tool_use/tool_result pairing valid | ✅ |
| MCP Inspector (CLI mode) against the server: `tools/list`, `tools/call` (ok + error), `resources/read`, `prompts/get`; T4 command with no token → clean `isError` | ✅ |
| `auth.py` / `auth.py --check` without files, `client.py` without key, `client.py` with a bad key → real Anthropic 401 shown, REPL keeps running | ✅ |
| **OpenAI switch:** real `openai` 3.31 SDK (HTTP mocked) + real `client.py` + real STDIO server: tools sent as `function` with `strict:false`; 2nd request input = `user, reasoning, message, function_call, function_call_output`; booking created; gate `n` → `ERROR: The user declined…` reaches model; API 401 → turn rolled back. Same suite passes for `--provider anthropic` | ✅ |

## Live-verified by Claude (2026-10-14, linked VM, real Google + real OpenAI, your repo files)

| Step | Result |
|---|---|
| T2 offline tests (Python 3.12 in VM) | ✅ |
| OpenAI key + `gpt-5.4-mini` live | ✅ |
| T3 OAuth (consent in your browser, code exchanged in VM) → `token.json` with refresh token + both scopes; `auth.py --check` | ✅ `OK: token works` |
| T5 live chat (real OpenAI + real `server.py` + real Calendar): name/email/topic collected, "tomorrow" → correct date, real slots listed, approval prompt, **real event + Meet link `meet.google.com/zjf-dywm-poc`** for Mon 2026-10-19 10:00 IST, invite to kuldeep.chouhan2408@gmail.com | ✅ |
| Idempotency live: identical `create_meeting` again → `already_existed`, same event id + Meet link | ✅ |
| T7.6 live: different meeting, same slot → `isError` "That slot is no longer free"; slots now start at 10:30 | ✅ |
| T7.2 past date (model refused by itself), T7.3 Saturday (empty slots + Mon-Fri note), T7.4 decline (no event) | ✅ |
| T8 live traces: `auto` = `server/discover` + `_meta` on every request; `legacy` = `initialize` → `notifications/initialized`, no `_meta` | ✅ |
| Bug found live + fixed: `gpt-5.4-mini` sometimes emits the same message twice → `llm.py` now de-duplicates | ✅ |

**Still for you (2 min):**
- [ ] The invite email arrived at kuldeep.chouhan2408@gmail.com
- [ ] Google Calendar shows exactly **one** event on Mon 19 Oct, 10:00–10:30, and the Meet link opens
- [ ] Optional: run `uv run python client.py --trace` on your Mac to see it yourself, and T4 (Inspector UI)
- [ ] Delete the test event afterwards if you like. Note: rebooking the *identical* meeting then gives the "reserved id" error (T7.5) — by design.

**(Before network access was opened) Not verifiable from Claude's side — now resolved above:**
- **Google OAuth consent.** It needs your browser and Google account, and the sandbox can't reach `googleapis.com`.
- **Live Calendar calls.** Specifically: that the `calendar.freebusy` scope is accepted, a real Meet link gets created, and the invite email is sent.
- **The live OpenAI tool loop.** My sandbox can't reach api.openai.com. Open question: OpenAI accepting the generated `parameters` schema (plain JSON Schema, `strict:false` — expected fine).
- **Python ≥ 3.11 on your Mac.** The linked VM only has 3.10.

→ Start at **T3**. T0–T2 are still worth 2 minutes on your Mac to confirm your local toolchain.

---

## T0. Prerequisites (5 min)

- [ ] `python3 --version` → 3.11 or newer
- [ ] `uv --version` works. If not, install it: `curl -LsSf https://astral.sh/uv/install.sh | sh`
- [ ] `node --version` works (needed only for T4 Inspector; optional)
- [ ] An **OpenAI API key** with billing/credits (platform.openai.com → Settings → API keys). Anthropic also works: `LLM_PROVIDER=anthropic`
- [ ] A **second email address you own**, to use as the attendee (every booking sends a real invite)

## T1. Repo hygiene (secrets never committed)

```bash
git pull
git check-ignore -v credentials.json token.json .env server.log
```
- [ ] All four paths are printed (each matched by a `.gitignore` rule). **Stop if any is missing.**
- [ ] Download the OAuth client JSON (type **Desktop app**) from GCP → save it in the repo root as `credentials.json`
- [ ] `git status` does **not** list `credentials.json`

## T2. Install + offline tests (no Google, no LLM)

```bash
uv sync
uv run python tests/test_offline.py 2>/dev/null
```
Expected:
```
PASS  mode=auto   protocol=2026-07-28
PASS  mode=legacy protocol=2025-11-25
All offline tests passed.
```
- [ ] Passes. (Without `2>/dev/null` you'll also see INFO logs like `Tool 'create_meeting' failed`. They are expected; those are the error cases being tested.)

**What this proved:**
- Tool schemas and annotations.
- Structured output.
- Errors come back as `isError` results.
- Idempotent retry → `already_existed` with only 1 insert.
- A deleted event id → explicit error.
- The resource and prompt work.
- Both protocol eras work.

## T3. Google OAuth (one time)

```bash
# .env already exists (created from .env.example) — edit it: set OPENAI_API_KEY=sk-proj-...
uv run python auth.py
```
- [ ] The browser opens → pick your **test-user** Gmail
- [ ] You see "Google hasn't verified this app" → **Continue**. This is expected in Testing mode.
- [ ] Approve both permissions (see/create calendar events, see free/busy)
- [ ] The terminal prints `Saved token.json. Scopes granted: [...calendar.events, ...calendar.freebusy]`
- [ ] Notice: the browser redirected to `http://localhost:<random port>/`. That is the **loopback** server, even though the Desktop client has no registered redirect URI.

```bash
uv run python auth.py --check
```
- [ ] Prints `OK: token works. N busy block(s) in the next 24h (Asia/Kolkata).`
- [ ] **If you get a 403 on freebusy:** delete `token.json`, tell me, and I'll adjust `SCOPES`.

## T4. Server alone, in MCP Inspector (optional, ~10 min)

```bash
npx @modelcontextprotocol/inspector uv run python server.py
```
Open the URL it prints → **Connect**.
- [ ] **Tools → List Tools** shows `get_available_slots` and `create_meeting`. Open one and read its JSON Schema (generated from the type hints + `Field` descriptions).
- [ ] Run `get_available_slots` with `date` = next working day → slots in `+05:30`, none overlapping your real events
- [ ] Run `get_available_slots` with `date` = `tomorrow` → error result (pattern mismatch). This is a tool error, not a crash.
- [ ] **Resources** → read `scheduler://config` → JSON config
- [ ] **Prompts** → `schedule_meeting` with topic `pricing` → a templated user message
- [ ] Run `create_meeting`:
  - `attendee_email` = your second address
  - `start_iso` = copied from a returned slot
  - Result: `status: "created"` plus a `meet_link`
- [ ] **Run the exact same `create_meeting` again** → `status: "already_existed"`, same `event_id`, and still only ONE event in Google Calendar ← **idempotency**
- [ ] The second inbox received the invite email and the Meet link opens

Leave that event in place for now (see T7.5).

## T5. End-to-end chat — happy path

```bash
uv run python client.py --trace
```
Startup output to check (first line should say `llm=openai:gpt-5.4-mini`):
- [ ] `→ server/discover` then `← ...result...` (no `initialize` — the 2026-07-28 era)
- [ ] `Connected: protocol 2026-07-28, server meet-scheduler`
- [ ] `→ tools/list`, `→ resources/read` (scheduler://config), `→ prompts/list`
- [ ] Every request carries `_meta` with `protocolVersion` and `clientCapabilities` → **this is what "stateless" means**

Conversation (example):
```
You: Hi, I'd like to book a call about MCP consulting
Bot: (asks for name / email)
You: Kul, <second-email>
Bot: (asks for a day)
You: tomorrow, 30 minutes      ← use a weekday; if tomorrow is Sat/Sun, say "Monday"
```
- [ ] `[tool] get_available_slots({"date": "<correct date>", ...})`: the LLM resolved "tomorrow" correctly from the date in the system prompt
- [ ] The bot shows a numbered list of slots that match your real calendar
- [ ] You pick one → the bot calls `create_meeting` → **`Allow 'create_meeting'? [y/N]`** appears (the confirmation gate; `get_available_slots` did NOT ask, because it is `readOnlyHint`)
- [ ] Type `y` → the result has `status: created` and a `meet_link`; the bot shows the link
- [ ] The invite arrives in the second inbox; the event is in your calendar with a Meet link
- [ ] Open `server.log` → the server's own logs. They went to stderr, never stdout.

## T6. Prompt primitive (user-controlled)

In the same chat (or a new one):
```
You: /schedule pricing discussion
```
- [ ] `(prompt expanded) I'd like to schedule a Google Meet call about: pricing discussion...`
- [ ] `--trace` shows `→ prompts/get`; the conversation continues from that text

## T7. Failure drills (each should be handled gracefully, no crash)

| # | Do this | Expected |
|---|---|---|
| 7.1 | Give the email `kul@nowhere` | Bot asks again, or `create_meeting` returns `isError` "Invalid attendee email" and the bot asks for a valid one |
| 7.2 | Ask for a date last week | `get_available_slots` → `isError` "...is in the past"; the bot recovers |
| 7.3 | Ask for a Saturday | Empty slots plus a note "meetings are only booked Mon-Fri" |
| 7.4 | At the `Allow 'create_meeting'?` prompt type `n` | No event created; the bot asks what to change |
| 7.5 | Delete the T4 event in Google Calendar, then book the **identical** meeting (same title, email, start, duration) in Inspector | `isError`: "...booked earlier and then deleted; Google keeps that event id reserved..." (honest failure, not fake success) |
| 7.6 | Let the bot list slots; **before picking**, create an event in Google Calendar covering one listed slot; then pick that slot | `isError` "That slot is no longer free"; the bot re-fetches slots (shows the re-check before insert) |
| 7.7 | `mv token.json token.bak`, then ask for slots | `isError` "Not authorized: token.json missing. Run ... auth.py"; restore with `mv token.bak token.json` |

- [ ] All 7 behave as expected

## T8. Protocol-era comparison (the interview demo)

```bash
uv run python client.py --trace --legacy
```
- [ ] Startup shows `→ initialize` (with `protocolVersion: 2025-11-25`, `capabilities`, `clientInfo`) → `← result` → `→ notifications/initialized`
- [ ] `Connected: protocol 2025-11-25`
- [ ] Later requests (`tools/list`, `tools/call`) have **no `_meta`**: the version and capabilities were agreed once, for the session
- [ ] You can explain the difference in 2 sentences: *stateful handshake once per connection* vs *stateless, every request self-describing (+ optional `server/discover`)*

## T9. "Schemas drive tool choice" demo (optional, 5 min)

1. In `server.py`, temporarily change the `get_available_slots` docstring to just `"""Does stuff."""` and remove the `description=` on `date`.
2. Restart `client.py --trace` and run `/tools` to see the weaker schema the LLM now receives.
3. Ask for "a call tomorrow".
- [ ] Observe the degradation: the wrong date format → pattern `isError`, the tool not called, or slots invented
4. `git checkout server.py` to restore.

---

## What to paste back if something fails

- The exact command and full output (with `--trace` if it's a chat issue)
- `tail -30 server.log`
- `uv run python -c "import importlib.metadata as m; print(m.version('mcp'), m.version('openai'))"`

## Observations to note during testing (SDK 2.3 behaviour, verified)

- **Unknown tool name** → the SDK returns an `isError` **result** ("Unknown tool: x"), not a JSON-RPC protocol error.
- **Schema validation failures** (e.g. a bad date pattern) → also an `isError` result, so the LLM can self-correct.
- **Unexpected exceptions** in a tool → the SDK masks the details ("Error executing tool X"); only `ToolError` messages reach the LLM. This is a deliberate security property: no stack traces leak to the model.
- The server only sees a **minimal environment** from the host (PATH, HOME, ...).
  - That's why `server.py` reads its 4 scheduler settings from `.env` itself.
  - It deliberately does **not** load `OPENAI_API_KEY` / `ANTHROPIC_API_KEY`, which is a credential boundary: the LLM key lives in the host, and the Google token lives in the server.
