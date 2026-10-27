"""The tenant-level variables are documented, straight from the table the code reads.

The inventory scanner matches literal os.getenv calls. After M3 the tenant-level reads go through
one table-driven loop (TenantConfig, TENANT_ENV_FIELDS), which a literal match cannot see, and the
generated inventory silently lost 25 of them while still saying none was unclassified. This test
reads the table itself, so a variable cannot be added to the code and missing from the document.
"""

from __future__ import annotations

from pathlib import Path

from app import tenant_config as tc

DOC = Path(__file__).resolve().parents[1] / "docs" / "tenant-config.md"


def test_every_tenant_level_variable_is_in_the_document():
    text = DOC.read_text(encoding="utf-8")
    missing = [name for name in tc.TENANT_ENV_FIELDS if f"`{name}`" not in text]
    assert missing == []


def test_the_document_does_not_invent_a_tenant_variable():
    text = DOC.read_text(encoding="utf-8")
    start = text.index("## Tenant-level variables (read in one place)")
    end = text.index("## Inventory of environment reads", start)
    listed = {
        line.split("`")[1]
        for line in text[start:end].splitlines()
        if line.startswith("| `")
    }
    assert listed == set(tc.TENANT_ENV_FIELDS)


def test_the_scan_is_not_vacuous():
    assert len(tc.TENANT_ENV_FIELDS) >= 30
