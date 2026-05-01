# Contributing to systemrdl-pro

This is a monorepo with one Python package (`systemrdl-lsp`) and three TypeScript
packages (`rdl-viewer-core`, `rdl-viewer-cli`, `vscode-systemrdl-pro`). You need
both `uv` and `bun` on PATH to work on the full stack.

## Setup

```bash
# Python side
curl -LsSf https://astral.sh/uv/install.sh | sh    # if you don't have uv
uv sync                                            # creates .venv with all deps
uv pip install -e packages/systemrdl-lsp           # install the LSP editable

# TypeScript side
curl -fsSL https://bun.sh/install | bash           # if you don't have bun
bun install                                        # installs workspace deps
```

## Running locally

```bash
# Sanity-check the LSP entry point
uv run systemrdl-lsp --version

# Build the viewer-core React bundle, then the VSCode extension
bun run --cwd packages/rdl-viewer-core build
bun run --cwd packages/vscode-systemrdl-pro build

# Package + install the dev .vsix into your local VSCode
bun run --cwd packages/vscode-systemrdl-pro package
code --install-extension packages/vscode-systemrdl-pro/vscode-systemrdl-pro-*.vsix --force
```

## Tests + lint + typecheck

```bash
# Python: 64 unit tests
uv run --directory packages/systemrdl-lsp pytest tests/ -q
uv run ruff check packages/systemrdl-lsp/

# TypeScript: typecheck (no separate test runner today)
bun run --cwd packages/rdl-viewer-core typecheck
bun run --cwd packages/vscode-systemrdl-pro typecheck
bun run --cwd packages/rdl-viewer-cli typecheck
```

CI (`.github/workflows/ci.yml`) runs both jobs on every push.

## Codegen

```bash
bun run codegen
```

This walks `schemas/elaborated-tree.json` and regenerates the Python
`TypedDict` and TS type files. The CI test suite asserts no diff between
the committed generated files and a fresh regen — so an unrun codegen
breaks the build deterministically.

## Project conventions

- **JSON Schema is the source of truth** for the elaborated tree shape.
  Edit `schemas/elaborated-tree.json` first, then run `bun run codegen`.
- **No `git add -A`** for shared paths — stage specific files. Some commits
  legitimately touch many files (refactors, codegen) but be deliberate.
- **Conventional Commits** style: `feat(lsp): add hover provider`,
  `fix(viewer): caret-toggle stays clickable when row is selected`,
  `docs(readme): document N`.
- **Locked architectural decisions** live in `docs/design.md` (Approach B,
  D-numbered UX decisions, decisions 1C/2B/3C/8B/9A). PRs that change
  these without an approved design review will be asked to rework.
- **Architecture diagrams** in `docs/architecture.md` (mermaid).
- **Reused-type-body heuristic** — when the same `regfile`/`reg` type is
  instantiated multiple times, its body lines are replayed in the
  elaborated tree once per instance. Hover, inlay-hints, and address-
  conflict diagnostics skip those lines (count of elaborated nodes per
  source line > 1) so we never paint a single absolute address on a
  multi-instance template. New features that walk the elaborated tree
  for source-line attribution should follow the same heuristic.

## Filing issues

Use the templates at https://github.com/seimei-d/systemrdl-pro/issues/new/choose:

- **Bug report** — for incorrect behaviour you can reproduce.
- **Feature request** — for capabilities you want added.
- **Question** — for usage / setup questions that don't yet rise to a bug.

Please include the `.rdl` snippet that triggers the problem (or a minimal
reduced version), the LSP and extension versions, and your platform (OS +
VSCode version).

## Pull requests

- Branch off `main`, open a PR against `main`.
- One concern per PR. Mixed-concern PRs ("fix bug X + refactor Y +
  add feature Z") get split.
- CI must pass: lint, typecheck, tests, codegen drift check.
- For UX-changing PRs: include a brief screenshot or screencast.
- For schema-changing PRs: regenerate types via `bun run codegen` and
  commit the result.

## Releases

Releases are cut as GitHub Releases (no Marketplace publish at the
moment — Azure DevOps PAT requirement deferred). The `.vsix` is
attached to each release; users install via "drag onto Extensions
sidebar" or `code --install-extension <path-to-vsix>`.

## License

By contributing you agree your contributions are licensed under
[MIT](LICENSE).
