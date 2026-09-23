"""Stage the navaja .mcpb bundle and generate its manifest.json.

The script is intentionally pure stdlib. It assembles ``build/mcpb/`` from the
minimum set of inputs uv needs to install and run the server, then leaves the
actual ``.mcpb`` packing to ``npx @anthropic-ai/mcpb pack``.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = PROJECT_ROOT / "mcpb" / "manifest.template.json"

# Placeholders that the build script is allowed to leave untouched.  The MCPB
# runtime substitutes ``${__dirname}`` with the directory that contains the
# installed manifest.json.
RUNTIME_PLACEHOLDERS = {"${__dirname}"}

_PLACEHOLDER_RE = re.compile(r"\$\{[^}]+\}")


def _pyproject_version() -> str:
    """Read the project version from ``pyproject.toml``."""
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as f:
        return tomllib.load(f)["project"]["version"]


def generate_manifest(version: str) -> dict:
    """Load the manifest template, inject *version*, and return the dict.

    Any ``${...}`` placeholder left after injection other than the runtime
    ``${__dirname}`` token raises :class:`ValueError` so the staged manifest
    cannot ship with an unresolved build-time variable.
    """
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    rendered = template.replace("${VERSION}", version)

    for placeholder in _PLACEHOLDER_RE.findall(rendered):
        if placeholder not in RUNTIME_PLACEHOLDERS:
            raise ValueError(f"Unresolved manifest placeholder: {placeholder}")

    return json.loads(rendered)


STAGED_SOURCE_DIRECTORIES = ("src", "mcpb")


def _ensure_safe_staging_target(bundle_root: Path) -> None:
    """Refuse destructive staging targets before anything is deleted.

    ``stage`` replaces the target directory wholesale, so a mistaken ``--out``
    must never delete existing data (issue: R3-destructive-output).  The
    target is rejected when it is (or contains) the project's own sources:

    * the project root itself, or any directory that is an ancestor of the
      project root (deleting it would delete the checkout);
    * any staged source directory (``src/``, ``mcpb/``).

    An existing *non-empty* target is only replaced when it looks like a
    previously staged navaja bundle (contains ``manifest.json`` and
    ``navaja_mcpb.py``); any other populated directory is refused so unrelated
    contents can never be lost.
    """
    if bundle_root == PROJECT_ROOT or bundle_root in PROJECT_ROOT.parents:
        raise ValueError(
            f"staging target {bundle_root} contains the project sources"
        )
    for name in STAGED_SOURCE_DIRECTORIES:
        source = PROJECT_ROOT / name
        if source == bundle_root:
            raise ValueError(
                f"staging target {bundle_root} would replace staged "
                f"source {source}"
            )

    if bundle_root.exists() and any(bundle_root.iterdir()):
        owned = (
            (bundle_root / "manifest.json").is_file()
            and (bundle_root / "navaja_mcpb.py").is_file()
        )
        if not owned:
            raise ValueError(
                f"refusing to replace non-bundle directory {bundle_root}; "
                "point --out at a fresh directory or a previous staging "
                "output"
            )


def stage(bundle_root: Path) -> None:
    """Stage the bundle inputs into *bundle_root*.

    Copies ``pyproject.toml``, the ``src/`` package tree, the launcher, and
    ``.mcpbignore``, then writes the generated ``manifest.json``.  Existing
    staged contents are removed first so repeated builds are deterministic.

    The target is validated first: directories that are (or contain) the
    project sources, and populated directories that are not previous staging
    outputs, are refused with :class:`ValueError` instead of being deleted.
    """
    _ensure_safe_staging_target(bundle_root)
    version = _pyproject_version()

    if bundle_root.exists():
        shutil.rmtree(bundle_root)
    bundle_root.mkdir(parents=True)

    shutil.copy2(PROJECT_ROOT / "pyproject.toml", bundle_root / "pyproject.toml")
    shutil.copytree(PROJECT_ROOT / "src", bundle_root / "src")
    # pyproject.toml declares these by path; hatchling cannot build the
    # project without them, so uv run inside the bundle would fail.
    shutil.copy2(PROJECT_ROOT / "README.md", bundle_root / "README.md")
    shutil.copy2(PROJECT_ROOT / "LICENSE", bundle_root / "LICENSE")
    shutil.copy2(PROJECT_ROOT / "mcpb" / "navaja_mcpb.py", bundle_root / "navaja_mcpb.py")
    shutil.copy2(PROJECT_ROOT / "mcpb" / ".mcpbignore", bundle_root / ".mcpbignore")

    manifest = generate_manifest(version)
    manifest_path = bundle_root / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Stage the navaja .mcpb bundle inputs.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("build/mcpb"),
        help="Output directory for the staged bundle (default: build/mcpb).",
    )
    args = parser.parse_args(argv)

    bundle_root = args.out.resolve()
    stage(bundle_root)
    print(bundle_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
