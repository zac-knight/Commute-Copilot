collect:  ## poll TransLink realtime every 60 s into backend/data/analytics.sqlite (Ctrl-C to stop)
	cd backend && uv run python -m app.analytics.collector

.PHONY: collect start start-demo setup dev test lint eval eval-live ui e2e demo

BACKEND = cd backend &&

start:  ## run API + web UI together (needs Ollama); opens the browser
	./start.sh

start-demo:  ## same, in demo mode (no model needed)
	./start.sh demo

setup:  ## install deps and git hooks
	$(BACKEND) uv sync
	cd frontend && npm install
	$(BACKEND) uv run pre-commit install --config ../.pre-commit-config.yaml

dev:  ## run the API on :8000 with reload
	$(BACKEND) uv run uvicorn app.main:app --reload --port 8000

test:  ## backend + frontend unit/integration tests (no network, no live LLM)
	$(BACKEND) uv run pytest --cov=app.core --cov=app.agents --cov=app.orchestrator --cov=app.main --cov-report=term-missing
	cd frontend && npm test

lint:  ## ruff + mypy (strict on app.core and app.agents) + tsc + eslint
	$(BACKEND) uv run ruff check .
	$(BACKEND) uv run ruff format --check .
	$(BACKEND) uv run mypy app
	cd frontend && npm run typecheck && npm run lint

eval:  ## deterministic eval suites (no model, no network) + regenerate docs/evaluation.md
	@cd backend && uv run python -m evals.run_eval det; s=$$?; uv run python -m evals.run_eval report; exit $$s

MODELS ?= qwen3:8b
eval-live:  ## live evals against Ollama (slow, manual): MODELS='qwen3:8b qwen3:14b' make eval-live
	@cd backend && for m in $(MODELS); do \
	  uv run python -m evals.run_eval router --model $$m && \
	  uv run python -m evals.run_eval e2e --model $$m && \
	  uv run python -m evals.run_eval judge --model $$m || exit 1; \
	done; uv run python -m evals.run_eval det; uv run python -m evals.run_eval report

ui:  ## frontend dev server on :5173 (proxies /api to :8000; run `make dev` in another terminal)
	cd frontend && npm run dev

demo:  ## DEMO=1 backend (recorded answers, fake calendar, no model needed) on :8000
	cd backend && DEMO=1 uv run uvicorn app.main:app --port 8000

e2e:  ## Playwright end-to-end test against demo mode (starts its own servers)
	cd frontend && npx playwright test
