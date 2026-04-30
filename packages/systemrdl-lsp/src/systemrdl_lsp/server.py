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
    CodeLens,
    Command,
    CompletionItem,
    CompletionItemKind,
    CompletionList,
    Diagnostic,
    DiagnosticSeverity,
    DidChangeConfigurationParams,
    DidChangeTextDocumentParams,
    DidOpenTextDocumentParams,
    DidSaveTextDocumentParams,
    FoldingRange,
    FoldingRangeKind,
    InitializedParams,
    InlayHint,
    InlayHintKind,
    Location,
    Position,
    PublishDiagnosticsParams,
    Range,
    SymbolInformation,
    SymbolKind,
)
from pygls.lsp.server import LanguageServer
from systemrdl import RDLCompileError, RDLCompiler
from systemrdl.messages import MessagePrinter, Severity

if TYPE_CHECKING:
    from systemrdl.node import RootNode

logger = logging.getLogger(__name__)

SERVER_NAME = "systemrdl-lsp"
SERVER_VERSION = "0.11.0"
DEBOUNCE_SECONDS = 0.3
ELABORATED_TREE_SCHEMA_VERSION = "0.1.0"
# Eng-review safety net #3: cap a single elaborate pass at 10s wall-clock.
# Past that we keep last-good (D7) and surface a synthetic diagnostic. A pathological
# Perl-style include cycle in a third-party RDL pack should NOT freeze the editor.
ELABORATION_TIMEOUT_SECONDS = 10.0


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
    ) -> CompilerMessage:
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


# Matches ``$VAR`` and ``${VAR}`` inside a string. We only substitute inside
# the path argument of ```include "..."`` directives (see _expand_include_vars)
# so this regex doesn't accidentally chew on field values or property names.
_INCLUDE_VAR_RE = __import__("re").compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")
_INCLUDE_DIRECTIVE_RE = __import__("re").compile(r'(`include\s+")([^"]*)(")')


def _expand_include_vars(text: str, vars_map: dict[str, str]) -> str:
    """Expand ``$VAR`` / ``${VAR}`` inside ```include "..."`` paths.

    A subset of the SystemRDL Perl preprocessor (clause 16) that covers ~80%
    of real-world use — env-var-driven include trees in shared IP libraries.
    Only substitutes inside the path argument of an `include directive so
    body code (which legitimately contains ``$``-prefixed identifiers in some
    SystemVerilog constructs) is left alone.

    Lookup order: ``vars_map`` first (explicit setting), then ``os.environ``.
    Unresolved variables are left literal so the diagnostic surfaces a
    "include not found: $UNDEFINED/foo.rdl" error rather than failing silently.
    """
    if not text:
        return text
    import os

    def expand_one(match: Any) -> str:
        name = match.group(1)
        if name in vars_map:
            return vars_map[name]
        if name in os.environ:
            return os.environ[name]
        return match.group(0)

    def expand_path(match: Any) -> str:
        return match.group(1) + _INCLUDE_VAR_RE.sub(expand_one, match.group(2)) + match.group(3)

    return _INCLUDE_DIRECTIVE_RE.sub(expand_path, text)


def _peakrdl_toml_paths(start: pathlib.Path) -> list[str]:
    """Walk upward from ``start`` looking for ``peakrdl.toml`` and read its
    ``[parser] incl_search_paths`` array.

    PeakRDL's own CLI honours this same key, so a project that already builds
    with PeakRDL just works in the editor without re-declaring its include
    tree under ``systemrdl-pro.includePaths``. Workspace-relative paths are
    resolved against the .toml's own directory, matching PeakRDL semantics.
    """
    try:
        import tomllib  # Python 3.11+
    except ImportError:
        return []
    parent = start.parent if start.is_file() else start
    seen: set[pathlib.Path] = set()
    for cur in [parent, *parent.parents]:
        if cur in seen:
            break
        seen.add(cur)
        toml_path = cur / "peakrdl.toml"
        if not toml_path.is_file():
            continue
        try:
            data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
        except Exception:
            logger.debug("failed to parse %s", toml_path, exc_info=True)
            return []
        parser = data.get("parser") or {}
        raw = parser.get("incl_search_paths") or []
        out: list[str] = []
        for p in raw:
            if not isinstance(p, str):
                continue
            candidate = pathlib.Path(p)
            if not candidate.is_absolute():
                candidate = (cur / candidate).resolve()
            out.append(str(candidate))
        return out
    return []


def _compile_text(
    uri: str,
    text: str,
    incl_search_paths: list[str] | None = None,
    include_vars: dict[str, str] | None = None,
) -> tuple[list[CompilerMessage], list[RootNode], pathlib.Path]:
    """Compile in-memory buffer text. Returns (messages, roots, temp_path).

    ``roots`` is a list of RootNode instances — one per top-level ``addrmap``
    *definition* in the file (Decision 3C). ``compiler.elaborate()`` with no
    ``top_def_name`` only elaborates the *last* defined addrmap, so we enumerate
    ``compiler.root.comp_defs`` and elaborate each one separately. An empty list
    means parse failed or the file has no top-level addrmap (a library file).

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
    from systemrdl.component import Addrmap

    original_path = _uri_to_path(uri)
    search_paths = list(incl_search_paths or [])
    # peakrdl.toml's [parser] incl_search_paths are appended *after* the user's
    # explicit setting, so per-workspace overrides win on collision.
    search_paths.extend(_peakrdl_toml_paths(original_path))
    if original_path.parent.exists():
        search_paths.append(str(original_path.parent))

    printer = CapturingPrinter()
    compiler = RDLCompiler(message_printer=printer)

    expanded_text = _expand_include_vars(text, include_vars or {})
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".rdl",
        prefix=".systemrdl-lsp-",
        encoding="utf-8",
        delete=False,
    ) as tf:
        tf.write(expanded_text)
        tmp_path = pathlib.Path(tf.name)

    roots: list[RootNode] = []
    try:
        compiler.compile_file(str(tmp_path), incl_search_paths=search_paths)
        # Enumerate every top-level addrmap definition in declaration order, then
        # elaborate each. Per the systemrdl-compiler docstring, the compiler must
        # be discarded if elaborate() raises — we still keep prior successful
        # roots so a single bad addrmap doesn't blank the viewer.
        addrmap_names = [
            name for name, comp in compiler.root.comp_defs.items()
            if isinstance(comp, Addrmap)
        ]
        for name in addrmap_names:
            try:
                roots.append(compiler.elaborate(top_def_name=name))
            except RDLCompileError:
                # Diagnostics for the failure are already in the printer.
                continue
            except Exception:
                logger.exception("elaborate failed for top_def_name=%r", name)
                continue
    except RDLCompileError:
        roots = []
    except Exception as exc:  # defensive: never crash the server
        logger.exception("unexpected error while compiling %s", original_path)
        printer.captured.append((Severity.ERROR, f"internal: {exc}", None))
        roots = []

    # Snapshot diagnostics while the temp file is still on disk — line/col are lazy.
    translate = {tmp_path: original_path}
    messages = [
        CompilerMessage.from_compiler(sev, text, src_ref, translate)
        for sev, text, src_ref in printer.captured
    ]
    return messages, roots, tmp_path


def _elaborate(path: pathlib.Path) -> list[tuple[Severity, str, Any]]:
    """Legacy disk-based elaboration; kept for tests and ``didSave`` warm path."""
    if not path.exists():
        printer = CapturingPrinter()
        printer.captured.append((Severity.ERROR, f"file not found: {path}", None))
        return printer.captured

    text = path.read_text(encoding="utf-8")
    messages, _roots, tmp_path = _compile_text(path.as_uri(), text)
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
    filename: pathlib.Path | None
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
    # One $root meta-component per top-level addrmap definition in the file
    # (Decision 3C). Empty list means the file has no addrmaps (a library file).
    roots: list[RootNode]
    text: str
    elaborated_at: float
    # Path to the temp file that backs lazy source refs in every RootNode's
    # underlying inst/SourceRef chain. Owned by the cache; unlinked when the
    # entry is replaced or cleared.
    temp_path: pathlib.Path | None = None


class ElaborationCache:
    """Per-URI cache of the last successful list of ``RootNode``\\ s.

    Hover / documentSymbol read from this cache. When a fresh parse fails we keep
    the prior entry so the viewer (Week 4) and hover can still answer about
    previously valid registers — matches design D7 ("zero-flicker stale UX").

    The cache also owns the temp file backing each ``RootNode`` (see
    :func:`_compile_text` for why). When ``put`` replaces an entry, the previous
    temp file is unlinked. ``clear`` drops everything.
    """

    def __init__(self) -> None:
        self._entries: dict[str, CachedElaboration] = {}

    def get(self, uri: str) -> CachedElaboration | None:
        return self._entries.get(uri)

    def put(
        self,
        uri: str,
        roots: list[RootNode],
        text: str,
        temp_path: pathlib.Path | None = None,
    ) -> None:
        old = self._entries.get(uri)
        if old is not None and old.temp_path is not None:
            old.temp_path.unlink(missing_ok=True)
        self._entries[uri] = CachedElaboration(
            roots=roots, text=text, elaborated_at=time.time(), temp_path=temp_path
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
    # Substitution map for ``$VAR`` / ``${VAR}`` inside ```include "..."`` paths.
    # Read from systemrdl-pro.includeVars; falls back to os.environ during expansion.
    include_vars: dict[str, str] = dataclasses.field(default_factory=dict)
    # URIs whose latest parse attempt failed but for which we still have a last-good
    # cache entry. The viewer renders a stale-bar when a URI is in this set (D7).
    stale_uris: set[str] = dataclasses.field(default_factory=set)


# ---------------------------------------------------------------------------
# Hover + documentSymbol
# ---------------------------------------------------------------------------


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

    1. User-defined component types from ``comp_defs`` — surfaces ``name``,
       ``desc``, and the kind. This catches every type reference in the file
       that completion already knew about.
    2. SystemRDL top-level keywords (``addrmap``, ``regfile``, ``reg``, …).
    3. Property keywords (``sw``, ``reset``, ``onwrite``, …).
    4. Access-mode values (``rw``, ``ro``, ``woclr``, ``wzc``, …).
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


# ---------------------------------------------------------------------------
# Elaborated tree serialization (rdl/elaboratedTree, schema v0.1.0)
# ---------------------------------------------------------------------------


def _safe_get_property(node: Any, prop: str) -> Any:
    """``node.get_property(prop)`` with LookupError + AttributeError swallowed.

    Some property reads (e.g. ``name``) raise ``LookupError`` when the property
    isn't set on this kind of node; others raise ``AttributeError`` when the node
    doesn't support property lookup at all (rare, but defensive).
    """
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


def _serialize_root(
    roots_input: list[RootNode] | RootNode | None,
    stale: bool,
    path_translate: dict[pathlib.Path, pathlib.Path] | None = None,
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
        "elaboratedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "stale": stale,
        "roots": serialized_roots,
    }


# ---------------------------------------------------------------------------
# Folding ranges, inlay hints, CodeLens, workspace symbols
# ---------------------------------------------------------------------------


def _folding_ranges_from_text(text: str) -> list[FoldingRange]:
    """Compute folding ranges from `{...}` block spans.

    A purely textual scan: every `{` opens a range, the matching `}` closes
    it. Strings/comments are stripped first so braces inside them don't
    confuse the matcher. Single-line blocks (`{ field { ... } x[0:0]=0; }`)
    are skipped — too small to fold meaningfully.
    """
    import re

    # Strip line comments and block comments + string literals so braces inside
    # don't open/close ranges. Replace with same-length whitespace to keep
    # offsets and line numbers stable.
    def _blank_match(m: re.Match[str]) -> str:
        return re.sub(r"[^\n]", " ", m.group(0))

    cleaned = re.sub(r'"(?:\\.|[^"\\])*"', _blank_match, text)
    cleaned = re.sub(r"//[^\n]*", _blank_match, cleaned)
    cleaned = re.sub(r"/\*[\s\S]*?\*/", _blank_match, cleaned)

    ranges: list[FoldingRange] = []
    stack: list[int] = []  # line numbers of unmatched `{`
    line = 0
    for ch in cleaned:
        if ch == "\n":
            line += 1
        elif ch == "{":
            stack.append(line)
        elif ch == "}" and stack:
            start = stack.pop()
            if line > start:  # skip single-line blocks
                ranges.append(
                    FoldingRange(
                        start_line=start, end_line=line, kind=FoldingRangeKind.Region
                    )
                )
    return ranges


def _inlay_hints_for_addressables(
    roots: list[RootNode], path: pathlib.Path, buffer_text: str
) -> list[InlayHint]:
    """Inlay hints showing the resolved absolute address at the **end of the line**
    containing each register / regfile / addrmap declaration.

    Earlier version placed the hint at ``line_selection[1]`` which fell inside
    the instance name in some compiler outputs (e.g. produced
    ``ctrl_reg_t CTR (0x0000_0100)L @ 0x0``). End-of-line placement avoids
    every overlap and makes typing names painless — the ghost text is always
    far to the right.
    """
    from systemrdl.node import AddressableNode

    hints: list[InlayHint] = []
    lines = buffer_text.splitlines() if buffer_text else []
    seen_lines: set[int] = set()

    def visit(node: Any) -> None:
        if not isinstance(node, AddressableNode):
            for c in getattr(node, "children", lambda **_: [])(unroll=True):
                visit(c)
            return

        inst = getattr(node, "inst", None)
        src_ref = getattr(inst, "inst_src_ref", None) or getattr(inst, "def_src_ref", None)
        line_1b = getattr(src_ref, "line", None)
        ref_filename = getattr(src_ref, "filename", None)

        if (
            line_1b is not None
            and ref_filename
            and pathlib.Path(ref_filename) == path
        ):
            line_0b = max(0, line_1b - 1)
            # One hint per line — multiple instances on the same line is rare
            # and we can't separate them visually anyway.
            if line_0b not in seen_lines and line_0b < len(lines):
                seen_lines.add(line_0b)
                col_0b = len(lines[line_0b])
                try:
                    addr = node.absolute_address
                except Exception:
                    addr = None
                if addr is not None:
                    hints.append(
                        InlayHint(
                            position=Position(line=line_0b, character=col_0b),
                            label=f"  → {_hex(addr)}",
                            padding_left=True,
                            kind=InlayHintKind.Type,
                        )
                    )

        for c in node.children(unroll=True):
            visit(c)

    for r in roots:
        for top in r.children(unroll=True):
            visit(top)
    return hints


def _code_lenses_for_addrmaps(
    roots: list[RootNode], path: pathlib.Path
) -> list[CodeLens]:
    """One `📊 N regs · 0xS..0xE` lens + one `📋 Open in Memory Map` lens
    above every top-level addrmap definition in the file.
    """
    from systemrdl.node import AddrmapNode, RegNode

    out: list[CodeLens] = []
    for r in roots:
        for top in r.children(unroll=True):
            if not isinstance(top, AddrmapNode):
                continue
            inst = getattr(top, "inst", None)
            src_ref = getattr(inst, "inst_src_ref", None) or getattr(inst, "def_src_ref", None)
            line_1b = getattr(src_ref, "line", None)
            ref_filename = getattr(src_ref, "filename", None)
            if line_1b is None or not ref_filename or pathlib.Path(ref_filename) != path:
                continue
            line_0b = max(0, line_1b - 1)
            rng = Range(
                start=Position(line=line_0b, character=0),
                end=Position(line=line_0b, character=0),
            )

            reg_count = 0
            min_addr: int | None = None
            max_addr: int | None = None

            def walk(node: Any) -> None:
                nonlocal reg_count, min_addr, max_addr
                if isinstance(node, RegNode):
                    reg_count += 1
                    a = node.absolute_address
                    min_addr = a if min_addr is None else min(min_addr, a)
                    end = a + max(1, getattr(node, "size", 1))
                    max_addr = end if max_addr is None else max(max_addr, end)
                if hasattr(node, "children"):
                    for c in node.children(unroll=True):
                        walk(c)

            walk(top)
            if reg_count:
                summary = f"📊 {reg_count} reg{'s' if reg_count != 1 else ''}"
                if min_addr is not None and max_addr is not None:
                    summary += f" · {_hex(min_addr)}..{_hex(max_addr)}"
                out.append(CodeLens(range=rng, command=Command(title=summary, command="")))
    return out


def _workspace_symbols_for_uri(
    uri: str, roots: list[RootNode], query: str
) -> list[SymbolInformation]:
    """Walk the cached elaboration of one URI looking for symbols matching ``query``.

    ``query`` is matched as case-insensitive substring against instance name.
    """
    from systemrdl.node import AddrmapNode, FieldNode, RegfileNode, RegNode

    q = query.lower()
    out: list[SymbolInformation] = []

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

    def visit(node: Any, parent_path: list[str]) -> None:
        name = getattr(node, "inst_name", None)
        if not name:
            for c in getattr(node, "children", lambda **_: [])(unroll=True):
                visit(c, parent_path)
            return
        path = [*parent_path, name]
        if q in name.lower():
            inst = getattr(node, "inst", None)
            src_ref = getattr(inst, "inst_src_ref", None) or getattr(inst, "def_src_ref", None)
            if src_ref is not None:
                line_1b = getattr(src_ref, "line", None) or 1
                sel = getattr(src_ref, "line_selection", None) or (1, 1)
                try:
                    cs, ce = sel
                except (TypeError, ValueError):
                    cs = ce = 1
                rng = _build_range(line_1b, cs, ce)
                out.append(
                    SymbolInformation(
                        name=name,
                        kind=kind_of(node),
                        location=Location(uri=uri, range=rng),
                        container_name=".".join(parent_path) if parent_path else None,
                    )
                )
        if hasattr(node, "children"):
            for c in node.children(unroll=True):
                visit(c, path)
        if hasattr(node, "fields"):
            try:
                for f in node.fields():
                    visit(f, path)
            except Exception:
                pass

    for r in roots:
        for top in r.children(unroll=True):
            visit(top, [])
    return out


def _address_conflict_diagnostics(
    roots: list[RootNode], path: pathlib.Path
) -> list[CompilerMessage]:
    """Detect overlapping register address ranges within the same parent and
    surface them as warning diagnostics. systemrdl-compiler usually catches
    this, but only for direct sibling overlaps; we extend the check across
    the full elaborated tree for defence-in-depth.
    """
    from systemrdl.node import RegNode

    out: list[CompilerMessage] = []
    flat: list[tuple[int, int, RegNode]] = []

    def visit(node: Any) -> None:
        if isinstance(node, RegNode):
            try:
                a = node.absolute_address
                size = max(1, int(getattr(node, "size", 1)))
                flat.append((a, a + size, node))
            except Exception:
                pass
        if hasattr(node, "children"):
            for c in node.children(unroll=True):
                visit(c)

    for r in roots:
        for top in r.children(unroll=True):
            visit(top)

    flat.sort(key=lambda t: t[0])
    for i in range(1, len(flat)):
        prev_start, prev_end, prev_node = flat[i - 1]
        cur_start, cur_end, cur_node = flat[i]
        if cur_start < prev_end:
            inst = getattr(cur_node, "inst", None)
            src_ref = getattr(inst, "inst_src_ref", None) or getattr(inst, "def_src_ref", None)
            ref_filename = getattr(src_ref, "filename", None)
            line_1b = getattr(src_ref, "line", None)
            sel = getattr(src_ref, "line_selection", None) or (1, 1)
            try:
                cs, ce = sel
            except (TypeError, ValueError):
                cs = ce = 1
            if line_1b and ref_filename and pathlib.Path(ref_filename) == path:
                out.append(
                    CompilerMessage(
                        severity=Severity.WARNING,
                        text=(
                            f"address overlap: {cur_node.inst_name} at {_hex(cur_start)} "
                            f"overlaps {prev_node.inst_name} ({_hex(prev_start)}..{_hex(prev_end)})"
                        ),
                        file_path=path,
                        line_1b=line_1b,
                        col_start_1b=cs,
                        col_end_1b=ce,
                    )
                )
    return out


# ---------------------------------------------------------------------------
# textDocument/completion (W2-7)
# ---------------------------------------------------------------------------


# Static catalogue: label → one-line markdown shown in the completion popup's
# detail panel. Coverage is intentionally narrower than the full SystemRDL 2.0
# spec — we cover the properties and access modes that matter for ~95% of real
# register definitions. Adding more is cheap; getting them right (correct legal
# domain, correct cross-property constraints) needs a scope-aware analyser.
SYSTEMRDL_TOP_KEYWORDS: dict[str, str] = {
    "addrmap": "Top-level address map. Wraps registers/regfiles into an addressable hierarchy.",
    "regfile": "Logical group of registers sharing a base address.",
    "reg": "Hardware register. Contains 1+ fields packed into `regwidth` bits (default 32).",
    "field": "Bit field inside a register. Has `sw` and `hw` access modes plus a reset value.",
    "enum": "Enumerated set of values, usable as a field value.",
    "mem": "External memory region (no internal storage, uses `external` accessor).",
    "signal": "External signal — wired to/from logic outside the register block.",
    "external": "Marks an instance as external — backing logic lives outside the generated RTL.",
    "internal": "Marks an instance as internal (default; usually omitted).",
    "default": "Default-property assignment — applies to every later sibling unless overridden.",
    "property": "User-defined property declaration.",
    "constraint": "User-defined constraint declaration (rarely used).",
    "true": "Boolean literal `true`.",
    "false": "Boolean literal `false`.",
}

SYSTEMRDL_PROPERTIES: dict[str, str] = {
    # Component metadata
    "name": 'Human-readable name shown in docs/viewers, e.g. `name = "Control register"`.',
    "desc": 'Long-form description, may contain multi-line markdown.',
    # Field access semantics
    "sw": "Software access mode. Values: `rw`, `ro`, `wo`, `r`, `w`, `na`.",
    "hw": "Hardware access mode. Values: `rw`, `ro`, `wo`, `r`, `w`, `na`.",
    "reset": "Reset value (hex, dec, or binary). Applied on system reset.",
    "resetsignal": "Override the reset signal driving this field.",
    "rclr": "On software read: clear the field to 0.",
    "rset": "On software read: set the field to all-ones.",
    "ruser": "Custom on-read action (user-defined).",
    "onread": "Read-side effect. Common values: `rclr`, `rset`, `ruser`.",
    "onwrite": (
        "Write-side effect. Common: `woclr`, `woset`, `wzc`, `wzs`, `wclr`, `wset`, `wuser`."
    ),
    "swacc": "Status flag: software just accessed (read or write).",
    "swmod": "Status flag: software just modified the field's value.",
    "swwe": "Software write-enable signal.",
    "swwel": "Software write-enable, active-low.",
    "we": "Hardware write-enable.",
    "wel": "Hardware write-enable, active-low.",
    "anded": "Bitwise-AND output of all bits in the field.",
    "ored": "Bitwise-OR output of all bits.",
    "xored": "Bitwise-XOR output of all bits.",
    "fieldwidth": "Force a field width independent of the bit-range.",
    "encode": "Reference an `enum` definition that names the legal values.",
    "singlepulse": "Field acts as a single-cycle strobe — auto-clears next cycle.",
    # Register
    "regwidth": "Register width in bits (default `32`, also legal: `8`, `16`, `64`).",
    "accesswidth": "Smallest access size in bits (must divide `regwidth`).",
    "shared": "Register is shared across multiple addrmap instances.",
    # Addrmap / regfile
    "alignment": "Force address alignment for child instances.",
    "sharedextbus": "All external children share one bus.",
    "errextbus": "External errors propagate to the bus.",
    "bigendian": "Use big-endian addressing.",
    "littleendian": "Use little-endian addressing (default).",
    "addressing": "Addressing mode: `compact`, `regalign`, `fullalign`.",
    "lsb0": "Bit 0 is the LSB (default).",
    "msb0": "Bit 0 is the MSB (uncommon).",
    # Counter
    "counter": "Field is an up/down counter.",
    "incr": "Increment input signal.",
    "decr": "Decrement input signal.",
    "incrwidth": "Width of the increment value.",
    "decrwidth": "Width of the decrement value.",
    "incrvalue": "Constant increment.",
    "decrvalue": "Constant decrement.",
    "saturate": "Saturate at min/max instead of wrapping.",
    "incrsaturate": "Saturate on overflow.",
    "decrsaturate": "Saturate on underflow.",
    "threshold": "Threshold flag triggers when the counter crosses the value.",
    "incrthreshold": "Threshold for increment direction.",
    "decrthreshold": "Threshold for decrement direction.",
    "overflow": "Status: increment overflowed.",
    "underflow": "Status: decrement underflowed.",
    # Interrupt
    "intr": "Field is an interrupt source.",
    "enable": "Interrupt enable mask.",
    "mask": "Interrupt mask (when `intr` is set).",
    "haltenable": "Halts further interrupts when set.",
    "haltmask": "Mask for halt-enable.",
    "stickybit": "Field bit sticks until cleared by software.",
    "sticky": "Whole field is sticky.",
}

# sw / hw access — the right-hand side of `sw =` / `hw =`.
SYSTEMRDL_RW_VALUES: dict[str, str] = {
    "rw": "Read-write.",
    "ro": "Read-only — writes are ignored.",
    "wo": "Write-only — reads return 0.",
    "r":  "Readable (alias of `ro`).",
    "w":  "Writable (alias of `wo`).",
    "na": "No access — software can neither read nor write.",
}

# onwrite values — the right-hand side of `onwrite =`.
SYSTEMRDL_ONWRITE_VALUES: dict[str, str] = {
    "woclr": "Write-1-to-clear: writing 1 to a bit clears it; writing 0 leaves it.",
    "woset": "Write-1-to-set: writing 1 to a bit sets it; writing 0 leaves it.",
    "wzc":   "Write-0-to-clear.",
    "wzs":   "Write-0-to-set.",
    "wclr":  "Any write clears the field.",
    "wset":  "Any write sets all field bits.",
    "wzt":   "Write-0-to-toggle.",
    "wuser": "User-defined write action.",
}

# onread values — the right-hand side of `onread =`.
SYSTEMRDL_ONREAD_VALUES: dict[str, str] = {
    "rclr":  "Read-to-clear: read clears the field after returning the old value.",
    "rset":  "Read-to-set: read sets all bits after returning the old value.",
    "ruser": "User-defined read action.",
}


def _completion_context(text: str, line_0b: int, char_0b: int) -> str:
    """Detect what the cursor is right of, so we can narrow the suggestion list.

    Looks at the line up to ``char_0b`` and matches the trailing token. Returns
    one of:

    - ``"sw_value"`` / ``"hw_value"`` — after ``sw =`` / ``hw =``
    - ``"onwrite_value"`` — after ``onwrite =``
    - ``"onread_value"`` — after ``onread =``
    - ``"general"`` — anywhere else (full catalogue)

    The match is single-line; SystemRDL property assignments rarely span lines
    in practice, and supporting that would require a real parser.
    """
    import re
    lines = text.splitlines()
    if line_0b < 0 or line_0b >= len(lines):
        return "general"
    prefix = lines[line_0b][:char_0b]
    m = re.search(r"\b(sw|hw|onwrite|onread)\s*=\s*\w*$", prefix)
    if m:
        return f"{m.group(1)}_value"
    return "general"


def _make_items(catalogue: dict[str, str], kind: CompletionItemKind) -> list[CompletionItem]:
    return [
        CompletionItem(label=label, kind=kind, detail=doc, documentation=doc)
        for label, doc in catalogue.items()
    ]


def _completion_items_static() -> list[CompletionItem]:
    """Full keyword + property + value catalogue with one-line docs.

    Used for the ``"general"`` context. Each item carries both ``detail`` (shown
    on the right side of the popup row) and ``documentation`` (shown when the
    user expands the side panel) so the user sees the explanation without
    extra interaction.
    """
    items: list[CompletionItem] = []
    items.extend(_make_items(SYSTEMRDL_TOP_KEYWORDS, CompletionItemKind.Keyword))
    items.extend(_make_items(SYSTEMRDL_PROPERTIES, CompletionItemKind.Property))
    # Combined access values — the union of all RHS catalogues so plain
    # autocomplete (no `=` context) still surfaces them. Context-aware
    # narrowing happens in the handler.
    items.extend(_make_items(SYSTEMRDL_RW_VALUES, CompletionItemKind.EnumMember))
    items.extend(_make_items(SYSTEMRDL_ONWRITE_VALUES, CompletionItemKind.EnumMember))
    items.extend(_make_items(SYSTEMRDL_ONREAD_VALUES, CompletionItemKind.EnumMember))
    return items


def _completion_items_for_context(context: str) -> list[CompletionItem]:
    """Return the value subset for a property-RHS context, or [] for general.

    ``"sw_value"`` / ``"hw_value"`` → access modes. ``"onwrite_value"`` →
    woclr/woset/wzc/etc. ``"onread_value"`` → rclr/rset/ruser. Anything else
    falls through to the static full catalogue.
    """
    if context in ("sw_value", "hw_value"):
        return _make_items(SYSTEMRDL_RW_VALUES, CompletionItemKind.EnumMember)
    if context == "onwrite_value":
        return _make_items(SYSTEMRDL_ONWRITE_VALUES, CompletionItemKind.EnumMember)
    if context == "onread_value":
        return _make_items(SYSTEMRDL_ONREAD_VALUES, CompletionItemKind.EnumMember)
    return []


def _completion_items_for_types(roots: list[RootNode]) -> list[CompletionItem]:
    """Pull every top-level component definition out of the cached compile.

    Uses :func:`_comp_defs_from_cached` to read ``inst.comp_defs`` — the same
    registry textDocument/definition resolves against — so the two providers
    can never disagree on what types exist. ``detail`` carries the component
    kind (``addrmap``, ``regfile``, ``reg``, ``field``, …); ``documentation``
    surfaces the component's ``name`` and ``desc`` properties when set, so the
    user sees the human-readable label and long description in the popup.
    """
    items: list[CompletionItem] = []
    defs = _comp_defs_from_cached(roots)
    for name, comp in defs.items():
        kind_label = type(comp).__name__.lower()  # "addrmap" / "regfile" / "reg" / "field"
        # `properties` is a dict on the Component; values may be None or the
        # actual property values. We surface ``name`` (display label) and
        # ``desc`` (long description) into the popup's documentation panel.
        props = getattr(comp, "properties", {}) or {}
        display_name = props.get("name")
        desc = props.get("desc")
        doc_lines: list[str] = []
        if display_name:
            doc_lines.append(f"**{display_name}**")
        if desc:
            doc_lines.append(str(desc))
        if not doc_lines:
            doc_lines.append(f"User-defined {kind_label} type.")
        items.append(
            CompletionItem(
                label=name,
                kind=CompletionItemKind.Class,
                detail=kind_label,
                documentation="\n\n".join(doc_lines),
            )
        )
    return items


# ---------------------------------------------------------------------------
# textDocument/definition (W2-6)
# ---------------------------------------------------------------------------


_IDENT_CHAR_RE = None  # lazy compile
_IDENT_RUN_RE = None


def _word_at_position(text: str, line_0b: int, col_0b: int) -> str | None:
    """Return the SystemRDL identifier the cursor sits inside, else None.

    SystemRDL identifiers match ``[A-Za-z_][A-Za-z0-9_]*``. We split the source
    into lines, locate the target line, then walk left and right from
    ``col_0b`` until we leave the identifier character class. If the cursor
    is on whitespace or punctuation, returns ``None``.
    """
    import re as _re
    global _IDENT_CHAR_RE
    if _IDENT_CHAR_RE is None:
        _IDENT_CHAR_RE = _re.compile(r"[A-Za-z0-9_]")

    lines = text.splitlines()
    if line_0b < 0 or line_0b >= len(lines):
        return None
    line = lines[line_0b]
    if col_0b < 0 or col_0b > len(line):
        return None

    # Cursor exactly at end-of-identifier (col == len(ident)) is still a hit —
    # VSCode reports definition requests at the position *after* the last char
    # when triggered via Ctrl-click on the trailing edge. Probe both sides.
    def _is_ident(c: str) -> bool:
        return bool(_IDENT_CHAR_RE.match(c))

    left = col_0b
    while left > 0 and _is_ident(line[left - 1]):
        left -= 1
    right = col_0b
    while right < len(line) and _is_ident(line[right]):
        right += 1
    if right <= left:
        return None
    word = line[left:right]
    # Skip pure-numeric runs ("0x100", "32") — those aren't identifiers.
    if word and word[0].isdigit():
        return None
    return word


def _comp_defs_from_cached(roots: list[RootNode]) -> dict[str, Any]:
    """Pick the first cached root and read its ``inst.comp_defs`` registry.

    All cached roots from the same buffer share the same compiler-internal
    component definition table (we elaborate them off a single compile pass),
    so any one of them is sufficient. Returns ``{}`` if the list is empty.
    """
    for r in roots:
        defs = getattr(getattr(r, "inst", None), "comp_defs", None)
        if defs:
            return dict(defs)
    return {}


def _definition_location(
    comp: Any,
    path_translate: dict[pathlib.Path, pathlib.Path] | None,
) -> Location | None:
    """Map a systemrdl component's ``def_src_ref`` to an LSP ``Location``."""
    src_ref = getattr(comp, "def_src_ref", None)
    if src_ref is None:
        return None
    raw_filename = getattr(src_ref, "filename", None)
    if not raw_filename:
        return None
    file_path = pathlib.Path(raw_filename)
    if path_translate:
        file_path = path_translate.get(file_path, file_path)
    line_1b = getattr(src_ref, "line", None) or 1
    sel = getattr(src_ref, "line_selection", None) or (1, 1)
    try:
        cs, ce = sel
    except (TypeError, ValueError):
        cs = ce = 1
    return Location(uri=file_path.as_uri(), range=_build_range(line_1b, cs, ce))


def _document_symbols(roots: list[RootNode] | RootNode) -> list[Any]:
    """Build a tree of LSP DocumentSymbols mirroring addrmap → regfile → reg → field.

    Accepts either a single RootNode (test convenience) or the list stored in
    the elaboration cache (one per top-level addrmap definition). Returns a
    flat list of top-level symbols across all roots.
    """
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

    root_list = roots if isinstance(roots, list) else [roots]
    out: list[DocumentSymbol] = []
    for r in root_list:
        for top in r.children(unroll=True):
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

    def _apply_compile_result(
        uri: str,
        buffer_text: str,
        messages: list[CompilerMessage],
        roots: list[RootNode],
        tmp_path: pathlib.Path,
    ) -> None:
        if roots:
            # Cache takes ownership of the temp file (it backs lazy src_ref reads
            # for hover/documentSymbol). Old entry's temp file is unlinked there.
            state.cache.put(uri, roots, buffer_text, tmp_path)
            state.stale_uris.discard(uri)
            # Address-conflict diagnostics walk the elaborated tree for overlapping
            # reg ranges (defence-in-depth — systemrdl-compiler catches direct
            # sibling overlaps but not all cross-level cases).
            try:
                conflicts = _address_conflict_diagnostics(roots, tmp_path)
                messages = list(messages) + conflicts
            except Exception:
                logger.debug("address-conflict scan failed", exc_info=True)
        else:
            # Parse failed (or library-only file) — keep the previous cache entry
            # intact (last-good D7). The just-written temp file isn't backing
            # anything we'll read again, drop it.
            tmp_path.unlink(missing_ok=True)
            if state.cache.get(uri) is not None:
                state.stale_uris.add(uri)
        _publish_diagnostics(server, uri, messages)

    async def _full_pass_async(uri: str, buffer_text: str | None) -> None:
        if buffer_text is None:
            try:
                buffer_text = _uri_to_path(uri).read_text(encoding="utf-8")
            except (OSError, ValueError):
                return

        loop = asyncio.get_running_loop()
        # Run the synchronous compiler off the event loop so a pathological elaborate
        # can't block hover/cancel/etc. wait_for() can't actually kill the worker thread
        # on timeout, so we attach a cleanup callback that unlinks the orphan temp file
        # whenever the late result eventually arrives.
        fut: asyncio.Future = loop.run_in_executor(
            None, _compile_text, uri, buffer_text, state.include_paths, state.include_vars
        )
        try:
            messages, roots, tmp_path = await asyncio.wait_for(
                asyncio.shield(fut), timeout=ELABORATION_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            logger.warning(
                "elaborate timeout on %s after %.0fs; keeping last-good",
                uri, ELABORATION_TIMEOUT_SECONDS,
            )

            def _drop_late_result(f: asyncio.Future) -> None:
                try:
                    _msgs, _root, late_tmp = f.result()
                    late_tmp.unlink(missing_ok=True)
                except Exception:
                    pass

            fut.add_done_callback(_drop_late_result)
            if state.cache.get(uri) is not None:
                state.stale_uris.add(uri)
            try:
                target_path = _uri_to_path(uri)
            except ValueError:
                return
            timeout_msg = CompilerMessage(
                severity=Severity.ERROR,
                text=(
                    f"systemrdl-lsp: elaborate exceeded {ELABORATION_TIMEOUT_SECONDS:.0f}s — "
                    "viewer is showing last-good tree."
                ),
                file_path=target_path,
                line_1b=1,
                col_start_1b=1,
                col_end_1b=1,
            )
            _publish_diagnostics(server, uri, [timeout_msg])
            return
        except Exception:
            logger.exception("unexpected error during async full-pass for %s", uri)
            return
        _apply_compile_result(uri, buffer_text, messages, roots, tmp_path)

    async def _debounced_full_pass(uri: str) -> None:
        try:
            await asyncio.sleep(DEBOUNCE_SECONDS)
        except asyncio.CancelledError:
            return
        try:
            await _full_pass_async(uri, _read_buffer(uri))
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
                    raw_vars = configs[0].get("includeVars") or {}
                    if isinstance(raw_vars, dict):
                        state.include_vars = {str(k): str(v) for k, v in raw_vars.items()}
                    logger.info("includePaths from initial config: %s", state.include_paths)
                    logger.info("includeVars from initial config: %s", list(state.include_vars))
            except Exception:
                logger.debug("could not fetch initial workspace configuration", exc_info=True)

        asyncio.ensure_future(fetch())

    @server.feature(TEXT_DOCUMENT_DID_OPEN)
    def _on_open(_ls: LanguageServer, params: DidOpenTextDocumentParams) -> None:
        asyncio.ensure_future(
            _full_pass_async(params.text_document.uri, params.text_document.text)
        )

    @server.feature(TEXT_DOCUMENT_DID_SAVE)
    def _on_save(_ls: LanguageServer, params: DidSaveTextDocumentParams) -> None:
        asyncio.ensure_future(
            _full_pass_async(params.text_document.uri, _read_buffer(params.text_document.uri))
        )

    @server.feature(TEXT_DOCUMENT_DID_CHANGE)
    def _on_change(_ls: LanguageServer, params: DidChangeTextDocumentParams) -> None:
        uri = params.text_document.uri
        existing = state.pending.get(uri)
        if existing is not None:
            existing.cancel()
        state.pending[uri] = asyncio.ensure_future(_debounced_full_pass(uri))

    @server.feature("$/cancelRequest")
    def _on_cancel(_ls: LanguageServer, _params: Any) -> None:
        # VSCode's language client sends $/cancelRequest when it's no longer interested
        # in a still-pending request (e.g. user typed during a slow elaborate). pygls 2.x
        # logs "Cancel notification for unknown message id …" by default for any request
        # that already completed before the cancel arrived — harmless but noisy in the
        # Output panel. Register a no-op handler to keep that channel quiet.
        return None

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
                    raw_vars = configs[0].get("includeVars") or {}
                    if isinstance(raw_vars, dict):
                        state.include_vars = {str(k): str(v) for k, v in raw_vars.items()}
            except Exception:
                logger.debug("config refresh failed", exc_info=True)

        asyncio.ensure_future(refresh())

    @server.feature("textDocument/hover")
    def _on_hover(_ls: LanguageServer, params: Any) -> Any | None:
        """Hover resolution combines four sources, most specific first:

        1. **Instance lookup** by source line — a register/field node whose
           ``inst_src_ref.line`` matches the cursor line. Wins because instance
           hover surfaces resolved address/reset/access — the live computed values.
        2. **User-defined types** from ``comp_defs`` — when the word matches a
           type definition, show its kind/name/desc.
        3. **Static catalogue** of keywords, properties, and access values.

        Anything not in 1–3 returns None and the editor shows nothing — better
        than displaying a useless "no info" tooltip.
        """
        from lsprotocol.types import Hover, MarkupContent, MarkupKind

        cached = state.cache.get(params.text_document.uri)
        if cached is None or not cached.roots:
            return None

        line, char = params.position.line, params.position.character

        # 1. Instance lookup first — gives the richest answer when it hits.
        node = _node_at_position(cached.roots, line, char)
        markdown = _hover_text_for_node(node) if node is not None else None

        # 2-3. Fall back to word-based catalogue lookup for keywords / properties /
        # access values / type names. Catches every identifier not already
        # answered by the elaborated tree's source-line matches.
        if markdown is None:
            word = _word_at_position(cached.text, line, char)
            if word:
                markdown = _hover_for_word(word, cached.roots)

        if markdown is None:
            return None
        return Hover(contents=MarkupContent(kind=MarkupKind.Markdown, value=markdown))

    @server.feature("textDocument/documentSymbol")
    def _on_document_symbol(_ls: LanguageServer, params: Any) -> list[Any]:
        cached = state.cache.get(params.text_document.uri)
        if cached is None or not cached.roots:
            return []
        return _document_symbols(cached.roots)

    @server.feature("textDocument/foldingRange")
    def _on_folding(_ls: LanguageServer, params: Any) -> list[FoldingRange]:
        cached = state.cache.get(params.text_document.uri)
        text = cached.text if cached is not None else _read_buffer(params.text_document.uri) or ""
        return _folding_ranges_from_text(text)

    @server.feature("textDocument/inlayHint")
    def _on_inlay_hint(_ls: LanguageServer, params: Any) -> list[InlayHint]:
        cached = state.cache.get(params.text_document.uri)
        if cached is None or not cached.roots:
            return []
        try:
            target_path = _uri_to_path(params.text_document.uri)
        except ValueError:
            return []
        path = cached.temp_path or target_path
        return _inlay_hints_for_addressables(cached.roots, path, cached.text)

    @server.feature("textDocument/codeLens")
    def _on_code_lens(_ls: LanguageServer, params: Any) -> list[CodeLens]:
        cached = state.cache.get(params.text_document.uri)
        if cached is None or not cached.roots:
            return []
        path = cached.temp_path or _uri_to_path(params.text_document.uri)
        return _code_lenses_for_addrmaps(cached.roots, path)

    @server.feature("workspace/symbol")
    def _on_workspace_symbol(_ls: LanguageServer, params: Any) -> list[SymbolInformation]:
        query = getattr(params, "query", "") or ""
        out: list[SymbolInformation] = []
        # Iterate every cached URI; we don't index the workspace, just return
        # whatever the user has touched recently. v1 — cheap and good enough
        # since users typically open the .rdl files they want to search.
        for uri, entry in state.cache._entries.items():
            if entry.roots:
                out.extend(_workspace_symbols_for_uri(uri, entry.roots, query))
        return out

    @server.feature("textDocument/completion")
    def _on_completion(_ls: LanguageServer, params: Any) -> CompletionList:
        """Suggest SystemRDL keywords, common properties, access values, and
        every type defined in the cached compile.

        Context-aware: looks at the line prefix to detect property assignments
        like ``sw =`` and narrows the catalogue accordingly. So typing ``sw =``
        followed by Ctrl-Space surfaces only ``rw`` / ``ro`` / ``wo`` / ``r`` /
        ``w`` / ``na`` rather than the full keyword soup.

        Outside an RHS context the full catalogue + user-defined types are
        offered; VSCode filters by what the user has typed.
        """
        cached = state.cache.get(params.text_document.uri)
        text = cached.text if cached is not None else _read_buffer(params.text_document.uri) or ""
        ctx = _completion_context(text, params.position.line, params.position.character)
        if ctx != "general":
            return CompletionList(is_incomplete=False, items=_completion_items_for_context(ctx))
        items = _completion_items_static()
        if cached is not None and cached.roots:
            items.extend(_completion_items_for_types(cached.roots))
        return CompletionList(is_incomplete=False, items=items)

    @server.feature("textDocument/definition")
    def _on_definition(_ls: LanguageServer, params: Any) -> list[Location] | Location | None:
        """F12 / Ctrl-click on a type identifier → jump to its declaration.

        v1 supports top-level component definitions (addrmap/regfile/reg/field
        types registered in ``compiler.root.comp_defs``). Cross-file definitions
        from ``include``\\ d files resolve transparently — ``def_src_ref.filename``
        carries the right path; we just ensure the LSP-internal temp path is
        translated back to the user's workspace path.

        Inline anonymous components (``reg { ... } CTRL @ 0;``) and instance
        names (``CTRL`` itself) are out of scope here — for those, hover and
        documentSymbol already give the answer. Adding instance-name navigation
        would require its own resolution step; defer until users ask.
        """
        uri = params.text_document.uri
        cached = state.cache.get(uri)
        if cached is None or not cached.roots:
            return None
        word = _word_at_position(cached.text, params.position.line, params.position.character)
        if not word:
            return None
        defs = _comp_defs_from_cached(cached.roots)
        comp = defs.get(word)
        if comp is None:
            return None
        try:
            original_path = _uri_to_path(uri)
        except ValueError:
            original_path = None
        translate = (
            {cached.temp_path: original_path}
            if cached.temp_path is not None and original_path is not None
            else None
        )
        return _definition_location(comp, translate)

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
            return _serialize_root([], stale=False)
        cached = state.cache.get(uri)
        if cached is None:
            return _serialize_root([], stale=False)
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
            cached.roots,
            stale=uri in state.stale_uris,
            path_translate=translate,
        )

    return server
