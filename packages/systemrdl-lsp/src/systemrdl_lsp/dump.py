"""One-shot CLI: ``python -m systemrdl_lsp.dump <file.rdl>`` → tree JSON.

Compiles a SystemRDL file and emits the same elaborated-tree JSON envelope
the LSP returns over ``rdl/elaboratedTree`` to stdout. Used by the
``rdl-viewer`` standalone Bun CLI as a side-channel — it spawns this
Python process per file change rather than speaking full LSP.

Diagnostics are printed to stderr (one per line, ``severity: file:line: text``).
Exit code:

* ``0`` — file compiled and produced at least one elaborated addrmap.
* ``1`` — file compiled but is library-only (no top-level addrmap).
* ``2`` — parse/elaborate errors. JSON still emitted (with ``stale=true``)
  so the viewer can keep showing the last good tree it had.

Stable contract: stdout is *exactly* one JSON object terminated by a newline.
The viewer parses one frame per child-process invocation.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

from systemrdl.messages import Severity

from systemrdl_lsp import __version__
from systemrdl_lsp.server import _compile_text, _serialize_root


def _severity_label(sev: Severity) -> str:
    if sev in (Severity.ERROR, Severity.FATAL):
        return "error"
    if sev == Severity.WARNING:
        return "warning"
    if sev == Severity.INFO:
        return "info"
    return "hint"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m systemrdl_lsp.dump",
        description="Compile a SystemRDL file and emit elaborated-tree JSON.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("file", type=pathlib.Path, help="Path to .rdl file")
    parser.add_argument(
        "--include",
        "-I",
        action="append",
        default=[],
        help="Extra include search path (repeatable).",
    )
    parser.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="Suppress diagnostics on stderr.",
    )
    args = parser.parse_args(argv)

    path: pathlib.Path = args.file
    if not path.exists():
        print(f"error: file not found: {path}", file=sys.stderr)
        return 2

    text = path.read_text(encoding="utf-8")
    messages, roots, tmp_path = _compile_text(path.as_uri(), text, args.include)
    try:
        translate = {tmp_path: path}
        envelope = _serialize_root(
            roots,
            stale=not roots,
            path_translate=translate,
        )
        json.dump(envelope, sys.stdout)
        sys.stdout.write("\n")
        sys.stdout.flush()

        if not args.quiet:
            for m in messages:
                if m.file_path is None:
                    continue
                line = m.line_1b or 1
                print(
                    f"{_severity_label(m.severity)}: {m.file_path}:{line}: {m.text}",
                    file=sys.stderr,
                )

        if not roots:
            return 1 if all(m.severity not in (Severity.ERROR, Severity.FATAL)
                            for m in messages) else 2
        return 0
    finally:
        tmp_path.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
