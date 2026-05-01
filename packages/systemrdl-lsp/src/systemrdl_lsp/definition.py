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


def _find_instance_by_name(
    roots: list[RootNode],
    name: str,
    path_translate: dict[pathlib.Path, pathlib.Path] | None,
) -> Location | None:
    """Locate an instance declaration by its ``inst_name`` (signal, reg, field…).

    Used as a fallback for goto-def when the word isn't a top-level type.
    Useful for jumping from ``resetsignal = my_rst;`` to ``signal {…} my_rst;``,
    or from a property reference ``incr = my_signal;`` to its declaration.

    Returns the FIRST match's source location — for reused-type bodies multiple
    elaborated nodes share the same source span, so the answer is unambiguous.
    """
    if not name:
        return None
    from systemrdl.node import Node

    def search(node: Any) -> Location | None:
        if isinstance(node, Node):
            inst_name = getattr(node, "inst_name", None)
            if inst_name == name:
                inst = getattr(node, "inst", None)
                src_ref = (
                    getattr(inst, "inst_src_ref", None)
                    or getattr(inst, "def_src_ref", None)
                )
                loc = _src_ref_to_location(src_ref, path_translate)
                if loc is not None:
                    return loc
        if hasattr(node, "children"):
            try:
                for c in node.children(unroll=True):
                    found = search(c)
                    if found is not None:
                        return found
            except Exception:
                pass
        if hasattr(node, "fields"):
            try:
                for f in node.fields():
                    found = search(f)
                    if found is not None:
                        return found
            except Exception:
                pass
        return None

    for r in roots:
        loc = search(r)
        if loc is not None:
            return loc
    return None


def _references_to_type(
    type_name: str,
    roots: list[RootNode],
    include_declaration: bool,
    path_translate: dict[pathlib.Path, pathlib.Path] | None,
) -> list[Location]:
    """All instantiation sites of ``type_name`` plus optionally its declaration.

    Walks every cached elaborated tree looking for instances whose
    ``inst.original_def`` matches the named component definition. ``include_declaration``
    controls whether the type's own ``def_src_ref`` is included — VSCode passes
    ``context.includeDeclaration`` to let the user pick.
    """
    from systemrdl.node import Node

    if not type_name:
        return []

    out: list[Location] = []

    def visit(node: Any) -> None:
        if not isinstance(node, Node):
            return
        inst = getattr(node, "inst", None)
        # ``inst.original_def`` is the Component the user typed by name (e.g.
        # ``my_ctrl_t``). For inline anonymous components it's None — we skip
        # those because the type-name search by definition doesn't apply.
        original_def = getattr(inst, "original_def", None)
        def_name = getattr(original_def, "type_name", None) if original_def is not None else None
        if def_name == type_name:
            inst_src = getattr(inst, "inst_src_ref", None)
            if inst_src is not None:
                loc = _src_ref_to_location(inst_src, path_translate)
                if loc is not None:
                    out.append(loc)
        if hasattr(node, "children"):
            try:
                for c in node.children(unroll=True):
                    visit(c)
            except Exception:
                pass
        if hasattr(node, "fields"):
            try:
                for f in node.fields():
                    visit(f)
            except Exception:
                pass

    for r in roots:
        visit(r)

    if include_declaration:
        defs = _comp_defs_from_cached(roots)
        comp = defs.get(type_name)
        if comp is not None:
            decl = _definition_location(comp, path_translate)
            if decl is not None:
                # Put the declaration first — VSCode lists hits in array order.
                out.insert(0, decl)

    return out


def _rename_locations(
    type_name: str,
    roots: list[RootNode],
    path_translate: dict[pathlib.Path, pathlib.Path] | None,
    file_reader: Any,
) -> list[Location]:
    """Locations whose **literal text** is ``type_name`` and need rewriting.

    Differs from ``_references_to_type``: that function returns *instance*
    source ranges (e.g. ``CTRL`` in ``my_ctrl_t CTRL @ 0;``). Rename needs the
    *type-name* token range (``my_ctrl_t``), so we re-scan each ref's line
    for the identifier. ``file_reader`` is a ``(path, line_idx) -> str | None``
    callable that lets the caller decide whether to read from the LSP buffer
    cache or from disk.
    """
    if not type_name:
        return []

    # Collect (file_path, line_1b) pairs for: the type's declaration + every
    # instantiation site. We dedup so a multi-instance regfile doesn't yield
    # duplicate edit ranges.
    sites: list[tuple[pathlib.Path, int]] = []
    seen_sites: set[tuple[str, int]] = set()

    def _push_site(src_ref: Any) -> None:
        raw_filename = getattr(src_ref, "filename", None)
        line_1b = getattr(src_ref, "line", None)
        if not raw_filename or line_1b is None:
            return
        file_path = pathlib.Path(raw_filename)
        if path_translate:
            file_path = path_translate.get(file_path, file_path)
        key = (str(file_path), line_1b)
        if key in seen_sites:
            return
        seen_sites.add(key)
        sites.append((file_path, line_1b))

    defs = _comp_defs_from_cached(roots)
    comp = defs.get(type_name)
    if comp is not None:
        _push_site(getattr(comp, "def_src_ref", None))

    from systemrdl.node import Node

    def visit(node: Any) -> None:
        if not isinstance(node, Node):
            return
        inst = getattr(node, "inst", None)
        original_def = getattr(inst, "original_def", None)
        def_name = getattr(original_def, "type_name", None) if original_def is not None else None
        if def_name == type_name:
            _push_site(getattr(inst, "inst_src_ref", None))
        if hasattr(node, "children"):
            try:
                for c in node.children(unroll=True):
                    visit(c)
            except Exception:
                pass
        if hasattr(node, "fields"):
            try:
                for f in node.fields():
                    visit(f)
            except Exception:
                pass

    for r in roots:
        visit(r)

    out: list[Location] = []
    pattern = re.compile(rf"\b{re.escape(type_name)}\b")
    for path, line_1b in sites:
        line_text = file_reader(path, line_1b - 1)
        if line_text is None:
            continue
        m = pattern.search(line_text)
        if m is None:
            # The identifier isn't textually on this line — happens when
            # the compiler points at the line *after* the declaration
            # (rare). Skip rather than emit a wrong edit.
            continue
        out.append(
            Location(
                uri=path.as_uri(),
                range=_build_range(line_1b, m.start() + 1, m.end()),
            )
        )
    return out


def _src_ref_to_location(
    src_ref: Any, path_translate: dict[pathlib.Path, pathlib.Path] | None
) -> Location | None:
    """Helper: ``inst_src_ref`` → LSP ``Location``."""
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
