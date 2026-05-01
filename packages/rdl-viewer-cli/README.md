# rdl-viewer

Standalone browser-served SystemRDL memory-map viewer. No editor required.
Same renderer as the [VSCode extension](https://marketplace.visualstudio.com/items?itemName=seimei-d.systemrdl-pro)
(shared React component in [`@systemrdl-pro/viewer-core`](../rdl-viewer-core)),
different transport.

```bash
# Install systemrdl-lsp once (provides the elaboration backend):
pip install systemrdl-lsp

# Then run the viewer against any .rdl file:
bun /path/to/rdl-viewer-cli/src/index.ts my-chip.rdl
# → http://localhost:5173/  (auto-opens in your default browser)
```

## What it does

- Compiles the file via `python -m systemrdl_lsp.dump` (one-shot)
- Watches the file with `fs.watch`, recompiles on every save
- Serves the same tree + detail-pane layout as the VSCode webview at
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
| `GET /` | SPA shell that loads the shared viewer-core bundle |
| `GET /tree` | Latest elaborated-tree JSON (matches `schemas/elaborated-tree.json`) |
| `GET /events` | Server-Sent Events stream — one frame per recompile |
| `GET /diagnostics` | Stderr from the latest `systemrdl_lsp.dump` invocation |
| `GET /viewer/*` | Static assets from `@systemrdl-pro/viewer-core/dist/` |

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
                        │ (viewer-core)  │
                        └────────────────┘
```

The CLI is a thin shell. The renderer lives in
[`@systemrdl-pro/viewer-core`](../rdl-viewer-core) and is shared with the
VSCode webview pixel-for-pixel — same React components, same CSS variables,
same elaborated-tree contract.

## Caveats

- Single-file watch only; `` `include ``d files don't auto-trigger recompile yet.
- No "reveal in editor" — there is no editor in this surface.
- Cmd-F filter, tabs, stale-bar, and the detail pane all match the VSCode
  webview behaviour.

## License

MIT — see [`LICENSE`](https://github.com/seimei-d/systemrdl-pro/blob/main/LICENSE).
