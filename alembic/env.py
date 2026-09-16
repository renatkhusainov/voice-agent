from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

# ── Import your models metadata and settings ──────────────────────────────────
from app.config import settings
from app.models.models import Base

# ── Alembic Config ────────────────────────────────────────────────────────────
config = context.config

# Point sqlalchemy.url at DATABASE_URL from .env
# ("%" is escaped because alembic.ini values go through ConfigParser interpolation)
config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))

# Interpret the config file for Python logging
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Metadata for autogenerate support
target_metadata = Base.metadata


# ── Run migrations ────────────────────────────────────────────────────────────
def run_migrations_offline() -> None:
    """Run migrations without a DB connection — outputs raw SQL."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations with a live DB connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
