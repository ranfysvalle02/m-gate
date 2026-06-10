# mdb-mcp-gateway — developer task runner.
#
# These targets mirror .github/workflows/ci.yml so you can reproduce the exact
# CI gate locally before pushing. The canonical pre-push command is `make ci`,
# which runs BOTH GitHub jobs end-to-end: the `quality` job (lint + format-check
# + types + coverage-gated unit tests) AND the `integration` job (the live
# Atlas Local + Ollama integration/load tier). A green `make ci` means a green
# cloud CI — so you catch failures here, not in the cloud.
#
# `make ci` runs the integration tier in STRICT mode: if Docker or Ollama is
# unavailable the tier FAILS instead of skipping, because a silent skip is just
# another way to miss the failure. Use `make check` for the fast inner loop, or
# `make quality` for the unit-only gate when you can't run the engine.

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

.PHONY: help install install-dev fetch-wasm lint format format-check typecheck \
        test test-cov test-integration test-integration-strict test-load \
        check quality ci precommit precommit-install clean

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

fetch-wasm: ## Download and verify pinned python.wasm runtime
	$(PYTHON) scripts/fetch_python_wasm.py

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

# `check` = fast inner loop; `quality` = the GitHub `quality` job; `ci` = the
# FULL gate (quality + integration/load), mirroring both GitHub jobs.
check: lint format-check typecheck test ## Fast local gate: lint + format + types + unit tests

quality: lint format-check typecheck test-cov ## Quality job only: lint + format + types + coverage-gated unit

ci: quality test-integration-strict ## FULL CI gate: quality + integration/load tier (needs Docker + Ollama)

## ---- integration tier (needs Docker + Ollama) ----------------------------

test-integration: ## Integration tier — testcontainers spins up Atlas Local (needs Docker + Ollama)
	$(PYTEST) -q -m "$(INTEGRATION_MARKERS)"

test-integration-strict: ## Integration tier where missing Docker/Ollama is a hard FAIL (used by `make ci`)
	INTEGRATION_STRICT=1 $(PYTEST) -q -m "$(INTEGRATION_MARKERS)"

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
