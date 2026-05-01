"""textDocument/documentLink: ``\\`include "..."`` directives become clickable.

Independent of elaboration — pure text scan, so links work even when the file
has parse errors. The link target is resolved through the same include-path
chain the compiler uses (``_resolve_search_paths`` + variable expansion).
"""

from __future__ import annotations

import os
import pathlib
import re
from typing import Any

from lsprotocol.types import DocumentLink, Position, Range

# Captures the path argument plus its position so we can build a Range. The
# regex anchors on backtick-include because that's the only directive form
# SystemRDL recognises (Verilog-2001 clause 16.2 + RDL clause 16).
_INCLUDE_RE = re.compile(r'`include\s+"([^"]*)"')
_VAR_RE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")


def _expand_vars(path: str, vars_map: dict[str, str]) -> str:
    """Resolve ``$VAR`` / ``${VAR}`` against the merged setting+env map.

    Mirrors ``_expand_include_vars`` but for a bare path string (vs. the full
    buffer text). Unknown vars are left literal so the resulting link will
    404 visibly rather than silently mis-resolve.
    """
    def expand_one(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in vars_map:
            return vars_map[name]
        if name in os.environ:
            return os.environ[name]
        return match.group(0)
    return _VAR_RE.sub(expand_one, path)


def _resolve_include(
    raw_path: str,
    primary_dir: pathlib.Path,
    search_paths: list[str],
    vars_map: dict[str, str],
) -> pathlib.Path | None:
    """First-existing-match across the include search chain.

    Order: explicit setting paths → peakrdl.toml → primary file's own dir.
    The compiler does the same thing on its end; we mirror it here so a
    Ctrl+click from the editor lands on the same file the compiler picked.
    """
    expanded = _expand_vars(raw_path, vars_map)
    candidate = pathlib.Path(expanded)
    if candidate.is_absolute():
        return candidate if candidate.is_file() else None

    for base in search_paths:
        try:
            joined = (pathlib.Path(base) / expanded).resolve()
        except (OSError, ValueError):
            continue
        if joined.is_file():
            return joined

    sibling = (primary_dir / expanded).resolve()
    if sibling.is_file():
        return sibling
    return None


def _document_links(
    buffer_text: str,
    primary_path: pathlib.Path,
    search_paths: list[str],
    vars_map: dict[str, str],
) -> list[DocumentLink]:
    """One DocumentLink per resolvable ``\\`include`` in the buffer."""
    if not buffer_text:
        return []
    out: list[DocumentLink] = []
    for line_idx, line in enumerate(buffer_text.splitlines()):
        for m in _INCLUDE_RE.finditer(line):
            raw_path = m.group(1)
            target = _resolve_include(raw_path, primary_path.parent, search_paths, vars_map)
            if target is None:
                # Unresolved includes are intentionally not surfaced as
                # documentLinks — Ctrl+click would land on a non-existent
                # path and confuse the user. The compiler will already raise
                # a diagnostic on the next compile.
                continue
            # Range covers the path text only, not the directive — clicking
            # the literal `include` keyword shouldn't navigate.
            start_col = m.start(1)
            end_col = m.end(1)
            out.append(
                DocumentLink(
                    range=Range(
                        start=Position(line=line_idx, character=start_col),
                        end=Position(line=line_idx, character=end_col),
                    ),
                    target=target.as_uri(),
                    tooltip=f"Open {target.name}",
                )
            )
    return out


__all__ = ["_document_links", "_resolve_include"]


# Suppress unused-import warning when re-exporting via server.__all__.
_ = Any
