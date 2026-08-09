.PHONY: install test lint typecheck test-integration test-e2e

PYTHON ?= python3
BACKEND_VENV ?= $(CURDIR)/backend/.venv
BACKEND_PYTHON ?= $(BACKEND_VENV)/bin/python

install:
	@test -x "$(BACKEND_PYTHON)" || "$(PYTHON)" -m venv "$(BACKEND_VENV)"
	"$(BACKEND_PYTHON)" -m pip install -e "./backend[dev]"
	cd frontend && npm install

test:
	cd backend && "$(BACKEND_PYTHON)" -m pytest tests/unit -q
	cd frontend && npm test -- --run

lint:
	cd backend && "$(BACKEND_PYTHON)" -m ruff check app tests
	cd frontend && npm run lint

typecheck:
	cd backend && "$(BACKEND_PYTHON)" -m mypy app
	cd frontend && npm run typecheck

test-integration:
	cd backend && "$(BACKEND_PYTHON)" -m pytest tests/integration -q

test-e2e:
	cd frontend && npm run test:e2e
