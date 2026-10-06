.PHONY: dev migrate test ngrok docker-build

dev:
	uv run uvicorn app.main:app --reload

migrate:
	uv run alembic upgrade head

test:
	uv run pytest tests/ -v

ngrok:
	ngrok http 8000 --url https://reword-finished-schnapps.ngrok-free.dev

docker-build:
	docker compose up -d --build
