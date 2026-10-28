"""The MCP distribution is staged from app/ sources, never hand-copied."""
import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("build_mcp_package", ROOT / "scripts" / "build_mcp_package.py")
bmp = importlib.util.module_from_spec(spec)
sys.modules["build_mcp_package"] = bmp
spec.loader.exec_module(bmp)


def test_staged_package_has_no_app_import_and_ships_the_table(tmp_path):
    out = bmp.stage(tmp_path / "s")
    pkg = out / bmp.PKG
    server = (pkg / "mcp_server.py").read_text(encoding="utf-8")
    assert bmp.NEW_IMPORT in server
    assert bmp.OLD_IMPORT not in server
    for f in pkg.rglob("*.py"):
        text = f.read_text(encoding="utf-8")
        assert "from app" not in text and "import app" not in text, f
    assert (pkg / "data" / "ach_return_codes.csv").read_bytes() == (
        ROOT / "app" / "data" / "ach_return_codes.csv").read_bytes()
    assert (pkg / "ach_return_map.py").read_bytes() == (ROOT / "app" / "ach_return_map.py").read_bytes()


def test_server_json_matches_pyproject_version_and_readme_name():
    import json
    import tomllib
    meta = tomllib.loads((ROOT / "packaging/mcp/pyproject.toml").read_text(encoding="utf-8"))
    sj = json.loads((ROOT / "packaging/mcp/server.json").read_text(encoding="utf-8"))
    assert sj["version"] == meta["project"]["version"] == sj["packages"][0]["version"]
    assert sj["packages"][0]["identifier"] == meta["project"]["name"]
    readme = (ROOT / "packaging/mcp/README.md").read_text(encoding="utf-8")
    assert f"mcp-name: {sj['name']}" in readme
