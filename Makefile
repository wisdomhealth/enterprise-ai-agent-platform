.PHONY: install test lint typecheck test-integration test-e2e

install:
	cd backend && python -m pip install -e ".[dev]"
	cd frontend && npm install

test:
	cd backend && python -m pytest tests/unit -q
	cd frontend && npm test -- --run

lint:
	cd backend && python -m ruff check app tests
	cd frontend && npm run lint

typecheck:
	cd backend && python -m mypy app
	cd frontend && npm run typecheck

test-integration:
	cd backend && python -m pytest tests/integration -q

test-e2e:
	cd frontend && npm run test:e2e
