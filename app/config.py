from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str
    postgres_user: str | None = None
    postgres_password: str | None = None
    postgres_db: str | None = None

    twilio_number: str
    twilio_account_sid: str
    twilio_auth_token: str

    deepgram_api_key: str

    anthropic_api_key: str

    # DialogState persistence (app/agent/store.py) — see docker-compose.yaml's
    # `redis` service. Defaults to localhost, the same as DATABASE_URL in
    # .env.example: running uvicorn on the host is the common local setup,
    # and docker compose sets REDIS_URL=redis://redis:6379/0 for the app
    # container itself (environment variables beat this default). The old
    # default, `redis:6379`, only resolves inside Docker and silenced a live
    # call run from the host.
    redis_url: str = "redis://localhost:6379/0"

    # Gates dev-only endpoints (GET /agent/sessions/{id} — app/routers/agent.py).
    # Fails *closed*: the gate is an allowlist ("only if == 'development'"),
    # not a denylist ("disabled only if == 'production'"), and the default
    # here is deliberately NOT "development" — an unconfigured deployment
    # that forgot to set this should not accidentally expose a raw internal-
    # state dump. Set ENVIRONMENT=development in .env to see it locally.
    environment: str = "production"

    # "Speak while working" (app/agent/live.py): if a tool call on a live call
    # is still running after TOOL_FILLER_DELAY_MS, say a short filler line so
    # the caller isn't left in silence. TOOL_FILLER_ENABLED exists for the A/B
    # in docs/notes/filler-ab.md — flip it, restart, make calls.
    tool_filler_enabled: bool = True
    tool_filler_delay_ms: int = 300

    # Tags every call_metrics row this process writes (app/services/latency.py),
    # so "before" and "after" runs of a latency change can be told apart in one
    # table. Set it per run, e.g. LATENCY_LABEL=baseline, then =prompt-cache.
    latency_label: str = "default"

    # Latency knobs, each measured in docs/notes/latency-budget.md. Defaults
    # are what ships; flip one per run and compare call_metrics by label.
    llm_prompt_caching: bool = False       # Anthropic prompt caching (Pipecat's enable_prompt_caching)
    stt_ttfs_p99_secs: float | None = None  # STT wait before releasing a turn; None = Pipecat's Deepgram default, 0.35 s. 0.25 measured: -20..60 ms, more cut-off turns; not shipped
    tts_text_aggregation: str = "sentence"  # "sentence" (Pipecat default) or "token": LLM tokens go to TTS unbuffered
    tts_flush_each_sentence: bool = True    # app/services/tts.py: speak each sentence as soon as it's written

    # MCP server (app/mcp_server.py, docs/mcp.md).
    # HTTP transport: API keys -> the one practice each may act for. Stored as
    # SHA-256 hex of the key, never the key itself: MCP_API_KEYS='{"<sha256>": 4}'.
    # Generate one with `python -m app.mcp_server new-key --practice-id 4`.
    mcp_api_keys: dict[str, int] = {}
    # stdio transport: no key (the client launches this process locally, as
    # the user), so the practice it acts for is set here instead.
    mcp_practice_id: int | None = None
    # Extra Host headers the HTTP transport accepts, e.g. an ngrok hostname.
    # Localhost is always allowed; anything else is rejected (DNS rebinding).
    mcp_allowed_hosts: list[str] = []

    # FHIR R4 server (app/fhir/client.py): HAPI from docker-compose.yaml.
    # localhost for a host run; compose sets http://hapi:8080/fhir for the app container.
    fhir_base_url: str = "http://localhost:8090/fhir"

    # Knowledge base (app/rag/, docs/notes/rag.md). Voyage embeddings; the
    # dimension is fixed in the schema (app/rag/embed.py DIM, migration).
    voyage_api_key: str | None = None
    embed_model: str = "voyage-4-lite"
    embed_timeout_secs: float = 10.0       # ingest: batches, retried
    embed_query_timeout_secs: float = 2.0  # a caller's question: one try, then keyword-only

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        # Never echo secret values from .env into validation error messages
        hide_input_in_errors=True,
    )


settings = Settings()