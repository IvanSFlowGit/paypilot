"""The schema splitter, which decides what Postgres actually receives.

A mis-split statement does not raise. Postgres gets a fragment, the
bootstrap reports success for the statements it did send, and the missing
policy looks exactly like a schema that never had one. That is why this
file exists separately from the schema tests: the hazard is silence.
"""

from __future__ import annotations

import pytest

from app.decision_audit import split_sql_statements as split


# --- the two hazards the old line-wise version mangled --------------------
# Both were MEASURED against the old implementation before it was changed,
# not imagined: the semicolon split one statement into two and the double
# dash truncated one and dropped the rest.


def test_a_semicolon_inside_a_literal_does_not_split_the_statement():
    sql = "CREATE POLICY p ON t USING (x = current_setting('a;b', true));"
    out = split(sql)
    assert len(out) == 1, out
    assert "'a;b'" in out[0]


def test_a_double_dash_inside_a_literal_is_not_a_comment():
    sql = "CREATE POLICY p ON t USING (x = current_setting('a--b', true));"
    out = split(sql)
    assert len(out) == 1, out
    assert "'a--b'" in out[0], "the literal was truncated as a comment"


def test_an_escaped_quote_leaves_a_later_semicolon_protected():
    """THIS ASSERTS THE OUTCOME, NOT A MECHANISM, because the mechanism
    turned out not to exist. A special case for '' was written, mutated
    away, and reddened nothing: '' is two quotes and therefore two toggles,
    so parity leaves the parser inside the literal regardless. Measured at
    255,987 inputs with zero divergence, then deleted. What still has to
    hold is this: a ; inside a literal containing '' does not split."""
    sql = "SELECT 'it''s fine; really' AS a; SELECT 2;"
    out = split(sql)
    assert len(out) == 2, out
    assert "it''s fine; really" in out[0]


# --- what must still behave exactly as it did ----------------------------
# The quiet half of the widening. A splitter that stopped stripping comments
# would send the schema's own prose to Postgres as a statement, which is the
# defect the original version was written to fix.


def test_a_comment_outside_a_literal_is_still_stripped():
    sql = "-- prose with a semicolon; in it\nCREATE TABLE t (a TEXT);"
    out = split(sql)
    assert out == ["CREATE TABLE t (a TEXT)"], out


def test_a_trailing_comment_on_a_statement_line_is_still_stripped():
    sql = "CREATE TABLE t (a TEXT); -- why\nCREATE INDEX i ON t (a);"
    out = split(sql)
    assert len(out) == 2, out
    assert "why" not in " ".join(out)


def test_the_real_schema_still_splits_and_every_statement_is_a_command():
    """Run against the shipped file rather than a fixture, because the only
    schema that matters is the one the bootstrap reads."""
    from pathlib import Path

    sql = Path("app/decision_schema.sql").read_text(encoding="utf-8")
    out = split(sql)
    assert out, "the real schema split to nothing"
    for stmt in out:
        first = stmt.split()[0].upper()
        assert first in {"CREATE", "ALTER", "DROP"}, stmt[:60]
        assert "--" not in stmt or "'" in stmt, stmt[:60]


# --- the refusals, because mis-splitting in silence is the whole problem --


def test_a_dollar_quoted_block_is_refused_rather_than_mis_split():
    """This parser does not implement dollar quoting. Refusing names the
    gap; mangling it would ship a fragment and look like a missing policy."""
    sql = "CREATE FUNCTION f() RETURNS int AS $$ SELECT 1; $$ LANGUAGE sql;"
    with pytest.raises(ValueError) as exc:
        split(sql)
    assert "dollar" in str(exc.value).lower()


def test_an_unterminated_literal_is_refused():
    with pytest.raises(ValueError) as exc:
        split("CREATE POLICY p ON t USING (x = 'oops);")
    assert "unterminated" in str(exc.value).lower()


def test_an_empty_schema_is_no_statements_rather_than_one_empty_one():
    assert split("") == []
    assert split("-- only a comment\n") == []
    assert split(";;\n;") == []
