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

    Skips lines belonging to the body of a reused type (e.g. a ``regfile``
    instantiated multiple times) — those lines map to multiple elaborated
    nodes with different absolute addresses, so picking any one of them
    would give a misleading hover. Caller falls through to the word-based
    catalogue lookup, which handles the type identifier itself correctly.
    """
    from collections import Counter

    from systemrdl.node import AddressableNode

    target_line_1b = line_0b + 1

    # Pre-pass: count elaborated nodes per (filename, line). count > 1 means
    # the line is reused-type body; we must not pick a single instance for it.
    # ``children(unroll=True)`` on RegNode already yields fields, so guard
    # against double-counting the same node via an id() set.
    line_uses: Counter[tuple[str, int]] = Counter()
    seen_nodes: set[int] = set()

    def collect(node: Any) -> None:
        nid = id(node)
        if nid in seen_nodes:
            return
        seen_nodes.add(nid)
        inst = getattr(node, "inst", None)
        src_ref = getattr(inst, "inst_src_ref", None) or getattr(inst, "def_src_ref", None)
        if src_ref is not None:
            line_1b = getattr(src_ref, "line", None)
            filename = getattr(src_ref, "filename", None)
            if line_1b is not None and filename:
                line_uses[(str(filename), line_1b)] += 1
        if isinstance(node, AddressableNode) or hasattr(node, "children"):
            try:
                for child in node.children(unroll=True):
                    collect(child)
            except Exception:
                pass
        if hasattr(node, "fields"):
            try:
                for f in node.fields():
                    collect(f)
            except Exception:
                pass

    root_list = roots if isinstance(roots, list) else [roots]
    for r in root_list:
        collect(r)

    best: Any = None
    best_span: int = 10**9

    def visit(node: Any) -> None:
        nonlocal best, best_span
        inst = getattr(node, "inst", None)
        src_ref = getattr(inst, "inst_src_ref", None) or getattr(inst, "def_src_ref", None)
        if src_ref is not None:
            ref_line = getattr(src_ref, "line", None)
            ref_filename = getattr(src_ref, "filename", None)
            if (
                ref_line == target_line_1b
                and ref_filename
                and line_uses.get((str(ref_filename), ref_line), 0) <= 1
            ):
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

    for r in root_list:
        visit(r)
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
        # Detect the SystemRDL `bridge` flag (clause 9.2) — only valid on
        # addrmap; surface it in the title so the user notices when an
        # addrmap is a bus bridge rather than a regular block.
        is_bridge = False
        try:
            is_bridge = bool(node.get_property("bridge"))
        except (LookupError, AttributeError):
            is_bridge = False
        title_extra = " · _bridge_" if is_bridge else ""
        lines.append(f"**{type_name}** `{name}`{title_extra}")
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
