# OpenClaw LiveKit — operational verbs.
#
# Prefer `make <target>` over invoking scripts/ directly; the Makefile is
# the stable public surface. Scripts can change; these names don't.

SHELL := /usr/bin/env bash

.PHONY: help bootstrap up down logs health test \
        deploy teardown cycle \
        register-sip tail truncate-logs \
        sync-venvs lint typecheck verify

help: ## List the common verbs
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  \033[1;34m%-20s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# ---- first-time setup ----------------------------------------------

bootstrap: ## First-time machine setup (deps, config dir, venvs)
	scripts/bootstrap.sh

sync-venvs: ## Re-sync the root workspace venv (one .venv/ for sdk+tools+all agents)
	uv sync --all-groups

# ---- infrastructure ------------------------------------------------

up: ## docker compose up -d + auto-register SIP routing (idempotent self-heal)
	docker compose up -d
	@printf "[up] waiting for redis + livekit-server... "
	@for i in $$(seq 1 15); do \
	  if docker exec openclaw-redis redis-cli ping >/dev/null 2>&1; then echo "ready"; break; fi; \
	  sleep 1; \
	done
	@sleep 2
	@scripts/register-sip-routing.sh

down: ## docker compose down
	docker compose down

logs: ## docker compose logs -f (server + sip + redis)
	docker compose logs -f

health: ## Run the health-check script
	scripts/health-check.sh

# ---- SIP routing ---------------------------------------------------

register-sip: ## Register/refresh SIP trunk + dispatch rules from ./config/ (or $LIVEKIT_CONFIG_DIR)
	scripts/register-sip-routing.sh

# ---- agents --------------------------------------------------------

deploy: ## Render plists, install, kickstart (all three agents)
	scripts/deploy-agents.sh

teardown: ## Bootout and remove all agent plists
	scripts/teardown-agents.sh

cycle: ## Restart all three agents in place (picks up code changes)
	scripts/cycle-agents.sh

# ---- observability -------------------------------------------------

tail: ## Follow all three agent logs with color-coded prefix
	scripts/tail-logs.sh

truncate-logs: ## Zero out all agent logs (clean baseline for testing)
	scripts/truncate-logs.sh

# ---- tests ---------------------------------------------------------

test: ## Run pytest across all workspace members (sdk + tools + three agents)
	@for d in sdk tools agents/nyla agents/aoi agents/party; do \
	  echo ">> $$d"; \
	  (cd $$d && uv run pytest -q) || { code=$$?; [[ $$code == 5 ]] && echo "  (no tests)" || exit $$code; }; \
	done

# ---- static checks (pre-release gate) ------------------------------

lint: ## Run ruff (lint + format check). Clean exit = ready.
	uv run ruff check .
	uv run ruff format --check .

typecheck: ## Run pyright across sdk/tools/agents.
	uv run pyright

verify: lint typecheck test ## Lint + typecheck + tests. Green before human testing.
