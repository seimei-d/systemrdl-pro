# TODOS

No deferred work pending. All items resolved as of v0.22.6.

## Resolved

- **TODO-1** — Version-gated tree push + `rdl/elaboratedTreeChanged` push
  notifications (commit `fa64908`, 2026-05-01).
- **TODO-4** — Perl preprocessor pre-flight check, `perlSafeOpcodes` setting,
  README docs for `<%=expr%>` whitespace gotcha (commit `5ef57d1`, 2026-05-01).
- **TODO-V1** — Caret-toggle redesigned as a real `<button>` with SVG chevrons
  and persistent affordance (commit `9f14cf9`, 2026-05-01).
- **TODO-R1** — `server.py` split into themed modules (commit `0bc6c2c`,
  2026-05-01). 1900-line monolith → 7 modules + ~470-line wiring shim.
- **TODO-D1** — User-overridable color palette via the
  `systemrdl-pro.viewer.colors` setting. Maps short keys (`rw`, `ro`,
  `accent`, `bg`, `panel`, …) to CSS custom properties; the extension
  injects them as `:root { --rdl-...: …; }` overrides on webview
  load (commit `3fca3eb`, 2026-05-01).
- **TODO-D2** — High-contrast theme via `@media (forced-colors: active)`.
  Tokens remap to system colour keywords (`Canvas`, `CanvasText`,
  `Highlight`, `LinkText`, …); selected rows get an outline since
  background tints don't survive HC. Activates on Windows High Contrast
  Mode and on VSCode's `hc-black` / `hc-light` themes (commit `3fca3eb`,
  2026-05-01).

## How to add new items

If something genuinely needs to be deferred, add a section above the
"Resolved" list with: **What**, **Why**, **Pros / Cons**, **Context**,
and a **Depends on / blocked by** clause that names the trigger
condition for promoting it from deferred to active.
