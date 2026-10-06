# MCP server

The front desk's tools, exposed over the **Model Context Protocol**, so any
MCP client (Claude Desktop, Claude Code, another agent) can check
availability and book into a practice. Code: [`app/mcp_server.py`](../app/mcp_server.py).

**What it's for:** the phone agent is one front end to the practice's
scheduling. MCP makes the same capabilities available to *other* AI front
ends with no custom integration: a patient's assistant, a staff member's
Claude, a partner's agent. Every one of them books through the same rules
(business hours, the 30-minute grid, no double booking, tenant isolation),
because it's the same code.

## MCP in five minutes

- **Primitives.** A server offers three kinds of things. *Tools* are actions
  the model decides to call (`book_appointment`). *Resources* are data the
  client application reads and attaches as context, addressed by URI
  (`practice://4/hours`). *Prompts* are templates the user picks, for example
  a persona (`front-desk-persona`). A server can also send requests back to
  the client: *elicitation* asks the human a question, *sampling* asks the
  client's model.
- **Transports.** *stdio*: the client launches the server as a local process
  and speaks JSON-RPC over stdin/stdout (so the server must never print to
  stdout). *Streamable HTTP*: one HTTP endpoint (`POST /mcp`); responses come
  back as JSON or as an SSE stream, and a session id header ties requests
  together. The older HTTP+SSE transport is deprecated.
- **Handshake.** The client sends `initialize` with its protocol version,
  capabilities (e.g. `elicitation`) and client info. The server answers with
  its own capabilities (tools, resources, prompts) and the negotiated
  version. The client sends `notifications/initialized`, then lists and calls
  things. What a server may do depends on what the client declared: this
  server only elicits from clients that declared `elicitation`.
- **Protocol versions matter in practice.** SDK 2.x (installed: `mcp 2.2.0`,
  where "FastMCP" is now `MCPServer`) speaks both generations. Up to
  `2025-11-25`, the server sends an elicitation *request* mid-call. From
  `2026-07-28`, the tool *returns* an input-required result and the client
  retries with the answer. The booking confirmation below uses the SDK's
  resolver mechanism, which handles both. That wasn't theoretical: with the
  SDK's in-memory client, a plain `ctx.elicit()` failed with
  `NoBackChannelError`.

## What's exposed

**Tools**: the five from [`app/agent/tools.py`](../app/agent/tools.py), no
second implementation. Each MCP tool is an adapter that calls the same
function through the same `loop.execute_tool` the phone agent uses
(validation, errors, redacted logging). Argument descriptions *are* the
`tools.py` Pydantic fields.

| Tool | Hints | Notes |
|---|---|---|
| `check_availability(date_range)` | read-only | Slots carry `start` (to book with) and `spoken` ("9 AM Monday the 5th") |
| `book_appointment(caller_name, callback_number, service, requested_slot, notes?)` | writes | Checks the slot, then asks the user to confirm (below) |
| `reschedule_appointment(appointment_id, new_slot, reason?)` | writes | Only this practice's appointments |
| `escalate_to_human(reason)` | writes | Records a callback request (Lead, intent=callback) |
| `answer_faq(question)` | read-only | Still the stub: always defers to staff |

No tool takes a `practice_id`: the practice comes from the credentials
(below), never from the model. Times go in as practice-local time with no
offset (`2026-10-05T10:00:00`) or as a `start` from `check_availability`.

**Resource**: `practice://{practice_id}/hours`, JSON: name, timezone, and hours
per day (`"mon": "09:00-17:00"`, `"sun": "closed"`). This is
`tools.business_hours()`, read exactly the way `check_availability` reads
them. Only the credential's own practice is readable; any other id is "not
found".

**Prompt**: `front-desk-persona`, the same system prompt the phone agent runs
on (`prompts.build_system_prompt`), for the credential's practice.

### Booking confirmation, MCP-style

On the phone, a state gate makes the model read the booking back and wait
for a "yes" on a later turn (`app/agent/state.py`), because nobody can check
the model before it books. Over MCP there are better levers, and this server
uses them:

1. Before anything is written, the booking is checked with the same
   `tools.check_booking` the phone agent uses: slot open, in hours, on the
   grid, in the future. An impossible time fails here, before the user is
   asked anything.
2. **If the client supports elicitation**, the *server* asks the human
   directly: "Book this appointment? Dana Lee, phone number ending in 0 1 4 2,
   for a cleaning at 9 AM Monday the 5th". That's the phone read-back, word
   for word. Decline, and nothing is booked.
3. **If it doesn't**, the client's own tool-approval prompt is the
   confirmation: it shows the user the exact arguments before running the
   call. The tool is annotated as a write (`readOnlyHint: false`) so clients
   treat it as one.

Each booking or escalation gets a `Call` row, `caller_number =
"mcp:stdio"` or `"mcp:practice-<id>"`, because everything in this app hangs
off a Call. That keeps the channel visible in the data.

## Auth and tenant scoping

**The rule is the one `/calls` follows: one practice's data is invisible to
another.** Over MCP, the credential decides the practice:

| Transport | Credential | Practice |
|---|---|---|
| stdio | none: the client launches this process locally, as the user | `MCP_PRACTICE_ID` in its env |
| Streamable HTTP | `Authorization: Bearer <api key>` | the key's entry in `MCP_API_KEYS` |

- **Keys are stored hashed.** `MCP_API_KEYS='{"<sha256 of key>": 4}'`, so
  `.env` never holds a usable key. `python -m app.mcp_server new-key
  --practice-id 4` prints a new key once, plus the line to add.
- **The key is checked by the MCP SDK's own `TokenVerifier` hook**
  (`ApiKeyVerifier`). No key or a wrong key gets **401** before any MCP
  message is read. Tools read the verified practice via `get_access_token()`.
- **Isolation, tested over the real HTTP transport with two keys:** key A
  books into practice A; key B can't read A's hours resource or reschedule
  A's appointments (they're "not found": existence doesn't leak). A
  `practice_id` smuggled into tool arguments is ignored.
- **DNS-rebinding protection is on.** Only `localhost`/`127.0.0.1` Host
  headers are accepted, plus `MCP_ALLOWED_HOSTS` (for example an ngrok host).
  A foreign Host gets **421**.

Found while building this: `reschedule_appointment` didn't check which
practice an appointment belonged to at all. Over MCP, key A could have
moved practice B's appointment by guessing its id, and the phone agent had
the same hole. It's scoped now, in `tools.py`: the phone path binds the
call's practice, the MCP path the key's.

### What OAuth would look like

API keys are fine for server-to-server integrations and a course project.
For end users connecting from their own Claude (remote connectors in
Claude apps are built around OAuth sign-in, not a pasted secret), the
upgrade is:

- **An authorization server** (Auth0, Cognito, WorkOS, or the SDK's own
  `OAuthAuthorizationServerProvider`) issues short-lived access tokens with
  OAuth 2.1 + PKCE. Clients register dynamically or via client metadata
  documents.
- **This server becomes an OAuth resource server.** Swap `ApiKeyVerifier`
  for a verifier that validates the JWT: signature via JWKS, expiry, issuer,
  and audience = this server's URL (RFC 8707). Set
  `AuthSettings(issuer_url=<AS>, resource_server_url=<https://…/mcp>,
  required_scopes=[…])`. The SDK then publishes Protected Resource Metadata
  (RFC 9728) and adds `WWW-Authenticate` to 401s, which is how clients
  discover where to log in.
- **Tenant scoping moves into claims.** The token carries the practice (a
  `practice_id` claim, or an org the user belongs to), and scopes such as
  `appointments:read` / `appointments:write` separate "can look" from "can
  book". The tools don't change: they already read the practice from the
  verified token, not from arguments.

## How to connect

Postgres (and for bookings, nothing else) must be running:
`docker compose up -d postgres`, `alembic upgrade head`.

### Claude Desktop (stdio)

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`,
then restart Claude Desktop:

```json
{
  "mcpServers": {
    "sunshine-front-desk": {
      "command": "/Users/renatkhusainov/.local/bin/uv",
      "args": ["--directory", "/Users/renatkhusainov/Documents/Study/FDE Course/fde-voice-agent",
               "run", "python", "-m", "app.mcp_server"],
      "env": { "MCP_PRACTICE_ID": "4" }
    }
  }
}
```

Then ask: *"Book me a cleaning at Sunshine Dental next week."* Check the row:
`curl "localhost:8000/calls?practice_id=4"` (the newest call is
`mcp:stdio`), or look in `appointments`.

### Claude Code

```bash
# stdio
claude mcp add sunshine-front-desk --env MCP_PRACTICE_ID=4 -- \
  uv --directory "/Users/renatkhusainov/Documents/Study/FDE Course/fde-voice-agent" run python -m app.mcp_server

# streamable HTTP (app running: uvicorn app.main:app --port 8000)
python -m app.mcp_server new-key --practice-id 4     # add the printed MCP_API_KEYS line to .env, restart the app
claude mcp add --transport http sunshine-front-desk-http http://localhost:8000/mcp \
  --header "Authorization: Bearer <the key>"
```

### Any client, over HTTP

`POST http://localhost:8000/mcp` with `Authorization: Bearer <key>` and
`Accept: application/json, text/event-stream`. It's mounted in the same
FastAPI app as the Twilio webhook (`app/main.py`), so one process serves the
phone line and MCP.

## What was verified

- **Tests** (`tests/test_mcp_server.py`, 17, offline). They use the SDK's own
  client over the real protocol: in-process for tools/resource/prompt, and
  over the streamable-HTTP transport against `app.main` with its real
  lifespan for auth and isolation. Mutation-checked: removing the
  reschedule scope, the resource scope, the HTTP key → practice mapping, or
  the confirmation each fails a test.
- **Live, against the dev database.** Over stdio (a real subprocess, as
  Claude Desktop runs it) and over HTTP (uvicorn, a fresh key): list tools,
  resource and prompt; read hours; `check_availability` for next week;
  `book_appointment` → confirmation read back → Appointment rows 11 and 12.
  HTTP without a key: 401.
- **Not verified by me:** Claude Desktop itself. It's the same stdio command,
  launched from another working directory with a minimal `PATH` (checked),
  but the chat → booking run is yours to do.

## Known limits

- **One Call per booking or escalation.** An MCP conversation isn't tied to
  one Call the way a phone call is, so an MCP booking isn't idempotent across
  retries: a retried booking finds its own slot taken.
- **`escalate_to_human` over MCP records the reason only.** It has no
  callback number unless the model puts one in the reason.
- **`answer_faq` is still the stub**, and `reschedule_appointment` has no
  confirmation step (true on the phone too).
- **stdio has no authentication by design.** Whoever can launch the process
  with database credentials already has the data. Don't expose stdio
  servers any other way.
