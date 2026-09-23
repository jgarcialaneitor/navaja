"""Offline tests for the .mcpb bundle staging inputs and build script."""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MCPB_DIR = REPO_ROOT / "mcpb"
BUILD_SCRIPT = REPO_ROOT / "scripts" / "build_mcpb.py"


def _load_build_script():
    """Load scripts/build_mcpb.py as a module (it is not part of a package)."""
    spec = importlib.util.spec_from_file_location("build_mcpb", BUILD_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["build_mcpb"] = module
    spec.loader.exec_module(module)
    return module


def _pyproject_version() -> str:
    with (REPO_ROOT / "pyproject.toml").open("rb") as f:
        return tomllib.load(f)["project"]["version"]


def test_manifest_template_is_valid_json_and_complete():
    template_path = MCPB_DIR / "manifest.template.json"
    manifest = json.loads(template_path.read_text(encoding="utf-8"))

    assert manifest["manifest_version"] == "0.4"
    assert manifest["name"] == "navaja"
    assert manifest["server"]["type"] == "uv"
    assert manifest["server"]["entry_point"] == "navaja_mcpb.py"

    mcp_config = manifest["server"]["mcp_config"]
    assert mcp_config["command"] == "uv"
    args = mcp_config["args"]
    assert "--directory" in args
    assert "${__dirname}" in args

    platforms = manifest["compatibility"]["platforms"]
    assert "win32" in platforms
    assert "darwin" in platforms
    assert manifest["compatibility"]["runtimes"]["python"] == ">=3.12"

    assert manifest["privacy_policies"]
    assert manifest["license"] == "MIT"


def test_manifest_tools_match_registered_server_tools():
    import navaja.server as server_module

    server = server_module.server
    tool_manager = getattr(server, "_tool_manager", None)

    registered: set[str]
    if tool_manager is not None and hasattr(tool_manager, "_tools"):
        registered = set(tool_manager._tools)
    elif tool_manager is not None and hasattr(tool_manager, "list_tools"):
        registered = {tool.name for tool in tool_manager.list_tools()}
    else:
        registered = {tool.name for tool in getattr(server, "list_tools", lambda: [])()}

    template_path = MCPB_DIR / "manifest.template.json"
    manifest = json.loads(template_path.read_text(encoding="utf-8"))
    declared = {tool["name"] for tool in manifest["tools"]}

    assert declared == registered, (
        f"Manifest tool list drift: declared={sorted(declared)} "
        f"registered={sorted(registered)}"
    )


def test_generate_manifest_injects_pyproject_version():
    build = _load_build_script()
    version = _pyproject_version()
    manifest = build.generate_manifest(version)

    assert manifest["version"] == version

    serialized = json.dumps(manifest)
    unexpected = []
    for token in _find_placeholders(serialized):
        if token != "${__dirname}":
            unexpected.append(token)
    assert not unexpected, f"Unexpected placeholders left in manifest: {unexpected}"


def _find_placeholders(text: str):
    """Yield simple ${...} placeholders found in *text*."""
    start = 0
    while True:
        idx = text.find("${", start)
        if idx == -1:
            break
        end = text.find("}", idx + 2)
        if end == -1:
            break
        yield text[idx : end + 1]
        start = end + 1


def test_launcher_imports_navaja_server():
    launcher_path = MCPB_DIR / "navaja_mcpb.py"
    tree = ast.parse(launcher_path.read_text(encoding="utf-8"))

    imports = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "navaja.server"
    ]
    assert imports, "Launcher must import from navaja.server"
    imported_names = {alias.name for alias in imports[0].names}
    assert "main" in imported_names

    calls_main = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "main"
        for node in ast.walk(tree)
    )
    assert calls_main, "Launcher must call main()"


def test_mcpbignore_excludes_caches():
    ignore_path = MCPB_DIR / ".mcpbignore"
    lines = {line.strip() for line in ignore_path.read_text(encoding="utf-8").splitlines()}

    assert ".venv/" in lines
    assert "__pycache__/" in lines
    assert "*.pyc" in lines


def test_staging_includes_files_required_by_pyproject(tmp_path):
    """The staged bundle must be buildable by uv (regression, 2026-09-23).

    ``pyproject.toml`` declares ``readme = "README.md"`` and
    ``license-files = ["LICENSE"]``; hatchling refuses to build without
    them, so ``uv run`` inside the staged bundle failed with "Readme file
    does not exist" before the smoke test caught it.
    """
    build_mcpb = _load_build_script()
    out = tmp_path / "mcpb"

    build_mcpb.stage(out)

    for name in (
        "pyproject.toml",
        "README.md",
        "LICENSE",
        "manifest.json",
        "navaja_mcpb.py",
        ".mcpbignore",
    ):
        assert (out / name).is_file(), f"staged bundle is missing {name}"
    assert (out / "src" / "navaja" / "server.py").is_file()
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["version"] == _pyproject_version()
