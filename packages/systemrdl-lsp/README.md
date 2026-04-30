# systemrdl-lsp

Language Server Protocol implementation for SystemRDL 2.0 — backed by
[systemrdl-compiler](https://github.com/SystemRDL/systemrdl-compiler).

> **Status:** v0.1 walking skeleton. Only `textDocument/publishDiagnostics` is implemented.
> Hover, document symbols, goto definition, completion, and the custom `rdl/elaboratedTree`
> push come in Week 2-5. See [`../../docs/ROADMAP.md`](../../docs/ROADMAP.md).

## Install

```bash
pip install systemrdl-lsp
```

Or with `uv`:

```bash
uv pip install systemrdl-lsp
```

## Use

The server speaks LSP over stdio. Editor integrations call:

```bash
systemrdl-lsp
```

For VSCode users, install the [`vscode-systemrdl-pro`](https://marketplace.visualstudio.com/items?itemName=seimei-d.vscode-systemrdl-pro)
extension — it manages the LSP for you.

For Vim/Neovim/Helix/Emacs, configure your client to launch `systemrdl-lsp` for files matching
`*.rdl`. Example for Neovim with `nvim-lspconfig` (config name `systemrdl_lsp` will be added in Week 2):

```lua
-- nvim-lspconfig snippet (Week 2 PR)
require'lspconfig'.systemrdl_lsp.setup{}
```

## Configuration

| Setting | Default | What |
|---------|---------|------|
| `systemrdl-lsp.includePaths` | `[]` | List of directories searched by `` `include `` (Week 2). |

## What works in v0.1

- Detect SystemRDL files (extension `.rdl`)
- On open / change: run parse + elaboration via `systemrdl-compiler`, publish diagnostics
  with file/line/column from `RDLCompileError`
- Stdio LSP framing via `pygls`

## What does NOT work yet

See [`../../docs/ROADMAP.md`](../../docs/ROADMAP.md). All of: hover, goto definition,
document symbols, completion, the elaborated-tree push, multi-root `incl_search_paths`,
LSP supervisor + restart, and Perl preprocessor support. They are scheduled — none have
been forgotten.

## License

MIT — see [`../../LICENSE`](../../LICENSE).
