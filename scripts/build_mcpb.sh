#!/usr/bin/env bash
# Build the navaja .mcpb bundle: stage inputs, validate the manifest, pack.
#
# Requires: uv (staging deps + version), node/npx on PATH (for the mcpb CLI,
# which is fetched on demand via npx).
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_BUNDLE="${1:-build/mcpb}"
OUT_MCPB="${2:-dist/navaja.mcpb}"

cd "$PROJECT_ROOT"

echo "==> staging bundle inputs into $OUT_BUNDLE"
uv run python scripts/build_mcpb.py --out "$OUT_BUNDLE"

echo "==> validating manifest"
npx -y @anthropic-ai/mcpb validate "$OUT_BUNDLE/manifest.json"

echo "==> packing"
npx -y @anthropic-ai/mcpb pack "$OUT_BUNDLE" "$OUT_MCPB"

echo "==> done: $OUT_MCPB"
