# Changelog

All notable changes to **SystemRDL Pro** are documented here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project uses [SemVer](https://semver.org/).

## [0.15.0] — 2026-04-30

First public Marketplace release. Walking skeleton ➝ feature-complete viewer.

### LSP

- `textDocument/diagnostics` — live, 300 ms debounce, 10 s elaborate timeout
  with last-good fallback
- `textDocument/hover` — resolved address/width/access on instances; markdown
  docs on every keyword / property / access value / user-defined type
- `textDocument/documentSymbol` — outline of `addrmap → regfile → reg → field`
- `textDocument/definition` — goto-def on type identifiers (cross-file)
- `textDocument/completion` — context-aware narrowing after `sw =` /
  `onwrite =` / `onread =`; user-defined types surface their `name` + `desc`
- `incl_search_paths` — explicit setting + auto-discovery from `peakrdl.toml`
- `systemrdl-pro.includeVars` — `$VAR` / `${VAR}` substitution in `include`
  paths (lightweight fallback for projects without `perl`; full Perl
  preprocessor works upstream when `perl` is on PATH)
- Auto-restart up to 3× in 60 s on LSP crash; manual `Restart LSP` command
- Multi-root elaboration — one tab per top-level `addrmap` definition

### Viewer

- Tree + detail-pane layout; tabs for multi-root files
- Collapsible containers (▼/▶) with caret-only toggle (body click reveals
  in editor)
- Cmd-F filter with scope selector (Name / Address / Field / All)
- Click register → editor scroll + 200 ms flash; cursor in editor → tree
  auto-selects matching node
- Right-click context menu: Copy Name / Address / Type / Reveal in Editor
- Stale-bar when current parse fails; viewer keeps last good tree
- Auto dark / light theme tokens via `prefers-color-scheme`
- Pulsing scroll-to-top button on long trees
- WAI-ARIA tree roles + tabindex for screen-reader navigation

### Architecture

- Renderer extracted from inline JS into shared
  [`@systemrdl-pro/viewer-core`](https://github.com/seimei-d/systemrdl-pro/tree/main/packages/rdl-viewer-core)
  React bundle, consumed by both the VSCode webview and the standalone
  `rdl-viewer` CLI

### Removed

- Arrow-key navigation in the tree — too easily disrupted by VSCode's editor
  focus model. ARIA roles + Tab-into still work for screen readers.
