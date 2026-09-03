.PHONY: install test lint typecheck test-integration test-e2e check-prometheus check-operability

PYTHON ?= $(shell \
	for candidate in python3.13 python3.12 python3; do \
		candidate_path=$$(command -v "$$candidate" 2>/dev/null) || continue; \
		if "$$candidate_path" -c 'import sys; raise SystemExit(not ((3, 12) <= sys.version_info[:2] < (3, 14)))' >/dev/null 2>&1; then \
			printf '%s' "$$candidate_path"; \
			break; \
		fi; \
	done)
BACKEND_VENV ?= $(CURDIR)/backend/.venv
BACKEND_PYTHON ?= $(BACKEND_VENV)/bin/python

install:
	@if [ ! -x "$(BACKEND_PYTHON)" ] || [ "$(origin PYTHON)" != "file" ]; then \
		if [ -z "$(PYTHON)" ] || ! "$(PYTHON)" -c 'import sys; raise SystemExit(not ((3, 12) <= sys.version_info[:2] < (3, 14)))' >/dev/null 2>&1; then \
			printf '%s\n' 'Compatible Python 3.12 or 3.13 is required. Set PYTHON=/path/to/python3.12-or-3.13.' >&2; \
			exit 1; \
		fi; \
	fi
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

check-prometheus:
	docker run --rm --entrypoint /bin/promtool \
		-v "$(CURDIR)/infra/prometheus:/etc/prometheus:ro" \
		prom/prometheus:v3.5.0 \
		check config /etc/prometheus/prometheus.yml

check-operability:
	scripts/check-operability --compose-file compose.yaml
