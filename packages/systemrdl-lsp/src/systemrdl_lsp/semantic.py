"""textDocument/semanticTokens/full: token-level coloring beyond TextMate.

TextMate scopes can colour ``addrmap`` as a keyword and ``my_ctrl_t`` as an
identifier, but it can't tell *property* from *value* (``sw`` vs ``rw``) or
*user-defined type reference* from *bare identifier*. Semantic tokens fill
the gap.

Strategy: pure textual scan, no elaboration dependency. Coloring works on
broken files. The scanner walks line by line, strips strings/comments
(replaced with whitespace to keep offsets stable), then matches identifier
runs against three catalogues:

- ``KEYWORDS`` — top-level + access-mode value enums + boolean literals
- ``PROPERTIES`` — common SystemRDL properties (``sw``, ``hw``, ``reset``…)
- ``RW_VALUES``, ``ONWRITE_VALUES``, ``ONREAD_VALUES`` — RHS enum values

Identifiers that aren't in any catalogue and follow ``addrmap``/``reg``/etc.
keywords get the ``type`` token; bare instance names get ``variable``. User-
defined type references (a ``my_ctrl_t`` from another file) are tagged as
``type`` when they precede an instance name. The catalogue lookup mirrors
``completion._completion_context`` so behavior stays consistent.
"""

from __future__ import annotations

import re

from .completion import (
    SYSTEMRDL_ONREAD_VALUES,
    SYSTEMRDL_ONWRITE_VALUES,
    SYSTEMRDL_PROPERTIES,
    SYSTEMRDL_RW_VALUES,
    SYSTEMRDL_TOP_KEYWORDS,
)

# LSP semantic-token type catalogue. Order is the source of truth — the int
# index in the encoded data corresponds to the position in this list. Don't
# reorder without checking the registration in server.py.
TOKEN_TYPES: list[str] = [
    "keyword",     # 0 — addrmap, regfile, reg, field, default, …
    "type",        # 1 — user-defined component types
    "variable",    # 2 — instance names (CTRL, DMA_BASE_ADDR, …)
    "property",    # 3 — sw, hw, reset, onwrite, …
    "enumMember",  # 4 — rw, ro, woclr, rclr, …
    "number",      # 5 — 0x100, 32, 0b1010
    "string",      # 6 — "Control register" (descriptions / names)
    "comment",     # 7 — // … or /* … */
]

# Empty modifier list — we don't differentiate readonly/static/etc.
TOKEN_MODIFIERS: list[str] = []

_KW_IDX = TOKEN_TYPES.index("keyword")
_TYPE_IDX = TOKEN_TYPES.index("type")
_VAR_IDX = TOKEN_TYPES.index("variable")
_PROP_IDX = TOKEN_TYPES.index("property")
_ENUM_IDX = TOKEN_TYPES.index("enumMember")
_NUM_IDX = TOKEN_TYPES.index("number")
_STR_IDX = TOKEN_TYPES.index("string")
_COMMENT_IDX = TOKEN_TYPES.index("comment")

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NUMBER_RE = re.compile(r"\b0[xX][0-9a-fA-F_]+\b|\b0[bB][01_]+\b|\b\d[\d_]*\b")
_STRING_RE = re.compile(r'"(?:\\.|[^"\\])*"')
_LINE_COMMENT_RE = re.compile(r"//[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*[\s\S]*?\*/")

_VALUE_CATALOGUES = (SYSTEMRDL_RW_VALUES, SYSTEMRDL_ONWRITE_VALUES, SYSTEMRDL_ONREAD_VALUES)


def _classify_identifier(word: str, prev_token: str | None) -> int | None:
    """Decide which TOKEN_TYPES index ``word`` belongs to.

    ``prev_token`` is the previous identifier on the same line, used to
    distinguish ``ctrl_t`` (a type after ``reg``) from a bare identifier
    elsewhere. Returns ``None`` to mean "skip" — the syntax highlighter's
    TextMate fallback handles plain identifiers.
    """
    if word in SYSTEMRDL_TOP_KEYWORDS:
        return _KW_IDX
    if word in SYSTEMRDL_PROPERTIES:
        return _PROP_IDX
    for cat in _VALUE_CATALOGUES:
        if word in cat:
            return _ENUM_IDX
    # User-defined type reference: ``reg ctrl_t {`` or ``ctrl_t CTRL @ 0``.
    # If the *previous* identifier is a top-level keyword (reg, regfile, …)
    # or capital-cased (an instance name typically follows the type), this
    # one is a type.
    if prev_token in {"reg", "regfile", "addrmap", "field", "mem", "signal", "enum"}:
        return _TYPE_IDX
    # All-caps with underscores → conventional instance name (CTRL, DMA_BASE).
    if word.isupper() or (word[:1].isupper() and "_" in word):
        return _VAR_IDX
    return None


def _semantic_tokens_for_text(text: str) -> list[int]:
    r"""Compute the flat LSP semantic-tokens encoding for the buffer.

    LSP wire format: ``[deltaLine, deltaStart, length, type, modifierBitmask]``
    repeated for each token, where ``deltaLine`` is the relative line offset
    from the previous token and ``deltaStart`` resets to absolute char on a
    new line. Tokens must be sorted by absolute (line, char).
    """
    if not text:
        return []

    raw_tokens: list[tuple[int, int, int, int]] = []  # (line, char, length, type_idx)

    # 1. Comments — pre-scan whole text so block comments work across lines.
    for m in _BLOCK_COMMENT_RE.finditer(text):
        # Decompose into per-line slices since semantic tokens are line-bound.
        chunk_start = m.start()
        chunk = m.group(0)
        line_idx = text.count("\n", 0, chunk_start)
        col = chunk_start - (text.rfind("\n", 0, chunk_start) + 1)
        for piece in chunk.split("\n"):
            if piece:
                raw_tokens.append((line_idx, col, len(piece), _COMMENT_IDX))
            line_idx += 1
            col = 0

    # 2. Strip strings/comments from the cleaned copy so identifier scanning
    #    doesn't misclassify their contents. Replace with whitespace to keep
    #    offsets stable.
    def _blank_match(m: re.Match[str]) -> str:
        return re.sub(r"[^\n]", " ", m.group(0))
    cleaned = _BLOCK_COMMENT_RE.sub(_blank_match, text)
    cleaned = _LINE_COMMENT_RE.sub(_blank_match, cleaned)

    # 3. Strings — scan original text (the cleaned copy has quotes blanked).
    for m in _STRING_RE.finditer(text):
        chunk_start = m.start()
        chunk = m.group(0)
        line_idx = text.count("\n", 0, chunk_start)
        col = chunk_start - (text.rfind("\n", 0, chunk_start) + 1)
        for piece in chunk.split("\n"):
            if piece:
                raw_tokens.append((line_idx, col, len(piece), _STR_IDX))
            line_idx += 1
            col = 0

    cleaned = _STRING_RE.sub(_blank_match, cleaned)
    # 4. Line comments — clean copy doesn't have them either; scan original.
    for m in _LINE_COMMENT_RE.finditer(text):
        line_idx = text.count("\n", 0, m.start())
        col = m.start() - (text.rfind("\n", 0, m.start()) + 1)
        raw_tokens.append((line_idx, col, len(m.group(0)), _COMMENT_IDX))

    # 5. Per-line identifier + number scan on the cleaned text.
    for line_idx, line in enumerate(cleaned.splitlines()):
        prev_ident: str | None = None
        # Walk identifiers and numbers in order to track prev_ident.
        # Combine matches and sort by start to preserve order.
        events: list[tuple[int, int, int, str]] = []  # (start, end, type_idx, kind)
        for m in _IDENT_RE.finditer(line):
            events.append((m.start(), m.end(), -1, "ident"))
        for m in _NUMBER_RE.finditer(line):
            events.append((m.start(), m.end(), _NUM_IDX, "number"))
        events.sort(key=lambda t: t[0])
        for start, end, type_idx, kind in events:
            length = end - start
            if kind == "number":
                raw_tokens.append((line_idx, start, length, type_idx))
                continue
            word = line[start:end]
            cls = _classify_identifier(word, prev_ident)
            prev_ident = word
            if cls is not None:
                raw_tokens.append((line_idx, start, length, cls))

    # 6. Sort + delta-encode for the LSP wire format.
    raw_tokens.sort(key=lambda t: (t[0], t[1]))
    out: list[int] = []
    prev_line = 0
    prev_char = 0
    for line, char, length, type_idx in raw_tokens:
        delta_line = line - prev_line
        delta_start = char if delta_line != 0 else char - prev_char
        out.extend([delta_line, delta_start, length, type_idx, 0])
        prev_line = line
        prev_char = char
    return out


__all__ = ["TOKEN_MODIFIERS", "TOKEN_TYPES", "_semantic_tokens_for_text"]
