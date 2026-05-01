"""textDocument/hover: instance-lookup + word-based catalogue lookup."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .completion import (
    SYSTEMRDL_ONREAD_VALUES,
    SYSTEMRDL_ONWRITE_VALUES,
    SYSTEMRDL_PROPERTIES,
    SYSTEMRDL_RW_VALUES,
    SYSTEMRDL_TOP_KEYWORDS,
)
from .definition import _comp_defs_from_cached

if TYPE_CHECKING:
    from systemrdl.node import RootNode


def _format_hex(value: int, width_hex_chars: int = 8) -> str:
    return f"0x{value:0{width_hex_chars}X}"


def _node_at_position(
    roots: list[RootNode] | RootNode, line_0b: int, char_0b: int
) -> Any | None:
    """Return the deepest elaborated node whose source span contains the cursor.

    Accepts either a single ``RootNode`` (legacy/test convenience) or the list
    stored in :class:`CachedElaboration` (multi-root, Decision 3C).
    """
    from systemrdl.node import AddressableNode

    best: Any = None
    best_span: int = 10**9
    target_line_1b = line_0b + 1

    def visit(node: Any) -> None:
        nonlocal best, best_span
        inst = getattr(node, "inst", None)
        src_ref = getattr(inst, "inst_src_ref", None) or getattr(inst, "def_src_ref", None)
        if src_ref is not None:
            ref_line = getattr(src_ref, "line", None)
            if ref_line == target_line_1b:
                # Approximate "smallest containing node" by counting depth.
                span = 1
                if span < best_span:
                    best = node
                    best_span = span
        if isinstance(node, AddressableNode) or hasattr(node, "children"):
            for child in node.children(unroll=True):
                visit(child)
        if hasattr(node, "fields"):
            try:
                for f in node.fields():
                    visit(f)
            except Exception:
                pass

    if isinstance(roots, list):
        for r in roots:
            visit(r)
    else:
        visit(roots)
    return best


def _hover_for_word(word: str, roots: list[RootNode]) -> str | None:
    """Resolve a SystemRDL identifier to its hover documentation.

    Resolution order — most specific first, so a token that's both a keyword
    and a user-defined type label (rare but possible: ``mem``-named regfile)
    surfaces the user's definition over the language docs.

    1. User-defined component types from ``comp_defs``
    2. SystemRDL top-level keywords
    3. Property keywords
    4. Access-mode values (sw/hw values, onwrite, onread)
    """
    if not word:
        return None

    # 1. User-defined types first.
    defs = _comp_defs_from_cached(roots)
    comp = defs.get(word)
    if comp is not None:
        kind = type(comp).__name__.lower()
        props = getattr(comp, "properties", {}) or {}
        out = [f"**{kind}** `{word}`"]
        display_name = props.get("name")
        desc = props.get("desc")
        if display_name:
            out.append("")
            out.append(f"**{display_name}**")
        if desc:
            out.append("")
            out.append(str(desc))
        if not display_name and not desc:
            out.append("")
            out.append(f"User-defined `{kind}` type.")
        return "\n".join(out)

    # 2-4. Static catalogues. Each gets its own role label so the user knows
    # *why* something matched.
    for catalogue, role in (
        (SYSTEMRDL_TOP_KEYWORDS,    "keyword"),
        (SYSTEMRDL_PROPERTIES,      "property"),
        (SYSTEMRDL_RW_VALUES,       "access mode"),
        (SYSTEMRDL_ONWRITE_VALUES,  "onwrite value"),
        (SYSTEMRDL_ONREAD_VALUES,   "onread value"),
    ):
        if word in catalogue:
            return f"**`{word}`** _({role})_\n\n{catalogue[word]}"

    return None


def _hover_text_for_node(node: Any) -> str | None:
    from systemrdl.node import AddressableNode, FieldNode, RegNode

    lines: list[str] = []
    name = getattr(node, "inst_name", None) or "(anonymous)"
    type_name = type(node).__name__.replace("Node", "").lower()

    if isinstance(node, FieldNode):
        lsb, msb = node.lsb, node.msb
        try:
            access = node.get_property("sw")
            access_label = getattr(access, "name", str(access)) if access else "?"
        except LookupError:
            access_label = "?"
        try:
            reset = node.get_property("reset")
            reset_str = _format_hex(int(reset)) if reset is not None else "—"
        except LookupError:
            reset_str = "—"
        lines.append(f"**field** `{name}` `[{msb}:{lsb}]`")
        lines.append("")
        lines.append(f"- **access**: {access_label}")
        lines.append(f"- **reset**: {reset_str}")
        try:
            desc = node.get_property("desc")
            if desc:
                lines.append("")
                lines.append(str(desc))
        except LookupError:
            pass
        return "\n".join(lines)

    if isinstance(node, AddressableNode):
        addr = node.absolute_address
        size = getattr(node, "size", None)
        lines.append(f"**{type_name}** `{name}`")
        lines.append("")
        lines.append(f"- **address**: {_format_hex(addr)}")
        if size is not None:
            lines.append(f"- **size**: {_format_hex(size)}")
        if isinstance(node, RegNode):
            try:
                lines.append(f"- **width**: {node.get_property('regwidth')}")
            except LookupError:
                pass
            # Roll up a reset value from constituent fields when every field has a reset.
            try:
                reg_reset = 0
                have_all = True
                for f in node.fields():
                    fr = f.get_property("reset")
                    if fr is None:
                        have_all = False
                        break
                    reg_reset |= (int(fr) & ((1 << (f.msb - f.lsb + 1)) - 1)) << f.lsb
                if have_all:
                    lines.append(f"- **reset**: {_format_hex(reg_reset)}")
            except (LookupError, AttributeError):
                pass
        try:
            desc = node.get_property("desc")
            if desc:
                lines.append("")
                lines.append(str(desc))
        except LookupError:
            pass
        return "\n".join(lines)

    return None
