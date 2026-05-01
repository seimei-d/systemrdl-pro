"""Generate TS + Python TypedDict types from schemas/elaborated-tree.json.

Decision 9A: schemas are the source of truth; both consumers (Python LSP and
TS extension/viewer) use generated shadow types so they can never drift.

The schema is small and stable, so we walk it directly rather than pulling
in a full JSON-Schema → code library. Supports the subset of constructs that
elaborated-tree.json actually uses:

- ``type: object`` with ``properties`` + ``required``
- ``type: string`` (with ``enum`` → string literal union)
- ``type: integer`` (with ``enum`` → numeric literal union)
- ``type: boolean``
- ``type: array`` with ``items``
- ``$ref`` to ``#/$defs/<Name>``
- ``oneOf`` of $refs (becomes a union)
- ``const`` (string only) → string literal type

Usage::

    uv run python tools/codegen.py
    # or via the wrapper:
    bun run codegen

Outputs:

- ``packages/systemrdl-lsp/src/systemrdl_lsp/_generated_types.py``
- ``packages/vscode-systemrdl-pro/src/types/elaborated-tree.generated.ts``
"""

from __future__ import annotations

import json
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "schemas" / "elaborated-tree.json"
PY_OUT = REPO_ROOT / "packages" / "systemrdl-lsp" / "src" / "systemrdl_lsp" / "_generated_types.py"
# Both TS consumers (extension + viewer-core) get their own copy so neither has
# to reach across the workspace boundary to the other's source tree.
TS_OUTS = [
    REPO_ROOT / "packages" / "vscode-systemrdl-pro" / "src" / "types" / "elaborated-tree.generated.ts",
    REPO_ROOT / "packages" / "rdl-viewer-core" / "src" / "_generated" / "elaborated-tree.ts",
]

PY_BANNER = """\
# ruff: noqa: E501, UP007, UP037
\"\"\"Auto-generated from ``schemas/elaborated-tree.json``. DO NOT EDIT.

Regenerate via ``bun run codegen`` (Decision 9A).
\"\"\"

from __future__ import annotations

from typing import Literal, TypedDict, Union

try:
    from typing import NotRequired  # Python 3.11+
except ImportError:
    from typing_extensions import NotRequired  # type: ignore[assignment]

"""

TS_BANNER = """\
// Auto-generated from `schemas/elaborated-tree.json`. DO NOT EDIT.
// Regenerate via `bun run codegen` (Decision 9A).

"""


def _ts_type(node: dict, *, top_level_oneof_alias: str | None = None) -> str:
    """Translate a JSON-Schema fragment to a TypeScript type expression."""
    if "$ref" in node:
        return node["$ref"].rsplit("/", 1)[-1]
    if "const" in node:
        v = node["const"]
        return f"'{v}'" if isinstance(v, str) else json.dumps(v)
    if "oneOf" in node:
        parts = [_ts_type(child) for child in node["oneOf"]]
        return "(" + " | ".join(parts) + ")"
    t = node.get("type")
    if t == "string":
        if "enum" in node:
            return " | ".join(f"'{v}'" for v in node["enum"])
        return "string"
    if t == "integer" or t == "number":
        if "enum" in node:
            return " | ".join(str(v) for v in node["enum"])
        return "number"
    if t == "boolean":
        return "boolean"
    if t == "array":
        inner = _ts_type(node["items"])
        # Wrap union element types so `(A | B)[]` parses correctly.
        if " | " in inner and not inner.startswith("("):
            inner = f"({inner})"
        return f"{inner}[]"
    if t == "object":
        return _ts_inline_object(node)
    return "unknown"


def _ts_inline_object(node: dict) -> str:
    required = set(node.get("required", []))
    props = node.get("properties", {})
    fields = []
    for name, schema in props.items():
        opt = "" if name in required else "?"
        fields.append(f"  {name}{opt}: {_ts_type(schema)};")
    if not fields:
        return "{}"
    return "{\n" + "\n".join(fields) + "\n}"


def _ts_alias(name: str, schema: dict) -> str:
    """Top-level export for a $defs entry."""
    if "oneOf" in schema:
        return f"export type {name} = {_ts_type(schema)};"
    if schema.get("type") == "string" and "enum" in schema:
        body = " | ".join(f"'{v}'" for v in schema["enum"])
        return f"export type {name} = {body};"
    if schema.get("type") == "object":
        return f"export type {name} = {_ts_inline_object(schema)};"
    if schema.get("type") == "string":
        return f"export type {name} = string;"
    return f"export type {name} = {_ts_type(schema)};"


# ---------------------------------------------------------------------------
# Python emit
# ---------------------------------------------------------------------------


# Reserved identifiers that can't appear as TypedDict attribute names. ``type``
# is a builtin name but valid as an attribute, so it's allowed.
PY_KEYWORDS = {"from", "import", "class", "def", "return", "in", "is", "and", "or", "not"}


def _py_type(node: dict) -> str:
    if "$ref" in node:
        return f'"{node["$ref"].rsplit("/", 1)[-1]}"'
    if "const" in node:
        v = node["const"]
        return f"Literal['{v}']" if isinstance(v, str) else f"Literal[{json.dumps(v)}]"
    if "oneOf" in node:
        parts = [_py_type(child) for child in node["oneOf"]]
        return f"Union[{', '.join(parts)}]"
    t = node.get("type")
    if t == "string":
        if "enum" in node:
            inner = ", ".join(f"'{v}'" for v in node["enum"])
            return f"Literal[{inner}]"
        return "str"
    if t == "integer" or t == "number":
        if "enum" in node:
            inner = ", ".join(str(v) for v in node["enum"])
            return f"Literal[{inner}]"
        return "int" if t == "integer" else "float"
    if t == "boolean":
        return "bool"
    if t == "array":
        return f"list[{_py_type(node['items'])}]"
    if t == "object":
        # No anonymous TypedDicts — every object the schema uses must be a $defs entry.
        return "dict"
    return "object"


def _py_class(name: str, schema: dict) -> str:
    required = set(schema.get("required", []))
    props = schema.get("properties", {})
    lines = [f"class {name}(TypedDict):"]
    if not props:
        lines.append("    pass")
        return "\n".join(lines)
    for prop_name, prop_schema in props.items():
        py_t = _py_type(prop_schema)
        # JSON Schema's optional fields → NotRequired in TypedDict.
        if prop_name not in required:
            py_t = f"NotRequired[{py_t}]"
        # Python identifier safety: TypedDict supports any string key, but using
        # the functional form for non-identifiers is rare. Our schema uses only
        # camelCase identifiers so the class form is fine.
        if prop_name in PY_KEYWORDS or not prop_name.isidentifier():
            raise ValueError(f"Field {prop_name!r} on {name} requires functional TypedDict form")
        lines.append(f"    {prop_name}: {py_t}")
    return "\n".join(lines)


def _py_alias(name: str, schema: dict) -> str:
    """Top-level for $defs entries that aren't object types."""
    if "oneOf" in schema:
        return f"{name} = {_py_type(schema)}"
    if schema.get("type") == "string" and "enum" in schema:
        return f"{name} = {_py_type(schema)}"
    if schema.get("type") == "string":
        return f"{name} = str"
    if schema.get("type") == "object":
        return _py_class(name, schema)
    return f"{name} = {_py_type(schema)}"


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def _topo_order(defs: dict[str, dict]) -> list[str]:
    """Order $defs so each name appears after its dependencies.

    Uses ``dict.fromkeys`` to track deps so iteration is insertion-ordered;
    plain ``set`` would re-order across runs (PYTHONHASHSEED is randomized
    per process) and we'd ship non-deterministic generated files.
    """
    deps: dict[str, dict[str, None]] = {k: {} for k in defs}
    for name, schema in defs.items():
        _collect_refs(schema, deps[name])
    out: list[str] = []
    visited: set[str] = set()
    visiting: set[str] = set()

    def visit(n: str) -> None:
        if n in visited:
            return
        if n in visiting:
            # Cycle (Addrmap → Addrmap): emit anyway, Python's string-ref
            # mechanism + TS native recursion handle it.
            return
        visiting.add(n)
        for d in deps.get(n, ()):
            if d in defs:
                visit(d)
        visiting.discard(n)
        visited.add(n)
        out.append(n)

    for n in defs:
        visit(n)
    return out


def _collect_refs(node: dict | list, into: dict[str, None]) -> None:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            into[ref.rsplit("/", 1)[-1]] = None
        for v in node.values():
            _collect_refs(v, into)
    elif isinstance(node, list):
        for v in node:
            _collect_refs(v, into)


def main() -> int:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    defs = schema.get("$defs", {})
    order = _topo_order(defs)

    # ----- Python emit ------------------------------------------------------
    py_chunks = [PY_BANNER]
    py_chunks.append("# $defs ---------------------------------------------------------------")
    for name in order:
        py_chunks.append("")
        py_chunks.append(_py_alias(name, defs[name]))
    # Top-level envelope: "ElaboratedTree".
    py_chunks.append("")
    py_chunks.append("# Top-level envelope ----------------------------------------------------")
    py_chunks.append("")
    py_chunks.append(_py_class("ElaboratedTree", schema))
    py_text = "\n".join(py_chunks).rstrip() + "\n"

    # ----- TS emit ----------------------------------------------------------
    ts_chunks = [TS_BANNER]
    for name in order:
        ts_chunks.append(_ts_alias(name, defs[name]))
        ts_chunks.append("")
    # Top-level envelope:
    ts_chunks.append(_ts_alias("ElaboratedTree", schema))
    ts_chunks.append("")
    ts_text = "\n".join(ts_chunks).rstrip() + "\n"

    PY_OUT.parent.mkdir(parents=True, exist_ok=True)
    PY_OUT.write_text(py_text, encoding="utf-8")
    print(f"[codegen] wrote {PY_OUT.relative_to(REPO_ROOT)}")
    for ts_out in TS_OUTS:
        ts_out.parent.mkdir(parents=True, exist_ok=True)
        ts_out.write_text(ts_text, encoding="utf-8")
        print(f"[codegen] wrote {ts_out.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
