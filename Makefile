# CA-DNS developer entry points. Run `make` for the list of targets.

SHELL := /usr/bin/env bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

# Load .env (POSTGRES_*) and export it, so compose can interpolate variables.
-include .env
export

COMPOSE := docker compose

##@ Setup

.PHONY: tools
tools: ## Install dev tools for your user (uv, pre-commit, clang-format, dig)
	scripts/dev/install-tools.sh

.PHONY: env
env: ## Create .env from .env.example with a generated DB password
	@if [[ -f .env ]]; then echo ".env already exists, leaving it untouched"; exit 0; fi
	@sed "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$$(openssl rand -hex 24)|" .env.example > .env
	@echo "Created .env"

##@ Stack

.PHONY: up
up: ## Build and start all services
	$(COMPOSE) up -d --build --wait

.PHONY: down
down: ## Stop all services (keeps data volumes)
	$(COMPOSE) down

.PHONY: ps
ps: ## Show service status
	$(COMPOSE) ps

.PHONY: logs
logs: ## Follow logs (optionally: make logs s=postgres)
	$(COMPOSE) logs -f --tail=100 $(s)

.PHONY: psql
psql: ## Open a psql shell in the database
	$(COMPOSE) exec postgres psql -U "$(POSTGRES_USER)" -d "$(POSTGRES_DB)"

.PHONY: nuke
nuke: ## Stop services AND delete all data volumes (asks for confirmation)
	@read -r -p "Delete all CA-DNS data volumes? [y/N] " ans; [[ $$ans == y ]]
	$(COMPOSE) down -v

##@ Quality

.PHONY: lint
lint: ## Run all pre-commit hooks on the whole repo
	pre-commit run --all-files

##@ Help

.PHONY: help
help: ## Show this help
	@awk 'BEGIN {FS = ":.*##"; printf "Usage: make \033[36m<target>\033[0m\n"} \
		/^[a-zA-Z_-]+:.*##/ { printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2 } \
		/^##@/ { printf "\n\033[1m%s\033[0m\n", substr($$0, 5) }' $(MAKEFILE_LIST)
