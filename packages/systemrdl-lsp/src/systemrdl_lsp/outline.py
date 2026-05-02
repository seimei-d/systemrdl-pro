"""Visibility/navigation features: documentSymbol, workspaceSymbol, foldingRange,
inlayHint, codeLens. Grouped together because each is a small read-only
elaborated-tree walk.
"""

from __future__ import annotations

import pathlib
import re
from typing import TYPE_CHECKING, Any

from lsprotocol.types import (
    CodeLens,
    Command,
    DocumentSymbol,
    FoldingRange,
    FoldingRangeKind,
    InlayHint,
    InlayHintKind,
    Location,
    Position,
    Range,
    SymbolInformation,
    SymbolKind,
)

from .compile import _format_hex
from .diagnostics import _build_range
from .serialize import _hex

if TYPE_CHECKING:
    from systemrdl.node import RootNode


# ---------------------------------------------------------------------------
# Folding ranges
# ---------------------------------------------------------------------------


_STRING_RE = re.compile(r'"(?:\\.|[^"\\])*"')
_LINE_COMMENT_RE = re.compile(r"//[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*[\s\S]*?\*/")
_NON_NEWLINE_RE = re.compile(r"[^\n]")


def _folding_ranges_from_text(text: str) -> list[FoldingRange]:
    """Compute folding ranges from `{...}` block spans.

    A purely textual scan: every `{` opens a range, the matching `}` closes
    it. Strings/comments are stripped first so braces inside them don't
    confuse the matcher. Single-line blocks (`{ field { ... } x[0:0]=0; }`)
    are skipped — too small to fold meaningfully.
    """

    def _blank_match(m: re.Match[str]) -> str:
        return _NON_NEWLINE_RE.sub(" ", m.group(0))

    cleaned = _STRING_RE.sub(_blank_match, text)
    cleaned = _LINE_COMMENT_RE.sub(_blank_match, cleaned)
    cleaned = _BLOCK_COMMENT_RE.sub(_blank_match, cleaned)

    ranges: list[FoldingRange] = []
    stack: list[int] = []  # line numbers of unmatched `{`
    line = 0
    for ch in cleaned:
        if ch == "\n":
            line += 1
        elif ch == "{":
            stack.append(line)
        elif ch == "}" and stack:
            start = stack.pop()
            if line > start:  # skip single-line blocks
                ranges.append(
                    FoldingRange(
                        start_line=start, end_line=line, kind=FoldingRangeKind.Region
                    )
                )
    return ranges


# ---------------------------------------------------------------------------
# Inlay hints
# ---------------------------------------------------------------------------


def _inlay_hints_for_addressables(
    roots: list[RootNode], path: pathlib.Path, buffer_text: str
) -> list[InlayHint]:
    """Inlay hints showing the resolved absolute address at the **end of the line**
    containing each register / regfile / addrmap declaration.

    Skips reused-type bodies: when the user defines ``regfile dma_channel_t {…}``
    and instantiates it twice as ``ch0 @ 0x100`` and ``ch1 @ 0x200``, the
    elaborated tree replays the type's internal source refs once per instance.
    Painting an absolute address on those internal lines would be a lie —
    the address differs per instance. We detect reuse by counting how many
    elaborated nodes share the same (filename, line) and skipping any line
    visited more than once.
    """
    from collections import Counter

    from systemrdl.node import AddressableNode

    hints: list[InlayHint] = []
    lines = buffer_text.splitlines() if buffer_text else []
    seen_lines: set[int] = set()
    line_uses: Counter[tuple[str, int]] = Counter()

    def collect_uses(node: Any) -> None:
        if isinstance(node, AddressableNode):
            inst = getattr(node, "inst", None)
            src_ref = getattr(inst, "inst_src_ref", None) or getattr(inst, "def_src_ref", None)
            line_1b = getattr(src_ref, "line", None)
            ref_filename = getattr(src_ref, "filename", None)
            if line_1b is not None and ref_filename:
                line_uses[(str(ref_filename), line_1b)] += 1
        if hasattr(node, "children"):
            try:
                for c in node.children(unroll=True):
                    collect_uses(c)
            except Exception:
                pass

    for r in roots:
        for top in r.children(unroll=True):
            collect_uses(top)

    def visit(node: Any) -> None:
        if not isinstance(node, AddressableNode):
            for c in getattr(node, "children", lambda **_: [])(unroll=True):
                visit(c)
            return

        inst = getattr(node, "inst", None)
        src_ref = getattr(inst, "inst_src_ref", None) or getattr(inst, "def_src_ref", None)
        line_1b = getattr(src_ref, "line", None)
        ref_filename = getattr(src_ref, "filename", None)

        if (
            line_1b is not None
            and ref_filename
            and pathlib.Path(ref_filename) == path
            # Skip lines reused by multiple elaborated instances — those are
            # the body of a multi-use regfile/reg type, where no single
            # absolute address is meaningful.
            and line_uses.get((str(ref_filename), line_1b), 0) <= 1
        ):
            line_0b = max(0, line_1b - 1)
            if line_0b not in seen_lines and line_0b < len(lines):
                seen_lines.add(line_0b)
                col_0b = len(lines[line_0b])
                try:
                    addr = node.absolute_address
                except Exception:
                    addr = None
                if addr is not None:
                    hints.append(
                        InlayHint(
                            position=Position(line=line_0b, character=col_0b),
                            label=f"  → {_hex(addr)}",
                            padding_left=True,
                            kind=InlayHintKind.Type,
                        )
                    )

        for c in node.children(unroll=True):
            visit(c)

    for r in roots:
        for top in r.children(unroll=True):
            visit(top)
    return hints


# ---------------------------------------------------------------------------
# CodeLens
# ---------------------------------------------------------------------------


def _code_lenses_for_addrmaps(
    roots: list[RootNode], path: pathlib.Path
) -> list[CodeLens]:
    """One ``📊 N regs · 0xS..0xE`` summary lens above every top-level addrmap."""
    from systemrdl.node import AddrmapNode, RegNode

    out: list[CodeLens] = []
    for r in roots:
        for top in r.children(unroll=True):
            if not isinstance(top, AddrmapNode):
                continue
            inst = getattr(top, "inst", None)
            src_ref = getattr(inst, "inst_src_ref", None) or getattr(inst, "def_src_ref", None)
            line_1b = getattr(src_ref, "line", None)
            ref_filename = getattr(src_ref, "filename", None)
            if line_1b is None or not ref_filename or pathlib.Path(ref_filename) != path:
                continue
            line_0b = max(0, line_1b - 1)
            rng = Range(
                start=Position(line=line_0b, character=0),
                end=Position(line=line_0b, character=0),
            )

            reg_count = 0
            min_addr: int | None = None
            max_addr: int | None = None

            def walk(node: Any) -> None:
                nonlocal reg_count, min_addr, max_addr
                if isinstance(node, RegNode):
                    reg_count += 1
                    a = node.absolute_address
                    min_addr = a if min_addr is None else min(min_addr, a)
                    end = a + max(1, getattr(node, "size", 1))
                    max_addr = end if max_addr is None else max(max_addr, end)
                if hasattr(node, "children"):
                    for c in node.children(unroll=True):
                        walk(c)

            walk(top)
            if reg_count:
                summary = f"📊 {reg_count} reg{'s' if reg_count != 1 else ''}"
                if min_addr is not None and max_addr is not None:
                    summary += f" · {_hex(min_addr)}..{_hex(max_addr)}"
                out.append(CodeLens(range=rng, command=Command(title=summary, command="")))
    return out


# ---------------------------------------------------------------------------
# workspace/symbol
# ---------------------------------------------------------------------------


def _workspace_symbols_for_uri(
    uri: str, roots: list[RootNode], query: str
) -> list[SymbolInformation]:
    """Walk the cached elaboration of one URI looking for symbols matching ``query``.

    ``query`` is matched as case-insensitive substring against instance name.
    """
    from systemrdl.node import AddrmapNode, FieldNode, RegfileNode, RegNode

    q = query.lower()
    out: list[SymbolInformation] = []

    def kind_of(node: Any) -> SymbolKind:
        if isinstance(node, AddrmapNode):
            return SymbolKind.Module
        if isinstance(node, RegfileNode):
            return SymbolKind.Namespace
        if isinstance(node, RegNode):
            return SymbolKind.Struct
        if isinstance(node, FieldNode):
            return SymbolKind.Field
        return SymbolKind.Variable

    def visit(node: Any, parent_path: list[str]) -> None:
        name = getattr(node, "inst_name", None)
        if not name:
            for c in getattr(node, "children", lambda **_: [])(unroll=True):
                visit(c, parent_path)
            return
        path = [*parent_path, name]
        if q in name.lower():
            inst = getattr(node, "inst", None)
            src_ref = getattr(inst, "inst_src_ref", None) or getattr(inst, "def_src_ref", None)
            if src_ref is not None:
                line_1b = getattr(src_ref, "line", None) or 1
                sel = getattr(src_ref, "line_selection", None) or (1, 1)
                try:
                    cs, ce = sel
                except (TypeError, ValueError):
                    cs = ce = 1
                rng = _build_range(line_1b, cs, ce)
                out.append(
                    SymbolInformation(
                        name=name,
                        kind=kind_of(node),
                        location=Location(uri=uri, range=rng),
                        container_name=".".join(parent_path) if parent_path else None,
                    )
                )
        if hasattr(node, "children"):
            for c in node.children(unroll=True):
                visit(c, path)
        if hasattr(node, "fields"):
            try:
                for f in node.fields():
                    visit(f, path)
            except Exception:
                pass

    for r in roots:
        for top in r.children(unroll=True):
            visit(top, [])
    return out


# ---------------------------------------------------------------------------
# documentSymbol
# ---------------------------------------------------------------------------


def _document_symbols(roots: list[RootNode] | RootNode) -> list[DocumentSymbol]:
    """Build a tree of LSP DocumentSymbols mirroring addrmap → regfile → reg → field.

    Accepts either a single RootNode (test convenience) or the list stored in
    the elaboration cache (one per top-level addrmap definition). Returns a
    flat list of top-level symbols across all roots.
    """
    from systemrdl.node import AddrmapNode, FieldNode, RegfileNode, RegNode

    def kind_of(node: Any) -> SymbolKind:
        if isinstance(node, AddrmapNode):
            return SymbolKind.Module
        if isinstance(node, RegfileNode):
            return SymbolKind.Namespace
        if isinstance(node, RegNode):
            return SymbolKind.Struct
        if isinstance(node, FieldNode):
            return SymbolKind.Field
        return SymbolKind.Variable

    def build(node: Any) -> DocumentSymbol | None:
        inst = getattr(node, "inst", None)
        src_ref = getattr(inst, "inst_src_ref", None) or getattr(inst, "def_src_ref", None)
        if src_ref is None:
            return None
        line_1b = getattr(src_ref, "line", None) or 1
        sel = getattr(src_ref, "line_selection", None) or (1, 1)
        try:
            cs, ce = sel
        except (TypeError, ValueError):
            cs = ce = 1
        rng = _build_range(line_1b, cs, ce)

        # ``children(unroll=True)`` already yields fields for RegNode — don't iterate
        # ``fields()`` separately or every field shows up twice in the outline.
        children: list[DocumentSymbol] = []
        if hasattr(node, "children"):
            try:
                for c in node.children(unroll=True):
                    sym = build(c)
                    if sym is not None:
                        children.append(sym)
            except Exception:
                pass

        name = getattr(node, "inst_name", None) or "(anonymous)"
        detail_parts: list[str] = []
        if hasattr(node, "absolute_address"):
            try:
                detail_parts.append(_format_hex(node.absolute_address))
            except Exception:
                pass

        return DocumentSymbol(
            name=name,
            detail=" ".join(detail_parts) or None,
            kind=kind_of(node),
            range=rng,
            selection_range=rng,
            children=children,
        )

    root_list = roots if isinstance(roots, list) else [roots]
    out: list[DocumentSymbol] = []
    for r in root_list:
        for top in r.children(unroll=True):
            sym = build(top)
            if sym is not None:
                out.append(sym)
    return out
