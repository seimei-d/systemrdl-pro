#!/usr/bin/env bash
# Generate Python TypedDict and TypeScript types from schemas/elaborated-tree.json (Decision 9A).
# Usage: bun run codegen   (or: bash scripts/codegen.sh)
#
# Inputs:  schemas/elaborated-tree.json
# Outputs: packages/systemrdl-lsp/src/systemrdl_lsp/_generated_types.py
#          packages/vscode-systemrdl-pro/src/types/elaborated-tree.generated.ts

set -euo pipefail

SCHEMA="schemas/elaborated-tree.json"

if [[ ! -f "$SCHEMA" ]]; then
  echo "ERROR: $SCHEMA not found (cwd: $(pwd))" >&2
  exit 1
fi

uv run python tools/codegen.py
