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
	git config core.hooksPath .githooks

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

hooks:  ## Install git hooks (strips Claude co-author trailers). Run once per clone.
	git config core.hooksPath .githooks
	@echo "hooks installed: $$(git config core.hooksPath)"

deck:  ## Render the investor deck to a PDF (needs a shell OUTSIDE the agent sandbox)
	@# Chrome cannot start under the agent sandbox: it is denied the Mach
	@# bootstrap and unix socket creation, so it aborts on
	@# "Failed to create a ProcessSingleton for your profile directory"
	@# whatever --user-data-dir is set to. Measured twice on 2026-10-17.
	@# The content gates in md_to_pdf.py DO run there; only the render needs you.
	python3 $(HOME)/streamflow-private/oskar-classifier/tools/md_to_pdf.py \
	  docs/deck-2026-11-investor.md \
	  -o docs/paypilot-deck-2026-11.pdf \
	  --title "PayPilot investor deck, November 2026"

deploy:  ## Deploy to Fly tagging the image with this commit (needs a terminal)
	@# flyctl cannot authenticate from a sandboxed agent session: it fails with
	@# "tls: failed to verify certificate: x509: OSStatus -26276", the same
	@# error gh and pip give there. Your own shell is not sandboxed.
	@# Once FLY_API_TOKEN is a repo secret, a push to main deploys and this
	@# target is only for deploying ahead of a push.
	@git diff --quiet || { echo "refusing: working tree is dirty, so the commit tag would name a build that is not this one"; exit 1; }
	@git fetch -q origin && git merge-base --is-ancestor HEAD origin/main || { \
	  echo "refusing: HEAD is not on origin/main, so /health would report a commit nobody can fetch"; exit 1; }
	flyctl deploy --build-arg PAYPILOT_COMMIT=$$(git rev-parse HEAD)
	@echo "--- verifying the hostname serves this commit ---"
	@want=$$(git rev-parse HEAD); \
	for i in 1 2 3 4 5 6; do \
	  got=$$(curl -fsS "https://paypilot.fly.dev/health?cb=$$RANDOM" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("commit",""))' 2>/dev/null || echo ""); \
	  echo "attempt $$i: serving '$$got'"; \
	  [ "$$got" = "$$want" ] && { echo "live on $$want"; exit 0; }; \
	  sleep 10; \
	done; \
	echo "FAIL: not serving $$want" >&2; exit 1
