# Roadmap

Tracking the build sequence locked in `docs/design.md` (Approach B, post eng + design reviews).

> **Status (2026-05-01):** all weekly milestones complete. Latest release
> [v0.22.10](https://github.com/seimei-d/systemrdl-pro/releases/latest)
> ships the full LSP + viewer surface. This file is now mostly historical.

## Week 1 — Walking skeleton (publishable) *complete*

Goal: `code --install-extension systemrdl-pro` + open `.rdl` → see live diagnostics in editor +
empty viewer panel that opens via "Show Memory Map" command.

- [x] Monorepo scaffold (4 packages, schemas, scripts, CI placeholders)
- [x] `schemas/elaborated-tree.json` v0.1 (codegen foundation)
- [x] `systemrdl-lsp` v0.1: pygls + `publishDiagnostics` wrapping `systemrdl-compiler`
- [x] `vscode-systemrdl-pro` v0.1: LSP client + "Show Memory Map" command + webview placeholder
- [x] First public release as a `.vsix` on GitHub Releases (Marketplace publish
      deferred — requires Azure DevOps PAT; users install via drag-onto-Extensions
      or `code --install-extension <path>`)

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

Source of truth: the locked Variant B — Tree + Detail Pane layout. All
design decisions D4-D15 in `docs/design.md` are non-negotiable inputs.

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
      feedback ("just always lay it out this way")
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
- TODO-V1: caret-toggle button visual polish ("the button UI is ugly")
- TODO-R1: refactor `server.py` (~1500 lines) into themed modules
- TODO-D1: user-overridable color palette
- TODO-D2: high-contrast theme tokens

## Beyond Week 6 (post-v0.20.0)

Most of the post-Week-6 work landed in batches; see the full
[`TODOS.md`](../TODOS.md) (now empty) and the v0.22.10 release notes:

- **server.py refactor** (TODO-R1) — split into seven themed modules
  (`compile`, `diagnostics`, `hover`, `completion`, `definition`,
  `serialize`, `outline`) with a thin wiring shim.
- **Tier 1.1**: user-defined properties surface in hover + completion.
- **Tier 1.2**: `encode = my_enum;` rendered as a collapsible value
  table inside the field row, with width-tight hex padding.
- **Tier 1.3**: counter / interrupt tags on field rows.
- **Tier 1.4**: hover annotates property origin (`(← default at line N)`,
  `(← dynamic at line N)`).
- **Tier 1.5**: instance-name fallback for goto-def (signals included).
- **Tier 2.1**: parametrized type hover (`my_reg #(WIDTH=16)`).
- **Tier 2.2**: reference-path goto-def (`top.regfile.CTRL.enable`).
- **Tier 2.3**: dynamic property assignments distinguished in hover.
- **Tier 2.4**: split-access banner when `accesswidth < regwidth`.
- **Tier 3.3**: register binary decoder in Detail panel.
- **Tier 3.4**: collapse-all / expand-all buttons.
- **Tier 3.5** (TODO-D1): user palette via `systemrdl-pro.viewer.colors`.
- **Tier 3.6** (TODO-D2): high-contrast theme via `forced-colors: active`.
- **Tier 4** quick wins: documentHighlight, selectionRange, signatureHelp.
- **Tier 4.4**: type hierarchy (subtypes ≡ instances).
- **Codegen** (Decision 9A): `bun run codegen` regenerates Python +
  TS types from `schemas/elaborated-tree.json`. CI fails on drift.
- **Cross-file diagnostics**: errors in `\include`d files land on the
  right URI; clear-on-resolve cycle.
- **Webview-panel serializer**: Memory Map panels survive `Reload Window`
  via `WebviewPanelSerializer`.
- **Theme-aware chrome**: viewer routes through `--vscode-*` CSS
  variables and inherits any active VSCode theme.
