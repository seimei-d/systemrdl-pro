# CLAUDE.md — Project conventions for AI assistants

## What this project is

`systemrdl-pro` — LSP server + interactive memory-map viewer for SystemRDL 2.0. Monorepo
with one Python package (`systemrdl-lsp`) and three TypeScript packages (`rdl-viewer-core`,
`rdl-viewer-cli`, `vscode-systemrdl-pro`). Editor-agnostic by design — the LSP and CLI viewer
are not VSCode-specific.

## Source-of-truth documents

Read these before making non-trivial changes:

- `docs/design.md` — full design doc, including the **Viewer UX** section that locks 12 design
  decisions (D4-D15). Do not change UX without an approved design review.
- `docs/ROADMAP.md` — Week 1-6 build sequence.
- `TODOS.md` — deferred work with rationale.
- `schemas/elaborated-tree.json` — JSON Schema source of truth for the elaborated AST shape.

## Locked architectural decisions (do not relitigate without explicit user request)

- **Approach B**: LSP + standalone viewer + thin VSCode embed. Not Approach A (webview wrapper)
  or Approach C (full web-IDE).
- **Backend**: `systemrdl-compiler` (Python). No custom parser in TS/Rust.
- **Decision 1C**: viewer assets bundled in the extension webview, NOT iframed from `localhost`.
- **Decision 2B**: explicit `systemrdl-pro.pythonPath` setting + fallback chain (workspace →
  ms-python.python → PATH) + actionable banner on missing module.
- **Decision 3C**: multi-root workspaces show one tab per addrmap root.
- **Decision 8B**: separate extension `vscode-systemrdl-pro` with language id `systemrdl-pro`
  (not `systemrdl`) — peaceful coexistence with `SystemRDL/vscode-systemrdl`.
- **Decision 9A**: JSON Schema codegen in both directions (Python `TypedDict` + TS `type`).
- **Locked viewer layout**: Variant B — Tree + Detail Pane (locked in design review;
  see `docs/design.md` for the D4-D15 UX decisions tied to this layout).

## Conventions

- **Tooling**: `uv` for Python, `bun` for TS. Both have workspace configs at the repo root.
- **Schema-driven types**: edit `schemas/elaborated-tree.json` first, then run `bun run codegen`
  to regenerate Python and TS types. Do not hand-edit generated files.
- **Commits**: Conventional Commits. Stage specific files (no `git add -A`).
- **Design tokens**: defined as CSS variables in `docs/design.md` "Design Tokens" section.
  Always reference via `var(--rdl-...)`. Never hardcode access-mode colors.

## Skill routing

When the user's request matches an available skill, invoke it via the Skill tool. When in doubt,
invoke the skill.

Key routing rules:

- Product ideas / brainstorming → invoke `/office-hours`
- Strategy / scope → invoke `/plan-ceo-review`
- Architecture → invoke `/plan-eng-review`
- UX / design system → invoke `/plan-design-review` or `/design-consultation`
- Full review pipeline → invoke `/autoplan`
- Bugs / errors → invoke `/investigate`
- QA / testing site behavior → invoke `/qa` or `/qa-only`
- Code review / diff check → invoke `/review`
- Visual polish on built UI → invoke `/design-review`
- Ship / deploy / PR → invoke `/ship` or `/land-and-deploy`
