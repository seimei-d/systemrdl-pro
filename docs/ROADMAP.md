# Roadmap

Tracking the build sequence locked in `docs/design.md` (Approach B, post eng + design reviews).

## Week 1 — Walking skeleton (publishable) *current*

Goal: `code --install-extension systemrdl-pro` + open `.rdl` → see live diagnostics in editor +
empty viewer panel that opens via "Show Memory Map" command.

- [x] Monorepo scaffold (4 packages, schemas, scripts, CI placeholders)
- [x] `schemas/elaborated-tree.json` v0.1 (codegen foundation)
- [x] `systemrdl-lsp` v0.1: pygls + `publishDiagnostics` wrapping `systemrdl-compiler`
- [x] `vscode-systemrdl-pro` v0.1: LSP client + "Show Memory Map" command + webview placeholder
- [ ] First Marketplace publish (manual `vsce publish`, gated on token setup)

## Week 2-3 — Full LSP

- [ ] `textDocument/hover` — resolved address, width, sw/hw access
- [ ] `textDocument/documentSymbol` — outline of regfiles + regs in editor sidebar
- [x] `textDocument/definition` — goto-def for component types and parameters
- [x] `textDocument/completion` — register names, properties
- [x] `incl_search_paths` setting + auto-discovery from `peakrdl.toml`
- [x] **Eng-review-locked Week 2 deliverables**:
  - [x] LSP supervisor + "Restart LSP" command (silent-failure gap #1)
  - [x] `if (panel.visible) postMessage(...)` guard (silent-failure gap #2)
  - [x] `asyncio.wait_for(elaborate, timeout=10)` + push last-good (silent-failure gap #3)
  - [x] Multi-root elaboration (moved here from Week 5 due to design decision 3C)

## Week 4-5 — Live Svelte viewer

Source of truth mockup: `~/.gstack/projects/systemrdl-vscode/designs/viewer-panel-20260429/variant-B-tree-detail.html`.
All design decisions D4-D15 from the design review are non-negotiable inputs.

- [ ] `rdl-viewer-core`: tree component, detail-pane component, tab strip with overflow menu
- [ ] Custom JSON-RPC `rdl/elaboratedTree` — push elaborated AST (full tree, see TODO-1 for diff later)
- [ ] Auto-select first register on tab open (D4)
- [ ] Cmd-F inline filter input (D15)
- [ ] Stale-bar + library-only empty state + partial-tab amber-dot (D7/D8/D9)
- [ ] Dark + light theme tokens (D14)
- [ ] Auto-stack at < 700px viewport (D13)
- [x] WAI-ARIA Tree (roles + tabindex retained for screen readers; arrow-key
      nav was removed per user request 2026-04-30 — too easily disrupted by
      VSCode's editor focus model and not worth the maintenance)
- [x] **`rdl-viewer` CLI walking skeleton**: Bun HTTP server + fs.watch +
      `python -m systemrdl_lsp.dump` backend + inline SPA + SSE push.
      Renderer is duplicated for now (extension webview + CLI SPA);
      collapse into `rdl-viewer-core` once Svelte arrives.
- [ ] Replace PeakRDL-html webview iframe with served `rdl-viewer-cli` SPA on `localhost:5173`

## Week 6 — Bidirectional source map

- [ ] Click register in viewer → editor `revealRange` smooth-scroll + 200ms line flash (U2)
- [ ] Cursor position in editor → viewer auto-selects matching tree node (D10, 500ms debounce)
- [x] Right-click context menu in viewer: Copy Address / Copy Name / Reveal in Editor (U5)

## Deferred (TODOS.md)

- TODO-1: diff-based JSON-RPC push (after profiling shows >200ms lag)
- TODO-4: Perl preprocessor support (after first user issue)
- TODO-D1: user-overridable color palette
- TODO-D2: high-contrast theme tokens
