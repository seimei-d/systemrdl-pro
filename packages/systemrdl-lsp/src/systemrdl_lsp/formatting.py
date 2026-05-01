"""textDocument/formatting: conservative whitespace fixes.

Scope is intentionally minimal in v1 — semantic-content rewrites (alignment,
property reordering, brace style) are easy to make opinionated and hard to
make universally welcome. We do only the safe layer:

- Trim trailing whitespace on every line.
- Normalise tabs to four spaces (matches VSCode's default editor.tabSize).
- Ensure the file ends with exactly one newline.

A single ``TextEdit`` replaces the whole document. LSP/VSCode handles this
efficiently and the diff view shows minimal noise when the only changes are
whitespace.
"""

from __future__ import annotations

from lsprotocol.types import Position, Range, TextEdit


def _format_text(text: str, tab_size: int = 4) -> str:
    """Return the formatted text. Idempotent — running it twice is a no-op."""
    if not text:
        return ""
    lines = text.split("\n")
    # Trim each line individually. ``rstrip()`` strips ``\r`` too which
    # neutralises CRLF line endings on Windows-saved files.
    cleaned = [line.rstrip().expandtabs(tab_size) for line in lines]
    # The split kept a trailing empty string when the original ended with a
    # newline. Drop trailing empties so we add exactly one back.
    while len(cleaned) > 1 and cleaned[-1] == "":
        cleaned.pop()
    return "\n".join(cleaned) + "\n"


def _document_formatting_edits(text: str, tab_size: int = 4) -> list[TextEdit]:
    """Return the LSP edit list for the buffer. Empty if no change needed."""
    if not text:
        return []
    formatted = _format_text(text, tab_size)
    if formatted == text:
        return []
    # Single edit replaces the whole document. End position spans past the
    # last char so the edit covers the full document regardless of length.
    line_count = text.count("\n") + 1
    return [
        TextEdit(
            range=Range(
                start=Position(line=0, character=0),
                end=Position(line=line_count, character=0),
            ),
            new_text=formatted,
        )
    ]


__all__ = ["_document_formatting_edits", "_format_text"]
