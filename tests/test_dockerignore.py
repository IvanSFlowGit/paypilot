"""Nothing that needs the mcp package may ship in the image that cannot import it.

The image installs requirements.txt and mcp is deliberately NOT in there: the
deployed dunning app never imports it, so the image does not grow for a feature
it does not use. But COPY app/ and COPY scripts/ are directory copies, so a
module importing mcp lands in the image anyway and raises ImportError for anyone
who runs it there. Harmless while nothing imports it, and a trap the moment
somebody tries.

This asserts the structure rather than the instance, so the SECOND mcp-dependent
module is caught too. It also pins the build-context exclusions, because Fly
warned the context was 1.6 GB and infra/ was 1.5 GB of Terraform provider
binaries the Dockerfile never copies.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IGNORE = ROOT / ".dockerignore"
COPIED = ("app", "scripts")
_IMPORTS_MCP = re.compile(r"^\s*(?:from|import)\s+mcp\b", re.M)


def _ignored() -> set[str]:
    return {
        line.strip()
        for line in IGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }


def _modules_importing_mcp() -> list[str]:
    found = []
    for directory in COPIED:
        for path in sorted((ROOT / directory).rglob("*.py")):
            if _IMPORTS_MCP.search(path.read_text(encoding="utf-8")):
                found.append(str(path.relative_to(ROOT)))
    return found


def test_control_at_least_one_module_imports_mcp():
    """Without this the assertion below passes on an empty list."""
    found = _modules_importing_mcp()
    assert found, "no module imports mcp; the test below would check nothing"
    assert "app/mcp_server.py" in found


def test_every_mcp_dependent_module_is_excluded_from_the_image():
    ignored = _ignored()
    for module in _modules_importing_mcp():
        assert module in ignored, (
            f"{module} imports mcp and would ship in the image, which installs "
            "requirements.txt where mcp is absent. Add it to .dockerignore."
        )


def test_mcp_is_still_absent_from_requirements():
    """If mcp ever becomes a runtime dependency the exclusions above are wrong
    and this is the test that says so, rather than an ImportError in production."""
    text = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    assert not re.search(r"^\s*mcp\b", text, re.M), (
        "mcp is now a runtime dependency; revisit the .dockerignore exclusions"
    )


def test_the_terraform_directory_is_out_of_the_build_context():
    assert "infra" in _ignored()
    assert not any(
        line.startswith("COPY infra") for line in (ROOT / "Dockerfile").read_text().splitlines()
    ), "the Dockerfile now copies infra; the exclusion would break the build"


def test_nothing_the_dockerfile_copies_is_wholly_excluded():
    """Excluding a whole copied directory would produce an empty COPY and a
    broken image, so the exclusions must be narrower than the copies."""
    ignored = _ignored()
    for directory in COPIED:
        assert directory not in ignored, directory
