# ruff: noqa: E501, UP007, UP037
"""Auto-generated from ``schemas/elaborated-tree.json``. DO NOT EDIT.

Regenerate via ``bun run codegen`` (Decision 9A).
"""

from __future__ import annotations

from typing import Literal, TypedDict, Union

try:
    from typing import NotRequired  # Python 3.11+
except ImportError:
    from typing_extensions import NotRequired  # type: ignore[assignment]


# $defs ---------------------------------------------------------------

HexU64 = str

class SourceLoc(TypedDict):
    uri: str
    line: int
    column: NotRequired[int]
    endLine: NotRequired[int]
    endColumn: NotRequired[int]

AccessMode = Literal['rw', 'ro', 'wo', 'w1c', 'w0c', 'w1s', 'w0s', 'rclr', 'rset', 'wclr', 'wset', 'na']

class EncodeEntry(TypedDict):
    name: str
    value: "HexU64"
    desc: NotRequired[str]

class Field(TypedDict):
    name: str
    displayName: NotRequired[str]
    lsb: int
    msb: int
    access: "AccessMode"
    reset: NotRequired["HexU64"]
    desc: NotRequired[str]
    source: NotRequired["SourceLoc"]
    isCounter: NotRequired[bool]
    isIntr: NotRequired[bool]
    encode: NotRequired[list["EncodeEntry"]]

class Reg(TypedDict):
    kind: Literal['reg']
    name: str
    type: NotRequired[str]
    displayName: NotRequired[str]
    address: "HexU64"
    width: Literal[8, 16, 32, 64]
    reset: NotRequired["HexU64"]
    accessSummary: NotRequired[str]
    desc: NotRequired[str]
    source: NotRequired["SourceLoc"]
    fields: list["Field"]

class Regfile(TypedDict):
    kind: Literal['regfile']
    name: str
    type: NotRequired[str]
    displayName: NotRequired[str]
    address: "HexU64"
    size: "HexU64"
    desc: NotRequired[str]
    source: NotRequired["SourceLoc"]
    children: list[Union["Regfile", "Reg"]]

class Addrmap(TypedDict):
    kind: Literal['addrmap']
    name: str
    type: NotRequired[str]
    displayName: NotRequired[str]
    address: "HexU64"
    size: "HexU64"
    desc: NotRequired[str]
    source: NotRequired["SourceLoc"]
    children: list[Union["Addrmap", "Regfile", "Reg"]]

# Top-level envelope ----------------------------------------------------

class ElaboratedTree(TypedDict):
    schemaVersion: Literal['0.1.0']
    version: NotRequired[int]
    unchanged: NotRequired[bool]
    elaboratedAt: NotRequired[str]
    stale: NotRequired[bool]
    roots: list["Addrmap"]
