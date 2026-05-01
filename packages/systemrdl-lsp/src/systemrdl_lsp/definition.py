"""textDocument/definition: identifier under cursor → component def location."""

from __future__ import annotations

import pathlib
import re
from typing import TYPE_CHECKING, Any

from lsprotocol.types import Location

from .diagnostics import _build_range

if TYPE_CHECKING:
    from systemrdl.node import RootNode


_IDENT_CHAR_RE = re.compile(r"[A-Za-z0-9_]")


def _word_at_position(text: str, line_0b: int, col_0b: int) -> str | None:
    """Return the SystemRDL identifier the cursor sits inside, else None.

    SystemRDL identifiers match ``[A-Za-z_][A-Za-z0-9_]*``. We split the source
    into lines, locate the target line, then walk left and right from
    ``col_0b`` until we leave the identifier character class. If the cursor
    is on whitespace or punctuation, returns ``None``.
    """
    lines = text.splitlines()
    if line_0b < 0 or line_0b >= len(lines):
        return None
    line = lines[line_0b]
    if col_0b < 0 or col_0b > len(line):
        return None

    # Cursor exactly at end-of-identifier (col == len(ident)) is still a hit —
    # VSCode reports definition requests at the position *after* the last char
    # when triggered via Ctrl-click on the trailing edge. Probe both sides.
    def _is_ident(c: str) -> bool:
        return bool(_IDENT_CHAR_RE.match(c))

    left = col_0b
    while left > 0 and _is_ident(line[left - 1]):
        left -= 1
    right = col_0b
    while right < len(line) and _is_ident(line[right]):
        right += 1
    if right <= left:
        return None
    word = line[left:right]
    # Skip pure-numeric runs ("0x100", "32") — those aren't identifiers.
    if word and word[0].isdigit():
        return None
    return word


def _comp_defs_from_cached(roots: list[RootNode]) -> dict[str, Any]:
    """Pick the first cached root and read its ``inst.comp_defs`` registry.

    All cached roots from the same buffer share the same compiler-internal
    component definition table (we elaborate them off a single compile pass),
    so any one of them is sufficient. Returns ``{}`` if the list is empty.
    """
    for r in roots:
        defs = getattr(getattr(r, "inst", None), "comp_defs", None)
        if defs:
            return dict(defs)
    return {}


def _definition_location(
    comp: Any,
    path_translate: dict[pathlib.Path, pathlib.Path] | None,
) -> Location | None:
    """Map a systemrdl component's ``def_src_ref`` to an LSP ``Location``."""
    src_ref = getattr(comp, "def_src_ref", None)
    if src_ref is None:
        return None
    raw_filename = getattr(src_ref, "filename", None)
    if not raw_filename:
        return None
    file_path = pathlib.Path(raw_filename)
    if path_translate:
        file_path = path_translate.get(file_path, file_path)
    line_1b = getattr(src_ref, "line", None) or 1
    sel = getattr(src_ref, "line_selection", None) or (1, 1)
    try:
        cs, ce = sel
    except (TypeError, ValueError):
        cs = ce = 1
    return Location(uri=file_path.as_uri(), range=_build_range(line_1b, cs, ce))
