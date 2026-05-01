# Design — locked decisions

This document records the architectural and UX decisions that any
non-trivial change must respect. The full historical design rationale
(problem statement, alternatives considered, eng + design review notes)
lives in the git history.

For the current implementation map, see [`architecture.md`](architecture.md).

## Approach (locked)

**Approach B** — LSP + standalone viewer + thin VSCode embed.

- A Python language server (`systemrdl-lsp`) wraps `systemrdl-compiler`,
  publishes diagnostics, hovers, outline, goto-def, completion,
  rename, references, semantic tokens, code actions, and a custom
  `rdl/elaboratedTree` JSON-RPC method.
- A shared React viewer (`@systemrdl-pro/viewer-core`) renders the
  elaborated tree, identical in the VSCode webview and the standalone
  CLI (`rdl-viewer file.rdl`).
- A thin VSCode extension (`vscode-systemrdl-pro`) supervises the LSP,
  hosts the webview, and bridges the source-map cycle.

Rejected alternatives:

- **Approach A** (webview wrapper around `peakrdl html`) — too thin,
  VSCode-only, no editor diagnostics.
- **Approach C** (full browser-based IDE with pyodide-bundled compiler)
  — out of scope, no faster path to the user-pain we set out to fix.

## Decisions (D-numbered, do not relitigate without explicit user request)

- **Decision 1C** — Viewer assets bundled in the extension webview, NOT
  iframed from `localhost`. Survives offline, no port conflicts, no CSP
  contortions.
- **Decision 2B** — Explicit `systemrdl-pro.pythonPath` setting + fallback
  chain (workspace → `ms-python.python` → `python3`/`python` on PATH) +
  actionable banner on missing module.
- **Decision 3C** — Multi-root workspaces show one tab per top-level
  addrmap definition. Each is its own elaborated `RootNode`.
- **Decision 8B** — Separate extension `vscode-systemrdl-pro` with
  language id `systemrdl-pro` (not `systemrdl`) — peaceful coexistence
  with the SystemRDL org's mainline `vscode-systemrdl` extension.
- **Decision 9A** — JSON Schema codegen in both directions: edit
  `schemas/elaborated-tree.json` first, then `bun run codegen`
  regenerates Python `TypedDict`s and TS types.

## Viewer UX decisions (D4-D15)

These lock the viewer's user-facing behaviour. Any UX change has to
either match an existing decision or go through a fresh design review.

- **D4** — Auto-select the first register on tab open so the Detail
  pane is never blank.
- **D7** — Last-good elaboration survives a parse failure; show a stale
  bar at the top of the tree instead of clearing the panel.
- **D8** — Library-only files (no top-level addrmap) get an empty-state
  message in the tree, not silence.
- **D9** — Partial-tab amber-dot deferred — multi-root rendering already
  isolates failures per addrmap.
- **D10** — Cursor in editor → tree auto-selects matching node, with a
  500 ms debounce to avoid thrash on selection drag.
- **D12** — The viewer palette is locked to a muted/professional set
  (mockup B from the design review). User overrides via
  `systemrdl-pro.viewer.colors`.
- **D13** — Body layout always stacks tree above detail (originally
  responsive at < 700 px viewport; user feedback locked it to always
  stacked).
- **D14** — Dark + light tokens via CSS variables, follow
  `prefers-color-scheme`. High-contrast tokens land via
  `forced-colors: active`.
- **D15** — Cmd-F filter input with explicit scope selector
  (All / Name / Address / Field).

## Source-map cycle (Week 6 deliverables, all in)

- **U2** — Click register in viewer → editor `revealRange` smooth-scroll
  + 200 ms line flash.
- **U5** — Right-click in viewer → Copy Address / Copy Name / Copy Type
  / Reveal in Editor.

## Out of scope (intentional)

- Live FPGA register reads via JTAG/OpenOCD — interesting future work,
  not part of the v1 surface.
- Two-way editing (modify the rendered map → patch the source) — same.
- A custom parser. We rely on `systemrdl-compiler` exclusively.

## Future work

Roadmap items (when there are any) live in
[`docs/ROADMAP.md`](ROADMAP.md). Bug reports and feature requests go
through
[GitHub Issues](https://github.com/seimei-d/systemrdl-pro/issues).
