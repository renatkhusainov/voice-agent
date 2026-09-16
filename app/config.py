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

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        # Never echo secret values from .env into validation error messages
        hide_input_in_errors=True,
    )


settings = Settings()