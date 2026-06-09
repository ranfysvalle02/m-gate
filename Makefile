# mdb-mcp-gateway — developer task runner.
#
# These targets mirror .github/workflows/ci.yml so you can reproduce the exact
# CI gate locally before pushing. The canonical pre-push command is `make ci`,
# which runs the same lint + format-check + types + coverage-gated unit tests
# that the `quality` job runs on GitHub.

# Use the project venv's tools if present, else fall back to PATH.
VENV       ?= .venv
BIN        := $(VENV)/bin
PYTHON     := $(if $(wildcard $(BIN)/python),$(BIN)/python,python)
PIP        := $(PYTHON) -m pip
RUFF       := $(if $(wildcard $(BIN)/ruff),$(BIN)/ruff,ruff)
MYPY       := $(if $(wildcard $(BIN)/mypy),$(BIN)/mypy,mypy)
PYTEST     := $(PYTHON) -m pytest

# Coverage floor — keep in lockstep with the CI `--cov-fail-under` value.
COV_MIN    ?= 82

# Marker expressions (match the pytest markers declared in pyproject.toml).
UNIT_MARKERS        := not integration and not load
INTEGRATION_MARKERS := integration or load

.DEFAULT_GOAL := help

.PHONY: help install install-dev lint format format-check typecheck \
        test test-cov test-integration test-load check ci precommit \
        precommit-install clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| sort \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

## ---- setup ---------------------------------------------------------------

install: ## Install runtime dependencies
	$(PIP) install -r requirements.txt

install-dev: ## Install dev + runtime dependencies (lint, types, test tooling)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements-dev.txt

## ---- quality gate (mirrors CI `quality` job) -----------------------------

lint: ## Ruff lint check
	$(RUFF) check .

format: ## Ruff auto-format the tree (writes changes)
	$(RUFF) format .

format-check: ## Ruff format check (no writes) — what CI runs
	$(RUFF) format --check .

typecheck: ## Mypy static type check
	$(MYPY) .

test: ## Run the offline unit tier (no external services)
	$(PYTEST) -q -m "$(UNIT_MARKERS)"

test-cov: ## Unit tier with coverage gate (exact CI command)
	$(PYTEST) -q -m "$(UNIT_MARKERS)" \
		--cov --cov-report=term-missing --cov-fail-under=$(COV_MIN)

# `check` is the fast inner-loop gate; `ci` is the full quality job CI runs.
check: lint format-check typecheck test ## Fast local gate: lint + format + types + unit tests

ci: lint format-check typecheck test-cov ## Full CI quality gate (lint + format + types + coverage)

## ---- integration tier (needs Docker + Ollama) ----------------------------

test-integration: ## Integration tier — testcontainers spins up Atlas Local (needs Docker + Ollama)
	$(PYTEST) -q -m "$(INTEGRATION_MARKERS)"

test-load: ## Load/concurrency tier only
	$(PYTEST) -q -m "load"

## ---- pre-commit & housekeeping -------------------------------------------

precommit-install: ## Install the pre-commit git hooks
	$(BIN)/pre-commit install

precommit: ## Run all pre-commit hooks against the whole tree
	$(BIN)/pre-commit run --all-files

clean: ## Remove caches and coverage artifacts
	rm -rf .pytest_cache .ruff_cache .mypy_cache .coverage htmlcov coverage.xml
	find . -type d -name __pycache__ -not -path './.venv/*' -prune -exec rm -rf {} +
