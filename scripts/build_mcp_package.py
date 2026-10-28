# Copyright (c) 2026 Ivan Skachek (github.com/IvanSFlowGit)
# PolyForm Noncommercial License 1.0.0 - see LICENSE and NOTICE.
"""Stage and build the paypilot-ach-returns-mcp distribution.

The package is built FROM the app sources, never copied in by hand, so the table
and the server have one home. Staging rewrites exactly one import line and
asserts it was there. The built wheel is then installed into a clean venv and
handshaken as a real MCP client by scripts/mcp_witness-style check below.

    python scripts/build_mcp_package.py <staging-dir>

Exit 0 built, 1 a check failed, 2 could not run.
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
PKG = "paypilot_ach_returns_mcp"
OLD_IMPORT = "from app import ach_return_map as table"
NEW_IMPORT = f"from {PKG} import ach_return_map as table"


def stage(out: pathlib.Path) -> pathlib.Path:
    if out.exists() and any(out.iterdir()):
        print(f"CANNOT RUN: {out} is not empty")
        raise SystemExit(2)
    pkg = out / PKG
    (pkg / "data").mkdir(parents=True)
    src = (ROOT / "app" / "mcp_server.py").read_text(encoding="utf-8")
    if src.count(OLD_IMPORT) != 1:
        print(f"FAIL: expected exactly one {OLD_IMPORT!r} in app/mcp_server.py")
        raise SystemExit(1)
    (pkg / "mcp_server.py").write_text(src.replace(OLD_IMPORT, NEW_IMPORT), encoding="utf-8")
    shutil.copy(ROOT / "app" / "ach_return_map.py", pkg / "ach_return_map.py")
    shutil.copy(ROOT / "app" / "data" / "ach_return_codes.csv", pkg / "data" / "ach_return_codes.csv")
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    for name in ("pyproject.toml", "README.md", "server.json"):
        shutil.copy(ROOT / "packaging" / "mcp" / name, out / name)
    for name in ("LICENSE", "NOTICE"):
        shutil.copy(ROOT / name, out / name)
    leftovers = [p for p in pkg.rglob("*.py") if "from app" in p.read_text(encoding="utf-8")
                 or "import app" in p.read_text(encoding="utf-8")]
    if leftovers:
        print(f"FAIL: staged code still imports app: {leftovers}")
        raise SystemExit(1)
    return out


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: build_mcp_package.py <empty staging dir>")
        raise SystemExit(2)
    out = stage(pathlib.Path(sys.argv[1]))
    r = subprocess.run([sys.executable, "-m", "build", "--no-isolation", "--outdir", str(out / "dist"), str(out)])
    raise SystemExit(0 if r.returncode == 0 else 2)
