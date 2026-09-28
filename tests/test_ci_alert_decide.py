"""Tests for scripts/ci_alert_decide.sh, the gate behind the CI failure alert.

The workflow pages the owner only on a SECOND consecutive failure on main. The
decision lives in a shell script so it can be tested here without Actions.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "ci_alert_decide.sh"

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")


def decide(current, runs):
    payload = runs if isinstance(runs, str) else json.dumps(runs)
    return subprocess.run(
        ["sh", str(SCRIPT), str(current), payload],
        capture_output=True,
        text=True,
        check=False,
    )


def run(run_id, conclusion):
    return {"databaseId": run_id, "conclusion": conclusion, "headSha": f"sha{run_id}"}


def test_two_failures_in_a_row_alerts():
    r = decide(200, [run(200, "failure"), run(100, "failure")])
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "alert"


def test_failure_after_success_is_silent():
    r = decide(200, [run(200, "failure"), run(100, "success")])
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "silent"


def test_failure_with_no_previous_run_is_silent():
    r = decide(200, [run(200, "failure")])
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "silent"


def test_empty_list_is_silent():
    r = decide(200, [])
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "silent"


def test_current_run_listed_first_is_excluded():
    # If the current run were not excluded, the head of the list (itself, a
    # failure) would make every single failure look like a second one.
    r = decide(300, [run(300, "failure"), run(200, "success"), run(100, "failure")])
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "silent"


def test_current_run_not_first_still_uses_its_predecessor():
    # A newer run that already finished is not this run's predecessor.
    r = decide(200, [run(300, "success"), run(200, "failure"), run(100, "failure")])
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "alert"


def test_malformed_json_fails_loud():
    r = decide(200, "{not json")
    assert r.returncode != 0
    assert r.stdout.strip() == ""


def test_non_array_json_fails_loud():
    r = decide(200, {"databaseId": 100, "conclusion": "failure"})
    assert r.returncode != 0
    assert r.stdout.strip() == ""


def test_bad_run_id_fails_loud():
    r = decide("abc", [run(100, "failure")])
    assert r.returncode != 0
