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
- **pytest + TestClient** — Automated test suite with in-memory SQLite isolation
- **Docker & Docker Compose** — Containerized local environment

---

## Project Structure

```text
fde-voice-agent/
├── app/
│   ├── config.py            # Pydantic Settings configuration from .env
│   ├── db.py                # SQLAlchemy engine, SessionLocal, and get_db dependency
│   ├── main.py              # FastAPI app initialization and route registration
│   ├── models/
│   │   └── models.py        # Database models (Practice, Call, Lead, Appointment)
│   ├── routers/
│   │   └── practice.py      # Practice and Call API endpoints
│   └── schemas/
│       └── practice.py      # Pydantic request/response validation schemas
├── alembic/
│   ├── env.py               # Alembic migration environment
│   ├── script.py.mako       # Migration template
│   └── versions/            # Database migration revision scripts
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

### 3. Start the database

```bash
# Start PostgreSQL service
docker compose up postgres -d
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

## API

| Method | Endpoint | Description |
|---|---|---|
| GET | `/health` | Health check |
| GET | `/practices` | List practices (`skip` ≥ 0, `limit` 1–100) |
| POST | `/practices` | Create a new practice (`409` if the phone is already registered) |
| GET | `/practices/{practice_id}` | Get practice by ID |
| GET | `/calls` | List calls for a practice, newest first (`practice_id`, `skip`, `limit`) |
| POST | `/calls` | Record a new call (timestamps must include a timezone, e.g. `2026-09-15T10:00:00Z`) |

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
