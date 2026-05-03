"""textDocument/formatting: whitespace + indent + property-per-line.

Three passes:

1. Trim trailing whitespace, expand tabs, ensure single trailing newline.
2. Re-indent each line by the running ``{`` / ``}`` depth so a closing brace
   that drifted to the wrong column lands back where it belongs.
3. Split single-line ``field { sw=rw; hw=r; reset=0; } NAME;`` blocks so
   each property assignment gets its own line.

Honours ``// fmt: off`` / ``// fmt: on`` (also ``// systemrdl-fmt: off``)
markers — lines between them are emitted verbatim.
"""

from __future__ import annotations

import re

from lsprotocol.types import Position, Range, TextEdit

# `<name> { body } [trailing];` on a single line. ``body`` excludes braces.
# We deliberately don't try to be SystemRDL-aware about ``<name>`` — any
# identifier-then-`{` pattern qualifies, including ``field``, ``reg``,
# ``signal``, etc. The post-brace ``trailing`` is the instance name +
# bit-range + reset for fields.
_FLAT_BLOCK_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<head>[A-Za-z_]\w*)[ \t]*\{(?P<body>[^{}\n]*?)\}(?P<trail>[^\n]*)$"
)


def _split_block_body(indent: str, head: str, body: str, trail: str, tab_size: int) -> str | None:
    """One attribute per line. Returns ``None`` when the line shouldn't be split.

    Skipped when the body has fewer than 2 ``;``-separated statements (no
    point fanning out a one-statement block) or contains string quotes that
    would need real parsing to split safely.
    """
    if '"' in body:
        return None
    stmts = [s.strip() for s in body.split(";") if s.strip()]
    if len(stmts) < 2:
        return None
    inner_indent = indent + (" " * tab_size)
    parts = [f"{indent}{head} {{"]
    parts.extend(f"{inner_indent}{s};" for s in stmts)
    parts.append(f"{indent}}}{trail}")
    return "\n".join(parts)


# Disable / enable markers — accept both the SystemRDL-specific spelling
# and the de-facto-standard ``fmt:`` / ``fmt-`` prefixes used by black,
# yapf, clang-format. Matched anywhere on the line, comma/space-tolerant.
_FMT_OFF_RE = re.compile(r"//\s*(?:systemrdl-)?fmt[:\s-]+off\b", re.IGNORECASE)
_FMT_ON_RE = re.compile(r"//\s*(?:systemrdl-)?fmt[:\s-]+on\b", re.IGNORECASE)


def _strip_strings_and_comments(line: str) -> str:
    """Replace string literals and ``//``-comments with empty so brace
    counting on the result reflects only structural braces. ``/* ... */``
    block comments are left for now (they'd need multi-line state)."""
    line = re.sub(r'"(?:\\.|[^"\\])*"', '', line)
    line = re.sub(r'//[^\n]*', '', line)
    return line


def _reindent(lines: list[str], tab_size: int) -> list[str]:
    """Set each line's leading whitespace from the running brace depth.

    A line that starts with ``}`` first decrements the depth so the brace
    aligns with its opening counterpart. Backtick directives (``\\`include``,
    ``\\`define``) keep their existing indent — they're typically file-scope
    and the depth-from-braces estimate would push them to column 0.
    """
    out: list[str] = []
    depth = 0
    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            out.append("")
            continue
        if stripped.startswith("`"):
            out.append(raw.expandtabs(tab_size).rstrip())
            continue
        cleaned = _strip_strings_and_comments(stripped)
        opens = cleaned.count("{")
        closes = cleaned.count("}")
        line_depth = depth - 1 if cleaned.startswith("}") and depth > 0 else depth
        out.append(" " * (line_depth * tab_size) + stripped)
        depth = max(0, depth + opens - closes)
    return out


def _format_text(text: str, tab_size: int = 4) -> str:
    """Return the formatted text. Idempotent — running it twice is a no-op."""
    if not text:
        return ""
    lines = text.split("\n")

    # Pass 1+2: whitespace + re-indent on the regions that aren't between
    # ``fmt:off`` / ``fmt:on`` markers. Marker lines themselves get
    # whitespace-cleaned but skip the indent rewrite.
    indented: list[str] = []
    buffer: list[str] = []
    formatting_on = True

    def flush_buffer() -> None:
        if not buffer:
            return
        indented.extend(_reindent(buffer, tab_size))
        buffer.clear()

    for line in lines:
        if _FMT_OFF_RE.search(line):
            flush_buffer()
            indented.append(line.rstrip().expandtabs(tab_size))
            formatting_on = False
            continue
        if _FMT_ON_RE.search(line):
            indented.append(line.rstrip().expandtabs(tab_size))
            formatting_on = True
            continue
        if not formatting_on:
            indented.append(line)
            continue
        buffer.append(line)
    flush_buffer()

    # Pass 3: split flat one-line ``head { body } trail;`` blocks.
    expanded: list[str] = []
    formatting_on = True
    for line in indented:
        if _FMT_OFF_RE.search(line):
            expanded.append(line)
            formatting_on = False
            continue
        if _FMT_ON_RE.search(line):
            expanded.append(line)
            formatting_on = True
            continue
        if not formatting_on:
            expanded.append(line)
            continue
        m = _FLAT_BLOCK_RE.match(line)
        if m is None:
            expanded.append(line)
            continue
        rewrote = _split_block_body(
            m.group("indent"), m.group("head"), m.group("body"),
            m.group("trail"), tab_size,
        )
        expanded.extend((rewrote or line).split("\n"))

    while len(expanded) > 1 and expanded[-1] == "":
        expanded.pop()
    return "\n".join(expanded) + "\n"


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
