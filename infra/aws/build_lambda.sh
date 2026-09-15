#!/usr/bin/env bash
# Build infra/aws/build/lambda.zip for the decision slice.
#
# The zip holds exactly: the decision-slice modules (including the bootstrap
# function that owns the schema and the app role), the Postgres schema,
# pg8000 and its pure-Python dependencies, and the RDS CA bundle the client
# verifies the database certificate against. Nothing from the LangGraph side.
#
# Asserts the outcome rather than trusting exit codes: the zip must contain the
# handler and the CA bundle, and must NOT contain app/nodes.py or langchain.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
BUILD="$HERE/build"
STAGE="$BUILD/stage"
ZIP="$BUILD/lambda.zip"
PY="${PY:-$REPO/.venv/bin/python}"
CA_URL="https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem"

rm -rf "$STAGE" "$ZIP"
mkdir -p "$STAGE/app"

cp "$REPO/app/__init__.py" \
   "$REPO/app/decision.py" \
   "$REPO/app/decision_audit.py" \
   "$REPO/app/lambda_handler.py" \
   "$REPO/app/decision_bootstrap.py" \
   "$REPO/app/decision_schema.sql" \
   "$STAGE/app/"

# --require-hashes: every wheel must match the sha256 pinned in the requirements
# file. --only-binary: no sdist, so no build step runs third-party code here.
"$PY" -m pip install --quiet --no-compile --require-hashes --only-binary=:all: \
  --target "$STAGE" -r "$HERE/requirements-lambda.txt"

curl -fsS "$CA_URL" -o "$STAGE/rds-ca.pem"
# A failed download that still wrote something (an error page) must not ship.
grep -q "BEGIN CERTIFICATE" "$STAGE/rds-ca.pem" || { echo "CA bundle is not a PEM" >&2; exit 1; }

find "$STAGE" -name "__pycache__" -type d -prune -exec rm -rf {} +
# Fixed timestamps so an unchanged build hashes the same and Terraform does not
# redeploy the function on every plan.
find "$STAGE" -exec touch -t 202601010000 {} +
(cd "$STAGE" && find . -type f | LC_ALL=C sort | zip -q -X "$ZIP" -@)

LISTING="$(unzip -Z1 "$ZIP")"
for required in app/lambda_handler.py app/decision_bootstrap.py app/decision.py app/decision_audit.py app/decision_schema.sql rds-ca.pem pg8000/native.py; do
  grep -qx "$required" <<<"$LISTING" || { echo "missing from zip: $required" >&2; exit 1; }
done
if grep -Eq '^(app/nodes\.py|app/graph\.py|app/api\.py|langchain|langgraph|openai|fastapi)' <<<"$LISTING"; then
  echo "zip contains the LangGraph/web stack; it must not" >&2
  exit 1
fi

echo "built $ZIP ($(wc -c <"$ZIP" | tr -d ' ') bytes, $(wc -l <<<"$LISTING" | tr -d ' ') files)"
