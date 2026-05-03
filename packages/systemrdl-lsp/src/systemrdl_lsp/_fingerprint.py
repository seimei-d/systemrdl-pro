"""AST fingerprinting (T2-B).

Stable SHA-256 over the viewer-facing semantics of an elaborated tree.
The LSP compares fingerprints between successive elaborates to detect
"semantic no-op" edits (whitespace, comments, formatting) and skip the
cache version bump + ``rdl/elaboratedTreeChanged`` push.

Hashed inputs: node type, instance name, absolute address, array
dimensions, regwidth/accesswidth (regs), lsb/msb + access modifiers
(fields), and recursive child hashes. Excluded on purpose: source line
numbers, file paths, ``inst_src_ref``, dynamic property origin — those
change with no semantic effect on the viewer.

Property lists below MUST mirror every ``_cached_prop(node, "X", cache)``
call in :mod:`.serialize`. When you add a property to the spine
envelope, add it here too — otherwise an edit to that property will
hash identically and the viewer will render stale data. There is a
matching test ``test_fingerprint_changes_on_<X>`` for each.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from systemrdl.node import RootNode


_FIELD_FP_PROPS = (
    # Access semantics — drives the access summary glyph + decoder.
    "sw", "hw", "onread", "onwrite",
    "we", "wel", "swacc", "swmod",
    "rclr", "rset", "woclr", "woset",
    # Field-level state.
    "reset", "singlepulse", "intr", "counter",
    # Documentation surface — viewer renders these.
    "name", "desc",
    # Enum encoding — viewer renders the value table.
    "encode",
    # Presence — flipping ispresent must invalidate the cached spine.
    "ispresent",
)
_REG_FP_PROPS = (
    "regwidth", "accesswidth", "shared",
    "name", "desc",
    "ispresent",
)
_CONTAINER_FP_PROPS = (
    # Addrmap / Regfile / Mem — viewer shows these in the tree row.
    "name", "desc", "bridge",
    "ispresent",
)


def _fingerprint_roots(roots: list[RootNode]) -> str:
    """Return a stable hex digest of the elaborated tree's viewer surface.

    Two compiles whose fingerprints match are guaranteed to produce
    identical spine envelopes — within a few hundred ms on 25k registers,
    cheaper than the spine serialize we avoid by short-circuiting.
    """
    from systemrdl.node import (
        AddrmapNode,
        FieldNode,
        MemNode,
        RegfileNode,
        RegNode,
    )
    from .compile import _children_safe

    h = hashlib.sha256()

    def feed(s: str) -> None:
        h.update(s.encode("utf-8", errors="replace"))
        h.update(b"\0")

    # Type-level memo, mirroring serialize.py's `_TypeCache` pattern.
    # `node.get_property()` walks the override → type → default chain on
    # every call — uncached, that's 200k+ traversals on a 25k-reg fixture
    # (one fingerprint per elaborate). Cache by `(id(original_def),
    # prop_name)`; bypass the cache when the property is overridden on
    # this specific instance (instance-level overrides are correct only
    # when read directly).
    _MISSING = object()
    prop_cache: dict[tuple[int, str], Any] = {}

    def safe_prop(node: Any, name: str) -> str:
        inst = getattr(node, "inst", None)
        if inst is not None:
            inst_props = getattr(inst, "properties", None)
            def_obj = getattr(inst, "original_def", None)
            if def_obj is not None and (
                inst_props is None or name not in inst_props
            ):
                key = (id(def_obj), name)
                cached = prop_cache.get(key, _MISSING)
                if cached is not _MISSING:
                    return cached
                try:
                    value = node.get_property(name)
                except Exception:
                    value = "ERR"
                rendered = (
                    "" if value is None
                    else value if value == "ERR"
                    else repr(value)
                )
                prop_cache[key] = rendered
                return rendered
        try:
            value = node.get_property(name)
        except Exception:
            return "ERR"
        if value is None:
            return ""
        # Enum values (AccessType, OnReadType, ...) and ints have stable repr.
        # Expressions get whatever __repr__ yields, consistent across runs on
        # the same compiler version.
        return repr(value)

    def visit(node: Any) -> None:
        feed(type(node).__name__)
        feed(str(node.inst_name or ""))
        try:
            feed(str(node.absolute_address))
        except Exception:
            feed("?addr")
        inst = getattr(node, "inst", None)
        if inst is not None:
            feed(str(getattr(inst, "type_name", "") or ""))
            try:
                dims = getattr(inst, "array_dimensions", None) or ()
                feed(",".join(str(d) for d in dims))
            except Exception:
                feed("?dims")
            try:
                stride = getattr(inst, "array_stride", None)
                feed(str(stride) if stride is not None else "")
            except Exception:
                feed("?stride")
        if isinstance(node, RegNode):
            for prop in _REG_FP_PROPS:
                feed(f"{prop}={safe_prop(node, prop)}")
            feed(f"is_alias={getattr(node, 'is_alias', False)}")
        elif isinstance(node, FieldNode):
            try:
                feed(f"lsb={node.lsb}")
                feed(f"msb={node.msb}")
            except Exception:
                feed("?bits")
            for prop in _FIELD_FP_PROPS:
                feed(f"{prop}={safe_prop(node, prop)}")
        elif isinstance(node, (AddrmapNode, RegfileNode, MemNode)):
            for prop in _CONTAINER_FP_PROPS:
                feed(f"{prop}={safe_prop(node, prop)}")
        kids = _children_safe(node)
        feed(f"#kids={len(kids)}")
        for kid in kids:
            visit(kid)

    for root in roots:
        feed("$ROOT")
        for child in _children_safe(root):
            visit(child)

    return h.hexdigest()
