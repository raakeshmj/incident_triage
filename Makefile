.PHONY: install fmt lint typecheck test test-unit test-integration test-e2e \
        infra-up infra-down infra-logs migrate run-api run-worker send-alert

install:
	pip install -e ".[dev]"

fmt:
	ruff format .
	ruff check --fix .

lint:
	ruff check .
	ruff format --check .

typecheck:
	mypy

test:
	pytest

test-unit:
	pytest tests/unit -m "not integration and not e2e"

test-integration:
	pytest tests/integration -m integration

test-e2e:
	pytest tests/e2e -m e2e

infra-up:
	docker compose up -d
	python scripts/wait_for_services.py

infra-down:
	docker compose down -v

infra-logs:
	docker compose logs -f

migrate:
	alembic upgrade head

migrate-down:
	alembic downgrade -1

run-api:
	uvicorn apps.api.main:app --reload --port $${API_PORT:-8000}

run-worker:
	python -m apps.worker.main

run-consumer:
	python -m apps.worker.consumer_main

send-alert:
	python simulator/send_alert.py
