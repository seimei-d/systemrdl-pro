# TODOS

## Deferred

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

---

## Resolved

- **TODO-1** — Version-gated tree push + `rdl/elaboratedTreeChanged` push
  notifications (commit `fa64908`, 2026-05-01).
- **TODO-4** — Perl preprocessor pre-flight check, `perlSafeOpcodes` setting,
  README docs for `<%=expr%>` whitespace gotcha (commit `5ef57d1`, 2026-05-01).
- **TODO-V1** — Caret-toggle redesigned as a real `<button>` with SVG chevrons
  and persistent affordance (commit `9f14cf9`, 2026-05-01).
- **TODO-R1** — `server.py` split into themed modules (commit `0bc6c2c`,
  2026-05-01). 1900-line monolith → 7 modules + ~470-line wiring shim.
