.PHONY: help install test lint demo-loop templates run clean consent-walk save-token rotate-decision-token rotate-now consent-check
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

consent-key:  ## Generate PAYPILOT_CONSENT_KEY and set it as a Fly secret (needs a terminal)
	@# THE VALUE IS NEVER PRINTED. It goes from the generator straight into
	@# flyctl in one shell variable and reaches no file, no screen and no
	@# transcript. A credential that appears in a transcript is burned and has
	@# to be rotated by a person, so this is a hard rule rather than a style.
	@#
	@# IT REFUSES RATHER THAN ROTATING. Re-keying the consent store makes every
	@# token already in it undecryptable, and undoing that is one consent
	@# journey per customer with their own bank.
	@#
	@# FAILS CLOSED ON NOT BEING ABLE TO CHECK. If flyctl cannot list secrets,
	@# this stops instead of setting one blind, because "the grep found nothing"
	@# and "the question was never asked" are different answers.
	@set -e; \
	existing=$$(flyctl secrets list 2>/dev/null) || { \
	  echo "refusing: cannot read the current secrets, so cannot tell whether a key already exists"; exit 2; }; \
	if printf '%s' "$$existing" | grep -q PAYPILOT_CONSENT_KEY; then \
	  echo "refusing: PAYPILOT_CONSENT_KEY already exists."; \
	  echo "  Rotating it makes every stored consent token undecryptable."; \
	  exit 1; \
	fi; \
	k=$$($(PY) -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'); \
	printf 'generated a %s character key. The value is not printed.\n' "$${#k}"; \
	flyctl secrets set PAYPILOT_CONSENT_KEY="$$k" >/dev/null; \
	unset k; \
	echo "set as a Fly secret; the app is restarting"
	@echo "--- waiting for the consent store to stop refusing ---"
	@for i in 1 2 3 4 5 6 7 8; do \
	  s=$$(curl -s -o /dev/null -w '%{http_code}' "https://paypilot.fly.dev/bank/consent/callback?cb=$$RANDOM"); \
	  echo "attempt $$i: /bank/consent/callback -> $$s"; \
	  [ "$$s" != "503" ] && { echo "the consent store is live"; exit 0; }; \
	  sleep 10; \
	done; \
	echo "still 503 after 80s; read the body to see which precondition it names" >&2; exit 1

consent-walk:  ## Walk the FAKE bank consent flow on the live host (needs DECISION_API_TOKEN)
	$(PY) scripts/consent_walk.py

save-token:  ## Save the decision API token via a hidden prompt (FORCE=1 to replace)
	$(PY) scripts/save_token.py $(if $(FORCE),--force)

rotate-decision-token:  ## Mint a new DECISION_API_TOKEN, set it on Fly, save it locally, never print it (ROTATE=1 to replace)
	$(PY) scripts/rotate_decision_token.py $(if $(ROTATE),--rotate)

rotate-now:  ## REPLACES the DECISION_API_TOKEN Fly secret with a new one, never printed (short form of ROTATE=1)
	$(PY) scripts/rotate_decision_token.py --rotate

consent-check:  ## Read back the consent the last walk filed (run after a redeploy to prove persistence)
	$(PY) scripts/consent_walk.py --check
