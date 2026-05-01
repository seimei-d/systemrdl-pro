"""Compilation primitives: in-memory buffer compile, captured diagnostics, last-good cache.

Owns the lifecycle of the temp file backing each elaborated ``RootNode`` —
``SegmentedSourceRef`` reads its source lazily, so the file must stay on disk
for as long as the cache holds the root.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import pathlib
import re
import tempfile
import time
from typing import TYPE_CHECKING, Any

from systemrdl import RDLCompileError, RDLCompiler
from systemrdl.messages import MessagePrinter, Severity

from ._uri import _uri_to_path

if TYPE_CHECKING:
    from systemrdl.node import RootNode

logger = logging.getLogger(__name__)


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
# Include-path resolution
# ---------------------------------------------------------------------------


# Matches ``$VAR`` and ``${VAR}`` inside a string. We only substitute inside
# the path argument of ```include "..."`` directives (see _expand_include_vars)
# so this regex doesn't accidentally chew on field values or property names.
_INCLUDE_VAR_RE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")
_INCLUDE_DIRECTIVE_RE = re.compile(r'(`include\s+")([^"]*)(")')


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

    def expand_one(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in vars_map:
            return vars_map[name]
        if name in os.environ:
            return os.environ[name]
        return match.group(0)

    def expand_path(match: re.Match[str]) -> str:
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


# ---------------------------------------------------------------------------
# In-memory compile
# ---------------------------------------------------------------------------


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
        CompilerMessage.from_compiler(sev, msg_text, src_ref, translate)
        for sev, msg_text, src_ref in printer.captured
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
