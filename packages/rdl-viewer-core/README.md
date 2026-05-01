# @systemrdl-pro/viewer-core

> **Internal workspace package.** Not published to npm.

Shared React components and CSS for the SystemRDL memory-map viewer.
Consumed by both surfaces:

- `vscode-systemrdl-pro` — runs inside the VSCode webview
- `rdl-viewer-cli` (`rdl-viewer`) — runs in a standalone browser tab,
  served by a Bun HTTP server

Both surfaces render the same tree + detail pane, the same field grid,
the same access-mode colour fill, the same address bands. Only the
transport differs (VSCode `postMessage` vs HTTP + SSE).

## What lives here

- `src/Viewer.tsx` — top-level layout (tabs, breadcrumb, tree, detail, address-map overview)
- `src/Tree.tsx`, `src/DetailPane.tsx`, `src/BitGrid.tsx`, `src/FieldRow.tsx`, ...
- `src/styles.css` — design tokens (CSS variables, see `docs/design.md` "Design Tokens")
- `src/types.ts` — TypeScript types codegened from `schemas/elaborated-tree.json`
  via `bun run codegen` at the repo root (do not hand-edit)

## Build

```bash
bun run build
```

Emits `dist/viewer.js` (ESM) + `dist/viewer.css`. The extension's `build.mjs`
and the CLI both consume these artifacts directly.

## Why a separate package?

The VSCode webview and the standalone browser CLI must render identically.
Sharing a built artifact (rather than re-implementing each surface) is the
only way that holds up over time.

See `docs/design.md` (Variant B locked: Tree + Detail Pane).

## Source

[github.com/seimei-d/systemrdl-pro](https://github.com/seimei-d/systemrdl-pro)
