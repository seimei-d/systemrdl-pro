"""LSP server for SystemRDL 2.0.

v0.2 (Week 2):

- Compile in-memory buffer (no save required) via a per-edit tempfile
- 300ms server-side debounce on ``textDocument/didChange``
- ``ElaborationCache`` keeps the last successful ``RootNode`` per URI; diagnostics
  are still published on every parse, so the user always sees the freshest error,
  but hover/outline data falls back to last-good (design D7: zero flicker)
- ``incl_search_paths`` from ``systemrdl-pro.includePaths`` workspace setting,
  plus the original .rdl file's directory as implicit fallback so a relative
  ``include`` keeps working
- Diagnostics filter: messages without a SourceRef are suppressed (they collapse
  to file line 1 and duplicate the real squiggle)

Hover and documentSymbol providers live alongside in this module.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
import logging
import pathlib
import tempfile
import time
import urllib.parse
from typing import TYPE_CHECKING, Any

from lsprotocol.types import (
    INITIALIZED,
    TEXT_DOCUMENT_DID_CHANGE,
    TEXT_DOCUMENT_DID_OPEN,
    TEXT_DOCUMENT_DID_SAVE,
    WORKSPACE_DID_CHANGE_CONFIGURATION,
    Diagnostic,
    DiagnosticSeverity,
    DidChangeConfigurationParams,
    DidChangeTextDocumentParams,
    DidOpenTextDocumentParams,
    DidSaveTextDocumentParams,
    InitializedParams,
    Position,
    PublishDiagnosticsParams,
    Range,
)
from pygls.lsp.server import LanguageServer
from systemrdl import RDLCompileError, RDLCompiler
from systemrdl.messages import MessagePrinter, Severity

if TYPE_CHECKING:
    from systemrdl.node import RootNode

logger = logging.getLogger(__name__)

SERVER_NAME = "systemrdl-lsp"
SERVER_VERSION = "0.4.0"
DEBOUNCE_SECONDS = 0.3
ELABORATED_TREE_SCHEMA_VERSION = "0.1.0"


# ---------------------------------------------------------------------------
# Capturing printer
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class CompilerMessage:
    """Severity + text + resolved location, decoupled from systemrdl's SourceRef.

    Decoupling is necessary because we compile a *temp* copy of the buffer; the
    raw SourceRef points at the temp path. We translate that to the original
    workspace path here so downstream consumers don't need to know about temp files.
    """

    severity: Severity
    text: str
    file_path: pathlib.Path | None
    line_1b: int | None  # 1-based line, None for messages with no location
    col_start_1b: int | None
    col_end_1b: int | None  # inclusive

    @classmethod
    def from_compiler(
        cls,
        severity: Severity,
        text: str,
        src_ref: Any,
        translate_path: dict[pathlib.Path, pathlib.Path] | None = None,
    ) -> "CompilerMessage":
        if src_ref is None:
            return cls(severity, text, None, None, None, None)

        raw_filename = getattr(src_ref, "filename", None)
        if raw_filename:
            file_path = pathlib.Path(raw_filename)
            if translate_path:
                file_path = translate_path.get(file_path, file_path)
        else:
            file_path = None

        line_1b = getattr(src_ref, "line", None)
        sel = getattr(src_ref, "line_selection", None) or (None, None)
        try:
            col_start_1b, col_end_1b = sel
        except (TypeError, ValueError):
            col_start_1b = col_end_1b = None

        return cls(severity, text, file_path, line_1b, col_start_1b, col_end_1b)


class CapturingPrinter(MessagePrinter):
    """Captures structured (severity, text, src_ref) tuples instead of writing to stderr."""

    def __init__(self) -> None:
        super().__init__()
        self.captured: list[tuple[Severity, str, Any]] = []

    def print_message(self, severity, text, src_ref):  # type: ignore[override]
        self.captured.append((severity, text, src_ref))


# ---------------------------------------------------------------------------
# Compilation
# ---------------------------------------------------------------------------


def _compile_text(
    uri: str,
    text: str,
    incl_search_paths: list[str] | None = None,
) -> tuple[list[CompilerMessage], "RootNode | None", pathlib.Path]:
    """Compile in-memory buffer text. Returns (messages, root_or_None, temp_path).

    Implementation: write ``text`` to a temp file (preserves line numbers verbatim),
    point ``incl_search_paths`` at the original file's directory so relative
    ``include`` paths resolve, run systemrdl-compiler, then translate temp file
    paths back to the original path in every captured message.

    The temp file is **not** unlinked here. ``SegmentedSourceRef`` reads its source
    file lazily when its ``.line`` / ``.line_selection`` properties are accessed — so
    while a ``RootNode`` is cached for hover/documentSymbol, its temp file must
    stay on disk. The caller owns the lifecycle: pass the temp path into
    :class:`ElaborationCache.put` (which unlinks the previous entry's temp file
    on replacement) or call ``unlink`` when discarding.
    """
    original_path = _uri_to_path(uri)
    search_paths = list(incl_search_paths or [])
    if original_path.parent.exists():
        search_paths.append(str(original_path.parent))

    printer = CapturingPrinter()
    compiler = RDLCompiler(message_printer=printer)

    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".rdl",
        prefix=".systemrdl-lsp-",
        encoding="utf-8",
        delete=False,
    ) as tf:
        tf.write(text)
        tmp_path = pathlib.Path(tf.name)

    root: RootNode | None = None
    try:
        compiler.compile_file(str(tmp_path), incl_search_paths=search_paths)
        root = compiler.elaborate()
    except RDLCompileError:
        root = None
    except Exception as exc:  # defensive: never crash the server
        logger.exception("unexpected error while compiling %s", original_path)
        printer.captured.append((Severity.ERROR, f"internal: {exc}", None))

    # Snapshot diagnostics while the temp file is still on disk — line/col are lazy.
    translate = {tmp_path: original_path}
    messages = [
        CompilerMessage.from_compiler(sev, text, src_ref, translate)
        for sev, text, src_ref in printer.captured
    ]
    return messages, root, tmp_path


def _elaborate(path: pathlib.Path) -> list[tuple[Severity, str, Any]]:
    """Legacy disk-based elaboration; kept for tests and ``didSave`` warm path."""
    if not path.exists():
        printer = CapturingPrinter()
        printer.captured.append((Severity.ERROR, f"file not found: {path}", None))
        return printer.captured

    text = path.read_text(encoding="utf-8")
    messages, _, tmp_path = _compile_text(path.as_uri(), text)
    tmp_path.unlink(missing_ok=True)  # legacy path doesn't cache, safe to drop now
    out: list[tuple[Severity, str, Any]] = []
    for m in messages:
        if m.line_1b is None:
            out.append((m.severity, m.text, None))
        else:
            ref = _SimpleRef(m.file_path, m.line_1b, m.col_start_1b or 1, m.col_end_1b or 1)
            out.append((m.severity, m.text, ref))
    return out


@dataclasses.dataclass(frozen=True)
class _SimpleRef:
    filename: pathlib.Path | None  # noqa: F841 (referenced via getattr)
    line: int
    _col_start: int
    _col_end: int

    @property
    def line_selection(self) -> tuple[int, int]:
        return (self._col_start, self._col_end)


# ---------------------------------------------------------------------------
# ElaborationCache (last-good per URI, design D7)
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class CachedElaboration:
    root: "RootNode"
    text: str
    elaborated_at: float
    # Path to the temp file that backs ``root``'s lazy source refs. Owned by the cache;
    # unlinked when the entry is replaced or the cache is dropped.
    temp_path: pathlib.Path | None = None


class ElaborationCache:
    """Per-URI cache of the last successful ``RootNode``.

    Hover / documentSymbol read from this cache. When a fresh parse fails we keep
    the prior entry so the viewer (Week 4) and hover can still answer about
    previously valid registers — matches design D7 ("zero-flicker stale UX").

    The cache also owns the temp file backing each ``RootNode`` (see
    :func:`_compile_text` for why). When ``put`` replaces an entry, the previous
    temp file is unlinked. ``clear`` and ``__del__`` drop everything.
    """

    def __init__(self) -> None:
        self._entries: dict[str, CachedElaboration] = {}

    def get(self, uri: str) -> CachedElaboration | None:
        return self._entries.get(uri)

    def put(
        self,
        uri: str,
        root: "RootNode",
        text: str,
        temp_path: pathlib.Path | None = None,
    ) -> None:
        old = self._entries.get(uri)
        if old is not None and old.temp_path is not None:
            old.temp_path.unlink(missing_ok=True)
        self._entries[uri] = CachedElaboration(
            root=root, text=text, elaborated_at=time.time(), temp_path=temp_path
        )

    def clear(self) -> None:
        for entry in self._entries.values():
            if entry.temp_path is not None:
                entry.temp_path.unlink(missing_ok=True)
        self._entries.clear()


# ---------------------------------------------------------------------------
# URI helpers
# ---------------------------------------------------------------------------


def _uri_to_path(uri: str) -> pathlib.Path:
    parsed = urllib.parse.urlparse(uri)
    if parsed.scheme not in ("file", ""):
        raise ValueError(f"Only file:// URIs are supported, got {uri!r}")
    return pathlib.Path(urllib.parse.unquote(parsed.path))


def _path_to_uri(p: pathlib.Path) -> str:
    return p.resolve().as_uri()


# ---------------------------------------------------------------------------
# Diagnostic conversion
# ---------------------------------------------------------------------------


def _severity_to_lsp(sev: Severity) -> DiagnosticSeverity:
    if sev in (Severity.ERROR, Severity.FATAL):
        return DiagnosticSeverity.Error
    if sev == Severity.WARNING:
        return DiagnosticSeverity.Warning
    if sev == Severity.INFO:
        return DiagnosticSeverity.Information
    return DiagnosticSeverity.Hint


def _src_ref_to_range(src_ref: Any) -> Range:
    """Legacy helper used by tests. Mirrors the conversion in :func:`_message_to_range`."""
    if src_ref is None:
        return Range(start=Position(line=0, character=0), end=Position(line=0, character=1))
    line_1b = getattr(src_ref, "line", None) or 1
    sel = getattr(src_ref, "line_selection", None) or (1, 1)
    try:
        start_col_1b, end_col_1b = sel
    except (TypeError, ValueError):
        start_col_1b = end_col_1b = 1
    return _build_range(line_1b, start_col_1b, end_col_1b)


def _message_to_range(msg: CompilerMessage) -> Range:
    if msg.line_1b is None:
        return Range(start=Position(line=0, character=0), end=Position(line=0, character=1))
    return _build_range(msg.line_1b, msg.col_start_1b or 1, msg.col_end_1b or msg.col_start_1b or 1)


def _build_range(line_1b: int, col_start_1b: int, col_end_1b: int) -> Range:
    line_0b = max(0, line_1b - 1)
    start_0b = max(0, col_start_1b - 1)
    end_0b = max(start_0b + 1, col_end_1b)
    return Range(
        start=Position(line=line_0b, character=start_0b),
        end=Position(line=line_0b, character=end_0b),
    )


# ---------------------------------------------------------------------------
# Diagnostics publishing
# ---------------------------------------------------------------------------


def _publish_diagnostics(
    server: LanguageServer,
    uri: str,
    messages: list[CompilerMessage],
) -> None:
    target_path = _uri_to_path(uri)
    diagnostics: list[Diagnostic] = []
    for m in messages:
        if m.file_path is None:
            # Sourceless meta messages (e.g. "Parse aborted due to previous errors")
            # would otherwise pin to file line 1 as redundant noise.
            continue
        try:
            same_file = m.file_path.resolve() == target_path.resolve()
        except OSError:
            same_file = m.file_path == target_path
        if not same_file:
            # Cross-file diagnostics (errors in `included files) — skip in this pass.
            # Week 3 will publish per-uri batches with a separate clear-on-resolve cycle.
            continue
        diagnostics.append(
            Diagnostic(
                range=_message_to_range(m),
                severity=_severity_to_lsp(m.severity),
                source=SERVER_NAME,
                message=m.text,
            )
        )

    server.text_document_publish_diagnostics(
        PublishDiagnosticsParams(uri=uri, diagnostics=diagnostics)
    )


# ---------------------------------------------------------------------------
# Server state
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class ServerState:
    cache: ElaborationCache = dataclasses.field(default_factory=ElaborationCache)
    pending: dict[str, asyncio.Task] = dataclasses.field(default_factory=dict)
    include_paths: list[str] = dataclasses.field(default_factory=list)
    # URIs whose latest parse attempt failed but for which we still have a last-good
    # cache entry. The viewer renders a stale-bar when a URI is in this set (D7).
    stale_uris: set[str] = dataclasses.field(default_factory=set)


# ---------------------------------------------------------------------------
# Hover + documentSymbol
# ---------------------------------------------------------------------------


def _format_hex(value: int, width_hex_chars: int = 8) -> str:
    return f"0x{value:0{width_hex_chars}X}"


def _node_at_position(root: "RootNode", line_0b: int, char_0b: int) -> Any | None:
    """Walk the elaborated tree, returning the deepest node whose source span contains the cursor."""
    from systemrdl.node import AddressableNode, FieldNode

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
        if isinstance(node, AddressableNode):
            # FieldNodes too
            for f in getattr(node, "fields", lambda: [])() if False else []:
                visit(f)
        if hasattr(node, "fields"):
            try:
                for f in node.fields():
                    visit(f)
            except Exception:
                pass

    visit(root)
    return best


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


# ---------------------------------------------------------------------------
# Elaborated tree serialization (rdl/elaboratedTree, schema v0.1.0)
# ---------------------------------------------------------------------------


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


def _serialize_field(
    node: Any, path_translate: dict[pathlib.Path, pathlib.Path] | None = None
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "name": node.inst_name or "",
        "lsb": node.lsb,
        "msb": node.msb,
        "access": _field_access_token(node),
    }
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
    type_name = type(node).__name__
    if hasattr(node.inst, "type_name") and node.inst.type_name:
        out["type"] = str(node.inst.type_name)
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
    del type_name  # quiet linters
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


def _serialize_root(
    root: "RootNode | None",
    stale: bool,
    path_translate: dict[pathlib.Path, pathlib.Path] | None = None,
) -> dict[str, Any]:
    """Build the JSON envelope matching ``schemas/elaborated-tree.json`` v0.1.0.

    ``path_translate`` rewrites filenames in source refs — used to swap the LSP's
    internal compile temp path for the user's real workspace path so that
    click-to-reveal in the viewer (Week 6) jumps to the editor's actual file.
    """
    roots: list[dict[str, Any]] = []
    if root is not None:
        try:
            for top in root.children(unroll=True):
                serialized = _serialize_addressable(top, path_translate)
                if serialized is not None and serialized.get("kind") == "addrmap":
                    roots.append(serialized)
        except Exception:
            logger.exception("failed to serialize elaborated tree")

    return {
        "schemaVersion": ELABORATED_TREE_SCHEMA_VERSION,
        "elaboratedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "stale": stale,
        "roots": roots,
    }


def _document_symbols(root: "RootNode") -> list[Any]:
    """Build a tree of LSP DocumentSymbols mirroring addrmap → regfile → reg → field."""
    from lsprotocol.types import DocumentSymbol, SymbolKind
    from systemrdl.node import AddrmapNode, FieldNode, RegfileNode, RegNode

    def kind_of(node: Any) -> SymbolKind:
        if isinstance(node, AddrmapNode):
            return SymbolKind.Module
        if isinstance(node, RegfileNode):
            return SymbolKind.Namespace
        if isinstance(node, RegNode):
            return SymbolKind.Struct
        if isinstance(node, FieldNode):
            return SymbolKind.Field
        return SymbolKind.Variable

    def build(node: Any) -> DocumentSymbol | None:
        inst = getattr(node, "inst", None)
        src_ref = getattr(inst, "inst_src_ref", None) or getattr(inst, "def_src_ref", None)
        if src_ref is None:
            return None
        line_1b = getattr(src_ref, "line", None) or 1
        sel = getattr(src_ref, "line_selection", None) or (1, 1)
        try:
            cs, ce = sel
        except (TypeError, ValueError):
            cs = ce = 1
        rng = _build_range(line_1b, cs, ce)

        # ``children(unroll=True)`` already yields fields for RegNode — don't iterate
        # ``fields()`` separately or every field shows up twice in the outline.
        children: list[DocumentSymbol] = []
        if hasattr(node, "children"):
            try:
                for c in node.children(unroll=True):
                    sym = build(c)
                    if sym is not None:
                        children.append(sym)
            except Exception:
                pass

        name = getattr(node, "inst_name", None) or "(anonymous)"
        detail_parts: list[str] = []
        if hasattr(node, "absolute_address"):
            try:
                detail_parts.append(_format_hex(node.absolute_address))
            except Exception:
                pass

        return DocumentSymbol(
            name=name,
            detail=" ".join(detail_parts) or None,
            kind=kind_of(node),
            range=rng,
            selection_range=rng,
            children=children,
        )

    out: list[DocumentSymbol] = []
    for top in root.children(unroll=True):
        sym = build(top)
        if sym is not None:
            out.append(sym)
    return out


# ---------------------------------------------------------------------------
# Server construction
# ---------------------------------------------------------------------------


def build_server() -> LanguageServer:
    server = LanguageServer(SERVER_NAME, SERVER_VERSION)
    state = ServerState()

    def _read_buffer(uri: str) -> str | None:
        try:
            doc = server.workspace.get_text_document(uri)
        except Exception:
            return None
        return doc.source

    def _full_pass(uri: str, buffer_text: str | None) -> None:
        if buffer_text is None:
            try:
                buffer_text = _uri_to_path(uri).read_text(encoding="utf-8")
            except (OSError, ValueError):
                return
        messages, root, tmp_path = _compile_text(uri, buffer_text, state.include_paths)
        if root is not None:
            # Cache takes ownership of the temp file (it backs lazy src_ref reads
            # for hover/documentSymbol). Old entry's temp file is unlinked there.
            state.cache.put(uri, root, buffer_text, tmp_path)
            state.stale_uris.discard(uri)
        else:
            # Parse failed and we keep the previous cache entry intact (last-good D7).
            # The just-written temp file isn't backing anything we'll read again — drop it.
            tmp_path.unlink(missing_ok=True)
            if state.cache.get(uri) is not None:
                state.stale_uris.add(uri)
        _publish_diagnostics(server, uri, messages)

    async def _debounced_full_pass(uri: str) -> None:
        try:
            await asyncio.sleep(DEBOUNCE_SECONDS)
        except asyncio.CancelledError:
            return
        try:
            _full_pass(uri, _read_buffer(uri))
        finally:
            state.pending.pop(uri, None)

    @server.feature(INITIALIZED)
    def _on_initialized(_ls: LanguageServer, _params: InitializedParams) -> None:
        # Fetch initial config snapshot. If the client doesn't return any, includePaths
        # stays empty and only sibling-dir resolution applies.
        async def fetch() -> None:
            try:
                from lsprotocol.types import (
                    ConfigurationItem,
                    WorkspaceConfigurationParams,
                )
                configs = await server.workspace_configuration_async(
                    WorkspaceConfigurationParams(
                        items=[ConfigurationItem(section="systemrdl-pro")]
                    )
                )
                if configs and isinstance(configs[0], dict):
                    paths = configs[0].get("includePaths") or []
                    state.include_paths = [str(p) for p in paths if p]
                    logger.info("includePaths from initial config: %s", state.include_paths)
            except Exception:
                logger.debug("could not fetch initial workspace configuration", exc_info=True)

        asyncio.ensure_future(fetch())

    @server.feature(TEXT_DOCUMENT_DID_OPEN)
    def _on_open(_ls: LanguageServer, params: DidOpenTextDocumentParams) -> None:
        _full_pass(params.text_document.uri, params.text_document.text)

    @server.feature(TEXT_DOCUMENT_DID_SAVE)
    def _on_save(_ls: LanguageServer, params: DidSaveTextDocumentParams) -> None:
        _full_pass(params.text_document.uri, _read_buffer(params.text_document.uri))

    @server.feature(TEXT_DOCUMENT_DID_CHANGE)
    def _on_change(_ls: LanguageServer, params: DidChangeTextDocumentParams) -> None:
        uri = params.text_document.uri
        existing = state.pending.get(uri)
        if existing is not None:
            existing.cancel()
        state.pending[uri] = asyncio.ensure_future(_debounced_full_pass(uri))

    @server.feature(WORKSPACE_DID_CHANGE_CONFIGURATION)
    def _on_config_change(
        _ls: LanguageServer, _params: DidChangeConfigurationParams
    ) -> None:
        async def refresh() -> None:
            try:
                from lsprotocol.types import (
                    ConfigurationItem,
                    WorkspaceConfigurationParams,
                )
                configs = await server.workspace_configuration_async(
                    WorkspaceConfigurationParams(
                        items=[ConfigurationItem(section="systemrdl-pro")]
                    )
                )
                if configs and isinstance(configs[0], dict):
                    paths = configs[0].get("includePaths") or []
                    state.include_paths = [str(p) for p in paths if p]
            except Exception:
                logger.debug("config refresh failed", exc_info=True)

        asyncio.ensure_future(refresh())

    @server.feature("textDocument/hover")
    def _on_hover(_ls: LanguageServer, params: Any) -> Any | None:
        from lsprotocol.types import Hover, MarkupContent, MarkupKind

        cached = state.cache.get(params.text_document.uri)
        if cached is None:
            return None
        node = _node_at_position(
            cached.root, params.position.line, params.position.character
        )
        if node is None:
            return None
        markdown = _hover_text_for_node(node)
        if markdown is None:
            return None
        return Hover(contents=MarkupContent(kind=MarkupKind.Markdown, value=markdown))

    @server.feature("textDocument/documentSymbol")
    def _on_document_symbol(_ls: LanguageServer, params: Any) -> list[Any]:
        cached = state.cache.get(params.text_document.uri)
        if cached is None:
            return []
        return _document_symbols(cached.root)

    @server.feature("rdl/elaboratedTree")
    def _on_elaborated_tree(_ls: LanguageServer, params: Any) -> dict[str, Any]:
        """Custom JSON-RPC: viewer fetches the latest elaborated tree for a URI.

        Schema: ``schemas/elaborated-tree.json`` v0.1.0. Returns the cached
        last-good tree when the current parse has failed (design D7).
        """
        uri = None
        if isinstance(params, dict):
            uri = params.get("uri") or params.get("textDocument", {}).get("uri")
        else:
            uri = getattr(params, "uri", None)
            if uri is None and hasattr(params, "text_document"):
                uri = params.text_document.uri
        if not uri:
            return _serialize_root(None, stale=False)
        cached = state.cache.get(uri)
        if cached is None:
            return _serialize_root(None, stale=False)
        try:
            original_path = _uri_to_path(uri)
        except ValueError:
            original_path = None
        translate = (
            {cached.temp_path: original_path}
            if cached.temp_path is not None and original_path is not None
            else None
        )
        return _serialize_root(
            cached.root,
            stale=uri in state.stale_uris,
            path_translate=translate,
        )

    return server
