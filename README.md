# FDE Voice Agent

AI-powered voice agent for dental practices. Handles inbound calls, captures leads, and books appointments.

---

## Stack

- **FastAPI**: the API, the Twilio webhook and Media Streams WebSocket, and the MCP endpoint
- **Pipecat**: the live call pipeline (Twilio audio → Deepgram STT → Claude → Deepgram TTS)
- **Anthropic Claude** (Haiku 4.5): the agent's model, with tool use
- **PostgreSQL 16 + pgvector**: calls, leads, transcripts, latency metrics, and the knowledge base's embeddings
- **SQLAlchemy 2.0 + Alembic**: ORM and migrations
- **Redis**: `DialogState` persistence for the confirmation gate (`app/agent/store.py`)
- **HAPI FHIR R4**: the scheduling system of record (Slots, Appointments)
- **Voyage AI** (`voyage-4-lite`): embeddings for knowledge-base search
- **MCP** (`mcp` 2.x): the same tools for Claude Desktop/Code and other MCP clients
- **uv**: dependencies and lockfile (`pyproject.toml`, `uv.lock`)
- **pytest**: SQLite for unit tests; real Postgres/HAPI/Redis integration tests skip when those aren't running
- **Docker Compose + ngrok**: the deployed stack, reachable by Twilio

---

## Project Structure

```text
fde-voice-agent/
├── app/
│   ├── config.py            # Pydantic Settings from .env
│   ├── db.py                # SQLAlchemy engine, SessionLocal, get_db
│   ├── main.py              # FastAPI app: routers, FHIR error handler, log scrubbing, MCP mount
│   ├── mcp_server.py        # The five tools over MCP (stdio + streamable HTTP at /mcp)
│   ├── agent/
│   │   ├── tools.py         # The five tools (check_availability, book_appointment, ...) + generated JSON schemas
│   │   ├── schema.py        # schema_from_model(): Pydantic model -> tool JSON schema
│   │   ├── loop.py          # run_agent_loop(): the tool-use loop against Anthropic's Messages API
│   │   ├── prompts.py       # Versioned system prompt (PROMPT_VERSION) + build_system_prompt()
│   │   ├── state.py         # DialogState + the confirmation gate: book_appointment can't execute on a prompt's say-so
│   │   ├── store.py         # Redis-backed DialogState persistence (JSON, TTL one hour)
│   │   ├── session.py       # Per-session conversation + DialogState, shared by /agent/turn and chat.py
│   │   ├── live.py          # The same tools + gate on a live call: Pipecat handlers, filler, escalation
│   │   ├── spoken.py        # Times/digits formatted for TTS: "9:30 AM Tuesday the 15th", "0 1 4 2"
│   │   └── chat.py          # `python -m app.agent.chat`: terminal REPL against the real agent loop
│   ├── fhir/
│   │   └── client.py        # Thin FHIR R4 client for HAPI + error mapping
│   ├── scheduling/
│   │   ├── __init__.py      # ScheduleGateway: where slots and appointments live
│   │   └── fhir.py          # FHIR implementation: Slot search, booking transaction with If-Match
│   ├── rag/
│   │   ├── chunk.py         # Markdown -> chunks: headings first, balanced packing
│   │   ├── embed.py         # Voyage embeddings: batching, cache, rate-limit handling
│   │   └── search.py        # Vector, keyword and hybrid (RRF) search, per practice
│   ├── services/
│   │   ├── bot.py           # The live call pipeline (Twilio + Deepgram + Claude)
│   │   ├── latency.py       # Per-stage timestamps per caller turn -> call_metrics
│   │   └── tts.py           # Deepgram TTS that speaks a reply's first sentence right away
│   ├── phi/
│   │   ├── redact.py        # redact(): masks phone numbers, SSNs, DOB-shaped dates before storage
│   │   └── log_scrub.py     # Keeps PHI and secrets out of loguru and stdlib logs
│   ├── models/
│   │   └── models.py        # Practice, Call, TranscriptTurn, Lead, Appointment, CallMetric, Document, Chunk
│   ├── routers/
│   │   ├── agent.py         # POST /agent/turn: text in, text out, per session_id
│   │   ├── practice.py      # Practices, calls, transcripts
│   │   ├── rag.py           # POST /rag/search
│   │   └── twilio.py        # Twilio webhook + Media Streams WebSocket (the real call path)
│   └── schemas/
│       └── practice.py      # Pydantic request/response schemas
├── scripts/
│   ├── seed_fhir.py         # Idempotent FHIR seed: practice, practitioners, schedules, a week of free Slots
│   └── ingest.py            # Idempotent KB ingest: markdown -> chunks -> embeddings -> rows
├── kb/
│   └── sunshine-dental/     # Practice 4's knowledge base: 31 docs + _FACTS.md (source of truth, not ingested)
├── experiments/             # Notes to myself, not product (excluded from the Docker image)
│   ├── raw_media_streams/   # Hand-rolled Twilio Media Streams protocol
│   ├── latency/             # Synthetic-call latency runs + report
│   ├── hl7v2/               # Parse ADT^A04 / ORU^R01, generate SIU^S12
│   ├── epic_sandbox/        # SMART on FHIR: a real Patient.read against Epic's sandbox
│   └── rag_eval/            # 20-question relevance check for the knowledge base
├── alembic/versions/        # Migrations
├── docs/
│   ├── hipaa-design.md      # BAA map, PHI classification, retention, what production requires
│   ├── phi-and-secrets.md   # Deepgram redaction, logging policy, secrets audit
│   ├── fhir-mapping.md      # SQL -> FHIR; FHIR is the scheduling system of record
│   ├── mcp.md               # MCP server: what's exposed, auth + tenant scoping, client configs
│   └── notes/               # Write-ups per module: latency, live-call tools, state machine, FHIR,
│                            #   HL7 v2, EHR landscape, RAG, raw media streams
├── tests/                   # pytest suite (see Tests)
├── .env.example             # Every setting, with defaults
├── Dockerfile               # The app image (applies migrations on start)
├── docker-compose.yaml      # postgres (pgvector), redis, hapi + hapi-db, app
├── Makefile                 # dev, migrate, test, ngrok, docker-build
├── pyproject.toml / uv.lock # Dependencies
└── README.md
```

---

## Run locally (app on the host)

### 1. Install

Python 3.12+ and [uv](https://docs.astral.sh/uv/):

```bash
git clone git@github.com:renatkhusainov/voice-agent.git fde-voice-agent
cd fde-voice-agent
uv sync --extra dev        # creates .venv from uv.lock, with test tools
source .venv/bin/activate
```

### 2. Configure

```bash
cp .env.example .env
```

Required (the app won't start without them):

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | Postgres for host runs (`postgresql+psycopg://…@localhost:5432/…`) |
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | The Postgres container's credentials |
| `TWILIO_NUMBER`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` | Telephony |
| `DEEPGRAM_API_KEY` | Speech-to-text and text-to-speech |
| `ANTHROPIC_API_KEY` | The agent's model |

Optional (defaults in `.env.example`):

| Variable | Purpose |
|---|---|
| `VOYAGE_API_KEY` | Knowledge-base embeddings. Without it, search is keyword-only and ingest can't run |
| `REDIS_URL` | Default `redis://localhost:6379/0` |
| `FHIR_BASE_URL` | Default `http://localhost:8090/fhir` (HAPI from compose) |
| `ENVIRONMENT` | `development` enables the dev-only `/agent/sessions/{id}` |
| `MCP_API_KEYS`, `MCP_PRACTICE_ID`, `MCP_ALLOWED_HOSTS` | MCP server auth and scoping ([docs/mcp.md](docs/mcp.md)) |
| `NGROK_AUTHTOKEN` | For `make ngrok` |

Secrets live only in `.env` (git-ignored, never copied into the image).

### 3. Start the services

```bash
docker compose up -d postgres redis hapi   # Postgres (pgvector), Redis, HAPI FHIR (~25 s to start)
alembic upgrade head
```

### 4. Seed scheduling and the knowledge base (first time, both idempotent)

```bash
python -m scripts.seed_fhir --practice-id 4   # a week of free Slots in HAPI
python -m scripts.ingest --practice-id 4      # kb/sunshine-dental -> chunks + embeddings
```

### 5. Start the server

```bash
make dev        # uvicorn with --reload on http://localhost:8000
```

---

## Deploy (Docker Compose + ngrok)

The deployed stack runs entirely in Docker on one machine. ngrok gives Twilio
a public HTTPS/WSS address for it.

```bash
# Build the app image and start everything: postgres (pgvector), redis, hapi, hapi-db, app.
# The app container runs `alembic upgrade head` before starting uvicorn.
docker compose up -d --build

# First deploy only: seed FHIR and ingest the KB from inside the app container
# (it reaches HAPI as http://hapi:8080/fhir and Postgres as postgres:5432).
# Seed every practice that takes calls, above all the one that owns
# TWILIO_NUMBER: a practice missing from HAPI can't offer times, only a callback.
docker compose exec app python -m scripts.seed_fhir --practice-id 4
docker compose exec app python -m scripts.seed_fhir --practice-id 2 --week-of $(date +%F) --days 14
docker compose exec app python -m scripts.ingest --practice-id 4

# Public address for Twilio (fixed ngrok domain, see Makefile)
make ngrok
```

- **Twilio:** set the number's *A call comes in* webhook to
  `https://<ngrok-domain>/twilio/inbound` (HTTP POST). The webhook answers
  with TwiML that connects the call to `wss://<same host>/twilio/ws`. The
  number must equal a practice's `phone` in Postgres, or the caller hears
  "This number is not configured."
- **Check:** `curl https://<ngrok-domain>/health`, then call the number.
- **Update:** `git pull && docker compose up -d --build app`. Migrations
  apply on start, and Postgres, Redis and HAPI data live in named volumes.
- **Logs:** `docker compose logs -f app`.
- **Port 8000:** stop a host `uvicorn` first; the container publishes 8000.
- **MCP over the public URL:** add the ngrok domain to `MCP_ALLOWED_HOSTS`
  (DNS-rebinding protection, [docs/mcp.md](docs/mcp.md)).

This is a single-machine dev deployment, not production. See the next
section before any real patient calls it.

---

## PHI and secrets

**Not production-ready for real patient data — see [docs/hipaa-design.md](docs/hipaa-design.md).**
No BAA is signed with Twilio, Deepgram, or Anthropic, and there is no
authentication on any endpoint yet. That document has the BAA map, the PHI
classification, the retention policy, and the full list of what's missing.

Transcript turns are redacted before they are written (`app/phi/redact.py`), Deepgram
is asked to mask card numbers and SSNs at the source, and log output is scrubbed of
PHI and credentials. Implementation-level decisions, the logging policy, and the
secrets audit: [docs/phi-and-secrets.md](docs/phi-and-secrets.md).

---

## Raw Media Streams (protocol exercise, not production)

`experiments/raw_media_streams/` hand-rolls the Twilio Media Streams WebSocket
protocol — no Pipecat, and no dependency on `app/` at all — and echoes a
caller's own audio back to them. It's a note to myself, not a product feature,
which is why it lives under `experiments/` and isn't wired into the app in
`app/main.py`. It exists to make visible what a framework normally hides:
framing/base64/mu-law, the streamSid handshake, backpressure, turn-taking
(VAD/turn detection/barge-in), and reconnect handling. Write-up:
[docs/notes/raw-media-streams.md](docs/notes/raw-media-streams.md).

Run it on its own port, separately from the product app:

```bash
uvicorn experiments.raw_media_streams.server:app --port 8001
```

---

## Latency baseline

Five real response-latency measurements (LLM time-to-first-byte + TTS
time-to-first-byte) against the live Anthropic and Deepgram APIs, using the
same model, voice, and prompt as production, plus real STT timing against
actual recorded call audio. Median response latency: **1090 ms**. Numbers,
methodology, and what's *not* measured (Twilio round-trip, live STT
endpointing): [docs/notes/latency-baseline.md](docs/notes/latency-baseline.md).

---

## Tools on a live call

A real Twilio call uses the same five tools, confirmation gate and
`DialogState` as the text harness below (`app/agent/live.py`). The agent can
check availability and book on the phone. It reads the booking back before
writing it, with times said the way people say them ("9:30 AM Tuesday the
15th"). A clinical call is handed off with a fixed handoff line and ends
gracefully. Each tool call shows up in `GET /calls/{id}/transcript` as
`role: tool`. Needs Redis (`docker compose up -d redis`; `REDIS_URL` defaults to
localhost) and migration `f3b9c2d4e5a1` applied. Details and
what's verified: [docs/notes/live-call-tools.md](docs/notes/live-call-tools.md).

`TOOL_FILLER_ENABLED` / `TOOL_FILLER_DELAY_MS` control the "Let me check that
for you" filler. See [docs/notes/filler-ab.md](docs/notes/filler-ab.md) for
the A/B.

---

## FHIR server (HAPI)

A local FHIR R4 server, so EHR integration can be built against the real
protocol: `docker compose up -d hapi`. It runs HAPI FHIR JPA server 8.12 on
its own Postgres, at `http://localhost:8090/fhir`, and is ready in ~25 s.
`app/fhir/client.py` is a thin httpx client (plain dicts) that maps HAPI's
errors to this API's status codes. What the server supports, the core
resources, and the traps (paging, `+` encoding, stale search totals):
[docs/notes/fhir-cheatsheet.md](docs/notes/fhir-cheatsheet.md).

**FHIR is the system of record for scheduling; Postgres keeps calls and
leads.** [docs/fhir-mapping.md](docs/fhir-mapping.md) has the reasoning and
the field-by-field mapping. Seed a practice's week of free slots (safe to
rerun):

```bash
python -m scripts.seed_fhir --practice-id 4
```

The agent's `check_availability` and `book_appointment` go through FHIR
(`app/scheduling/`). Availability is free Slots; a booking is one transaction
(Appointment + Slot busy, guarded by `If-Match`). The Postgres `appointments`
row is a shadow holding the FHIR id. **HAPI must be running and the practice
seeded** for the agent to book, otherwise it offers a callback.

HL7 v2, for hospitals that don't speak FHIR: `experiments/hl7v2/` parses an
ADT^A04 and an ORU^R01 and turns a FHIR booking into a validated SIU^S12.
The message-type table and how the agent would plug into a v2-only hospital
are in [docs/notes/hl7v2.md](docs/notes/hl7v2.md).

Who you'd actually integrate with: Epic, Oracle Health, athenahealth and the
dental PMSs (Open Dental, Dentrix, Eaglesoft, Curve, Denticon). The doc covers
their developer programs, SMART on FHIR auth, TEFCA, and information blocking:
[docs/notes/ehr-landscape.md](docs/notes/ehr-landscape.md).
`experiments/epic_sandbox/` makes a real `Patient.read` against Epic's sandbox.

---

## Knowledge base (RAG)

The practice's services, prices, insurance, hours and policies, searchable by
meaning. Postgres runs the pgvector image. Embeddings come from Voyage
(`VOYAGE_API_KEY`); without a key, search falls back to keyword-only.

```bash
alembic upgrade head
python -m scripts.ingest --practice-id 4            # kb/sunshine-dental, idempotent
python -m experiments.rag_eval.run --practice-id 4  # 20-question relevance check
```

`POST /rag/search` takes `{"practice_id": 4, "query": "Do you do Invisalign?", "k": 3}` and returns
ranked chunks with scores (`mode`: `hybrid` default, `vector`, or `keyword`). Design, the eval, and
why hybrid lost to vector on caller questions:
[docs/notes/rag.md](docs/notes/rag.md).

---

## MCP server

The agent's five tools, plus an opening-hours resource and the front-desk
persona prompt, over the Model Context Protocol. Claude Desktop, Claude Code
or any MCP client can check availability and book into a practice. Same
`tools.py` as the phone agent, no second implementation. Two transports:
- **stdio:** `python -m app.mcp_server`, practice from `MCP_PRACTICE_ID`.
- **streamable HTTP:** `/mcp` in this FastAPI app, `Authorization: Bearer
  <key>`, where each key maps to one practice.

Setup and client configs: [docs/mcp.md](docs/mcp.md).

---

## Latency

Every caller turn on a live call gets a `call_metrics` row: when the caller
stopped speaking, and how long after that each stage happened (VAD, STT,
turn release, LLM first/last token, each tool, TTS first byte, first audio
out). The numbers, the budget, and what did and didn't help are in
[docs/notes/latency-budget.md](docs/notes/latency-budget.md). Shipped result
on synthetic calls: end-to-end p50 1519 → 1368 ms, tool turns 1896 → 1383 ms.

Measure a change with synthetic calls (a fake Twilio on `/twilio/ws`), or tag
real calls with `LATENCY_LABEL`:

```bash
experiments/latency/run.sh my-change SOME_SETTING=value
python -m experiments.latency.report baseline my-change
```

---

## Chat with the agent (no phone call needed)

`POST /agent/turn` is the text-in/text-out harness every later module (evals,
module 7+) tests the agent against — the same tools, gate and prompt the
real call path uses (a call's tool-use loop is Pipecat's, but every tool call
goes through the same `loop.execute_tool`), driven over plain HTTP instead of
a phone call. Conversation history and the one `Call` row a session's tools
need are kept in memory, per `session_id`, for the life of the server process.
`DialogState` — what's been collected toward a booking, and whether it's been
confirmed — is different: it's persisted in Redis (`app/agent/store.py`, TTL
one hour), so it survives a server restart. `book_appointment` is gated on it:
the model calling that tool doesn't book anything by itself — the *second*
matching call, on a later turn, after the read-back, does. Needs Redis
running (`docker compose up -d redis`); see [docs/notes/conversation-state-machine.md](docs/notes/conversation-state-machine.md).

```bash
curl -X POST http://localhost:8000/agent/turn \
  -H 'content-type: application/json' \
  -d '{"session_id": "demo-1", "practice_id": 1, "message": "Do you have anything open Tuesday afternoon?"}'
```

Send another request with the same `session_id` to continue the conversation.
Response includes `prompt_version` (see `app/agent/prompts.py`) so a transcript
can always be traced back to the prompt that produced it.

To inspect a session's raw `DialogState` — intent, collected slots
(unmasked), confirmation status, turn count — set `ENVIRONMENT=development`
(the default in `.env.example`; production defaults to disabled) and:

```bash
curl http://localhost:8000/agent/sessions/demo-1
```

Dev-only on purpose: it's an unredacted dump of internal state, not something
to expose past a local machine.

For interactive manual testing from a terminal instead:

```bash
python -m app.agent.chat                  # demo practice, auto-created if needed
python -m app.agent.chat --practice-id 3   # a specific existing practice
```

---

## API

| Method | Endpoint | Description |
|---|---|---|
| GET | `/health` | Health check |
| GET | `/practices` | List practices (`skip` ≥ 0, `limit` 1–100) |
| POST | `/practices` | Create a new practice (`409` if the phone is already registered) |
| GET | `/practices/{practice_id}` | Get practice by ID |
| GET | `/calls` | List calls for a practice, newest first (`practice_id`, `skip`, `limit`) |
| POST | `/calls` | Record a new call (timestamps must include a timezone, e.g. `2026-09-15T10:00:00Z`) |
| GET | `/calls/{call_id}/transcript` | A call's turns (caller, assistant, tool), redacted |
| POST | `/agent/turn` | Chat with the agent (see above) |
| GET | `/agent/sessions/{id}` | That session's raw `DialogState` (dev-only, `404` unless `ENVIRONMENT=development`) |
| POST | `/rag/search` | Ranked knowledge-base chunks with scores for one practice (`mode`: hybrid, vector, keyword) |
| POST | `/twilio/inbound` | Twilio voice webhook: TwiML connecting the call to the stream |
| WS | `/twilio/ws` | Twilio Media Streams: the live call |
| POST | `/mcp` | MCP over streamable HTTP (`Authorization: Bearer <key>`) |

Interactive Swagger documentation: `http://localhost:8000/docs`  
ReDoc documentation: `http://localhost:8000/redoc`

---

## Tests

```bash
pytest                                   # the main suite (tests/)
pytest experiments                       # experiments' own tests (HL7 v2, Epic sandbox, raw media streams)
pytest tests/test_rag.py -v              # one file
pytest tests/test_api.py::test_create_practice -v
```

Unit tests use in-memory SQLite and stub API keys: no `.env`, network or
Docker needed. Integration tests run against the real thing and **skip** when
it isn't up:

- `test_fhir_scheduling.py` and the live part of `test_fhir_client.py`: HAPI (`docker compose up -d hapi`)
- `test_rag.py` search/ingest: Postgres with pgvector. It creates and drops its own `fde_rag_test` database and never touches the dev data.
- `test_restart.py`: real Redis. The other Redis tests use fakeredis.

No test calls Anthropic, Deepgram or Voyage.

---

## Migrations

```bash
# Generate migration from model changes
alembic revision --autogenerate -m "description"

# Apply all pending migrations
alembic upgrade head

# Roll back one migration (test round-trips on a throwaway database, not the dev DB)
alembic downgrade -1

# Check current migration revision
alembic current

# Show migration history
alembic history
```

---

## Database Management

The `postgres` service uses `pgvector/pgvector:pg16-trixie`: Postgres 16 with
pgvector, on the same Debian release as the official `postgres:16` image, so
existing data keeps its collation.

```bash
# Start database
docker compose up postgres -d

# Stop (data preserved in docker volume)
docker compose down

# Stop and delete data volume
docker compose down -v

# Connect to psql interactive shell
docker exec -it fde_postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'

# Back up before anything risky
docker exec fde_postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB"' > app/temp/backup-$(date +%Y%m%d).sql
```
