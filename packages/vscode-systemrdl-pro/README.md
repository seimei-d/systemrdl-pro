# SystemRDL Pro

VSCode extension for **SystemRDL 2.0** — live diagnostics, an interactive
memory-map viewer, and the full LSP feature surface most tools have for
mainstream languages.

Powered by [`systemrdl-lsp`](https://pypi.org/project/systemrdl-lsp/) +
[`systemrdl-compiler`](https://github.com/SystemRDL/systemrdl-compiler).

## What you get

### In the editor (LSP)

- 🔴 **Live diagnostics** on every keystroke — 300 ms debounce, 10 s
  timeout fallback, last-good cache, per-URI bucketing for `` `include ``d
  files (clear-on-resolve cycle).
- 💬 **Hover** on any identifier — instance address/width/access for regs,
  parameter values for parametrized types, `bridge` flag for addrmaps,
  `(← default at line N)` annotation when a property comes from a
  `default` or dynamic assignment.
- ⏯ **Goto-definition** (F12 / Ctrl-click) — top-level types, instance
  names (signals, registers), reference paths like `top.regfile.CTRL.enable`
  (segment-by-segment), cross-file via `` `include ``.
- 🔎 **Find references** (Shift+F12) — every instantiation of a type,
  cross-file.
- ✏ **Rename** (F2) — workspace-wide, refuses on collision with an
  existing type.
- 🔤 **Autocomplete** with ~85 keywords / properties / access values +
  user-defined types + user-defined properties. Context-aware: after
  `sw =` only access modes; after `addressing =` only
  `compact / regalign / fullalign`; etc.
- 📑 **Outline** (`addrmap → regfile → reg → field`) in the sidebar.
- 🎯 **Inlay hints** — resolved absolute address ghost-grey at end-of-line
  (skipped on reused-type bodies where no single address is meaningful).
- 📊 **CodeLens** — `📊 N regs · 0xS..0xE` summary above every `addrmap`.
- 🗂 **Workspace symbols** (Ctrl+T) with optional pre-index for cross-file
  search.
- 🌳 **Type hierarchy** — subtypes ≡ instances of the type.
- 🔗 **Document links** on `` `include "..." `` paths (Ctrl+click to open).
- ✨ **Document highlight** — every textual occurrence of the cursor word.
- 🎯 **Selection range** — smart selection word → enclosing `{}` block(s) → file.
- 💡 **Code action** — quick-fix "Add `= 0` reset value" on field
  declarations missing a reset.
- 🎨 **Document formatting** — conservative whitespace normaliser.
- ⚠ **Address conflict warnings** — per-addrmap-scoped, skips reused-type
  bodies (no false positives on multi-instance regfiles).
- 🌈 **Semantic tokens** — distinguishes properties / values / types
  beyond TextMate scopes.
- 🪶 **Perl preprocessor** (clause 16.3) when `perl` is on PATH —
  parametric register generation via `<% for ... %>`. One-shot warning
  when `<%` markers appear but `perl` is missing.
- 📂 **Auto-discovers include paths** from `peakrdl.toml`; supports
  `$VAR` / `${VAR}` substitution in `` `include `` directives.

### In the Memory-Map panel

- 🪟 **Multi-tab** — one panel per `.rdl` file (markdown-preview-style);
  one tab per top-level addrmap inside the file. **Click a tab** → editor
  jumps to the addrmap declaration.
- 🌳 **Tree + Detail** layout with collapsible `addrmap` / `regfile` containers.
  Click any reg → editor scrolls + 200 ms flash; click an addrmap/regfile
  → editor reveals; cursor in editor → tree auto-selects.
- 🔎 **Cmd-F filter** with explicit scope (Name / Address / Field / All).
  Collapse-all / expand-all buttons.
- 📋 **Right-click** for Copy Name / Copy Address / Copy Type / Reveal in
  Editor / Copy Source Path.
- 📐 **Bit-field grid** — datasheet-style, 16 bits per row, multi-line
  field names, access-mode colour fill, counter (◷) / interrupt (⚡)
  glyphs.
- 📊 **Field rows** — bit range, name, access pill, reset, description,
  collapsible enum value table when `encode = my_enum;` is set.
- 🔢 **Register binary decoder** — paste hex/bin/dec → live per-field
  decode with enum-name lookup.
- 🪧 **Split-access banner** when `accesswidth < regwidth`.
- ⚠ **Stale-bar** — viewer keeps last-good tree visible when current parse fails.
- 🌗 **Auto dark / light** via `prefers-color-scheme`; user palette
  override via `systemrdl-pro.viewer.colors`; **high-contrast theme**
  via `forced-colors: active`.

## Install

The latest `.vsix` lives on
[GitHub Releases](https://github.com/seimei-d/systemrdl-pro/releases).

1. **Download** `vscode-systemrdl-pro-<version>.vsix` from the assets.
2. **Install** in VSCode — either drag the file onto the Extensions
   sidebar (`Ctrl+Shift+X`) or run
   `code --install-extension /path/to/vscode-systemrdl-pro-<version>.vsix`.
3. **LSP backend**:

   ```bash
   pip install systemrdl-lsp
   ```

   (When the module is missing, the extension shows a banner with an
   "Install with pip…" button that runs the command for you.)

4. **Python interpreter** is resolved in this order:
   1. `systemrdl-pro.pythonPath` setting (explicit win)
   2. Active interpreter from the `ms-python.python` extension
   3. `python3` / `python` on `PATH`

## Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `systemrdl-pro.pythonPath` | _(empty — fallback chain)_ | Explicit Python interpreter path. |
| `systemrdl-pro.includePaths` | `[]` | Directories searched by `` `include ``. Workspace-relative paths supported. |
| `systemrdl-pro.includeVars` | `{}` | Map for `$VAR` / `${VAR}` substitution inside `` `include "..." `` paths. Falls back to `os.environ` for unknown names. |
| `systemrdl-pro.perlSafeOpcodes` | `[]` | Override the Perl `Safe` opcode set. Empty = compiler default. Add `:base_io` to allow `print`-based codegen. See _Perl preprocessor_ below. |
| `systemrdl-pro.preindex.enabled` | `false` | Pre-elaborate every `.rdl` in the workspace at startup so workspace-wide symbol search (Ctrl+T) finds names without first opening the source. Off by default — multi-window setups can peg the CPU. |
| `systemrdl-pro.preindex.maxFiles` | `200` | Cap on the pre-index walker. |
| `systemrdl-pro.viewer.colors` | `{}` | Override viewer access-mode colours and chrome tokens. Keys map to `--rdl-...` CSS custom properties. Recognised: `rw`, `ro`, `wo`, `w1c`, `rsv`, `accent`, `warning`, `bg`, `panel`, `chrome`, `border`, `fg`, `dim`, `selected`. |
| `systemrdl-pro.trace.server` | `off` | LSP communication trace level: `off` / `messages` / `verbose`. |

## Commands

| Command | Default shortcut | What |
|---------|------------------|------|
| `SystemRDL: Show Memory Map` | **Ctrl+Shift+V** (Cmd+Shift+V on macOS), only on `.rdl` files | Open the memory-map viewer panel beside the editor. |
| `SystemRDL: Restart Language Server` | — | Manually restart `systemrdl-lsp` (the extension also auto-restarts up to three times in 60 s on crash). |
| `SystemRDL: Show effective include paths` | — | Quick-pick of the deduped include path list for the current `.rdl` file, labeled by source (`setting` / `peakrdl.toml` / `sibling`). Press Enter on a row to reveal it in your OS file manager. |

## Examples

The repo's
[`examples/`](https://github.com/seimei-d/systemrdl-pro/tree/main/examples)
directory has six demos for hands-on learning:

- `sample.rdl` — multi-feature SystemRDL demo with three top-level addrmaps.
- `features_demo.rdl` — comprehensive showcase: user-defined property,
  enums + `encode`, signals, parametrized type, counters, interrupts,
  default propagation, alias, `bridge`, `ispresent`, `accesswidth`,
  dynamic property assignment.
- `enum_demo.rdl` — minimal `enum` + `encode` field binding.
- `alias_demo.rdl` — same-storage mirror at a different address.
- `perl_demo.rdl` — Perl preprocessor generates 8 DMA channels via `<% for ... %>`.
- `stress_1000.rdl` — 1000 registers × 30 fields performance fixture.

## Standalone CLI

A no-VSCode standalone viewer serves the same UI in your browser —
`bun rdl-viewer file.rdl` opens `http://localhost:5173/` with live
`fs.watch` updates. See the
[`rdl-viewer-cli`](https://github.com/seimei-d/systemrdl-pro/tree/main/packages/rdl-viewer-cli)
package.

## Perl preprocessor

`systemrdl-compiler` supports the SystemRDL 2.0 Perl preprocessor (clause 16.3)
by shelling out to a real `perl` binary. When `perl` is on `PATH`, you can use
`<% … %>` for control flow and `<%=expr%>` for inline expansion:

```rdl
<% for my $i (0..3) { %>
reg ch_<%=$i%> @ <%=0x100+$i*4%> { ... };
<% } %>
```

**Gotcha — no leading whitespace inside `<%= %>`.** The compiler rejects
`<%= $i %>` with _"Invalid text found in Perl macro expansion"_. Write
`<%=$i%>` (or `<%= ($i) %>`) instead.

If your buffer contains `<%` markers but `perl` is not on `PATH`, the
extension shows a one-time warning so you don't hit a wall of cryptic
diagnostics on every save. Install Perl from your package manager
(`apt install perl`, `brew install perl`, etc.) — no LSP restart required.

The compiler runs Perl inside a `Safe` compartment with a default opcode set
that bans `print` and most I/O. If you need them for codegen
(`<% print "..." %>`), extend the opcode list via
`systemrdl-pro.perlSafeOpcodes`, e.g.:

```jsonc
"systemrdl-pro.perlSafeOpcodes": [
  ":base_core", ":base_mem", ":base_loop", ":base_orig",
  ":base_math", ":base_thread", ":filesys_read", ":sys_db",
  ":load", ":base_io",
  "sort", "tied", "pack", "unpack", "reset"
]
```

## Architecture

[`docs/architecture.md`](https://github.com/seimei-d/systemrdl-pro/blob/main/docs/architecture.md)
has mermaid diagrams covering the component graph, the on-edit data
flow, the schema-codegen pipeline, and the cache-version state machine.

## Coexistence with `SystemRDL/vscode-systemrdl`

This extension uses language id `systemrdl-pro`. The mainline community
extension uses `systemrdl`. They install side-by-side without conflict —
SystemRDL Pro adds the LSP + viewer on top of TextMate-only support.

## License

[MIT](LICENSE)
