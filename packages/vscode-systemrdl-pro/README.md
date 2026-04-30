# SystemRDL Pro

VSCode extension for **SystemRDL 2.0** — live diagnostics, hover, outline,
goto-definition, autocomplete, and an interactive memory-map viewer that
mirrors the editor cursor in real time.

Powered by [`systemrdl-lsp`](https://pypi.org/project/systemrdl-lsp/) +
[`systemrdl-compiler`](https://github.com/SystemRDL/systemrdl-compiler).

## What you get

**In the editor:**

- 🔴 Live diagnostics on every keystroke (300 ms debounce, ten-second timeout
  fallback so a pathological include can't freeze the editor)
- 💬 Hover over any identifier — register fields show resolved address /
  width / sw-hw access / reset; type names show the kind, `name`, `desc`;
  keywords (`addrmap`, `sw`, `onwrite`, …) explain themselves
- 📑 Outline of `addrmap → regfile → reg → field` in the sidebar
- ⏯ Goto-definition (F12 / Ctrl-click) on type identifiers, jumps cross-file
  through `` `include`` directives
- 🔤 Autocomplete with ~85 keywords + properties + access values; narrows
  contextually (after `sw =` only suggests access modes)
- 📂 Auto-discovers include paths from `peakrdl.toml`; supports
  `$VAR` / `${VAR}` substitution in `` `include `` directives
- 🪶 Full SystemRDL Perl preprocessor (`<% %>` / `<%=expr%>`) when `perl`
  is on PATH — no extra setup needed

**In the Memory-Map panel:**

- 🌳 Tree view with collapsible `addrmap`/`regfile` containers (▼/▶)
- 📍 Click any register → editor scrolls to its declaration with a
  200 ms line flash; cursor in editor → tree auto-selects (D10)
- 🔎 Cmd-F filter with explicit scope (Name / Address / Field / All)
- 📋 Right-click for Copy Name / Copy Address / Copy Type / Reveal in Editor
- 📑 Tabs for multi-root files (one `addrmap` definition per tab)
- ⚠ Stale-bar when current parse fails — viewer keeps the last good tree
  visible so you can keep navigating
- 🌗 Auto dark / light theme tokens; works on stress fixtures up to 1000
  registers × 30 fields

## Install

1. Install from the [Visual Studio Marketplace](https://marketplace.visualstudio.com/items?itemName=seimei-d.vscode-systemrdl-pro).
2. Install the LSP backend in your Python interpreter:

   ```bash
   pip install systemrdl-lsp
   ```

   (When the module is missing, the extension shows a banner with an
   "Install with pip…" button that runs the command for you.)

3. The extension finds Python in this order:
   1. `systemrdl-pro.pythonPath` setting (explicit win)
   2. Active interpreter from the `ms-python.python` extension
   3. `python3` / `python` on `PATH`

## Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `systemrdl-pro.pythonPath` | _(empty — fallback chain)_ | Explicit Python interpreter path. |
| `systemrdl-pro.includePaths` | `[]` | Directories searched by `` `include ``. Workspace-relative paths supported. |
| `systemrdl-pro.includeVars` | `{}` | Map for `$VAR` / `${VAR}` substitution inside `` `include "..." `` paths. Falls back to `os.environ` for unknown names. |
| `systemrdl-pro.trace.server` | `off` | LSP communication trace level: `off` / `messages` / `verbose`. |

## Commands

| Command | Default shortcut | What |
|---------|------------------|------|
| `SystemRDL: Show Memory Map` | **Ctrl+Shift+V** (Cmd+Shift+V on macOS), only on `.rdl` files | Open the memory-map viewer panel beside the editor. |
| `SystemRDL: Restart Language Server` | — | Manually restart `systemrdl-lsp` (the extension also auto-restarts up to three times in 60 s on crash). |

## Standalone CLI

There's also a no-VSCode standalone viewer that serves the same UI in your
browser — `bun rdl-viewer file.rdl` opens `http://localhost:5173/` with live
fs.watch updates. See the [`rdl-viewer`](https://github.com/seimei-d/systemrdl-pro/tree/main/packages/rdl-viewer-cli)
package.

## Coexistence with `SystemRDL/vscode-systemrdl`

This extension uses language id `systemrdl-pro`. The mainline community
extension uses `systemrdl`. They install side-by-side without conflict —
SystemRDL Pro adds the LSP + viewer on top of TextMate-only support.

## License

[MIT](LICENSE)
