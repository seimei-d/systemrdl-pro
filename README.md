<img src="packages/vscode-systemrdl-pro/media/icon.svg" width="96" alt="systemrdl-pro icon" align="right">

# systemrdl-pro

LSP server + interactive memory-map viewer for the
[SystemRDL 2.0](https://www.accellera.org/downloads/standards/systemrdl)
hardware register-description language. Live diagnostics, schema-driven
codegen, full LSP feature surface (rename, references, semantic tokens,
code actions, formatting), and a React-based memory-map viewer that
runs both inside VSCode and as a standalone browser app.

[![CI](https://github.com/seimei-d/systemrdl-pro/actions/workflows/ci.yml/badge.svg)](https://github.com/seimei-d/systemrdl-pro/actions/workflows/ci.yml)
[![Latest release](https://img.shields.io/github/v/release/seimei-d/systemrdl-pro)](https://github.com/seimei-d/systemrdl-pro/releases/latest)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

![SystemRDL Pro: live diagnostics, memory-map viewer, click-to-reveal](docs/demo.gif)

> 30-second tour: live diagnostics, hover with resolved values, F12
> goto-def, click-to-reveal, register binary decode, theme follows
> VSCode.

## Install (end-user)

The latest `.vsix` lives on
[GitHub Releases](https://github.com/seimei-d/systemrdl-pro/releases/latest).

1. **Download** `vscode-systemrdl-pro-<version>.vsix` from the release assets.
2. **Install** in VSCode — drag onto the Extensions sidebar (`Ctrl+Shift+X`),
   or `code --install-extension /path/to/vscode-systemrdl-pro-<version>.vsix`.
3. **LSP backend**: `pip install systemrdl-lsp`.

See [`packages/vscode-systemrdl-pro/README.md`](packages/vscode-systemrdl-pro/README.md)
for the feature tour and settings reference.

## What's in this repo

| Package | What |
|---------|------|
| [`systemrdl-lsp`](packages/systemrdl-lsp/) | Python LSP server. `pygls 2.x` + `systemrdl-compiler`. Diagnostics, hover, outline, goto-def, completion, references, rename, semantic tokens, inlay hints, code actions, formatting, document links, document highlight, selection range, signature help, type hierarchy, custom `rdl/elaboratedTree` JSON-RPC. |
| [`rdl-viewer-core`](packages/rdl-viewer-core/) | Shared **React** components: `Viewer`, `Tree`, `TreeRow`, `Detail`, `BitGrid`, `ContextMenu`. Single bundle consumed by both the VSCode webview and the CLI viewer. |
| [`rdl-viewer-cli`](packages/rdl-viewer-cli/) | Standalone browser viewer. `bun rdl-viewer file.rdl` opens `http://localhost:5173/` with `fs.watch` updates over SSE. |
| [`vscode-systemrdl-pro`](packages/vscode-systemrdl-pro/) | VSCode extension. LSP supervisor, multi-tab Memory Map webview, source-map cycle, walkthrough, custom commands. |

## Why another SystemRDL extension

The existing [`SystemRDL/vscode-systemrdl`](https://github.com/SystemRDL/vscode-systemrdl)
ships a TextMate grammar only — no diagnostics, no hover, no goto-def, no
viewer. `systemrdl-pro` uses a different language id (`systemrdl-pro`) so
both can be installed side by side; you keep the upstream grammar's
syntax-only fallback if you don't want the LSP for some files.

The closest commercial alternatives (Agnisys IDesignSpec, Semifore CSRCompiler)
sit at tens of thousands of dollars per seat. This project is MIT.

## Project structure

```
systemrdl-pro/
├── packages/
│   ├── systemrdl-lsp/         # Python LSP (pip install systemrdl-lsp)
│   ├── rdl-viewer-core/       # React components (workspace dep)
│   ├── rdl-viewer-cli/        # Standalone browser viewer (bun)
│   └── vscode-systemrdl-pro/  # Extension (.vsix on GitHub Releases)
├── schemas/
│   └── elaborated-tree.json   # JSON Schema — source of truth
├── tools/
│   └── codegen.py             # Schema → Python TypedDict + TS types
├── scripts/
│   └── codegen.sh             # Wrapper invoked by `bun run codegen`
├── examples/
│   ├── sample.rdl             # Multi-feature demo
│   ├── features_demo.rdl      # Showcase: bridge, alias, encode, …
│   ├── enum_demo.rdl
│   ├── alias_demo.rdl
│   ├── perl_demo.rdl
│   ├── stress_1000.rdl        # 1000-reg performance fixture
│   └── README.md              # Per-file feature matrix
├── docs/
│   ├── architecture.md        # Mermaid diagrams: components, data flow, state
│   ├── design.md              # Locked architectural decisions (Approach B, D4-D15)
│   └── ROADMAP.md             # Build sequence (mostly historical now)
├── .github/
│   ├── workflows/             # ci.yml, *-publish.yml
│   ├── ISSUE_TEMPLATE/        # bug / feature / question
│   └── pull_request_template.md
├── package.json               # Bun workspace
├── pyproject.toml             # uv workspace
├── CONTRIBUTING.md
└── LICENSE                    # MIT
```

## Quickstart (development)

```bash
# Python side
curl -LsSf https://astral.sh/uv/install.sh | sh    # if you don't have uv
uv sync                                            # creates .venv with all deps
uv pip install -e packages/systemrdl-lsp           # editable install

# TypeScript side
curl -fsSL https://bun.sh/install | bash           # if you don't have bun
bun install                                        # workspace deps

# Build + install dev .vsix
bun run --cwd packages/rdl-viewer-core build
bun run --cwd packages/vscode-systemrdl-pro build
bun run --cwd packages/vscode-systemrdl-pro package
code --install-extension packages/vscode-systemrdl-pro/vscode-systemrdl-pro-*.vsix --force
```

Tests + lint + typecheck:

```bash
uv run --directory packages/systemrdl-lsp pytest tests/ -q
uv run ruff check packages/systemrdl-lsp/
bun run --cwd packages/rdl-viewer-core typecheck
bun run --cwd packages/vscode-systemrdl-pro typecheck
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full workflow including
codegen and release process.

## What works today

**LSP**: live diagnostics (300 ms debounce, last-good fallback,
per-URI bucketing for `\`include`d files), hover with property-origin
hints, goto-def with reference-path support, find references, rename,
context-aware completion, ~85 keyword/property/value catalogue +
user-defined types + user-defined properties, outline, folding,
inlay hints, CodeLens, workspace symbols (with optional pre-index),
type hierarchy, document links, document highlight, selection range,
signature help, code action, conservative formatter, address-conflict
warnings (per-addrmap-scoped), semantic tokens, snippets, Perl
preprocessor support (clause 16.3) with pre-flight check.

**Viewer**: multi-tab Memory Map (one panel per `.rdl` file, survives
window reload), tree + detail layout with bidirectional source-map
cycle, datasheet-style 16-bit-per-row bit grid with multi-line
field names and counter/intr glyphs, per-field encode-enum tables
(collapsible), live binary decoder with enum-name lookup,
split-access banner for `accesswidth < regwidth`, theme-aware chrome
that follows VSCode color theme via `--vscode-*` CSS variables,
high-contrast theme via `forced-colors: active`, user palette
override.

**Standards coverage**: `addrmap`, `regfile`, `reg`, `field`, `mem`,
`signal`, `enum`, `bridge` (clause 9.2), `alias` (clause 10.5),
`ispresent` (clause 9.5 conditional elaboration), parametrized types
with `#(WIDTH)`, dynamic property assignments (`inst->prop = value`),
default-property propagation with origin-line annotation in hover,
user-defined `property foo { … };` declarations, `accesswidth`
split access, full property catalogue (`name`, `desc`, access modes,
counter set, interrupt set, `precedence`, `addressing`, `encode`,
plus the rare ones — `donttest`, `rsvdset`, `arbiter`).

For a hands-on tour, open
[`examples/features_demo.rdl`](examples/features_demo.rdl) and read
its header comment.

## Architecture

[`docs/architecture.md`](docs/architecture.md) has mermaid diagrams
covering:

- The component graph (LSP modules ↔ schema ↔ viewer ↔ extension).
- The on-edit data flow (debounce → compile → cache → diagnostics
  publish → push notification → version-gated tree fetch).
- The schema-codegen pipeline (`schemas/elaborated-tree.json` →
  Python TypedDict + TS types, with drift detection in CI).
- The cache-version state machine for the
  `rdl/elaboratedTreeChanged` push protocol.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Issue templates at
[Issues → New issue](https://github.com/seimei-d/systemrdl-pro/issues/new/choose).

## License

[MIT](LICENSE).

## Credits

- Parsing + elaboration: [systemrdl-compiler](https://github.com/SystemRDL/systemrdl-compiler)
  by Alex Mykyta (MIT).
- LSP framework: [pygls](https://github.com/openlawlibrary/pygls) (Apache-2.0).
- TextMate grammar forked from
  [SystemRDL/vscode-systemrdl](https://github.com/SystemRDL/vscode-systemrdl) (MIT).
- LSP wire types: [lsprotocol](https://github.com/microsoft/lsprotocol).
