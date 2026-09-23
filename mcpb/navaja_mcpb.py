"""Entry point for the navaja .mcpb bundle (Claude Desktop, uv runtime).

This file is staged into the bundle root by ``scripts/build_mcpb.py`` and is
executed by ``uv run --directory <bundle> navaja_mcpb.py`` after uv has
installed the project from ``pyproject.toml``.
"""

from navaja.server import main

main()
