# Changelog

All notable changes to **SystemRDL Pro** are documented here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project uses [SemVer](https://semver.org/).

## [0.21.0] — 2026-05-01

### Added (viewer)

- **Memory map overview strip.** A new horizontal pane above the tree
  shows every direct child of the active addrmap as a clickable tile.
  Tiles flex-grow by `log²(size)` so multi-MB regfiles take more visual
  space than 4-byte registers but the smallest items never disappear.
  Reserved gaps render as dashed empty tiles between named children.
  - **Click on a regfile/addrmap tile** drills into it; a breadcrumb at
    the top tracks the stack and lets you climb back up.
  - **Click on a register tile** reveals it in the editor and selects the
    matching node in the tree below.
  - Hover any tile for full address + size + access summary tooltip.
  - Toggle button in the tabs row hides/shows the overview pane.
- Tiles are colour-accented by access mode (left-border stripe — RW
  green, RO blue, W1C amber, etc.) without overpowering the chrome
  background.

### Fixed

- **`textDocument/semanticTokens/full` failure resolved.** Diagnosed
  via the user-shared traceback as a pygls signature-introspection
  edge case: `from __future__ import annotations` + `get_type_hints`
  evaluating a return-type annotation imported only in a local scope
  returned NameError, which `has_ls_param_or_annotation`'s try/except
  swallowed, so pygls thought the handler didn't take an `ls` arg
  and called it with one positional instead of two — `TypeError` on
  every keystroke, visible as editor lag. Fix: import
  `SemanticTokens`, `SemanticTokensLegend`, and the method constant
  at module level.

## [0.20.1] — 2026-05-01

Hot-fixes for the four issues reported on 0.20.0:

### Fixed

- **Semantic tokens request failure caused editor lag.** The handler
  was throwing on some buffers, and VSCode retries failing
  `semanticTokens/full` on every keystroke — that's where the
  "the bigger the window the more it lags" came from. Switched
  registration to the simpler `SemanticTokensLegend` form (was
  `SemanticTokensRegistrationOptions`) and wrapped the handler in
  defensive try/except so a future bug returns empty tokens instead
  of looping forever.
- **Workspace pre-index defaulted to ON.** Multi-window setups had
  every VSCode window racing its own pre-index walk, pegging CPU.
  Default flipped to OFF; users who want workspace-wide search opt
  in via `systemrdl-pro.preindex.enabled`. When enabled, the walker
  is now serial (1 file at a time) with a 5 s startup delay so it
  doesn't compete with initial editor activity.

### Changed (viewer)

- **Bit-field grid: multi-line names + no duplicate bit indices.**
  Names like `TRANSMIT_BUFFER_FULL` were truncating to "f…" because
  cells were locked single-line. They now wrap (`word-break`,
  `overflow-wrap: anywhere`) and cells are taller. Bit ranges no
  longer appear inside cells — the header row above already shows
  every index, datasheet-style.
- **Scroll-to-top button redesigned.** The pulsing blue circle was
  too loud for a navigation aid. Replaced with a small chip-style
  button (28×28, panel background, accent border on hover) carrying
  an SVG chevron. Same position, much quieter.

## [0.20.0] — 2026-05-01

Seven LSP features in one release. Hardware register-map editing now has
the table stakes most language servers offer (rename, find references,
formatter, etc.).

### Added (LSP)

- **Document links** on `\` `include "..."` directives. Ctrl+click jumps
  to the included file. Resolves through the same search-path chain the
  compiler uses, including `$VAR` substitution and peakrdl.toml.
- **Find references** (`Shift+F12`). Identifier under cursor →
  every instantiation site, cross-file. Optional declaration in the result.
- **Rename refactoring** (`F2`). Renames a top-level type
  identifier across its declaration and every instantiation. Validates
  the new name as a SystemRDL identifier and refuses to shadow existing
  types.
- **Semantic tokens** (`textDocument/semanticTokens/full`).
  Distinguishes properties (`sw`, `hw`, `reset`) from access values
  (`rw`, `ro`, `woclr`) at the LSP level — TextMate alone can't tell
  them apart. Works on broken files (no elaboration dependency).
- **Code action: "Add `= 0` reset value"**. Lightbulb on field
  declarations missing an explicit reset. Inserts ` = 0` before the
  semicolon.
- **Document formatting** (`Shift+Alt+F`). Conservative: trims trailing
  whitespace, normalises tabs to spaces (respecting editor tabSize),
  ensures a single trailing newline. Idempotent. No opinionated
  alignment.
- **Workspace pre-index**. On first launch, walks every `.rdl` file in
  the workspace and pre-elaborates it in the background (4-way
  concurrent, capped at 200 files by default). `workspace/symbol`
  (`Ctrl+T`) now finds symbols across files the user hasn't yet
  opened.

### Added (settings)

- `systemrdl-pro.preindex.enabled` — toggle the pre-index walker.
- `systemrdl-pro.preindex.maxFiles` — cap on files visited (default 200).

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
