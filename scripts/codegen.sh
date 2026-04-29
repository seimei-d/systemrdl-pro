#!/usr/bin/env bash
# Generate Python TypedDict and TypeScript types from schemas/elaborated-tree.json (Decision 9A).
# Usage: bun run codegen   (or: bash scripts/codegen.sh)
#
# Inputs:  schemas/elaborated-tree.json
# Outputs: packages/systemrdl-lsp/src/systemrdl_lsp/_generated_types.py
#          packages/rdl-viewer-core/src/_generated_types.ts
#
# Implementation deferred to Week 2: until then, both consumers use hand-written shadow
# types that match the schema. CI validates the shadow types against the schema via
# fixture round-trips. See docs/ROADMAP.md.

set -euo pipefail

SCHEMA="schemas/elaborated-tree.json"

if [[ ! -f "$SCHEMA" ]]; then
  echo "ERROR: $SCHEMA not found (cwd: $(pwd))" >&2
  exit 1
fi

echo "[codegen] schema present: $SCHEMA ($(wc -c <"$SCHEMA") bytes)"
echo "[codegen] codegen step is a placeholder until Week 2; see docs/ROADMAP.md."
echo "[codegen] no files were regenerated."
