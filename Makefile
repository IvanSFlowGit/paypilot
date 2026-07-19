.PHONY: help install test lint demo-loop templates run clean
.DEFAULT_GOAL := help

PY ?= .venv/bin/python

help:  ## Show the available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  %-14s %s\n", $$1, $$2}'

install:  ## Create the venv and install pinned dependencies
	python3 -m venv .venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r requirements.txt

test:  ## Run the full offline suite (no key, no network)
	$(PY) -m pytest -q

lint:  ## Style gates: no em/en dashes, no undeclared inference call sites
	$(PY) scripts/lint_style.py
	$(PY) -m pytest tests/test_zero_token.py -q

demo-loop:  ## End-to-end proof against Stripe TEST mode (needs STRIPE_API_KEY)
	@echo "Running the full fail -> dun -> recover cycle against Stripe test mode."
	@echo "Sending is a dry run unless PAYPILOT_SEND_EMAIL=1 and the address is allowlisted."
	$(PY) scripts/demo_loop.py --email "$${PAYPILOT_DEMO_EMAIL:?set PAYPILOT_DEMO_EMAIL to the inbox you want the dunning email in}"

templates:  ## Regenerate dunning copy as a DRAFT for human review (never publishes)
	$(PY) scripts/generate_templates.py

run:  ## Serve the app locally on :8000
	.venv/bin/uvicorn app.api:app --reload

clean:  ## Remove the demo ledger and caches
	rm -f data/demo-loop.db data/demo-loop.db-wal data/demo-loop.db-shm
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -rf .pytest_cache
