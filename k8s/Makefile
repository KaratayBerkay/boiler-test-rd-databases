# Kubernetes runtime for the rd-databases lab: local Harbor registry + k3d cluster + generated manifests.
# Run from rd-databases/k8s (or `make -C k8s <target>`). See ../docs/kubernetes.md.
SHELL := /usr/bin/env bash
ROOT  := $(abspath ..)
export FIREBIRD_CLIENT_LIB ?= /dev/null
UV    := uv run --project $(ROOT)/harness

.PHONY: help
help: ## show this help
	@echo "rd-databases on k3s -- one-time: env harbor push gen up"; echo
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-14s %s\n", $$1, $$2}'

.PHONY: env
env: ## create k8s/.env from the template (edit HARBOR_HOST after)
	@test -f .env || { cp .env.example .env && chmod 600 .env && echo "created k8s/.env -- set HARBOR_HOST to a LAN IP"; }

.PHONY: harbor
harbor: ## install + start the local Harbor registry, create project + robots
	@./harbor/up.sh

.PHONY: harbor-down
harbor-down: ## stop Harbor (keeps data)
	@./harbor/up.sh --down

.PHONY: push
push: ## mirror every stack image into Harbor
	@./push-images.sh

.PHONY: gen
gen: ## regenerate manifests + ports.json + k3d.yaml from the compose files
	@$(UV) python gen.py manifests
	@$(UV) python gen.py k3d

.PHONY: up
up: ## create the k3d cluster (wired to Harbor) and smoke-test a pull
	@./up.sh

.PHONY: recreate
recreate: ## delete + recreate the cluster (needed after ports.json changes)
	@./up.sh --recreate

.PHONY: down
down: ## delete the k3d cluster (Harbor keeps running)
	@./down.sh

.PHONY: deploy
deploy: ## deploy one or more stacks: make deploy STACKS="postgres clickhouse"
	@./scenarios.sh deploy $(STACKS)

.PHONY: status
status: ## show a stack's pods/services/pvcs: make status STACK=postgres
	@./scenarios.sh status $(STACK)

.PHONY: failover
failover: ## kill + self-heal a stack's primary pod: make failover STACK=postgres
	@./scenarios.sh failover $(STACK)

.PHONY: run
run: ## run harness phases against the cluster: make run STACK=postgres PHASES=load,bench SCALE=0.3
	@RDLAB_PLATFORM=k3s $(UV) rdlab run $(STACK) --no-up --keep --phases $(or $(PHASES),load,bench) --scale $(or $(SCALE),1)
