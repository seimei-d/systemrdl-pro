"""Elaborated tree → JSON serializer (rdl/elaboratedTree, schema v0.1.0)."""

from __future__ import annotations

import datetime
import logging
import pathlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from systemrdl.node import RootNode

logger = logging.getLogger(__name__)

ELABORATED_TREE_SCHEMA_VERSION = "0.1.0"


def _safe_get_property(node: Any, prop: str) -> Any:
    """``node.get_property(prop)`` with LookupError + AttributeError swallowed."""
    try:
        return node.get_property(prop)
    except (LookupError, AttributeError):
        return None


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


def _field_access_token(node: Any) -> str:
    """Map systemrdl-compiler field semantics to the schema's AccessMode enum."""
    try:
        sw = node.get_property("sw")
    except LookupError:
        sw = None
    try:
        onwrite = node.get_property("onwrite")
    except LookupError:
        onwrite = None

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
    node: Any, path_translate: dict[pathlib.Path, pathlib.Path] | None = None
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "name": node.inst_name or "",
        "lsb": node.lsb,
        "msb": node.msb,
        "access": _field_access_token(node),
    }
    display_name = _safe_get_property(node, "name")
    if display_name:
        out["displayName"] = str(display_name)
    try:
        reset = node.get_property("reset")
        if reset is not None:
            out["reset"] = _hex(int(reset), node.msb - node.lsb + 1)
    except LookupError:
        pass
    try:
        desc = node.get_property("desc")
        if desc:
            out["desc"] = str(desc)
    except LookupError:
        pass
    inst = getattr(node, "inst", None)
    src = _src_ref_to_dict(
        getattr(inst, "inst_src_ref", None) or getattr(inst, "def_src_ref", None),
        path_translate,
    )
    if src:
        out["source"] = src
    return out


def _serialize_reg(
    node: Any, path_translate: dict[pathlib.Path, pathlib.Path] | None = None
) -> dict[str, Any]:
    fields = []
    accesses: list[str] = []
    reg_reset = 0
    have_all_resets = True
    for f in node.fields():
        sf = _serialize_field(f, path_translate)
        fields.append(sf)
        accesses.append(sf["access"].upper())
        try:
            fr = f.get_property("reset")
            if fr is None:
                have_all_resets = False
            else:
                width = f.msb - f.lsb + 1
                reg_reset |= (int(fr) & ((1 << width) - 1)) << f.lsb
        except (LookupError, AttributeError):
            have_all_resets = False

    width_bits = 32
    try:
        width_bits = int(node.get_property("regwidth"))
    except (LookupError, ValueError):
        pass

    out: dict[str, Any] = {
        "kind": "reg",
        "name": node.inst_name or "",
        "address": _hex(node.absolute_address, max(32, width_bits)),
        "width": width_bits,
        "fields": fields,
    }
    if hasattr(node.inst, "type_name") and node.inst.type_name:
        out["type"] = str(node.inst.type_name)
    display_name = _safe_get_property(node, "name")
    if display_name:
        out["displayName"] = str(display_name)
    if accesses:
        out["accessSummary"] = "/".join(dict.fromkeys(accesses))  # ordered unique
    if have_all_resets:
        out["reset"] = _hex(reg_reset, width_bits)
    try:
        desc = node.get_property("desc")
        if desc:
            out["desc"] = str(desc)
    except LookupError:
        pass
    inst = getattr(node, "inst", None)
    src = _src_ref_to_dict(
        getattr(inst, "inst_src_ref", None) or getattr(inst, "def_src_ref", None),
        path_translate,
    )
    if src:
        out["source"] = src
    return out


def _serialize_addressable(
    node: Any, path_translate: dict[pathlib.Path, pathlib.Path] | None = None
) -> dict[str, Any] | None:
    from systemrdl.node import AddrmapNode, RegfileNode, RegNode

    if isinstance(node, RegNode):
        return _serialize_reg(node, path_translate)
    if isinstance(node, (AddrmapNode, RegfileNode)):
        kind = "addrmap" if isinstance(node, AddrmapNode) else "regfile"
        children: list[dict[str, Any]] = []
        try:
            for c in node.children(unroll=True):
                child = _serialize_addressable(c, path_translate)
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
        if hasattr(node.inst, "type_name") and node.inst.type_name:
            out["type"] = str(node.inst.type_name)
        display_name = _safe_get_property(node, "name")
        if display_name:
            out["displayName"] = str(display_name)
        try:
            desc = node.get_property("desc")
            if desc:
                out["desc"] = str(desc)
        except LookupError:
            pass
        inst = getattr(node, "inst", None)
        src = _src_ref_to_dict(
            getattr(inst, "inst_src_ref", None) or getattr(inst, "def_src_ref", None),
            path_translate,
        )
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

    serialized_roots: list[dict[str, Any]] = []
    for r in root_list:
        try:
            for top in r.children(unroll=True):
                serialized = _serialize_addressable(top, path_translate)
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
