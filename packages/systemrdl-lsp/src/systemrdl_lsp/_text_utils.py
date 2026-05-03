"""Pure text-level helpers extracted from server.py.

Self-contained: no LSP state, no compiler imports. Live here so server.py
stays focused on feature wiring instead of carrying utility code.
"""

from __future__ import annotations

import bisect as _bisect
import pathlib
import re as _re
from typing import Any

from lsprotocol.types import Position, Range

from .definition import _word_at_position


def _iter_rdl_files(root: pathlib.Path, exclude_dirs: set[str]):
    """Yield every ``.rdl`` file under ``root``, skipping noisy directory names.

    Used by the workspace pre-index. Errors during traversal (permission denied,
    broken symlinks) are silently skipped — the pre-index is best-effort, not
    a hard guarantee.
    """
    try:
        for entry in root.iterdir():
            if entry.is_dir():
                if entry.name in exclude_dirs or entry.name.startswith("."):
                    continue
                yield from _iter_rdl_files(entry, exclude_dirs)
            elif entry.is_file() and entry.suffix.lower() == ".rdl":
                yield entry
    except (PermissionError, OSError):
        return


def _build_selection_ranges(
    text: str, lines: list[str], line_0b: int, char_0b: int
) -> list[Any]:
    """Walk outward from the cursor position through enclosing `{...}` blocks.

    Returns a list of LSP ``Range`` objects, **innermost first**. Caller links
    them parent-pointer-style for ``textDocument/selectionRange``. Pure textual
    scan — strings/comments are stripped to whitespace so braces inside them
    don't confuse the matcher (same trick as folding ranges).
    """
    if line_0b < 0 or line_0b >= len(lines):
        return []
    line = lines[line_0b]
    if char_0b < 0 or char_0b > len(line):
        return []

    word = _word_at_position(text, line_0b, char_0b)
    word_range: Range | None = None
    if word:
        m = _re.search(rf"\b{_re.escape(word)}\b", line)
        if m and m.start() <= char_0b <= m.end():
            word_range = Range(
                start=Position(line=line_0b, character=m.start()),
                end=Position(line=line_0b, character=m.end()),
            )

    cleaned = _re.sub(r'"(?:\\.|[^"\\])*"', lambda m: " " * len(m.group(0)), text)
    cleaned = _re.sub(r"//[^\n]*", lambda m: " " * len(m.group(0)), cleaned)
    cleaned = _re.sub(
        r"/\*[\s\S]*?\*/",
        lambda m: _re.sub(r"[^\n]", " ", m.group(0)),
        cleaned,
    )

    # Prefix-sum of line-start offsets — `pos_of` becomes O(log n) instead
    # of the O(n) per-call scan it was before. Deep nesting in 10k-line
    # files is otherwise O(K*N) where K is brace depth.
    line_starts: list[int] = [0]
    for ln in lines:
        line_starts.append(line_starts[-1] + len(ln) + 1)

    def offset_of(li: int, co: int) -> int:
        if li < 0:
            return co
        if li >= len(lines):
            return line_starts[-1]
        return line_starts[li] + co

    def pos_of(off: int) -> Position:
        idx = _bisect.bisect_right(line_starts, off) - 1
        if idx < 0:
            return Position(line=0, character=0)
        if idx >= len(lines):
            return Position(line=len(lines), character=0)
        return Position(line=idx, character=off - line_starts[idx])

    cursor_off = offset_of(line_0b, char_0b)
    ranges: list[Range] = []
    if word_range is not None:
        ranges.append(word_range)

    stack: list[int] = []
    pairs: list[tuple[int, int]] = []
    for i, ch in enumerate(cleaned):
        if ch == "{":
            stack.append(i)
        elif ch == "}" and stack:
            start = stack.pop()
            pairs.append((start, i + 1))
    enclosing = [(s, e) for s, e in pairs if s <= cursor_off <= e]
    enclosing.sort(key=lambda t: t[1] - t[0])
    for s, e in enclosing:
        ranges.append(Range(start=pos_of(s), end=pos_of(e)))

    last_line = max(0, len(lines) - 1)
    ranges.append(Range(
        start=Position(line=0, character=0),
        end=Position(line=last_line, character=len(lines[last_line]) if lines else 0),
    ))
    return ranges


def _is_valid_identifier(name: str) -> bool:
    """Match SystemRDL identifier syntax: ``[A-Za-z_][A-Za-z0-9_]*``.

    Used by rename to reject input that would corrupt the buffer.
    """
    return bool(_re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name))
