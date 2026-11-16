MAKEFLAGS += --warn-undefined-variables

.ONESHELL:
.SHELLFLAGS := -o errtrace -o pipefail -o noclobber -o errexit -o nounset -c
SHELL := bash

PYTHON_VERSION := $(shell cat .python-version)
PYTHON ?= python$(PYTHON_VERSION)
RUFF_VERSION ?= 0.17.2
RUFF_CMD ?= uvx ruff@$(RUFF_VERSION)
PRE_COMMIT_VERSION ?= 4.7
PRE_COMMIT_CMD ?= uvx pre-commit@$(PRE_COMMIT_VERSION)

ARGS ?=


.DEFAULT_GOAL := help


##@ General


.PHONY: help
help: ## Display this help screen
	@awk 'BEGIN {FS = ":.*##"; printf "\nUsage:\n  make \033[36m<target>\033[0m\n"} \
		/^[a-zA-Z_0-9\-\\.%]+:.*?##/ { printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2 } \
		/^##@/ { printf "\n\033[1m%s\033[0m\n", substr($$0, 5) } ' $(MAKEFILE_LIST)


##@ Build


.PHONY: build
build: ## Build the sdist and wheel into dist/ (ARGS=... for uv build)
	uv build --no-managed-python $(ARGS)

.PHONY: install
install: ## Install or refresh the CLI via uv tool (ARGS=--editable for development)
	uv tool install --force --compile-bytecode --no-managed-python --python $(PYTHON_VERSION) . $(ARGS)


##@ Lint


.PHONY: lint
lint: lint.ruff lint.format lint.pre-commit ## Run all lint and formatting checks

.PHONY: lint.ruff
lint.ruff: ## Run Ruff (ARGS=... for extra check flags)
	$(RUFF_CMD) check . $(ARGS)

.PHONY: lint.format
lint.format: ## Check formatting without writing
	$(RUFF_CMD) format --check --diff .

.PHONY: lint.pre-commit
lint.pre-commit: ## Check repository metadata, workflows, and secrets
	$(PRE_COMMIT_CMD) run --all-files

.PHONY: lint.fix
lint.fix: format ## Format sources and apply safe Ruff fixes (ARGS=... for check flags)
	$(RUFF_CMD) check --fix . $(ARGS)


##@ Test


.PHONY: test
test: test.unittest ## Run tests without accessing the EC

.PHONY: test.unittest
test.unittest: ## Run unittest discovery (ARGS=... for discovery flags)
	PYTHONPATH=src $(PYTHON) -m unittest discover -s tests -p 'test_*.py' $(ARGS)


##@ Development


.PHONY: format
format: ## Format Python sources
	$(RUFF_CMD) format .
