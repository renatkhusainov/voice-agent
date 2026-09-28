# FDE Voice Agent

AI-powered voice agent for dental practices. Handles inbound calls, captures leads, and books appointments.

---

## Stack

- **FastAPI** — Modern Python web framework for building APIs
- **PostgreSQL 16** — Primary relational database
- **SQLAlchemy 2.0** — SQL toolkit and ORM
- **Alembic** — Database schema migrations
- **Pydantic / Pydantic Settings** — Data validation and environment settings
- **Pipecat** — Voice pipeline (Twilio + Deepgram + Anthropic)
- **Redis** — `DialogState` persistence for the agent's confirmation gate (`app/agent/store.py`)
- **pytest + TestClient** — Automated test suite with in-memory SQLite isolation
- **Docker & Docker Compose** — Containerized local environment

---

## Project Structure

```text
fde-voice-agent/
├── app/
│   ├── config.py            # Pydantic Settings configuration from .env
│   ├── db.py                # SQLAlchemy engine, SessionLocal, and get_db dependency
│   ├── main.py              # FastAPI app initialization, route registration, log scrubbing setup
│   ├── agent/
│   │   ├── tools.py         # The five tools (check_availability, book_appointment, ...) + JSON schemas generated from their Pydantic models
│   │   ├── schema.py        # schema_from_model(): Pydantic model -> tool JSON schema, never hand-written twice
│   │   ├── loop.py          # run_agent_loop(): the tool-use loop against Anthropic's Messages API
│   │   ├── prompts.py       # Versioned system prompt (PROMPT_VERSION) + build_system_prompt(practice, state)
│   │   ├── state.py         # DialogState + the confirmation gate: book_appointment can't execute on a prompt's say-so
│   │   ├── store.py         # Redis-backed DialogState persistence — get/set, JSON, TTL one hour
│   │   ├── session.py       # Per-session conversation (in-memory) + DialogState (Redis) — /agent/turn and chat.py share this
│   │   ├── live.py          # The same tools + gate on a live call: Pipecat handlers, per-call state, filler, escalation
│   │   ├── spoken.py        # Times/digits formatted for TTS: "9:30 AM Tuesday the 15th", "0 1 4 2"
│   │   └── chat.py          # `python -m app.agent.chat` — terminal REPL against the real agent loop
│   ├── phi/
│   │   ├── redact.py        # redact(): masks phone numbers, SSNs, DOB-shaped dates before storage
│   │   └── log_scrub.py     # Keeps PHI and secrets out of loguru and stdlib logs
│   ├── models/
│   │   └── models.py        # Database models (Practice, Call, Lead, Appointment)
│   ├── routers/
│   │   ├── agent.py         # POST /agent/turn — text in, text out, conversation kept per session_id
│   │   ├── practice.py      # Practice and Call API endpoints
│   │   └── twilio.py        # Twilio webhook + Media Streams WebSocket (the real call path)
│   └── schemas/
│       └── practice.py      # Pydantic request/response validation schemas
├── alembic/
│   ├── env.py               # Alembic migration environment
│   ├── script.py.mako       # Migration template
│   └── versions/            # Database migration revision scripts
├── docs/
│   ├── hipaa-design.md      # BAA map, PHI classification, retention policy, what production requires
│   ├── phi-and-secrets.md   # Implementation detail: Deepgram redaction, logging policy, audit
│   └── notes/
│       ├── raw-media-streams.md         # Hand-rolled Twilio Media Streams protocol: what a framework buys you
│       ├── latency-baseline.md          # Five real LLM+TTS response-latency measurements against live APIs
│       ├── conversation-state-machine.md  # greeting -> intent -> collecting -> confirming -> done | escalated
│       ├── live-call-tools.md           # Tools on a live call: flow, decisions, what's verified
│       └── filler-ab.md                 # "Let me check that for you" A/B protocol + results table
├── tests/
│   ├── conftest.py          # Pytest fixtures and DB session overrides
│   └── test_api.py          # API and model test suite
├── .dockerignore            # Keeps .env, .venv, node_modules out of the image
├── .env.example             # Template for environment variables
├── alembic.ini              # Alembic configuration
├── Dockerfile               # Docker container definition for the API
├── docker-compose.yaml      # Docker Compose setup (PostgreSQL + App)
├── pytest.ini               # Pytest configuration and warning filters
├── requirements.txt         # Project dependencies
└── README.md
```

---

## Run

### 1. Clone and set up virtual environment

```bash
git clone <repo>
cd fde-voice-agent

python3.12 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
```

Then fill in the real values. All of these are required — the app will not start without them:

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | PostgreSQL connection string for local runs |
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | Used by docker compose for the database container |
| `TWILIO_NUMBER`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` | Twilio telephony |
| `DEEPGRAM_API_KEY` | Deepgram speech-to-text |
| `ANTHROPIC_API_KEY` | Anthropic LLM |

### 3. Start the database (and Redis, for the agent harness)

```bash
# PostgreSQL — required
docker compose up postgres -d

# Redis — required for POST /agent/turn and python -m app.agent.chat
# (app/agent/store.py); not needed just to run the API/migrations.
docker compose up redis -d
```

### 4. Run migrations

```bash
alembic upgrade head
```

### 5. Start the server

```bash
uvicorn app.main:app --reload
```

Server is running at `http://localhost:8000`

---

## Docker Compose (Full Stack)

To run both PostgreSQL and the FastAPI application in containers:

```bash
# Build and start all services (the app container applies migrations on startup)
docker compose up -d --build

# View logs
docker compose logs -f
```

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
| POST | `/agent/turn` | Chat with the agent — see above |
| GET | `/agent/sessions/{id}` | That session's raw `DialogState` (dev-only, `404` unless `ENVIRONMENT=development`) |

Interactive Swagger documentation: `http://localhost:8000/docs`  
ReDoc documentation: `http://localhost:8000/redoc`

---

## Tests

```bash
# Run all tests
pytest tests/ -v

# Run specific test file
pytest tests/test_api.py -v

# Run specific test
pytest tests/test_api.py::test_create_practice -v
```

> **Note**: Tests use an isolated SQLite in-memory database and stub API keys — no `.env`, external database, or Docker container required.

---

## Migrations

```bash
# Generate migration from model changes
alembic revision --autogenerate -m "description"

# Apply all pending migrations
alembic upgrade head

# Roll back one migration
alembic downgrade -1

# Check current migration revision
alembic current

# Show migration history
alembic history
```

---

## Database Management

```bash
# Start database
docker compose up postgres -d

# Stop (data preserved in docker volume)
docker compose down

# Stop and delete data volume
docker compose down -v

# Connect to psql interactive shell
docker exec -it fde_postgres psql -U fde -d fde_db
```
