# systemrdl-pro

LSP server + interactive memory-map viewer for SystemRDL 2.0 hardware description language.

> **Status:** early walking skeleton (Week 1). LSP boots, reports parse diagnostics, opens an empty
> viewer panel. Live memory map (Week 4-5) and source-map jumps (Week 6) are not yet implemented.
> See [docs/design.md](docs/design.md) for the full plan.

## What's in this repo

| Package | What | Status |
|---------|------|--------|
| [`systemrdl-lsp`](packages/systemrdl-lsp/) | Python LSP server (pygls + systemrdl-compiler) | v0.1 — `publishDiagnostics` only |
| [`rdl-viewer-core`](packages/rdl-viewer-core/) | Shared Svelte components for the memory-map viewer | scaffold |
| [`rdl-viewer-cli`](packages/rdl-viewer-cli/) | Standalone CLI: `rdl-viewer file.rdl --serve` | scaffold |
| [`vscode-systemrdl-pro`](packages/vscode-systemrdl-pro/) | VSCode extension (LSP client + webview panel) | v0.1 — diagnostics + placeholder webview |

## Why another SystemRDL extension

Existing [`SystemRDL/vscode-systemrdl`](https://github.com/SystemRDL/vscode-systemrdl) provides
TextMate grammar only. It is the right starting point for syntax highlighting (we fork the
grammar) but does not give diagnostics, hover, goto-definition, completion, or a live memory
map. `systemrdl-pro` is a separate extension built around the LSP — install both is fine.

## Quickstart (development)

```bash
# Python side (LSP server)
uv sync
uv run systemrdl-lsp --help

# JS/TS side (extension + viewer)
bun install
bun run --cwd packages/vscode-systemrdl-pro build
```

For end-user install instructions see [`packages/vscode-systemrdl-pro/README.md`](packages/vscode-systemrdl-pro/README.md).

## Project structure

```
systemrdl-pro/
├── packages/
│   ├── systemrdl-lsp/         # Python LSP (PyPI publish)
│   ├── rdl-viewer-core/       # Svelte components (workspace dep)
│   ├── rdl-viewer-cli/        # CLI binary (npm publish)
│   └── vscode-systemrdl-pro/  # Extension (Marketplace publish)
├── schemas/
│   └── elaborated-tree.json   # JSON Schema source of truth
├── scripts/
│   └── codegen.sh             # Schema → Python types + TS types
├── docs/
│   ├── design.md              # Full design doc
│   └── ROADMAP.md             # Week 1-6 build sequence
├── .github/workflows/
│   ├── ci.yml
│   ├── lsp-publish.yml
│   ├── viewer-publish.yml
│   └── extension-publish.yml
├── package.json               # Bun workspace
├── pyproject.toml             # uv workspace
└── LICENSE                    # MIT
```

## Roadmap

- [x] **Week 1** — walking skeleton, Marketplace publish, LSP diagnostics, PeakRDL-html webview
- [ ] **Week 2-3** — full LSP (hover, documentSymbol, definition, completion, `incl_search_paths`)
- [ ] **Week 4-5** — Svelte live viewer, custom JSON-RPC `rdl/elaboratedTree`, multi-root tabs
- [ ] **Week 6** — bidirectional source map (click in viewer → editor jump, hover in editor → viewer highlight)

See [docs/ROADMAP.md](docs/ROADMAP.md) for the detailed sequence.

## Contributing

Issues and PRs welcome. The design decisions for the viewer UX are locked — see
[docs/design.md](docs/design.md) section "Viewer UX" before proposing UI changes.

## License

MIT — see [LICENSE](LICENSE).

## Credits

- Built on [systemrdl-compiler](https://github.com/SystemRDL/systemrdl-compiler) (Alex Mykyta, MIT)
- LSP framework: [pygls](https://github.com/openlawlibrary/pygls) (Apache-2.0)
- TextMate grammar forked from [SystemRDL/vscode-systemrdl](https://github.com/SystemRDL/vscode-systemrdl) (MIT)
