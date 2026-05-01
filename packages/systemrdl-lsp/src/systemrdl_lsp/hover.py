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


def _format_hex(value: int, width_hex_chars: int = 8) -> str:
    return f"0x{value:0{width_hex_chars}X}"


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


def _property_origin_hint(node: Any, prop_name: str) -> str:
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
    # Peek at the source line to label the kind of off-line assignment.
    label = "set"
    try:
        from pathlib import Path as _Path
        filename = getattr(prop_ref, "filename", None)
        if filename:
            line_text = _Path(filename).read_text(encoding="utf-8", errors="replace")\
                .splitlines()[prop_line - 1]
            stripped = line_text.lstrip()
            if "->" in line_text:
                label = "dynamic"
            elif stripped.startswith("default"):
                label = "default"
    except (OSError, IndexError):
        pass
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

    # Pre-pass: count elaborated nodes per (filename, line). count > 1 means
    # the line is reused-type body; we must not pick a single instance for it.
    # Walk only via ``children(unroll=True)`` — that already yields FieldNodes
    # for RegNode, so iterating fields() separately would double-count. (id()
    # guards don't help because systemrdl-compiler returns fresh Python wrapper
    # objects on every children()/fields() call.)
    line_uses: Counter[tuple[str, int]] = Counter()

    def collect(node: Any) -> None:
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
            # The reused-type-body filter (line shared by multiple elaborated
            # nodes → skip) protects against picking an arbitrary instance
            # for a line where addresses would differ per instance. That only
            # matters for AddressableNode (reg/regfile/addrmap) — those have
            # an absolute_address. Fields don't, so picking any instance for
            # a field-on-shared-line is safe (access / reset / encode are
            # intrinsic to the type body, not the instance).
            is_field = not isinstance(node, AddressableNode)
            if (
                ref_line == target_line_1b
                and ref_filename
                and (
                    is_field
                    or line_uses.get((str(ref_filename), ref_line), 0) <= 1
                )
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
        access_origin = _property_origin_hint(node, "sw")
        reset_origin = _property_origin_hint(node, "reset")
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
