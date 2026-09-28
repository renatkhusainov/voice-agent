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

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        # Never echo secret values from .env into validation error messages
        hide_input_in_errors=True,
    )


settings = Settings()