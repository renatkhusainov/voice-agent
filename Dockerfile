FROM python:3.12-slim

# Install uv binary
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Install dependencies using uv and uv.lock
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Copy application code
COPY . .

# Ensure the virtualenv is on PATH
ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000

# Apply pending migrations before starting the API
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
