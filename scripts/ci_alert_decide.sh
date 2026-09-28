#!/bin/sh
# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
# Commercial use requires a separate licence from the author.
#
# Decide whether a failed CI run on main should page the owner.
#
# Usage: ci_alert_decide.sh <current_run_id> <runs_json>
#   runs_json: output of
#     gh run list --workflow CI --branch main --status completed \
#       --json databaseId,conclusion,headSha
#
# Prints "alert" when the most recent completed run OLDER than the current one
# also failed (second consecutive failure), otherwise "silent". A single
# failure usually clears itself, so it stays silent.
#
# Fails loud (exit 2) on a bad run id or input that is not a JSON array: a
# decision that could not be made is not a "silent".
set -eu

if [ "$#" -ne 2 ]; then
    echo "usage: $0 <current_run_id> <runs_json>" >&2
    exit 2
fi

current="$1"
runs="$2"

case "$current" in
    '' | *[!0-9]*)
        echo "error: current run id must be a positive integer, got '$current'" >&2
        exit 2
        ;;
esac

if ! command -v jq >/dev/null 2>&1; then
    echo "error: jq is required" >&2
    exit 2
fi

# The current run is excluded explicitly, and so is any run newer than it (a
# later run that finished first is not this run's predecessor).
prev="$(printf '%s' "$runs" | jq -er --argjson cur "$current" '
    if type != "array" then error("runs_json is not a JSON array") else . end
    | map(select(.databaseId != $cur and .databaseId < $cur))
    | sort_by(.databaseId) | reverse
    | (.[0].conclusion // "none")
')" || {
    echo "error: could not parse runs_json" >&2
    exit 2
}

if [ "$prev" = "failure" ]; then
    echo alert
else
    echo silent
fi
