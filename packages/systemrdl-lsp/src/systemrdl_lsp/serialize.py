"""Elaborated tree → JSON serializer (rdl/elaboratedTree, schema v0.1.0).

Performance note: a per-call ``_TypeCache`` memoizes type-level property
lookups (``get_property`` walks the override → type → default chain on every
call, which is O(N) work repeated for every instance of the same regtype).
Instance overrides are detected via ``inst.properties`` and bypass the cache,
preserving correctness. For stress-fixture-shaped designs (one regtype
instantiated thousands of times) this turns the serializer from
super-linear into linear in instance count.
"""

from __future__ import annotations

import datetime
import logging
import pathlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from systemrdl.node import RootNode

logger = logging.getLogger(__name__)

ELABORATED_TREE_SCHEMA_VERSION = "0.1.0"


class _TypeCache:
    """Per-call cache of type-level lookups, keyed by id(component_def).

    Discarded at the end of each ``_serialize_root`` call so it never leaks
    state across LSP requests. Two stores:

    - ``props[(def_id, prop_name)]`` -> resolved property value (or None)
    - ``def_src_refs[def_id]`` -> serialized def_src_ref dict (or None)
    """

    __slots__ = ("def_src_refs", "props")

    def __init__(self) -> None:
        self.props: dict[tuple[int, str], Any] = {}
        self.def_src_refs: dict[int, dict[str, Any] | None] = {}


def _cached_prop(node: Any, name: str, cache: _TypeCache) -> Any:
    """``node.get_property(name)`` memoized by component def id.

    Bypasses the cache when the property is overridden on this specific
    instance (``inst.properties`` contains the name) — correctness wins
    over speed for the rare per-instance override.
    """
    inst = getattr(node, "inst", None)
    if inst is None:
        try:
            return node.get_property(name)
        except (LookupError, AttributeError):
            return None
    inst_props = getattr(inst, "properties", None)
    if inst_props is not None and name in inst_props:
        try:
            return node.get_property(name)
        except (LookupError, AttributeError):
            return None
    def_obj = getattr(inst, "original_def", None)
    if def_obj is None:
        try:
            return node.get_property(name)
        except (LookupError, AttributeError):
            return None
    key = (id(def_obj), name)
    cached = cache.props.get(key, _MISSING)
    if cached is not _MISSING:
        return cached
    try:
        value = node.get_property(name)
    except (LookupError, AttributeError):
        value = None
    cache.props[key] = value
    return value


_MISSING = object()


def _hex(value: int, width_bits: int = 32) -> str:
    """Format an unsigned int as ``0xAAAA_BBBB`` matching the JSON Schema regex."""
    digits = max(8, (width_bits + 3) // 4)
    digits = ((digits + 3) // 4) * 4  # round up to multiple of 4 so underscores are clean
    raw = f"{value:0{digits}X}"
    chunks = [raw[max(0, i - 4) : i] for i in range(len(raw), 0, -4)]
    return "0x" + "_".join(reversed(chunks))


def _src_ref_to_dict(
    src_ref: Any,
    path_translate: dict[pathlib.Path, pathlib.Path] | None = None,
) -> dict[str, Any] | None:
    if src_ref is None:
        return None
    filename = getattr(src_ref, "filename", None)
    line_1b = getattr(src_ref, "line", None)
    if not filename or line_1b is None:
        return None
    file_path = pathlib.Path(filename)
    if path_translate:
        file_path = path_translate.get(file_path, file_path)
    sel = getattr(src_ref, "line_selection", None) or (1, 1)
    try:
        cs, ce = sel
    except (TypeError, ValueError):
        cs = ce = 1
    return {
        "uri": file_path.as_uri(),
        "line": max(0, line_1b - 1),
        "column": max(0, cs - 1),
        "endLine": max(0, line_1b - 1),
        "endColumn": max(cs, ce),
    }


def _cached_def_src_ref(
    node: Any,
    cache: _TypeCache,
    path_translate: dict[pathlib.Path, pathlib.Path] | None,
) -> dict[str, Any] | None:
    """``def_src_ref`` (where the type is *defined*) is identical across all
    instances of the same type, so cache by component def id. ``inst_src_ref``
    (where this *instance* is declared) varies per instance and isn't cached.
    """
    inst = getattr(node, "inst", None)
    if inst is None:
        return None
    def_obj = getattr(inst, "original_def", None)
    if def_obj is None:
        return _src_ref_to_dict(getattr(inst, "def_src_ref", None), path_translate)
    def_id = id(def_obj)
    if def_id in cache.def_src_refs:
        return cache.def_src_refs[def_id]
    result = _src_ref_to_dict(getattr(inst, "def_src_ref", None), path_translate)
    cache.def_src_refs[def_id] = result
    return result


def _field_access_token(node: Any, cache: _TypeCache) -> str:
    """Map systemrdl-compiler field semantics to the schema's AccessMode enum.

    Cached at the type level — ``sw`` and ``onwrite`` for a given field
    definition are the same across every instance unless explicitly
    overridden, and overrides are surfaced through ``_cached_prop``.
    """
    sw = _cached_prop(node, "sw", cache)
    onwrite = _cached_prop(node, "onwrite", cache)
    sw_name = getattr(sw, "name", "").lower() if sw else ""
    on_name = getattr(onwrite, "name", "").lower() if onwrite else ""

    # Order matters: more specific tokens win.
    if on_name == "woclr":
        return "w1c"
    if on_name == "woset":
        return "w1s"
    if on_name == "wzc":
        return "w0c"
    if on_name == "wzs":
        return "w0s"
    if on_name == "wclr":
        return "wclr"
    if on_name == "wset":
        return "wset"
    if sw_name in {"rw", "ro", "wo"}:
        return sw_name
    if sw_name == "r":
        return "ro"
    if sw_name == "w":
        return "wo"
    return "na"


def _serialize_field(
    node: Any,
    cache: _TypeCache,
    path_translate: dict[pathlib.Path, pathlib.Path] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "name": node.inst_name or "",
        "lsb": node.lsb,
        "msb": node.msb,
        "access": _field_access_token(node, cache),
    }
    display_name = _cached_prop(node, "name", cache)
    if display_name:
        out["displayName"] = str(display_name)
    reset = _cached_prop(node, "reset", cache)
    if reset is not None:
        # Width-tight hex: a 1-bit field shows `0x0`, not `0x00000000`.
        width_bits = max(1, node.msb - node.lsb + 1)
        digits = max(1, (width_bits + 3) // 4)
        out["reset"] = f"0x{int(reset):0{digits}X}"
    desc = _cached_prop(node, "desc", cache)
    if desc:
        out["desc"] = str(desc)
    if _cached_prop(node, "counter", cache):
        out["isCounter"] = True
    if _cached_prop(node, "intr", cache):
        out["isIntr"] = True
    enc = _cached_prop(node, "encode", cache)
    if enc is not None:
        out["encode"] = _serialize_encode_entries(enc, node.msb - node.lsb + 1)
    inst = getattr(node, "inst", None)
    inst_src = getattr(inst, "inst_src_ref", None) if inst is not None else None
    src = _src_ref_to_dict(inst_src, path_translate) if inst_src is not None \
        else _cached_def_src_ref(node, cache, path_translate)
    if src:
        out["source"] = src
    return out


def _serialize_encode_entries(enc: Any, width_bits: int) -> list[dict[str, Any]]:
    """Walk a systemrdl-compiler ``enum`` component → list of entries.

    Each entry has ``name`` + ``value`` (hex). ``desc`` if present. Hex
    padding is **field-width-tight** — a 3-bit field produces ``0x4``, not
    ``0x00000000``. ``_hex`` (used elsewhere for addresses) has a 32-bit
    floor that's wrong for enum values bound to narrow fields.
    """
    entries: list[dict[str, Any]] = []
    members = getattr(enc, "members", None) or {}
    digits = max(1, (max(1, width_bits) + 3) // 4)
    for member_name, member in members.items():
        try:
            value = int(getattr(member, "value", 0))
        except (TypeError, ValueError):
            continue
        item: dict[str, Any] = {
            "name": member_name,
            "value": f"0x{value:0{digits}X}",
        }
        # systemrdl-compiler exposes the enum member's RDL `desc` and `name`
        # properties as `rdl_desc` / `rdl_name`. The unprefixed `name` /
        # `desc` attributes return the Python class name and docstring,
        # which aren't what the user wrote in the .rdl file.
        rdl_desc = getattr(member, "rdl_desc", None)
        if rdl_desc:
            item["desc"] = str(rdl_desc)
        rdl_name = getattr(member, "rdl_name", None)
        if rdl_name:
            item["displayName"] = str(rdl_name)
        entries.append(item)
    return entries


def _serialize_reg(
    node: Any,
    cache: _TypeCache,
    path_translate: dict[pathlib.Path, pathlib.Path] | None = None,
) -> dict[str, Any]:
    fields = []
    accesses: list[str] = []
    reg_reset = 0
    have_all_resets = True
    for f in node.fields():
        sf = _serialize_field(f, cache, path_translate)
        fields.append(sf)
        accesses.append(sf["access"].upper())
        fr = _cached_prop(f, "reset", cache)
        if fr is None:
            have_all_resets = False
        else:
            try:
                width = f.msb - f.lsb + 1
                reg_reset |= (int(fr) & ((1 << width) - 1)) << f.lsb
            except (TypeError, ValueError):
                have_all_resets = False

    width_bits = 32
    rw = _cached_prop(node, "regwidth", cache)
    if rw is not None:
        try:
            width_bits = int(rw)
        except (TypeError, ValueError):
            pass

    access_width = width_bits
    aw = _cached_prop(node, "accesswidth", cache)
    if aw is not None:
        try:
            access_width = int(aw)
        except (TypeError, ValueError):
            pass

    out: dict[str, Any] = {
        "kind": "reg",
        "name": node.inst_name or "",
        "address": _hex(node.absolute_address, max(32, width_bits)),
        "width": width_bits,
        "fields": fields,
    }
    if access_width != width_bits:
        out["accessWidth"] = access_width
    inst = getattr(node, "inst", None)
    if inst is not None and getattr(inst, "type_name", None):
        out["type"] = str(inst.type_name)
    display_name = _cached_prop(node, "name", cache)
    if display_name:
        out["displayName"] = str(display_name)
    if accesses:
        out["accessSummary"] = "/".join(dict.fromkeys(accesses))  # ordered unique
    if have_all_resets:
        out["reset"] = _hex(reg_reset, width_bits)
    desc = _cached_prop(node, "desc", cache)
    if desc:
        out["desc"] = str(desc)
    inst_src = getattr(inst, "inst_src_ref", None) if inst is not None else None
    src = _src_ref_to_dict(inst_src, path_translate) if inst_src is not None \
        else _cached_def_src_ref(node, cache, path_translate)
    if src:
        out["source"] = src
    return out


def _serialize_addressable(
    node: Any,
    cache: _TypeCache,
    path_translate: dict[pathlib.Path, pathlib.Path] | None = None,
) -> dict[str, Any] | None:
    from systemrdl.node import AddrmapNode, RegfileNode, RegNode

    if isinstance(node, RegNode):
        return _serialize_reg(node, cache, path_translate)
    if isinstance(node, (AddrmapNode, RegfileNode)):
        kind = "addrmap" if isinstance(node, AddrmapNode) else "regfile"
        children: list[dict[str, Any]] = []
        try:
            for c in node.children(unroll=True):
                child = _serialize_addressable(c, cache, path_translate)
                if child is not None:
                    children.append(child)
        except Exception:
            logger.exception("error walking children of %r", node)
        out: dict[str, Any] = {
            "kind": kind,
            "name": node.inst_name or "",
            "address": _hex(node.absolute_address),
            "size": _hex(node.size),
            "children": children,
        }
        inst = getattr(node, "inst", None)
        if inst is not None and getattr(inst, "type_name", None):
            out["type"] = str(inst.type_name)
        # Bridge flag (clause 9.2) — only meaningful on AddrmapNode but
        # safe to read on regfiles too (returns False / LookupError there).
        if isinstance(node, AddrmapNode):
            if _cached_prop(node, "bridge", cache):
                out["isBridge"] = True
        display_name = _cached_prop(node, "name", cache)
        if display_name:
            out["displayName"] = str(display_name)
        desc = _cached_prop(node, "desc", cache)
        if desc:
            out["desc"] = str(desc)
        inst_src = getattr(inst, "inst_src_ref", None) if inst is not None else None
        src = _src_ref_to_dict(inst_src, path_translate) if inst_src is not None \
            else _cached_def_src_ref(node, cache, path_translate)
        if src:
            out["source"] = src
        return out
    return None


def _unchanged_envelope(version: int) -> dict[str, Any]:
    """Tiny ``{unchanged: true, version}`` reply for `since_version` cache hits.

    The schema permits omitting ``roots`` when ``unchanged`` is true so the
    payload is constant-size regardless of tree size — that's the whole point
    of TODO-1's version-gated path.
    """
    return {
        "schemaVersion": ELABORATED_TREE_SCHEMA_VERSION,
        "version": version,
        "unchanged": True,
        "stale": False,
        "roots": [],
    }


def _serialize_root(
    roots_input: list[RootNode] | RootNode | None,
    stale: bool,
    path_translate: dict[pathlib.Path, pathlib.Path] | None = None,
    version: int = 0,
) -> dict[str, Any]:
    """Build the JSON envelope matching ``schemas/elaborated-tree.json`` v0.1.0.

    ``roots_input`` is either a list of ``RootNode`` (multi-root, Decision 3C —
    one per top-level ``addrmap`` definition) or a single ``RootNode | None``
    for legacy/test calls. Each ``RootNode``'s elaborated child addrmap becomes
    one entry in the output ``roots`` array — the viewer renders one tab per entry.

    ``path_translate`` rewrites filenames in source refs — used to swap the LSP's
    internal compile temp path for the user's real workspace path so that
    click-to-reveal in the viewer (Week 6) jumps to the editor's actual file.
    """
    if isinstance(roots_input, list):
        root_list = roots_input
    elif roots_input is None:
        root_list = []
    else:
        root_list = [roots_input]

    cache = _TypeCache()
    serialized_roots: list[dict[str, Any]] = []
    for r in root_list:
        try:
            for top in r.children(unroll=True):
                serialized = _serialize_addressable(top, cache, path_translate)
                if serialized is not None and serialized.get("kind") == "addrmap":
                    serialized_roots.append(serialized)
        except Exception:
            logger.exception("failed to serialize elaborated tree")

    return {
        "schemaVersion": ELABORATED_TREE_SCHEMA_VERSION,
        "version": version,
        "unchanged": False,
        "elaboratedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "stale": stale,
        "roots": serialized_roots,
    }


# Public alias retained for backwards compatibility with the (private-by-convention)
# `_safe_get_property` name used elsewhere in the codebase. Not used internally any
# more — `_cached_prop` supersedes it for serialize.py's hot path.
def _safe_get_property(node: Any, prop: str) -> Any:
    """``node.get_property(prop)`` with LookupError + AttributeError swallowed."""
    try:
        return node.get_property(prop)
    except (LookupError, AttributeError):
        return None
