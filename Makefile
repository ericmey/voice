# LiveKit voice — operational verbs.
#
# Prefer `make <target>` over invoking scripts/ directly; the Makefile is
# the stable public surface. Scripts can change; these names don't.

SHELL := /usr/bin/env bash

.PHONY: help bootstrap up down logs health test \
        build-agent deploy cycle \
        register-sip tail truncate-logs loki-smoke \
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
	  if docker exec voice-redis redis-cli ping >/dev/null 2>&1; then echo "ready"; break; fi; \
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

# ---- agents (docker compose) ---------------------------------------
#
# The four agents run as containers (voice-agent-<name>) from the shared
# voice-agent:latest image. docker-compose.agents.yaml has no build
# stanza, so build the image first, then bring the full stack up with
# both compose files. Run these on the agent host (mizuki).

# service.version on every span and metric. Baked into the image so it describes the
# code inside it. The retired launchd deploy derived this the same way; losing it made
# every trace report service.version=dev.
VOICE_SERVICE_VERSION ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo dev)

build-agent: ## Build the voice-agent:latest image from Dockerfile.agent
	docker build -f Dockerfile.agent \
		--build-arg VOICE_SERVICE_VERSION=$(VOICE_SERVICE_VERSION) \
		-t voice-agent:latest .

deploy: build-agent ## Build the image + bring up infra and the four agents
	docker compose -f docker-compose.yaml -f docker-compose.agents.yaml up -d

cycle: build-agent ## Rebuild the image + recreate the agent containers (picks up code changes)
	docker compose -f docker-compose.yaml -f docker-compose.agents.yaml up -d

# ---- observability -------------------------------------------------

tail: ## Follow all voice agent logs with color-coded prefix
	scripts/tail-logs.sh

truncate-logs: ## Zero out all agent logs (clean baseline for testing)
	scripts/truncate-logs.sh

loki-smoke: ## Query Grafana/Loki for post-smoke-test failures (requires GRAFANA_TOKEN; pass LOKI_ARGS="--since 5m")
	uv run python sdk/scripts/loki_smoke_check.py $${LOKI_ARGS:-}

# ---- Observability backend ----------------------------------------
#
# This project ships traces / logs / metrics over OTLP/HTTP to the
# configured OTLP backend (for example Grafana + Loki + Tempo + Mimir
# behind an OTel Collector). The collector is NOT in this compose — it
# runs externally (shiori.mey.house:4318 in this deployment). Configure
# the agents via VOICE_OTLP_ENDPOINT. See docs/OBSERVABILITY.md.

# ---- LangSmith provisioning (archived) — see docs/LANGSMITH.md.
# Kept around so a future operator can reactivate the LangSmith IaC
# (projects, datasets, evaluator config) without rebuilding it from
# scratch. The agent SDK no longer dual-exports to LangSmith;
# reactivation requires either reverting the enricher deletion or
# adding a fan-out OTel collector. Hidden from `make help` by
# omitting the leading `##`.

langsmith-plan-legacy:
	uv run python -m ops.langsmith.provision --dry-run

langsmith-provision-legacy:
	uv run python -m ops.langsmith.provision

# ---- tests ---------------------------------------------------------

test: ## Run pytest across all workspace members (sdk + tools + agents)
	@for d in sdk tools agents/nyla agents/aoi agents/yua agents/party; do \
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
