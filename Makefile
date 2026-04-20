.PHONY: help build up down logs test lint fmt shell db-shell redis-shell clean

# ── Colors ─────────────────────────────────────────────────────────────────────
GREEN  := \033[0;32m
YELLOW := \033[0;33m
RESET  := \033[0m

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "$(GREEN)%-20s$(RESET) %s\n", $$1, $$2}'

# ── Docker ─────────────────────────────────────────────────────────────────────
build: ## Build Docker images
	docker-compose build

up: ## Start all services
	docker-compose up -d
	@echo "$(GREEN)✅ Stack running$(RESET)"
	@echo "   API:    http://localhost:8000"
	@echo "   Docs:   http://localhost:8000/docs"
	@echo "   Flower: http://localhost:5555"

down: ## Stop all services
	docker-compose down

restart: ## Restart all services
	docker-compose restart

logs: ## Tail logs from all services
	docker-compose logs -f

logs-api: ## Tail API logs
	docker-compose logs -f api

logs-worker: ## Tail worker logs
	docker-compose logs -f worker

scale-workers: ## Scale workers (usage: make scale-workers N=4)
	docker-compose up -d --scale worker=$(N)

# ── Development ────────────────────────────────────────────────────────────────
dev: ## Run API in dev mode with hot-reload
	uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

worker-dev: ## Run Celery worker locally
	celery -A app.tasks.celery_app worker --loglevel=debug --concurrency=4

beat-dev: ## Run Celery beat locally
	celery -A app.tasks.celery_app beat --loglevel=info

flower-dev: ## Run Flower locally
	celery -A app.tasks.celery_app flower --port=5555

# ── Testing ────────────────────────────────────────────────────────────────────
test: ## Run full test suite
	pytest tests/ -v --cov=app --cov-report=term-missing

test-fast: ## Run tests without coverage
	pytest tests/ -v -x

test-unit: ## Run unit tests only
	pytest tests/ -v -k "not integration"

# ── Code quality ───────────────────────────────────────────────────────────────
lint: ## Run flake8 linter
	flake8 app/ tests/ --max-line-length=100 --ignore=E501,W503

fmt: ## Format code with black + isort
	black app/ tests/
	isort app/ tests/

typecheck: ## Run mypy type checker
	mypy app/ --ignore-missing-imports

# ── Database ───────────────────────────────────────────────────────────────────
db-migrate: ## Run Alembic migrations
	alembic upgrade head

db-rollback: ## Rollback last migration
	alembic downgrade -1

db-shell: ## Open psql shell
	docker-compose exec postgres psql -U postgres -d promptdb

db-reset: ## Drop and recreate database (DANGEROUS)
	docker-compose exec postgres psql -U postgres -c "DROP DATABASE IF EXISTS promptdb;"
	docker-compose exec postgres psql -U postgres -c "CREATE DATABASE promptdb;"

# ── Utilities ──────────────────────────────────────────────────────────────────
redis-shell: ## Open Redis CLI
	docker-compose exec redis redis-cli

shell: ## Open bash in API container
	docker-compose exec api bash

env: ## Copy .env.example to .env
	cp .env.example .env
	@echo "$(YELLOW)⚠️  Edit .env and add your API keys$(RESET)"

clean: ## Remove containers, volumes, and caches
	docker-compose down -v
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null; true
	find . -name "*.pyc" -delete
	rm -f test.db

# ── Quick test requests ────────────────────────────────────────────────────────
test-health: ## Hit the health endpoint
	curl -s http://localhost:8000/api/v1/health | python3 -m json.tool

test-submit: ## Submit a test prompt
	curl -s -X POST http://localhost:8000/api/v1/prompts \
		-H "Content-Type: application/json" \
		-d '{"prompt": "Explain the CAP theorem in one sentence", "provider": "anthropic"}' \
		| python3 -m json.tool

test-queue: ## Check queue stats
	curl -s http://localhost:8000/api/v1/queue/stats | python3 -m json.tool

test-cache: ## Check cache stats
	curl -s http://localhost:8000/api/v1/cache/stats | python3 -m json.tool
