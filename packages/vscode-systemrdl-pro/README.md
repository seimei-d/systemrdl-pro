# SystemRDL Pro

VSCode extension for SystemRDL 2.0 — live diagnostics + interactive memory-map viewer.

> **Status:** v0.1 walking skeleton. Diagnostics work via [`systemrdl-lsp`](https://pypi.org/project/systemrdl-lsp/).
> The "Show Memory Map" command opens a placeholder webview — the real viewer ships in Week 4-5.

## Install

1. Install the extension from the [Visual Studio Marketplace](https://marketplace.visualstudio.com/items?itemName=madeinheaven-dev.vscode-systemrdl-pro).
2. The extension calls Python on activation. It will:
   - Use `systemrdl-pro.pythonPath` if set in your settings.
   - Otherwise read the active interpreter from the official `ms-python.python` extension.
   - Otherwise fall back to `python3` / `python` on `PATH`.
3. Ensure `systemrdl-lsp` is installed in that interpreter:

   ```bash
   pip install systemrdl-lsp
   ```

   When the module is missing, the extension shows a banner with an "Install with pip…" button
   that runs the command for you.

## Coexistence with `SystemRDL/vscode-systemrdl`

This extension uses language id `systemrdl-pro`. The other extension uses `systemrdl`.
Both can be installed side-by-side. SystemRDL Pro provides the LSP and viewer; the other
provides only TextMate grammar (Week 2 will fork that grammar into this extension).

## Settings

| Setting | Default | What |
|---------|---------|------|
| `systemrdl-pro.pythonPath` | _(empty — fallback chain)_ | Explicit Python interpreter path. |
| `systemrdl-pro.includePaths` | `[]` | Directories searched by `` `include `` (Week 2). |
| `systemrdl-pro.trace.server` | `off` | LSP communication trace level: `off` / `messages` / `verbose`. |

## Commands

| Command | What |
|---------|------|
| `SystemRDL: Show Memory Map` | Open the memory-map viewer panel (placeholder in v0.1). |
| `SystemRDL: Restart Language Server` | Restart `systemrdl-lsp` after a crash. |

## License

MIT — see [`../../LICENSE`](../../LICENSE).
