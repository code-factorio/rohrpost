.DEFAULT_GOAL := help

PY := uv run

.PHONY: help sync install format lint ty mypy pyright typecheck security structure \
       complexity test coverage mutation pre-commit clean check

help:  ## Show this help
	@awk 'BEGIN {FS = ":.*##"; printf "\nUsage:\n  make \033[36m<target>\033[0m\n\nTargets:\n"} \
	/^[a-zA-Z_-]+:.*?##/ { printf "  \033[36m%-13s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

sync:  ## Install/refresh the project and the dev toolchain (uv sync)
	uv sync

install:  ## Alias for `sync`
	uv sync

format:  ## Format the codebase with ruff
	$(PY) ruff format

lint:  ## Lint the codebase with ruff
	$(PY) ruff check

ty:  ## Type-check with ty
	$(PY) ty check src tests

mypy:  ## Type-check with mypy
	$(PY) mypy

pyright:  ## Type-check with pyright
	$(PY) pyright

typecheck:  ## Run all three type checkers (ty, mypy, pyright)
typecheck: ty mypy pyright

security:  ## Security lint with bandit
	$(PY) bandit -c pyproject.toml -r src

structure:  ## Structural analysis (DRY/YAGNI) with pyscn
	$(PY) pyscn check src

complexity:  ## Cyclomatic complexity & maintainability index (radon + xenon)
	$(PY) radon cc -a -s src
	$(PY) radon mi -s src
	$(PY) xenon --max-absolute B --max-modules A --max-average A src

test:  ## Run the test suite
	$(PY) pytest

coverage:  ## Run tests with coverage and emit term + html reports
	$(PY) pytest --cov=rohrpost --cov-report=term-missing --cov-report=html

mutation:  ## Mutation testing with mutmut (slow; not part of `make check`)
	$(PY) mutmut run

pre-commit:  ## Run every pre-commit hook against all files
	$(PY) pre-commit run --all-files

clean:  ## Remove caches and build artifacts
	rm -rf .mypy_cache .pytest_cache .ruff_cache .hypothesis htmlcov coverage.xml
	rm -rf .mutmut-cache mutants build dist
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

# The full deterministic gate: lint, types, security, structure, metrics, tests.
# Mutation testing is intentionally excluded (too slow for a default gate).
check:  ## Full deterministic gate (everything except mutation)
check: lint typecheck security structure complexity test
	@echo "all deterministic checks passed"
