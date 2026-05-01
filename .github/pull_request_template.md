<!-- Thanks for contributing! Please fill in the bits relevant to your change.
     One concern per PR — split mixed-concern changes into separate PRs. -->

## Summary

<!-- One or two sentences. What and why. -->

## Type of change

<!-- Tick all that apply. -->

- [ ] Bug fix
- [ ] New feature
- [ ] Refactor (no behaviour change)
- [ ] Docs / examples
- [ ] Schema change (re-ran `bun run codegen`)
- [ ] UX change (screenshot / screencast included below)

## Screenshots / screencasts

<!-- Required for UX changes. Drag-drop into the field. -->

## Test plan

<!-- How a reviewer should verify this works. Checklist or numbered steps.
     Examples:
       - `uv run --directory packages/systemrdl-lsp pytest tests/ -q`
       - Open `examples/features_demo.rdl`, click `core0` in the tree, expect editor to scroll to line 56
       - Open VSCode in dark theme, then switch to Solarized Light — expect viewer chrome to follow
-->

## Checklist

- [ ] Tests pass: `uv run --directory packages/systemrdl-lsp pytest tests/ -q`
- [ ] Lint passes: `uv run ruff check packages/systemrdl-lsp/`
- [ ] Typecheck passes: `bun run --cwd packages/rdl-viewer-core typecheck` and `bun run --cwd packages/vscode-systemrdl-pro typecheck`
- [ ] Codegen up-to-date if I touched `schemas/elaborated-tree.json`
- [ ] No `git add -A` for shared paths
- [ ] Conventional Commits in commit messages

## Related issue(s)

<!-- "Closes #N" / "Relates to #N". Optional. -->
