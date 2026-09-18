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
env: ## Create .env from .env.example (non-secret settings)
	@if [[ -f .env ]]; then echo ".env already exists, leaving it untouched"; else cp .env.example .env; echo "Created .env"; fi
	@echo "Now run 'make secrets' to create the password files."

.PHONY: secrets
secrets: ## Create ./secrets/* (passwords and API tokens; taken from .env if still there)
	@# The directory is private (0700); the files must stay readable inside the
	@# containers, which run as other users (postgres, cadns) - compose can only
	@# set a secret's uid/gid/mode under Swarm.
	@mkdir -p secrets && chmod 700 secrets
	@set -Eeuo pipefail; \
	write_secret() { \
		file="secrets/$$1"; \
		if [[ -s $$file ]]; then echo "  kept       $$file"; return; fi; \
		if [[ -n $${2:-} ]]; then printf '%s' "$$2" > $$file; echo "  from .env  $$file"; \
		elif [[ $$3 == generate ]]; then openssl rand -hex 24 > $$file; echo "  generated  $$file"; \
		else : > $$file; echo "  empty      $$file (fill in when you have the credential)"; fi; \
		chmod 644 $$file; \
	}; \
	write_secret postgres_password "$${POSTGRES_PASSWORD:-}" generate; \
	write_secret cadns_dlz_password "$${CADNS_DLZ_PASSWORD:-}" generate; \
	write_secret cadns_app_password "$${CADNS_APP_PASSWORD:-}" generate; \
	write_secret watttime_password "$${CADNS_WATTTIME_PASSWORD:-}" optional; \
	write_secret ipinfo_token "$${CADNS_IPINFO_TOKEN:-}" optional
	@echo "Secrets live in ./secrets (gitignored; directory mode 700). Delete the password lines from .env."

.PHONY: _require-secrets
_require-secrets:
	@for f in postgres_password cadns_dlz_password cadns_app_password watttime_password ipinfo_token; do \
		[[ -f secrets/$$f ]] || { echo "error: secrets/$$f is missing - run 'make secrets'" >&2; exit 1; }; \
	done

##@ Stack

.PHONY: up
up: _require-secrets ## Build, apply migrations, and start all services
	@mkdir -p data/ipinfo
	$(COMPOSE) run --rm --build migrate
	$(COMPOSE) up -d --build --wait $$($(COMPOSE) config --services | grep -vx migrate)

.PHONY: down
down: ## Stop all services (keeps data volumes)
	$(COMPOSE) down

.PHONY: ps
ps: ## Show service status
	$(COMPOSE) ps

.PHONY: logs
logs: ## Follow logs (optionally: make logs s=postgres)
	$(COMPOSE) logs -f --tail=100 $(s)

.PHONY: migrate
migrate: _require-secrets ## Apply pending database migrations
	$(COMPOSE) run --rm --build migrate

.PHONY: seed
seed: _require-secrets ## Load demo data (db/seed) into the database
	$(COMPOSE) run --rm --build migrate seed

.PHONY: dig
dig: ## Query the resolver (make dig q="www.example.test AAAA")
	dig @$(or $(RESOLVER_LISTEN),127.0.0.1) $(q)

.PHONY: measure
measure: _require-secrets ## Measure one domain now and store the result (make measure d=www.youtube.com)
	@mkdir -p data/ipinfo
	$(COMPOSE) --profile tools run --rm --build cli measure $(d)

IPINFO_DB ?= ipinfo_location

.PHONY: ipinfo-db
ipinfo-db: ## Download an IPinfo MMDB into data/ipinfo (IPINFO_DB=ipinfo_location|ipinfo_core)
	@mkdir -p data/ipinfo
	@curl -fsSL -o data/ipinfo/$(IPINFO_DB).mmdb.tmp "https://ipinfo.io/data/$(IPINFO_DB).mmdb?token=$(CADNS_IPINFO_TOKEN)"
	@mv data/ipinfo/$(IPINFO_DB).mmdb.tmp data/ipinfo/$(IPINFO_DB).mmdb
	@ls -lh data/ipinfo/$(IPINFO_DB).mmdb

.PHONY: psql
psql: ## Open a psql shell in the database
	$(COMPOSE) exec postgres psql -U "$(POSTGRES_USER)" -d "$(POSTGRES_DB)"

.PHONY: nuke
nuke: ## Stop services AND delete all data volumes (asks for confirmation)
	@read -r -p "Delete all CA-DNS data volumes? [y/N] " ans; [[ $$ans == y ]]
	$(COMPOSE) down -v

##@ Quality

.PHONY: test
test: test-services test-integration ## Run all tests

.PHONY: test-services
test-services: _require-secrets ## Python services unit + DB tests (pytest args: make test-services a="-k watttime")
	$(COMPOSE) --profile test run --rm --build services-tests $(a)

.PHONY: test-integration
test-integration: _require-secrets ## Integration tests against the stack, except slow ones (make test-integration a="-k ttl")
	$(COMPOSE) --profile test run --rm --build tests $(or $(a),-m "not slow")

.PHONY: test-slow
test-slow: _require-secrets ## Slow end-to-end tests (minutes): active domains stay served
	$(COMPOSE) --profile test run --rm --build tests -m slow -v $(a)

.PHONY: lint
lint: ## Run all pre-commit hooks on the whole repo
	pre-commit run --all-files

##@ Help

.PHONY: help
help: ## Show this help
	@awk 'BEGIN {FS = ":.*##"; printf "Usage: make \033[36m<target>\033[0m\n"} \
		/^[a-zA-Z_-]+:.*##/ { printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2 } \
		/^##@/ { printf "\n\033[1m%s\033[0m\n", substr($$0, 5) }' $(MAKEFILE_LIST)
