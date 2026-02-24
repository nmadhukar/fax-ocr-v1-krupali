.PHONY: help install dev-install lint format test docker-up docker-down migrate celery-worker api-ingress api-review

# Default target
help:
	@echo "Healthcare Fax Processing System - Available Commands"
	@echo ""
	@echo "Setup:"
	@echo "  install         Install production dependencies"
	@echo "  dev-install     Install development dependencies"
	@echo ""
	@echo "Docker:"
	@echo "  docker-up       Start all services (Postgres, Redis, MinIO)"
	@echo "  docker-down     Stop all services"
	@echo "  docker-logs     View container logs"
	@echo ""
	@echo "Database:"
	@echo "  migrate         Run database migrations"
	@echo "  migrate-down    Rollback last migration"
	@echo ""
	@echo "Services:"
	@echo "  api-ingress     Start Fax Ingress API"
	@echo "  api-review      Start Fax Review API"
	@echo "  celery-worker   Start Celery worker"
	@echo "  all-services    Start all services"
	@echo ""
	@echo "Quality:"
	@echo "  lint            Run linter (ruff)"
	@echo "  format          Format code (black + isort)"
	@echo "  type-check      Run mypy type checking"
	@echo "  test            Run all tests"
	@echo "  test-unit       Run unit tests only"
	@echo "  test-integration Run integration tests only"

# Setup
install:
	pip install -r requirements.txt

dev-install:
	pip install -r requirements.txt
	pip install -e ".[dev]"

# Docker
docker-up:
	docker-compose -f infra/docker-compose.yml up -d

docker-down:
	docker-compose -f infra/docker-compose.yml down

docker-logs:
	docker-compose -f infra/docker-compose.yml logs -f

# Database
migrate:
	@echo "Running migrations..."
	python -m alembic -c infra/alembic.ini upgrade head

migrate-down:
	@echo "Rolling back last migration..."
	python -m alembic -c infra/alembic.ini downgrade -1

# Services
api-ingress:
	uvicorn services.fax_ingress_api.main:app --host 0.0.0.0 --port 8001 --reload

api-review:
	uvicorn services.fax_review_api.main:app --host 0.0.0.0 --port 8002 --reload

celery-worker:
	celery -A workers.fax_processing_worker.celery_app worker --loglevel=info

celery-beat:
	celery -A workers.fax_processing_worker.celery_app beat --loglevel=info

all-services:
	@echo "Starting all services..."
	make docker-up
	@sleep 5
	make migrate
	@echo "Services ready!"

# Quality
lint:
	ruff check libs services workers tests

format:
	black libs services workers tests
	isort libs services workers tests

type-check:
	mypy libs services workers

test:
	pytest tests/ -v

test-unit:
	pytest tests/unit/ -v

test-integration:
	pytest tests/integration/ -v

# Clean
clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	find . -type f -name "*.pyo" -delete
	find . -type d -name ".pytest_cache" -exec rm -rf {} +
	find . -type d -name ".mypy_cache" -exec rm -rf {} +
