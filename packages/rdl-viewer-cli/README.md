# rdl-viewer

Standalone browser-served SystemRDL memory-map viewer. No editor required.

```bash
# Install systemrdl-lsp once (provides the elaboration backend):
pip install systemrdl-lsp

# Then run the viewer against any .rdl file:
bun /path/to/rdl-viewer-cli/src/index.ts my-chip.rdl
# → http://localhost:5173/  (auto-opens in your default browser)
```

## What it does

- Compiles the file via `systemrdl-lsp` (one-shot `python -m systemrdl_lsp.dump`)
- Watches the file with `fs.watch`, recompiles on every save
- Serves the same tree+detail-pane layout as the VSCode webview at
  `http://localhost:<port>/`
- Pushes updates to the browser over Server-Sent Events (`/events`)

## Options

| flag | default | description |
|---|---|---|
| `--port`, `-p` | `5173` | HTTP port |
| `--no-open` | (open) | Skip auto-opening the browser |
| `--python <path>` | (auto) | Python interpreter with `systemrdl-lsp` |

## Python resolution

1. `--python <path>` (explicit win)
2. `$VIRTUAL_ENV/bin/python` (picks up `uv run` and `source .venv/bin/activate`)
3. `python3`, `python` on `PATH`

## Endpoints

| path | content |
|---|---|
| `GET /` | Inline single-page app (HTML + CSS + JS, ~17 KB) |
| `GET /tree` | Latest elaborated-tree JSON (matches `schemas/elaborated-tree.json`) |
| `GET /events` | Server-Sent Events stream — one frame per recompile |
| `GET /diagnostics` | Stderr from the latest `systemrdl-lsp.dump` invocation |

## Architecture

```
┌──────────┐  fs.watch  ┌────────────────┐  spawn   ┌─────────────────┐
│ user.rdl │ ─────────▶ │  rdl-viewer    │ ───────▶ │ python -m       │
└──────────┘            │  (Bun HTTP)    │ ◀─JSON── │ systemrdl_lsp   │
                        └────────────────┘          │ .dump file.rdl  │
                               │                    └─────────────────┘
                               │ HTTP + SSE
                               ▼
                        ┌────────────────┐
                        │ browser SPA    │
                        └────────────────┘
```

The CLI is a thin shell that re-uses the same elaborated-tree contract
(`schemas/elaborated-tree.json` v0.1.0) the VSCode webview consumes — the
renderer is currently inlined here, will move to `@systemrdl-pro/viewer-core`
once Svelte/codegen lands (see `docs/ROADMAP.md` Week 5).

## Caveats

- Single-file watch only; `\`include`-d files don't auto-trigger recompile yet.
- No "reveal in editor" — there is no editor in this surface.
- Cmd-F filter, tabs, stale-bar, and the detail pane all match the VSCode
  webview pixel-for-pixel (Decision 1C: same renderer, different transport).
