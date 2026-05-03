"""Compilation primitives: in-memory buffer compile, captured diagnostics, last-good cache.

Owns the lifecycle of the temp file backing each elaborated ``RootNode`` —
``SegmentedSourceRef`` reads its source lazily, so the file must stay on disk
for as long as the cache holds the root.
"""

from __future__ import annotations

import dataclasses
import functools
import hashlib
import logging
import os
import pathlib
import pickle
import re
import shutil
import tempfile
import time
import zlib
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


def _format_hex(value: int, width_hex_chars: int = 8) -> str:
    """Render an integer as a zero-padded uppercase hex literal."""
    return f"0x{value:0{width_hex_chars}X}"


# ---------------------------------------------------------------------------
# T2-D: pre-elaborate "is this just whitespace?" canonicalization
# ---------------------------------------------------------------------------


def _canonicalize_for_skip(text: str) -> str:
    """Return a normalized form that strips comments and collapses non-string
    whitespace. Two buffers with the same canonical form will produce
    identical AST out of ``systemrdl-compiler``, so we can skip elaborate
    entirely (no banner, no version bump, no notification).

    Token-aware enough not to corrupt:

    - Quoted strings (``"hello world"`` keeps both spaces).
    - Line comments (``//`` and ``#`` — Perl preprocessor markers ``<%``
      / ``%>`` are NOT comments; left untouched so a Perl-section edit
      still triggers re-elaborate).
    - Block comments (``/* ... */``).
    - Backticks for ```include`` / ```define`` (preserved literally; the
      directive itself is whitespace-collapsed normally).

    Edits this catches as no-ops:

    - Reformatting (extra blank lines, indent changes).
    - Adding / removing / editing comments.
    - Trailing whitespace.

    Edits this does NOT catch as no-ops:

    - Anything that touches a string literal (even adding a single
      space inside ``"foo bar"``).
    - Identifier renames (``REG_488`` → ``REG_4888`` differ in
      canonical form).
    - Perl preprocessor section changes.

    The check runs in ``_full_pass_async`` between the exact-equality
    short-circuit and ``_compile_text`` — adds one O(n) string scan per
    edit. ~50µs on a 1MB buffer; saves seconds when it matches.
    """
    if not text:
        return ""
    out: list[str] = []
    i = 0
    n = len(text)
    last_was_space = True  # leading whitespace gets stripped
    while i < n:
        c = text[i]
        # Quoted string — copy literally, never collapse internal whitespace.
        if c == '"':
            j = i + 1
            while j < n:
                if text[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                if text[j] == '"':
                    j += 1
                    break
                j += 1
            out.append(text[i:j])
            last_was_space = False
            i = j
            continue
        # Line comment // ... \n
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        # Line comment # ... \n  (RDL allows '#' comments inside Perl
        # context only, but stripping it everywhere is safe — the
        # compiler would treat a stray '#' outside Perl as an error
        # both before and after stripping, so the canonical comparison
        # stays consistent with the compiler's actual behaviour.)
        if c == "#":
            while i < n and text[i] != "\n":
                i += 1
            continue
        # Block comment /* ... */
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            j = i + 2
            while j + 1 < n and not (text[j] == "*" and text[j + 1] == "/"):
                j += 1
            i = min(n, j + 2)
            continue
        # Whitespace run → single space (or nothing at start/end).
        if c.isspace():
            if not last_was_space:
                out.append(" ")
                last_was_space = True
            i += 1
            continue
        out.append(c)
        last_was_space = False
        i += 1
    # Trim trailing space if we left one.
    if out and out[-1] == " ":
        out.pop()
    return "".join(out)


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


def _resolve_search_paths(
    uri: str, setting_paths: list[str] | None = None
) -> list[tuple[str, str]]:
    r"""Return the deduped, source-labeled include search path list for ``uri``.

    Sources, in priority order:

    1. ``"setting"`` — entries from ``systemrdl-pro.includePaths`` (workspace
       settings.json). User-explicit, wins on ties.
    2. ``"peakrdl.toml"`` — paths from a ``[parser] incl_search_paths``
       block in any ancestor ``peakrdl.toml`` (auto-discovered, matches
       PeakRDL CLI semantics).
    3. ``"sibling"`` — implicit fallback to the file's own directory so
       relative `\include "x.rdl"` works without extra config.

    Dedup is by literal path string. Workspace-relative paths from the setting
    are left unresolved here (handled downstream by the compiler); peakrdl.toml
    paths are already absolutized.
    """
    seen: set[str] = set()
    out: list[tuple[str, str]] = []

    def push(p: str, source: str) -> None:
        if not p or p in seen:
            return
        seen.add(p)
        out.append((p, source))

    for p in setting_paths or []:
        push(str(p), "setting")

    original_path = _uri_to_path(uri)
    for p in _peakrdl_toml_paths(original_path):
        push(p, "peakrdl.toml")

    if original_path.parent.exists():
        push(str(original_path.parent), "sibling")

    return out


@functools.lru_cache(maxsize=128)
def _peakrdl_toml_paths(start: pathlib.Path) -> list[str]:
    """Walk upward from ``start`` looking for ``peakrdl.toml`` and read its
    ``[parser] incl_search_paths`` array.

    PeakRDL's own CLI honours this same key, so a project that already builds
    with PeakRDL just works in the editor without re-declaring its include
    tree under ``systemrdl-pro.includePaths``. Workspace-relative paths are
    resolved against the .toml's own directory, matching PeakRDL semantics.

    Cached because the toml location and contents rarely change during a
    session, and ``_resolve_search_paths`` is on the hot path of every
    didOpen / didSave / documentLink. Cache key is the start path; we
    bound the cache so a long-running LSP with many distinct workspace
    files doesn't grow it unbounded. Restart the LSP to invalidate.
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


# Detection helpers for the Perl preprocessor (clause 16 of the RDL spec).
# systemrdl-compiler shells out to a real ``perl`` binary, so the feature is
# only available when one is on PATH. We surface a one-time notification when
# the user's source has Perl markers but the binary is missing.
_PERL_MARKER_RE = re.compile(r"<%")


def _perl_in_source(text: str) -> bool:
    """True when the buffer contains a Perl preprocessor marker."""
    return bool(text) and bool(_PERL_MARKER_RE.search(text))


@functools.lru_cache(maxsize=1)
def _perl_available() -> bool:
    """Cached ``shutil.which('perl')`` check.

    Cleared from the test suite via ``_perl_available.cache_clear()`` if needed.
    Cached for the LSP's lifetime — installing perl while the editor is open is
    rare enough that one cache miss is acceptable; in practice users either have
    perl or they don't.
    """
    return shutil.which("perl") is not None


def _compile_text(
    uri: str,
    text: str,
    incl_search_paths: list[str] | None = None,
    include_vars: dict[str, str] | None = None,
    perl_safe_opcodes: list[str] | None = None,
) -> tuple[
    list[CompilerMessage],
    list[RootNode],
    pathlib.Path,
    set[pathlib.Path],
    dict[str, Any],
    dict[str, Any] | None,
]:
    """Compile in-memory buffer text. Returns
    ``(messages, roots, temp_path, consumed_files, node_index, spine_envelope)``.

    ``roots`` is a list of RootNode instances — one per top-level ``addrmap``
    *definition* in the file (Decision 3C). ``compiler.elaborate()`` with no
    ``top_def_name`` only elaborates the *last* defined addrmap, so we enumerate
    ``compiler.root.comp_defs`` and elaborate each one separately. An empty list
    means parse failed or the file has no top-level addrmap (a library file).

    ``consumed_files`` is the set of source files the compiler actually opened
    while processing this buffer, minus ``temp_path`` itself. Drives the
    include reverse-dep map (T2-A) so editing ``types.rdl`` proactively
    re-elaborates every open file that included it.

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
    # Deduped, source-labeled list — _resolve_search_paths is also exposed
    # to clients via the rdl/includePaths JSON-RPC for the "Show effective
    # include paths" command.
    search_paths = [p for p, _src in _resolve_search_paths(uri, incl_search_paths)]

    printer = CapturingPrinter()
    compiler_kwargs: dict[str, Any] = {"message_printer": printer}
    if perl_safe_opcodes:
        # Forward only when the user supplied a non-empty list so the compiler's
        # built-in default (covers ~95% of preprocessor needs) stays in effect
        # for everyone else.
        compiler_kwargs["perl_safe_opcodes"] = list(perl_safe_opcodes)
    compiler = RDLCompiler(**compiler_kwargs)

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
    consumed = _harvest_consumed_files(roots, printer.captured, tmp_path)
    # Build the index AND pre-serialize the spine envelope here. In the
    # pool path this all happens in the worker process — no GIL contention
    # with the main process, so other URIs stay unblocked while a 25k-reg
    # design churns through serialization. Pickle's memo keeps shared
    # refs aligned across the process boundary. Spine envelope's version
    # field is a placeholder (0); main process patches it to the real
    # cache version after cache.put.
    node_index: dict[str, Any] = {}
    spine_envelope: dict[str, Any] | None = None
    if roots:
        from .serialize import _build_node_index, _serialize_spine
        node_index = _build_node_index(roots)
        spine_envelope = _serialize_spine(
            roots, stale=False,
            path_translate={tmp_path: original_path},
            version=0,
        )
    return messages, roots, tmp_path, consumed, node_index, spine_envelope


# ---------------------------------------------------------------------------
# T3: subprocess wrapper (pickle + zlib) for ProcessPoolExecutor IPC
# ---------------------------------------------------------------------------


# Pickle protocol pin. 5 is the highest stable across Python 3.10-3.13
# (the LSP's supported range). Bumping this requires confirming all
# deployment targets support the new protocol; mismatch shows up as
# UnpicklingError in the parent immediately, so a regression is loud
# rather than silent.
_T3_PICKLE_PROTOCOL = 5

# zlib level 1 (fastest) chosen over 6 (default) per the PoC bench:
# - L=1 on stress_25k_multi: 5.4 MB, encode 2.6s, decode 2.1s
# - L=6: 2.1 MB, encode 2.85s, decode 2.1s
# 60% smaller wire vs L=6 costs 0.25s more encode for 3.3 MB less
# pipe traffic — negligible either way relative to the 34s elaborate.
# L=1 wins because pipe bandwidth on a local Unix socket is gigabytes/s
# anyway; the limiting cost is pickle.dumps itself, not transport.
_T3_ZLIB_LEVEL = 1


def _compile_text_compressed(
    uri: str,
    text: str,
    incl_search_paths: list[str] | None = None,
    include_vars: dict[str, str] | None = None,
    perl_safe_opcodes: list[str] | None = None,
) -> bytes:
    """``_compile_text`` wrapped for ProcessPoolExecutor IPC.

    The subprocess elaborates, then pickles + zlibs the 4-tuple to a
    bytes blob. The parent decompresses + unpickles via
    ``_decompress_compile_result``. Compression is essentially free on
    a homogeneous SystemRDL tree (50 banks × 500 regs × 30 fields =
    massive structural redundancy that general-purpose compressors
    crush — see ``docs/perf-poc-processpool.md``).

    Why this wrapper exists rather than letting ``ProcessPoolExecutor``
    pickle the result itself: the executor's built-in pickling is raw,
    no compression. On ``stress_25k_multi.rdl`` raw pickle is 174 MB
    on the wire; zlib L=1 compresses it to 5.4 MB at the same encode
    speed. Wrapping is a one-liner; integrating compression into the
    executor's pickle path is not.
    """
    result = _compile_text(
        uri, text, incl_search_paths, include_vars, perl_safe_opcodes,
    )
    return zlib.compress(
        pickle.dumps(result, protocol=_T3_PICKLE_PROTOCOL),
        _T3_ZLIB_LEVEL,
    )


def _decompress_compile_result(
    blob: bytes,
) -> tuple[
    list[CompilerMessage],
    list[RootNode],
    pathlib.Path,
    set[pathlib.Path],
    dict[str, Any],
    dict[str, Any] | None,
]:
    """Reverse of ``_compile_text_compressed``. Called in the parent
    process to materialize the subprocess's elaborate result."""
    return pickle.loads(zlib.decompress(blob))


def _pool_worker_init() -> None:
    """Runs once per ``ProcessPoolExecutor`` worker at startup.

    Imports the heavy modules each elaborate touches so the first real
    submission doesn't pay the import cost on the user's typing
    latency. ``systemrdl-compiler`` import alone is ~150 ms.
    """
    import systemrdl  # noqa: F401

    import systemrdl_lsp.compile
    import systemrdl_lsp.serialize  # noqa: F401


def _pool_warmup_noop() -> None:
    """No-op submitted to a fresh pool so each worker actually spawns
    (``ProcessPoolExecutor`` is lazy — workers don't start until the
    first task lands on them). Top-level function so pickle can find
    it across the process boundary; lambdas would not pickle."""
    return None


# ---------------------------------------------------------------------------
# T2-A: include reverse-dep harvesting
# ---------------------------------------------------------------------------


def _children_safe(node: Any) -> list[Any]:
    """Version-portable ``node.children()`` accessor.

    ``systemrdl-compiler`` added ``skip_not_present=False`` to
    ``children()`` in a specific version; older installs raise
    ``TypeError`` on the unknown kwarg. extracted as a
    helper so the 3-tier try/except dance lives in one place
    instead of being copy-pasted four times across
    ``_harvest_consumed_files`` and ``_fingerprint_roots``.
    Returns ``[]`` on any failure — node has no children, or the
    underlying compiler raised.
    """
    try:
        return list(node.children(skip_not_present=False))
    except TypeError:
        try:
            return list(node.children())
        except Exception:
            return []
    except Exception:
        return []


def _harvest_consumed_files(
    roots: list[RootNode],
    captured: list[tuple[Severity, str, Any]],
    tmp_path: pathlib.Path,
) -> set[pathlib.Path]:
    """Return the set of source files the compiler opened, minus ``tmp_path``.

    Walks every ``src_ref`` we can reach: diagnostic messages and every
    instance's ``def_src_ref`` / ``inst_src_ref`` in the elaborated tree.
    Used by the LSP to maintain the include-graph reverse map so an edit to
    a library file proactively re-elaborates open consumers (T2-A).

    T4-C A7 + P7: ``_children_safe`` for the version-portability dance
    around ``skip_not_present``; ``seen_raw`` dedups before the
    expensive ``pathlib.resolve()`` syscall (was N calls where K
    unique would do — ~50-250 ms saved on 25k regs).
    """
    files: set[pathlib.Path] = set()
    seen_raw: set[str] = set()
    try:
        tmp_resolved = tmp_path.resolve()
    except OSError:
        tmp_resolved = tmp_path

    def feed(raw: Any) -> None:
        if not raw:
            return
        # dedup before resolve(). On 25k regs the harvest visits
        # ~50k src_refs with ~2 unique filenames; resolving each one
        # issues a stat()/realpath() syscall (~1-5 µs each). Hashing
        # the raw string first cuts the syscall count to K, not N.
        raw_str = str(raw)
        if raw_str in seen_raw:
            return
        seen_raw.add(raw_str)
        try:
            path = pathlib.Path(raw).resolve()
        except (OSError, ValueError):
            return
        if path == tmp_resolved:
            return
        files.add(path)

    for _sev, _text, src_ref in captured:
        if src_ref is None:
            continue
        feed(getattr(src_ref, "filename", None))

    def walk(node: Any) -> None:
        inst = getattr(node, "inst", None)
        if inst is not None:
            for attr in ("def_src_ref", "inst_src_ref"):
                ref = getattr(inst, attr, None)
                if ref is not None:
                    feed(getattr(ref, "filename", None))
        for kid in _children_safe(node):
            walk(kid)

    for root in roots:
        for child in _children_safe(root):
            walk(child)

    return files


# Re-export the fingerprint hash so existing imports (server.py, tests)
# continue working after the move to its own module.
from ._fingerprint import _fingerprint_roots  # noqa: E402,F401


def _elaborate(path: pathlib.Path) -> list[tuple[Severity, str, Any]]:
    """Legacy disk-based elaboration; kept for tests and ``didSave`` warm path."""
    if not path.exists():
        printer = CapturingPrinter()
        printer.captured.append((Severity.ERROR, f"file not found: {path}", None))
        return printer.captured

    text = path.read_text(encoding="utf-8")
    messages, _roots, tmp_path, _consumed, _node_index, _spine = _compile_text(path.as_uri(), text)
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
    # Monotonic per-URI version. Incremented on every successful ``put`` so
    # ``rdl/elaboratedTree`` clients can pass ``sinceVersion`` and the LSP
    # answers with a tiny ``{unchanged: true}`` envelope when nothing changed.
    version: int = 0
    # Serialized JSON dict, lazily populated on first ``rdl/elaboratedTree``
    # request and re-used for every subsequent same-version request. Cleared
    # whenever the entry is replaced. May be either a full envelope (legacy
    # client) or a spine envelope (lazy-mode client) — the server picks one
    # consistently per session based on client capability.
    serialized: dict[str, Any] | None = None
    # T1.5: per-node memoization for ``rdl/expandNode`` responses. Keyed by
    # ``nodeId`` (the base-36 string clients get from the spine). Lazily
    # initialized on first expand request. Cleared whenever the entry is
    # replaced (next elaboration regenerates ids so old entries are stale).
    expanded: dict[str, dict[str, Any]] | None = None
    # nodeId → RegNode lookup table for O(1) expand. Walk order matches
    # the spine's DFS so ids line up. Refs only — RegNodes live in roots.
    node_index: dict[str, Any] | None = None
    # T2-B: SHA-256 fingerprint of the elaborated tree's viewer-facing
    # semantics. Lets ``_apply_compile_result`` skip the cache version
    # bump + ``rdl/elaboratedTreeChanged`` push when an edit produced an
    # identical AST (whitespace, comments, dead-code rename, etc.).
    # ``None`` only on legacy entries seeded before T2 — treated as
    # "always different" to preserve current behaviour.
    ast_fingerprint: str | None = None
    # T2-D: canonicalized form of ``text`` (comments stripped + non-string
    # whitespace collapsed). Compared in ``_full_pass_async`` *before*
    # elaborate so a whitespace-only edit doesn't even start the
    # compiler — no banner, no version bump, no notification. Computed
    # once per cache.put (~50µs on 1MB buffers).
    text_canonical: str | None = None


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
        ast_fingerprint: str | None = None,
        node_index: dict[str, Any] | None = None,
        serialized: dict[str, Any] | None = None,
    ) -> None:
        old = self._entries.get(uri)
        old_version = 0
        if old is not None:
            old_version = old.version
            if old.temp_path is not None:
                old.temp_path.unlink(missing_ok=True)
        self._entries[uri] = CachedElaboration(
            roots=roots,
            text=text,
            elaborated_at=time.time(),
            temp_path=temp_path,
            version=old_version + 1,
            ast_fingerprint=ast_fingerprint,
            text_canonical=_canonicalize_for_skip(text),
            node_index=node_index,
            serialized=serialized,
        )

    def clear(self) -> None:
        for entry in self._entries.values():
            if entry.temp_path is not None:
                entry.temp_path.unlink(missing_ok=True)
        self._entries.clear()
