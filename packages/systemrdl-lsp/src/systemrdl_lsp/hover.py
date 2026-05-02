"""textDocument/hover: instance-lookup + word-based catalogue lookup."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .completion import (
    SYSTEMRDL_ADDRESSING_VALUES,
    SYSTEMRDL_ONREAD_VALUES,
    SYSTEMRDL_ONWRITE_VALUES,
    SYSTEMRDL_PRECEDENCE_VALUES,
    SYSTEMRDL_PROPERTIES,
    SYSTEMRDL_RW_VALUES,
    SYSTEMRDL_TOP_KEYWORDS,
    _user_properties_from_cached,
)
from .definition import _comp_defs_from_cached

if TYPE_CHECKING:
    from systemrdl.node import RootNode


# re-exported from compile.py so outline.py doesn't have to
# import from hover.py (outline is structurally higher-level than
# hover — hover is "tooltip on a single position", outline is "the
# whole document's symbol tree" — outline depending on hover for a
# pure number-formatting utility was an inverted dependency).
from .compile import _format_hex


def _literal_value(value: Any) -> Any:
    """Unwrap a systemrdl `StringLiteral` (or any other AST literal) to its
    Python value. Plain strings/ints/etc pass through unchanged. Returns
    None for None.

    Component-level `comp.properties` (and user-property `default`) hold
    AST-literal nodes, not raw Python values. Without this unwrap, hover
    prints `<systemrdl.ast.literals.StringLiteral object at 0x…>`.
    """
    if value is None:
        return None
    if hasattr(value, "get_value"):
        try:
            return value.get_value()
        except Exception:
            pass
    if hasattr(value, "val"):
        return value.val
    return value


def _format_parameter(p: Any) -> str:
    """Render a Parameter object as `NAME` for the type signature line."""
    return getattr(p, "name", "?") or "?"


def _param_default_label(expr: Any) -> str:
    """Best-effort stringification of a parameter's default expression."""
    val = getattr(expr, "value", None)
    if val is not None:
        return str(val)
    return repr(expr)


def _property_origin_hint(
    node: Any,
    prop_name: str,
    line_reader: Any = None,
) -> str:
    """Annotate a property's origin if it wasn't set on the node's own line.

    Distinguishes the two off-line origins:

    - **dynamic property assignment** — line contains ``->``. SystemRDL
      lets the user post-assign properties to an instance with
      ``some_inst->prop = value;`` outside the component body.
    - **default inheritance** — line begins with ``default`` (a parent
      scope's default-property assignment).

    Returns an empty string when the property is missing or set on the
    node's own declaration line.
    """
    inst = getattr(node, "inst", None)
    psrc = getattr(inst, "property_src_ref", None)
    if not isinstance(psrc, dict):
        return ""
    prop_ref = psrc.get(prop_name)
    if prop_ref is None:
        return ""
    prop_line = getattr(prop_ref, "line", None)
    own_ref = getattr(inst, "inst_src_ref", None) or getattr(inst, "def_src_ref", None)
    own_line = getattr(own_ref, "line", None) if own_ref else None
    if prop_line is None or own_line is None or prop_line == own_line:
        return ""
    # prefer the LSP buffer-cache line reader over a synchronous
    # disk read. Hover handlers run on the asyncio event loop; a slow
    # NFS / spinning-disk read here used to block diagnostics, completion
    # and every other LSP response for hundreds of milliseconds. The
    # caller passes in a ``line_reader(path, line_idx)`` that consults
    # the in-memory buffer first; we fall back to disk only when no
    # reader is provided (legacy callers + test paths).
    label = "set"
    line_text: str | None = None
    filename = getattr(prop_ref, "filename", None)
    if filename:
        if line_reader is not None:
            try:
                from pathlib import Path as _Path
                line_text = line_reader(_Path(filename), prop_line - 1)
            except Exception:
                line_text = None
        if line_text is None:
            try:
                from pathlib import Path as _Path
                lines = _Path(filename).read_text(encoding="utf-8", errors="replace").splitlines()
                if 0 <= prop_line - 1 < len(lines):
                    line_text = lines[prop_line - 1]
            except OSError:
                line_text = None
    if line_text is not None:
        stripped = line_text.lstrip()
        if "->" in line_text:
            label = "dynamic"
        elif stripped.startswith("default"):
            label = "default"
    return f" _(← {label} at line {prop_line})_"


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

    # single-pass walk. Pre-T4-B walked the tree TWICE (once
    # to build the line-uses Counter, once to find the matching node)
    # AND then double-visited fields (children(unroll=True) already
    # yields fields for RegNode, but the visit() loop also iterated
    # node.fields() on top — inflating each field's visit count and
    # blocking the asyncio event loop on every hover for ~2x as long
    # as needed). One walk now: collect the Counter and remember
    # candidates; filter at the end.
    line_uses: Counter[tuple[str, int]] = Counter()
    candidates: list[tuple[Any, bool, int, str]] = []

    def walk(node: Any) -> None:
        inst = getattr(node, "inst", None)
        src_ref = getattr(inst, "inst_src_ref", None) or getattr(inst, "def_src_ref", None)
        if src_ref is not None:
            line_1b = getattr(src_ref, "line", None)
            filename = getattr(src_ref, "filename", None)
            if line_1b is not None and filename:
                fname = str(filename)
                line_uses[(fname, line_1b)] += 1
                if line_1b == target_line_1b:
                    is_field = not isinstance(node, AddressableNode)
                    candidates.append((node, is_field, line_1b, fname))
        if isinstance(node, AddressableNode) or hasattr(node, "children"):
            try:
                for child in node.children(unroll=True):
                    walk(child)
            except Exception:
                pass

    root_list = roots if isinstance(roots, list) else [roots]
    for r in root_list:
        walk(r)

    # Pick the first viable candidate. The reused-type-body filter
    # (line shared by multiple AddressableNodes → skip; fields are
    # safe because their access / reset / encode is intrinsic to the
    # type body, not the instance).
    for node, is_field, ref_line, ref_filename in candidates:
        if is_field or line_uses.get((ref_filename, ref_line), 0) <= 1:
            return node
    return None


def _hover_for_word(word: str, roots: list[RootNode]) -> str | None:
    """Resolve a SystemRDL identifier to its hover documentation.

    Resolution order — most specific first, so a token that's both a keyword
    and a user-defined type label (rare but possible: ``mem``-named regfile)
    surfaces the user's definition over the language docs.

    1. User-defined component types from ``comp_defs``
    2. User-defined properties (`property foo { … };`)
    3. SystemRDL top-level keywords
    4. Property keywords
    5. Access-mode values (sw/hw values, onwrite, onread)
    """
    if not word:
        return None

    # 1. User-defined types first.
    defs = _comp_defs_from_cached(roots)
    comp = defs.get(word)
    if comp is not None:
        kind = type(comp).__name__.lower()
        props = getattr(comp, "properties", {}) or {}
        # Parametrized types: surface their parameter list in the title so the
        # user knows the type expects `#(...)` instantiation.
        params = list(getattr(comp, "parameters", []) or [])
        param_str = ""
        if params:
            param_str = " #(" + ", ".join(
                _format_parameter(p) for p in params
            ) + ")"
        out = [f"**{kind}** `{word}{param_str}`"]
        # Component property values are AST literals (`StringLiteral` etc),
        # not raw Python strings. Unwrap before printing or hover shows
        # `<systemrdl.ast.literals.StringLiteral object at 0x...>`.
        display_name = _literal_value(props.get("name"))
        desc = _literal_value(props.get("desc"))
        if display_name:
            out.append("")
            out.append(f"**{display_name}**")
        if desc:
            out.append("")
            out.append(str(desc))
        if not display_name and not desc and not params:
            out.append("")
            out.append(f"User-defined `{kind}` type.")
        if params:
            out.append("")
            out.append("**Parameters:**")
            for p in params:
                pt = getattr(getattr(p, "param_type", None), "__name__", "?")
                pdefault = getattr(p, "default_expr", None)
                line = f"- `{p.name}` : `{pt}`"
                if pdefault is not None:
                    line += f" (default: `{_param_default_label(pdefault)}`)"
                out.append(line)
        return "\n".join(out)

    # 2. User-defined property (`property foo { ... };`).
    user_props = _user_properties_from_cached(roots)
    user_prop = user_props.get(word)
    if user_prop is not None:
        bindable = getattr(user_prop, "bindable_to", None) or set()
        kinds = ", ".join(sorted(c.__name__.lower() for c in bindable)) or "any"
        valid = getattr(user_prop, "valid_type", None)
        valid_name = getattr(valid, "__name__", str(valid)) if valid else "any"
        default = _literal_value(getattr(user_prop, "default", None))
        out_lines = [f"**property** `{word}` _(user-defined)_", ""]
        out_lines.append(f"- **type**: `{valid_name}`")
        out_lines.append(f"- **bindable to**: {kinds}")
        if default is not None:
            out_lines.append(f"- **default**: `{default!r}`")
        return "\n".join(out_lines)

    # 3-5. Static catalogues. Each gets its own role label so the user knows
    # *why* something matched.
    for catalogue, role in (
        (SYSTEMRDL_TOP_KEYWORDS,        "keyword"),
        (SYSTEMRDL_PROPERTIES,          "property"),
        (SYSTEMRDL_RW_VALUES,           "access mode"),
        (SYSTEMRDL_ONWRITE_VALUES,      "onwrite value"),
        (SYSTEMRDL_ONREAD_VALUES,       "onread value"),
        (SYSTEMRDL_ADDRESSING_VALUES,   "addressing mode"),
        (SYSTEMRDL_PRECEDENCE_VALUES,   "precedence value"),
    ):
        if word in catalogue:
            return f"**`{word}`** _({role})_\n\n{catalogue[word]}"

    return None


def _hover_text_for_node(node: Any, line_reader: Any = None) -> str | None:
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
        access_origin = _property_origin_hint(node, "sw", line_reader)
        reset_origin = _property_origin_hint(node, "reset", line_reader)
        lines.append(f"**field** `{name}` `[{msb}:{lsb}]`")
        lines.append("")
        lines.append(f"- **access**: {access_label}{access_origin}")
        lines.append(f"- **reset**: {reset_str}{reset_origin}")
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
        # Parametrized instance: surface resolved parameter values (e.g.
        # `my_reg<WIDTH=16>`) so reused-with-different-params instances are
        # distinguishable in hover.
        inst = getattr(node, "inst", None)
        inst_params = list(getattr(inst, "parameters", []) or [])
        param_str = ""
        if inst_params:
            param_str = " #(" + ", ".join(
                f"{p.name}={getattr(p, 'value', '?')}" for p in inst_params
            ) + ")"
        title_extra = " · _bridge_" if is_bridge else ""
        lines.append(f"**{type_name}** `{name}{param_str}`{title_extra}")
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
