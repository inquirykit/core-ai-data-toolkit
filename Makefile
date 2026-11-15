# Day-to-day commands; `make` alone lists them. Local work needs uv
# (https://docs.astral.sh/uv/) and, for the browser pages, Node 22.12+.
.DEFAULT_GOAL := help
# Podman: make up COMPOSE="podman compose"
COMPOSE ?= docker compose

help:  ## List these commands
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-10s %s\n", $$1, $$2}'

install:  ## Python dependencies into .venv (from uv.lock) and the browser pages
	uv sync
	cd frontend && npm ci && npm run build

run:  ## Start the server on :8000 (first start logs the setup wizard's link)
	uv run main.py

dev:  ## Start the server with auto-reload
	RELOAD=true uv run main.py

test:  ## Unit tests, Python and frontend (no model downloads, no torch needed)
	uv run pytest
	cd frontend && npm test

lint:  ## Unused imports and variables, undefined names (ruff); the wiring's types (pyright on api/app.py); the frontend's types (tsc)
	uv run ruff check .
	uvx pyright==1.1.414 --pythonpath "$$(uv run python -c 'import sys; print(sys.executable)')" api/app.py
	cd frontend && npm run typecheck

lock:  ## After editing pyproject.toml: refresh uv.lock and the requirements.txt the image installs
	uv lock
	uv export --no-dev --no-hashes --no-emit-project -o requirements.txt

up:  ## Build the image and start the container
	$(COMPOSE) up --build -d

down:  ## Stop the container (volumes with weights and settings are kept)
	$(COMPOSE) down

logs:  ## Follow the container's logs
	$(COMPOSE) logs -f

zip:  ## Zip the project's files (what git tracks, no .env or data/) into dist/
	scripts/make_zip.sh

.PHONY: help install run dev test lint lock up down logs zip
