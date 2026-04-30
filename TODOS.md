# TODOS

## Deferred

### TODO-1: Diff-based JSON-RPC push for elaborated tree

**What:** Replace full-tree push (`rdl/elaboratedTree`) with diff push for incremental updates.

**Why:** On large register maps (5MB+ JSON), pushing the full tree on every edit causes
serialization lag (~1s on the LSP side, similar on viewer parse). User-perceptible.

**Pros:** Latency stays sub-100ms even for very large maps. Less memory churn in viewer.
**Cons:** Tree-id + version tracking on every node. More complex invalidation logic. Risk
of subtle bugs where viewer state diverges from LSP state.

**Context:** Current MVP design (Approach B, week 4-5) does full tree push on every
elaboration with 300ms debounce. Profile real-world maps (typical chip: 500-2000 regs,
200KB-1MB JSON) before deciding if needed. If profiling shows >200ms lag on viewer side,
graduate this from TODO to active work.

**Depends on / blocked by:** Need real usage profiling first. Premature optimization
otherwise. Triggered when first user reports "viewer feels slow on my chip."

---

### TODO-4: Perl preprocessor support (`<% ... %>` and `<%= $VAR %>`)

**What:** Add support for SystemRDL Perl-level preprocessor (clause 16 of spec).

**Why:** Real industrial SystemRDL projects often use Perl directives for variables in
include paths (e.g., `` `include "<%=$ENV{IP_ROOT}%>/lib.rdl" ``) and for loop-based
register generation. Without support, these projects fail to load entirely.

**Pros:** Unblocks adoption from teams using commercial-tool conventions. Removes a
hard limitation. Differentiator vs basic vscode-systemrdl extension.
**Cons:** systemrdl-compiler itself does not support Perl preprocessing. Implementation
must be a pre-pass (text transformation) before passing to systemrdl-compiler. Source
locations get scrambled by the pre-pass — diagnostics need source map back to original.

**Context:** SystemRDL 2.0 spec defines two preprocessor levels:
- Verilog-style (` `include`, ` `define`, ` `ifdef`) — supported by systemrdl-compiler
- Perl-style (`<% %>`, `<%= %>`) — NOT supported by systemrdl-compiler

Implementation options:
- External `m4` pre-step (limited but standard)
- Embedded `Text::EP3` Perl module (matches spec, requires Perl interpreter)
- Custom subset interpreter in Python (just env-var expansion, covers 80% of real use)

For MVP (v1.0): document limitation in README. After first issue from user requesting it,
choose implementation based on which use case they need.

**Depends on / blocked by:** v1.0 must ship first. User issue driving prioritization.

---

### TODO-D1: User-overridable color palette via VSCode settings.json

**What:** Allow workspace/user override of access-mode colors and chrome tokens via
`systemrdl-pro.viewer.colors.*` settings (e.g., `colors.rw`, `colors.ro`,
`colors.background`).

**Why:** Some hardware teams have internal documentation conventions (e.g., RW=blue
in their existing tooling) and want viewer screenshots to match. Locked palette = paste
mismatch between viewer and team docs.

**Pros:** Power-user escape hatch, theme-customization friendly. CSS variables already
exist (Pass 5 design tokens) so plumbing is light: workspace settings reader → CSS var
overrides on webview load.
**Cons:** Premature without first user request. Requires +1 settings schema entry per
token, +1 docs section explaining defaults.

**Context:** Mockup B locked the muted/professional palette in design review on
2026-04-29 (decision D12). Override TODO surfaced as Pass 7 unresolved item U6.
Add when first user opens an issue with concrete corp palette.

**Depends on / blocked by:** v1.0 must ship first. CSS variable architecture is a
prerequisite, already in Pass 5 design tokens.

---

### TODO-V1: Polish caret-toggle button visual

**What:** The `.caret-toggle` span in the tree-pane container row works but reads
as a hover-highlighted text glyph, not a button. User feedback 2026-04-30:
"UI кнопки ужасен."

**Why:** Affordance is unclear — the caret looks identical to a regular row's
caret column except on hover. New users don't realise it's a click target.

**Pros (of fix):** Higher discoverability, more polished feel.
**Cons:** Risks breaking the tree's information density — anything bigger
crowds the addr/name columns.

**Sketch of fix:**
- Slightly bolder caret glyph (▾/▸ instead of ▼/▶, or use Codicon chevrons
  via vscode-codicons font when bundled in the webview)
- Subtle background even when not hovered (low-opacity panel tint)
- Increase clickable area without growing visible width (use ::before
  pseudo-element with negative margin)
- Possibly switch to a real `<button>` element with VSCode-themed background
  for proper a11y semantics (also helps WAI-ARIA work).

**Context:** Logged at 0.9.1. Defer until WAI-ARIA + keyboard nav lands so the
visual + a11y work happens in one pass.

---

### TODO-R1: Refactor `systemrdl_lsp/server.py` into focused modules

**What:** Split the now ~1300-line `server.py` into themed modules:

- `compile.py` — `_compile_text`, `_elaborate`, `ElaborationCache`,
  `CapturingPrinter`, `CompilerMessage`
- `diagnostics.py` — severity mapping, `_publish_diagnostics`, range builders
- `hover.py` — `_hover_text_for_node`, `_hover_for_word`, `_node_at_position`
- `completion.py` — keyword/property/value catalogues, `_completion_*`
- `definition.py` — `_word_at_position`, `_definition_location`,
  `_comp_defs_from_cached`
- `serialize.py` — `_serialize_root`, `_serialize_addressable`, `_serialize_reg`,
  `_serialize_field`
- `outline.py` — `_document_symbols`
- `server.py` — keeps `build_server()`, `ServerState`, and the LSP feature
  registrations only

**Why:** `server.py` mixes seven distinct concerns. New features (more
hover scopes, signature help, code actions) inevitably grow the same file
because everything is closure-bound to `build_server`. Module split unblocks
typed test surface (each helper testable in isolation), reduces merge friction
between feature additions, and makes onboarding read-the-source viable.

**Pros:** ~7 ×150-line files vs one 1300-liner; LSP request handlers stay
small and obvious. Tests already import each helper individually so the
move is mechanical.
**Cons:** Cache, server state, and capture-printer have to thread through as
explicit args (currently closure-captured by `build_server`). Some helpers
need shared imports (e.g. `systemrdl.node.AddrmapNode`) — pull into a
`_node_imports.py` to avoid circular deps.

**Context:** User flagged file size 2026-04-30 after the completion +
context-aware hover landed. Defer until the LSP feature surface stabilises
(after Week 6 right-click context menu lands) to avoid double-refactor.

**Depends on / blocked by:** Feature-complete LSP first (Week 2-3 + Week 6).
Plan: do the refactor in one PR per module, each passing the existing tests
unchanged.

---

### TODO-D2: High-contrast theme support (hc-black / hc-light)

**What:** Add a third theme variant beyond the locked dark and light palettes —
high-contrast tokens that meet WCAG AAA (7:1) for low-vision users and
Windows High Contrast Mode integration.

**Why:** Webview content does NOT inherit OS-level high-contrast settings. Without
explicit hc tokens, a low-vision user with hc-black VSCode theme still sees the
mid-contrast viewer chrome — inaccessible. Universal Pass 6 a11y rule: contrast
ratios must be respected.

**Pros:** Reputation factor (open source dev tool with explicit a11y wins). Good
first-issue for community contributors. Detection via `window.matchMedia('(forced-colors: active)')`
+ VSCode `vscode.window.activeColorTheme.kind === ColorThemeKind.HighContrast`.
**Cons:** +1 token set to maintain; access-mode colors must re-derive at 7:1 (some
combinations impossible — may need to drop saturation for pattern fills as fallback).

**Context:** Pass 6 design review on 2026-04-29 acknowledged contrast verification
in light + dark passes WCAG AA (4.5:1) but explicitly skipped AAA hc tokens.
Mockup B colors (D12) need parallel hc set.

**Depends on / blocked by:** v1.0 ships first. Light + dark token system from Pass 5
must be finalized before adding the third variant.
