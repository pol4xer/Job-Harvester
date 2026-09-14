UV ?= uv
PYTHON_TARGETS := job_harvester scripts run.py tests

.PHONY: fix lint test build smoke check demo

fix:
	$(UV) run --frozen ruff check --fix $(PYTHON_TARGETS)
	$(UV) run --frozen ruff format $(PYTHON_TARGETS)
	$(MAKE) lint

lint:
	$(UV) run --frozen ruff check $(PYTHON_TARGETS)
	$(UV) run --frozen ruff format --check $(PYTHON_TARGETS)

test:
	PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 $(UV) run --frozen pytest

build:
	$(UV) run --frozen python -m build --wheel --no-isolation

smoke: build
	$(UV) run --frozen python scripts/smoke_wheel.py

check: lint test smoke

demo:
	$(UV) run --frozen job-harvester demo
