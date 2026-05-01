# Changelog

All notable changes to **SystemRDL Pro** are documented here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project uses [SemVer](https://semver.org/).

## [0.19.0] — 2026-05-01

Three more cleanups: types are now schema-driven, cross-file diagnostics
land in the right editor, and include-paths UX is no longer opaque.

### Added

- **New command `SystemRDL: Show effective include paths`.** Quick-pick
  of every directory the LSP will search for `\` `include`d files,
  labeled by source (`setting` / `peakrdl.toml` / `sibling`). Press Enter
  on a row to reveal it in the OS file manager.
- **Cross-file diagnostics.** A syntax error inside an `\` `include`d
  file is now reported against that file's URI, not silently dropped.
  Fixing the error clears the squiggle (clear-on-resolve cycle).

### Internal

- **Codegen for elaborated-tree types** (Decision 9A). `bun run codegen`
  walks `schemas/elaborated-tree.json` and emits Python TypedDicts +
  TypeScript types. The hand-written shadow types in `extension.ts`,
  `viewer-core/types.ts`, etc. now re-export the generated copies.
  Drift detection: a CI test asserts the generated file matches the
  committed one.
- **Include-path resolution unified.** `_resolve_search_paths(uri)`
  returns one deduped, source-labeled list. Setting > peakrdl.toml >
  sibling-dir on collision (first-source-wins).

## [0.18.0] — 2026-05-01

Backlog cleanup: four TODOs closed in one batch.

### Added

- **`systemrdl-pro.perlSafeOpcodes` setting.** Override the Perl `Safe`
  opcode set (defaults are conservative — bans `print` and most I/O).
  Add `:base_io` to allow `print`-based code generation in `<% … %>`.
- **Perl pre-flight check.** When a buffer contains `<%` markers but
  `perl` is missing from PATH, surface a one-time warning notification
  instead of letting the compiler's fatal diagnostic fire on every save.
- **Push-driven Memory Map updates.** LSP now sends an
  `rdl/elaboratedTreeChanged` notification on every successful
  elaboration; the extension refreshes proactively without waiting for
  `didSaveTextDocument`. Open the panel, type — tree updates live.

### Changed

- **Version-gated tree fetches.** `rdl/elaboratedTree` accepts
  `sinceVersion`. If the LSP's cached version matches, the response is
  a constant-size `{unchanged: true, version}` envelope — skip
  serialization + transport on no-op refreshes (e.g. focus changes,
  panel re-mount). Same-version repeat fetches reuse a cached
  serialized dict on the LSP side.
- **Polished caret-toggle button.** Tree expand/collapse glyph was a
  text `<span>` with hover-only background, reading as a glyph rather
  than an affordance. Replaced with a real `<button>` (proper a11y),
  22×22 click target, persistent subtle background, SVG chevron at
  10×10 / 1.6 px stroke (sharper at HiDPI than `▼/▶`).

### Internal

- **`server.py` refactored** from a 1900-line monolith into seven
  themed modules (`compile`, `diagnostics`, `hover`, `completion`,
  `definition`, `serialize`, `outline`) plus a ~470-line LSP wiring
  shim. All 44 tests pass unchanged — the existing test surface
  imports through `systemrdl_lsp.server` re-exports.

### Docs

- README: new **Perl preprocessor** section documents the `perl` PATH
  requirement, the `<%=$i%>` no-leading-whitespace gotcha, and the new
  opcode-override setting.

## [0.17.0] — 2026-04-30

### Changed

- **Multi-tab Memory Map.** One panel per `.rdl` file (markdown-preview-style)
  instead of a single shared panel. Open `chip_a.rdl`, run Show Memory Map,
  switch to `chip_b.rdl`, run again — both tabs now coexist. Re-running on a
  file that already has a panel just brings it forward.
- **Status bar follows the active editor.** When you switch between two
  `.rdl` files with open panels, the reg/error count tracks the focused file.
- **Inlay hints moved to end-of-line** with `→ 0xADDR` glyph. Earlier
  position broke names mid-word (`CTR (0x...)L`); end-of-line never collides.

### Removed

- **`📋 Open in Memory Map` CodeLens** — redundant with `Ctrl+Shift+V`
  shortcut and the `📊 N regs · 0x..0x` summary lens stays.

### Fixed

- **Bit-field grid redesign.** Fields now span their full width as one cell
  (was: one cell per bit, name clipped to 1 letter). Datasheet-style row
  with bit indices on top, colored field cells underneath, gaps render as
  reserved cells.

## [0.16.0] — 2026-04-30

Major UX upgrade across editor and viewer.

### Added (editor)

- **Snippets** — `addrmap`, `regfile`, `reg`, `regtyped`, `field`, `fieldw1c`,
  `fieldcounter`, `include`, `perlloop` expansions with tab-stops.
- **Folding ranges** — collapsible `{...}` blocks via dedicated LSP provider
  (more reliable than indent-based folding on irregular formatting).
- **Inlay hints** — resolved absolute address shown ghost-grey after each
  register name (e.g. `} CTRL @ 0x0   (0x0000_0010)` for nested instances).
- **CodeLens** above every `addrmap` declaration — `📊 N regs · 0x0..0xN`
  summary + `📋 Open in Memory Map` clickable link.
- **Workspace symbols** (`Ctrl+T`) — search registers across every `.rdl`
  file the LSP has touched.
- **Address conflict warnings** — overlapping reg ranges anywhere in the
  elaborated tree now emit a warning diagnostic (defence-in-depth on top of
  systemrdl-compiler's direct sibling check).
- **Onboarding walkthrough** — first-run "Get Started" page with 4 cards.
- **Status bar diagnostics counter** — current file's `$(error) N`
  / `$(warning) M` count appended next to the reg/root summary; updates
  on every diagnostic change.

### Added (viewer)

- **Bit-field grid** — visual `[width-1..0]` cell strip in the detail pane
  with colour-coded RW / RO / W1C / etc. fields and field names overlaid
  inside their bit ranges.

## [0.15.1] — 2026-04-30

### Added

- Keybinding **Ctrl+Shift+V** (Cmd+Shift+V on macOS) opens the Memory Map
  panel when a `.rdl` file is focused in the editor. Mirrors the markdown
  preview shortcut so the muscle memory carries over.

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
