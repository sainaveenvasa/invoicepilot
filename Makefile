# Developer commands. Run `make` or `make help` for the list.
# Host commands load .env when it exists (uv run --env-file).

UV ?= uv
ENV_FILE := $(if $(wildcard .env),--env-file .env,)
RUN := $(UV) run $(ENV_FILE)
MANAGE := $(RUN) python backend/manage.py
CELERY := $(RUN) celery --workdir backend -A config
COMPOSE ?= docker compose

.DEFAULT_GOAL := help
.PHONY: help install env run migrate makemigrations superuser shell worker beat ping test lint format check ci \
	up down logs worker-logs compose-ping compose-test compose-shell

help: ## Show this help
	@grep -hE '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# --- Setup ---------------------------------------------------------------------

install: ## Install dependencies from uv.lock and the git pre-commit hooks
	$(UV) sync --locked
	$(UV) run pre-commit install

env: ## Create .env from .env.example (never overwrites)
	@test -f .env && echo ".env already exists" || (cp .env.example .env && echo "Created .env")

# --- Running on your machine (needs PostgreSQL 17 on POSTGRES_HOST:POSTGRES_PORT) ---

run: migrate ## Migrate, then start the dev server on http://localhost:8000
	$(MANAGE) runserver

migrate: ## Apply migrations and create the cache table
	$(MANAGE) migrate
	$(MANAGE) createcachetable

makemigrations: ## Create migrations for model changes
	$(MANAGE) makemigrations

superuser: ## Create an admin user
	$(MANAGE) createsuperuser

shell: ## Django shell
	$(MANAGE) shell

worker: ## Start a Celery worker on every queue (needs Redis on REDIS_URL)
	$(CELERY) worker --concurrency 4

beat: ## Start Celery beat. Run exactly one: two would double every schedule
	$(CELERY) beat

ping: ## Queue the safe test task; the worker logs "pong"
	$(CELERY) call core.ping

# --- Quality gates (the same ones CI runs) ---------------------------------------

test: ## Run the test suite (PostgreSQL required)
	$(RUN) pytest

lint: ## Ruff lint and format check
	$(UV) run ruff check .
	$(UV) run ruff format --check .

format: ## Auto-fix lint issues and format
	$(UV) run ruff check --fix .
	$(UV) run ruff format .

check: ## Django system checks and the missing-migrations check
	$(MANAGE) check --fail-level WARNING
	$(MANAGE) makemigrations --check --dry-run

ci: lint check test ## Everything CI runs, locally

# --- Docker Compose ----------------------------------------------------------------

up: ## Start Postgres, Redis, Mailpit, the web app, a worker and beat (http://localhost:8000, mail on :8025)
	$(COMPOSE) up --build -d

down: ## Stop the Compose stack (data volume is kept)
	$(COMPOSE) down

logs: ## Follow the web app's logs
	$(COMPOSE) logs -f web

worker-logs: ## Follow the worker's and beat's logs
	$(COMPOSE) logs -f worker beat

compose-ping: ## Queue the safe test task from inside the worker container
	$(COMPOSE) exec worker celery -A config call core.ping

compose-test: ## Run the test suite inside the web container
	$(COMPOSE) run --rm web pytest -p no:cacheprovider

compose-shell: ## Django shell inside the web container
	$(COMPOSE) run --rm web python manage.py shell
