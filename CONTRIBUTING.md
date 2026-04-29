# Contributing to systemrdl-pro

This is a monorepo with a Python package (`systemrdl-lsp`) and three TypeScript packages
(`rdl-viewer-core`, `rdl-viewer-cli`, `vscode-systemrdl-pro`). You need both `uv` and `bun`
on PATH to work on the full stack.

## Setup

```bash
# Python side
curl -LsSf https://astral.sh/uv/install.sh | sh    # if you don't have uv
uv sync                                             # creates .venv with all deps

# JS side
curl -fsSL https://bun.sh/install | bash           # if you don't have bun
bun install                                         # installs workspace deps
```

## Running locally

```bash
# Run the LSP server (stdio mode)
uv run systemrdl-lsp

# Build the VSCode extension (.vsix)
bun run --cwd packages/vscode-systemrdl-pro build

# Install the dev .vsix into your local VSCode
code --install-extension packages/vscode-systemrdl-pro/systemrdl-pro-*.vsix
```

## Project conventions

- **JSON Schema is the source of truth** for the elaborated tree shape.
  Edit `schemas/elaborated-tree.json` then run `bun run codegen` to regenerate
  Python typed dicts and TypeScript types.
- **No `git add -A`** in commits — stage specific files.
- **Conventional Commits** style: `feat(lsp): add hover provider`, `fix(extension): handle webview disposal`.
- **Design decisions** for the viewer UX are locked (see `docs/design.md` "Viewer UX" section).
  PRs that change the UX without an approved design review will be asked to rework.

## Tests

- Python: `uv run pytest packages/systemrdl-lsp/tests/`
- TS: `bun test --cwd packages/vscode-systemrdl-pro`
- Schema: `bun run --cwd packages/rdl-viewer-core schema:validate` (validates fixtures)

## License

By contributing you agree your contributions are licensed under MIT (see `LICENSE`).
