# systemrdl-lsp

Language Server Protocol implementation for **SystemRDL 2.0** — backed by
[`systemrdl-compiler`](https://github.com/SystemRDL/systemrdl-compiler) and
[pygls](https://github.com/openlawlibrary/pygls). Editor-agnostic; ships
the full LSP feature surface most tools have for mainstream languages.

## Install

```bash
pip install systemrdl-lsp
# or
uv pip install systemrdl-lsp
```

The server speaks LSP over stdio. Editor integrations launch the
`systemrdl-lsp` CLI and pipe.

## Use it from your editor

### VSCode / VSCodium / Cursor / Theia

Install the [`SystemRDL Pro`](https://marketplace.visualstudio.com/items?itemName=seimei-d.systemrdl-pro)
extension (also on [Open VSX](https://open-vsx.org/extension/seimei-d/systemrdl-pro)).
The extension supervises this LSP for you — restart, crash recovery, the lot.

### Neovim (`nvim-lspconfig`)

```lua
require'lspconfig.configs'.systemrdl_lsp = {
  default_config = {
    cmd = { 'systemrdl-lsp' },
    filetypes = { 'systemrdl' },
    root_dir = require'lspconfig.util'.root_pattern('peakrdl.toml', '.git'),
    settings = {
      ['systemrdl-pro'] = {
        includePaths = {},   -- list of dirs searched by `include
        includeVars = {},    -- $VAR / ${VAR} substitution in include paths
      },
    },
  },
}
require'lspconfig'.systemrdl_lsp.setup{}
```

### Helix

Add to `~/.config/helix/languages.toml`:

```toml
[language-server.systemrdl-lsp]
command = "systemrdl-lsp"

[[language]]
name = "systemrdl"
scope = "source.rdl"
file-types = ["rdl"]
language-servers = ["systemrdl-lsp"]
```

### Emacs (`eglot`)

```elisp
(add-to-list 'eglot-server-programs '(systemrdl-mode . ("systemrdl-lsp")))
```

## Feature surface

Implemented and shipped in v0.15.0:

- **Live diagnostics** — 300 ms debounce, 10 s timeout fallback, last-good
  cache, per-URI bucketing for `` `include ``d files (clear-on-resolve cycle).
- **Hover** — instance address/width/access for regs, parameter values for
  parametrized types, `bridge` flag for addrmaps, `(← default at line N)`
  annotation when a property comes from a `default` or dynamic assignment.
- **Goto-definition** (F12 / Ctrl-click) — top-level types, instance names
  (signals, registers), reference paths like `top.regfile.CTRL.enable`
  (segment-by-segment), cross-file via `` `include ``.
- **Find references** (Shift+F12) — every instantiation of a type, cross-file.
- **Rename** (F2) — workspace-wide, refuses on collision.
- **Completion** — ~85 keywords / properties / access values + user-defined
  types + user-defined properties. Context-aware narrowing: after `sw =` only
  access modes, after `addressing =` only `compact / regalign / fullalign`.
- **Document symbols / outline** — `addrmap → regfile → reg → field`.
- **Folding ranges**, **inlay hints** (resolved absolute address ghost-grey
  at end-of-line), **CodeLens** (`📊 N regs · 0xS..0xE` summary above every
  `addrmap`).
- **Workspace symbols** (Ctrl+T) with optional pre-index for cross-file search.
- **Type hierarchy** — subtypes ≡ instances of the type.
- **Document links** on `` `include "..." `` paths.
- **Document highlight**, **selection range**, **signature help** inside `#(...)`.
- **Code action** — quick-fix "Add `= 0` reset value" on field declarations
  missing a reset.
- **Document formatting** — conservative whitespace normaliser.
- **Address conflict warnings** — per-addrmap-scoped, skips reused-type bodies.
- **Semantic tokens** — distinguishes properties / values / types beyond
  TextMate scopes.

The custom `rdl/elaboratedTree` JSON-RPC method powers the memory-map viewer
in the VSCode extension and the standalone `rdl-viewer` CLI; the schema
lives at [`schemas/elaborated-tree.json`](https://github.com/seimei-d/systemrdl-pro/blob/main/schemas/elaborated-tree.json).

## Configuration

| Setting | Default | What |
|---------|---------|------|
| `systemrdl-pro.includePaths` | `[]` | Directories searched by `` `include ``. Auto-discovered from `peakrdl.toml`; sibling-directory fallback. |
| `systemrdl-pro.includeVars` | `{}` | `$VAR` / `${VAR}` substitution in `` `include "..." `` paths. |

## One-shot CLI

`systemrdl-lsp` also exposes a non-LSP one-shot for tooling:

```bash
python -m systemrdl_lsp.dump my-chip.rdl > tree.json
```

Emits the full elaborated tree (schema: `schemas/elaborated-tree.json`) on
stdout. Used by the standalone `rdl-viewer` CLI to drive the browser-served
viewer without an editor.

## Compatibility

- **Python:** 3.10+
- **`perl`** on `PATH` (optional) unlocks the SystemRDL Perl preprocessor
  (clause 16.3) for parametric register generation.

## Source

[github.com/seimei-d/systemrdl-pro](https://github.com/seimei-d/systemrdl-pro)
(monorepo: LSP + viewer + VSCode extension).

## License

MIT — see [`LICENSE`](https://github.com/seimei-d/systemrdl-pro/blob/main/LICENSE).
