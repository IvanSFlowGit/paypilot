#!/bin/sh
# Container start: migrate the ledger, then serve.
#
# WHY IT RUNS HERE AND NOT AS fly.toml's release_command. The ledger lives on a
# volume that is mounted when the machine runs. A release command runs on a
# temporary machine, and whether that machine can see the volume is not
# established for this app, so a migration there could run against nothing and
# report success. This script runs on the machine that owns the volume, after the
# old machine has released it, so there is no second writer.
#
# set -eu and no "|| true": a migration that refuses or cannot run must stop the
# container. THAT TAKES THE WHOLE APP DOWN, landing page included: a machine with a
# volume is updated in place, so there is no old version left serving. That is the
# loud outcome and it is chosen on purpose, because serving over an unmigrated
# file is the quiet one (every store path raises). Recovery is in README.md,
# "Rolling back a migration".
#
# --allow-create: on a brand new volume there is no file yet and the app would
# create the current schema itself on first use, so creating it here changes
# nothing. ASSUMPTION, NOT PROVED: uid 10001 can write to the volume root. Consent
# rows already persist on /data under that user, which is the evidence for it.
set -eu

python -m app.migrate_tenancy --allow-create

exec uvicorn app.api:app --host 0.0.0.0 --port 8000
