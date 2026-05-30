.PHONY: install run-api run-admin run-clinician collect ingest-all reindex smoke-test test lint docker-up docker-down bootstrap clean

PYTHON := python
PIP := pip
UVICORN := uvicorn
STREAMLIT := streamlit

install:
	$(PIP) install -r requirements.txt

run-api:
	$(UVICORN) apps.api.main:app --host 0.0.0.0 --port 8000 --reload

run-admin:
	$(STREAMLIT) run apps/admin/streamlit_app.py --server.port 8501

run-clinician:
	$(STREAMLIT) run apps/clinician/streamlit_app.py --server.port 8502

collect:
	$(PYTHON) scripts/run_collectors.py

ingest-all:
	$(PYTHON) -m pipelines.run_all_ingestion

reindex:
	$(PYTHON) scripts/reindex.py

smoke-test:
	$(PYTHON) scripts/smoke_test.py

test:
	$(PYTHON) -m pytest tests/ -v --tb=short

lint:
	ruff check apps/ pipelines/ knowledge/ scripts/ tests/

docker-up:
	docker compose up -d

docker-down:
	docker compose down

bootstrap:
	bash scripts/bootstrap.sh

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".ruff_cache" -exec rm -rf {} + 2>/dev/null || true
	@echo "Clean complete."
