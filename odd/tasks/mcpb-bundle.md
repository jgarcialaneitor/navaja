# Feature: .mcpb one-click bundle for Claude Desktop

Pending item from memory (user's stated goal, 2026-09-24 and again 2026-09-23).
Branch: `feat/mcpb-bundle` (off `main` `9d89677`)

## Problem

README documents three install paths, none is one-click. A user without
technical knowledge should be able to install navaja into Claude Desktop by
double-clicking a single `.mcpb` file (MCP Bundle, ex-DXT).

## Design (from the MCPB spec, MANIFEST.md v0.4, anthropics/mcpb repo)

- **`server.type = "uv"`** (manifest v0.4): cross-platform Python bundles with
  NO bundled deps — the host installs dependencies from `pyproject.toml` with
  uv; "No user Python installation required". Avoids the Python-compat trap
  (mcpb issue #17) and keeps the bundle ~100 KB.
- Bundle is **staged, not packed from repo root**: `scripts/build_mcpb.py`
  assembles `build/mcpb/` from `pyproject.toml`, `src/`, a launcher, and a
  manifest generated with the version read from `pyproject.toml` (single
  source of truth), then runs `npx @anthropic-ai/mcpb pack`.
- `manifest.json` (generated):
  - `manifest_version: "0.4"`, name `navaja`, display_name, version from
    pyproject, author, description, license MIT.
  - `server: { type: "uv", entry_point: "navaja_mcpb.py", mcp_config:
    { command: "uv", args: ["run", "--directory", "${__dirname}",
    "navaja_mcpb.py"] } }` — mirrors the official `hello-world-uv` example.
  - `compatibility: { platforms: [darwin, linux, win32], runtimes:
    { python: ">=3.12" } }` (host manages Python via uv, so this is the
    project floor, not the user's Python).
  - `tools`: the 8 registered tools with one-line descriptions
    (buscar_sentencias, listar_localizaciones, ver_texto_completo,
    iniciar_descargas, estado_descargas, recoger_descarga, cancelar_lote,
    estado_servidor); `tools_generated: false`.
  - `privacy_policies`: CENDOJ aviso legal URL (the only external service).
  - no `user_config` in v1: every env var has a working default and is
    documented in README; configuration surfaces can be added later.
  - no icon in v1 (spec says optional); future follow-up if wanted.
- **Launcher `navaja_mcpb.py`** (bundle root, not repo root):
  `from navaja.server import main; main()` — works because `uv run
  --directory` installs the project (hatchling, src layout) into the
  bundle venv.
- **`.mcpbignore`** copied into staging: excludes `.venv/`, `__pycache__/`,
  `*.pyc`, caches, `tests/`, `build/`.
- Build script: `scripts/build_mcpb.sh` → stages `build/mcpb/`, generates
  `manifest.json` from the template with the pyproject version injected,
  runs `npx @anthropic-ai/mcpb validate` then `pack` → `dist/navaja.mcpb`.

## Tests (offline, no npx needed)

- `tests/test_mcpb.py`: manifest template is valid JSON; generated manifest
  (invoke the generation logic) has name/version matching `pyproject.toml`,
  `server.type == "uv"`, entry_point file exists in staging inputs, the
  declared tools list matches **exactly** the tools registered by
  `navaja.server` (introspect `@server.tool()`), privacy policy present.
- Real packing is validated manually / opt-in (needs npx + network), like the
  live tests.

## Tasks

- [ ] 1. Branch + this doc
- [ ] 2. Staging inputs: launcher, manifest template, .mcpbignore, build script
- [ ] 3. `tests/test_mcpb.py` (manifest ↔ server tool parity guard)
- [ ] 4. Local smoke: build + `mcpb validate` + pack → dist/navaja.mcpb
- [ ] 5. README install section (one-click path first)
- [ ] 6. Work-unit commits, PR
