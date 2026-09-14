# CA-DNS developer entry points. Run `make` for the list of targets.

SHELL := /usr/bin/env bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

# Load .env (VM_SSH, DOCKER_CONTEXT, POSTGRES_*) and export it, so the docker
# CLI picks up DOCKER_CONTEXT and compose can interpolate variables.
-include .env
export

COMPOSE := docker compose

##@ Setup

.PHONY: bootstrap-mac
bootstrap-mac: ## Install Mac-side tools (docker CLI, uv, pre-commit, ...)
	brew bundle --file scripts/dev/Brewfile
	@# Homebrew's compose/buildx plugins are only found if docker is told where they live.
	@if ! docker compose version >/dev/null 2>&1; then \
		if [[ -f ~/.docker/config.json ]]; then \
			echo "Add \"cliPluginsExtraDirs\": [\"$$(brew --prefix)/lib/docker/cli-plugins\"] to ~/.docker/config.json" >&2; exit 1; \
		fi; \
		mkdir -p ~/.docker; \
		printf '{\n  "cliPluginsExtraDirs": ["%s/lib/docker/cli-plugins"]\n}\n' "$$(brew --prefix)" > ~/.docker/config.json; \
	fi
	docker compose version
	pre-commit install

.PHONY: env
env: ## Create .env from .env.example with a generated DB password
	@if [[ -f .env ]]; then echo ".env already exists, leaving it untouched"; exit 0; fi
	@sed "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$$(openssl rand -hex 24)|" .env.example > .env
	@echo "Created .env; set VM_SSH to your VM's user@host"

.PHONY: vm-copy-key
vm-copy-key: _require-vm ## Install your SSH public key on the VM (asks for the VM password)
	ssh-copy-id $(VM_SSH)

.PHONY: vm-bootstrap
vm-bootstrap: _require-vm ## Set up Docker on the VM, free port 53 (sudo password; GROW_ROOT=1 grows disk)
	scp scripts/dev/bootstrap-vm.sh $(VM_SSH):/tmp/cadns-bootstrap-vm.sh
	ssh -t $(VM_SSH) 'sudo GROW_ROOT=$(or $(GROW_ROOT),0) bash /tmp/cadns-bootstrap-vm.sh && rm /tmp/cadns-bootstrap-vm.sh'

.PHONY: context
context: _require-vm ## Create the docker context that points at the VM
	@if docker context inspect $(DOCKER_CONTEXT) >/dev/null 2>&1; then \
		echo "docker context '$(DOCKER_CONTEXT)' already exists"; \
	else \
		DOCKER_CONTEXT=default docker context create $(DOCKER_CONTEXT) \
			--description "CA-DNS dev VM" --docker host=ssh://$(VM_SSH); \
	fi
	docker version --format 'Engine {{.Server.Version}} on {{.Server.Os}}/{{.Server.Arch}}'

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
	@read -r -p "Delete all CA-DNS data volumes on $(DOCKER_CONTEXT)? [y/N] " ans; [[ $$ans == y ]]
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

.PHONY: _require-vm
_require-vm:
	@if [[ -z "$${VM_SSH:-}" || "$${VM_SSH}" == user@vm-host ]]; then \
		echo "error: set VM_SSH in .env (run 'make env' first)" >&2; exit 1; fi
