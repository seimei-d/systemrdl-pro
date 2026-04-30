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

- [x] `textDocument/hover` — resolved address, width, sw/hw access
      (extended: word-based hover for keywords/properties/values/types too)
- [x] `textDocument/documentSymbol` — outline of regfiles + regs in editor sidebar
- [x] `textDocument/definition` — goto-def for component types and parameters
- [x] `textDocument/completion` — register names, properties (context-aware
      after `sw =` / `onwrite =` / `onread =`, with per-keyword markdown docs)
- [x] `incl_search_paths` setting + auto-discovery from `peakrdl.toml`
- [x] `systemrdl-pro.includeVars` — `$VAR` / `${VAR}` substitution in `include
      paths (lightweight fallback for projects without `perl` installed; full
      Perl preprocessor already works upstream when `perl` is on PATH)
- [x] **Eng-review-locked Week 2 deliverables**:
  - [x] LSP supervisor + "Restart LSP" command (silent-failure gap #1)
  - [x] `if (panel.visible) postMessage(...)` guard (silent-failure gap #2)
  - [x] `asyncio.wait_for(elaborate, timeout=10)` + push last-good (silent-failure gap #3)
  - [x] Multi-root elaboration (moved here from Week 5 due to design decision 3C)

## Week 4-5 — Live Svelte viewer

Source of truth mockup: `~/.gstack/projects/systemrdl-vscode/designs/viewer-panel-20260429/variant-B-tree-detail.html`.
All design decisions D4-D15 from the design review are non-negotiable inputs.

- [x] `rdl-viewer-core`: React components (Viewer, Tree, TreeRow, Detail,
      ContextMenu) — went with React over Svelte per user choice 2026-04-30
- [x] Custom JSON-RPC `rdl/elaboratedTree` — push elaborated AST (full tree, see TODO-1 for diff later)
- [x] Auto-select first register on tab open (D4)
- [x] Cmd-F inline filter input (D15) — with explicit scope selector
      (All / Name / Address / Field; Access dropped 2026-04-30 as too noisy)
- [x] Stale-bar wired (D7); library-only empty state shown when 0 addrmaps
      (D8); partial-tab amber-dot (D9) **deferred** — multi-root tree never
      partial-fails; if one addrmap fails to elaborate the others still render
- [x] Dark + light theme tokens (D14) — defined as CSS variables, follow
      `prefers-color-scheme`
- [x] Auto-stack at < 700px viewport (D13) — actually always-stacked per user
      feedback ("сделай всегда такой view")
- [x] WAI-ARIA Tree (roles + tabindex retained for screen readers; arrow-key
      nav was removed per user request 2026-04-30 — too easily disrupted by
      VSCode's editor focus model and not worth the maintenance)
- [x] **`rdl-viewer` CLI walking skeleton**: Bun HTTP server + fs.watch +
      `python -m systemrdl_lsp.dump` backend + SPA + SSE push.
- [x] Both surfaces consume the same `@systemrdl-pro/viewer-core` React bundle
      (extension webview loads it via webview.asWebviewUri; CLI serves it as
      static. Replaces the PeakRDL-html iframe approach entirely.)
- [x] Pulsing scroll-to-top button (added 2026-04-30 for the 1000-reg stress fixture)

## Week 6 — Bidirectional source map

- [x] Click register in viewer → editor `revealRange` smooth-scroll + 200ms line flash (U2)
- [x] Cursor position in editor → viewer auto-selects matching tree node (D10, 500ms debounce)
- [x] Right-click context menu in viewer: Copy Address / Copy Name / Reveal in Editor (U5)

## Deferred (TODOS.md)

- TODO-1: diff-based JSON-RPC push (after profiling shows >200ms lag — current
  full-tree push handles 1000-reg fixture in <1s)
- TODO-4: Perl preprocessor polish — surface "perl missing" notification +
  `perlSafeOpcodes` setting (the preprocessor itself already works upstream)
- TODO-V1: caret-toggle button visual polish ("UI кнопки ужасен")
- TODO-R1: refactor `server.py` (~1500 lines) into themed modules
- TODO-D1: user-overridable color palette
- TODO-D2: high-contrast theme tokens

## Open

- [ ] **First Marketplace publish** — needs `vsce login` with a Personal Access
      Token. Manual one-shot when ready; nothing in the codebase blocks it.
