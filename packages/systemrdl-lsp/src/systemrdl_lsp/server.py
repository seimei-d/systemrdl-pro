"""LSP server for SystemRDL 2.0 — thin LSP feature wiring.

Helper logic lives in themed modules:

- :mod:`.compile` — in-memory buffer compile, ``ElaborationCache``,
  ``CompilerMessage``, include-path resolution
- :mod:`.diagnostics` — severity mapping, range builders, publishing,
  cross-tree address-conflict scan
- :mod:`.hover` — instance-lookup + word-based catalogue lookup
- :mod:`.completion` — keyword/property/value catalogues, context detection
- :mod:`.definition` — identifier-under-cursor → component def location
- :mod:`.serialize` — elaborated tree → JSON envelope (rdl/elaboratedTree)
- :mod:`.outline` — documentSymbol, workspaceSymbol, foldingRange,
  inlayHint, codeLens

This module wires those helpers into pygls feature handlers and owns the
``ServerState`` that threads the cache, pending debounce tasks, and workspace
configuration through every callback.
"""

from __future__ import annotations

import asyncio
import atexit
import dataclasses
import logging
import multiprocessing as mp
import pathlib
import time
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from typing import TYPE_CHECKING, Any

from lsprotocol.types import (
    INITIALIZED,
    TEXT_DOCUMENT_DID_CHANGE,
    TEXT_DOCUMENT_DID_CLOSE,
    TEXT_DOCUMENT_DID_OPEN,
    TEXT_DOCUMENT_DID_SAVE,
    TEXT_DOCUMENT_SEMANTIC_TOKENS_FULL,
    WORKSPACE_DID_CHANGE_CONFIGURATION,
    CodeLens,
    DidChangeConfigurationParams,
    DidChangeTextDocumentParams,
    DidCloseTextDocumentParams,
    DidOpenTextDocumentParams,
    DidSaveTextDocumentParams,
    FoldingRange,
    InitializedParams,
    InlayHint,
    Location,
    ParameterInformation,
    Position,
    Range,
    SemanticTokens,
    SemanticTokensLegend,
    SignatureHelp,
    SignatureHelpOptions,
    SignatureInformation,
    SymbolInformation,
)
from pygls.lsp.server import LanguageServer
from systemrdl.messages import Severity

from ._uri import _path_to_uri, _uri_to_path
from .cache import DiskCache, make_key
from .code_actions import _code_actions_for_range
from .compile import (
    CachedElaboration,
    CapturingPrinter,
    CompilerMessage,
    ElaborationCache,
    _canonicalize_for_skip,
    _compile_text,
    _compile_text_compressed,
    _decompress_compile_result,
    _elaborate,
    _expand_include_vars,
    _fingerprint_roots,
    _peakrdl_toml_paths,
    _perl_available,
    _perl_in_source,
    _pool_warmup_noop,
    _pool_worker_init,
    _resolve_search_paths,
    _SimpleRef,
)
from .completion import (
    SYSTEMRDL_ONREAD_VALUES,
    SYSTEMRDL_ONWRITE_VALUES,
    SYSTEMRDL_PROPERTIES,
    SYSTEMRDL_RW_VALUES,
    SYSTEMRDL_TOP_KEYWORDS,
    _completion_context,
    _completion_items_for_context,
    _completion_items_for_types,
    _completion_items_for_user_properties,
    _completion_items_static,
    _make_items,
)
from .definition import (
    _comp_defs_from_cached,
    _definition_location,
    _find_instance_by_name,
    _path_at_position,
    _references_to_type,
    _rename_locations,
    _resolve_path,
    _word_at_position,
)
from .diagnostics import (
    SERVER_NAME,
    _address_conflict_diagnostics,
    _build_range,
    _message_to_range,
    _publish_diagnostics,
    _severity_to_lsp,
    _src_ref_to_range,
)
from .formatting import _document_formatting_edits, _format_text
from .hover import (
    _format_hex,
    _hover_for_word,
    _hover_text_for_node,
    _node_at_position,
)
from .links import _document_links
from .outline import (
    _code_lenses_for_addrmaps,
    _document_symbols,
    _folding_ranges_from_text,
    _inlay_hints_for_addressables,
    _workspace_symbols_for_uri,
)
from .semantic import (
    TOKEN_MODIFIERS,
    TOKEN_TYPES,
    _semantic_tokens_for_text,
)
from .serialize import (
    ELABORATED_TREE_SCHEMA_VERSION,
    _field_access_token,
    _hex,
    _safe_get_property,
    _serialize_addressable,
    _serialize_field,
    _serialize_reg,
    _serialize_root,
    _serialize_spine,
    _src_ref_to_dict,
    _unchanged_envelope,
    _build_node_index,
    expand_node,
)

if TYPE_CHECKING:
    from systemrdl.node import RootNode

logger = logging.getLogger(__name__)

from systemrdl_lsp import __version__ as SERVER_VERSION  # noqa: E402  pulled from package __init__


def _iter_rdl_files(root: pathlib.Path, exclude_dirs: set[str]):
    """Yield every ``.rdl`` file under ``root``, skipping noisy directory names.

    Used by the workspace pre-index. Errors during traversal (permission denied,
    broken symlinks) are silently skipped — the pre-index is best-effort, not
    a hard guarantee.
    """
    try:
        for entry in root.iterdir():
            if entry.is_dir():
                if entry.name in exclude_dirs or entry.name.startswith("."):
                    continue
                yield from _iter_rdl_files(entry, exclude_dirs)
            elif entry.is_file() and entry.suffix.lower() == ".rdl":
                yield entry
    except (PermissionError, OSError):
        return


def _build_selection_ranges(
    text: str, lines: list[str], line_0b: int, char_0b: int
) -> list[Any]:
    """Walk outward from the cursor position through enclosing `{...}` blocks.

    Returns a list of LSP ``Range`` objects, **innermost first**. Caller links
    them parent-pointer-style for ``textDocument/selectionRange``. Pure textual
    scan — strings/comments are stripped to whitespace so braces inside them
    don't confuse the matcher (same trick as folding ranges).
    """
    import re as _re

    if line_0b < 0 or line_0b >= len(lines):
        return []
    line = lines[line_0b]
    if char_0b < 0 or char_0b > len(line):
        return []

    # Innermost: the word under cursor.
    word = _word_at_position(text, line_0b, char_0b)
    word_range: Range | None = None
    if word:
        m = _re.search(rf"\b{_re.escape(word)}\b", line)
        if m and m.start() <= char_0b <= m.end():
            word_range = Range(
                start=Position(line=line_0b, character=m.start()),
                end=Position(line=line_0b, character=m.end()),
            )

    # Strip strings/comments to neutral whitespace so brace counting doesn't
    # see literals or block-comment braces.
    cleaned = _re.sub(r'"(?:\\.|[^"\\])*"', lambda m: " " * len(m.group(0)), text)
    cleaned = _re.sub(r"//[^\n]*", lambda m: " " * len(m.group(0)), cleaned)
    cleaned = _re.sub(
        r"/\*[\s\S]*?\*/",
        lambda m: _re.sub(r"[^\n]", " ", m.group(0)),
        cleaned,
    )

    # Prefix-sum of line start offsets so both offset_of and pos_of
    # are O(1) / O(log n) instead of O(n) per call. pos_of is called
    # 2x per enclosing brace pair, so deep nesting in a 10k-line file
    # used to be O(K*N) where K is nesting depth.
    line_starts: list[int] = [0]
    for ln in lines:
        line_starts.append(line_starts[-1] + len(ln) + 1)

    def offset_of(li: int, co: int) -> int:
        if li < 0:
            return co
        if li >= len(lines):
            return line_starts[-1]
        return line_starts[li] + co

    import bisect as _bisect

    def pos_of(off: int) -> Position:
        # bisect_right gives the index of the line whose start is > off,
        # so the line containing off is at idx-1.
        idx = _bisect.bisect_right(line_starts, off) - 1
        if idx < 0:
            return Position(line=0, character=0)
        if idx >= len(lines):
            return Position(line=len(lines), character=0)
        return Position(line=idx, character=off - line_starts[idx])

    cursor_off = offset_of(line_0b, char_0b)
    ranges: list[Range] = []
    if word_range is not None:
        ranges.append(word_range)

    # Walk outward through balanced `{...}` pairs that contain the cursor.
    # Strategy: scan all `{` `}` pairs in the file, keep those whose span
    # includes the cursor offset, sort by span size ascending → innermost first.
    stack: list[int] = []
    pairs: list[tuple[int, int]] = []
    for i, ch in enumerate(cleaned):
        if ch == "{":
            stack.append(i)
        elif ch == "}" and stack:
            start = stack.pop()
            pairs.append((start, i + 1))
    enclosing = [(s, e) for s, e in pairs if s <= cursor_off <= e]
    enclosing.sort(key=lambda t: t[1] - t[0])
    for s, e in enclosing:
        ranges.append(Range(start=pos_of(s), end=pos_of(e)))

    # Outermost: whole file.
    last_line = max(0, len(lines) - 1)
    ranges.append(Range(
        start=Position(line=0, character=0),
        end=Position(line=last_line, character=len(lines[last_line]) if lines else 0),
    ))
    return ranges


def _is_valid_identifier(name: str) -> bool:
    """Match SystemRDL identifier syntax: ``[A-Za-z_][A-Za-z0-9_]*``.

    Used by rename to reject input that would corrupt the buffer.
    """
    import re as _re
    return bool(_re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name))


DEBOUNCE_SECONDS = 0.3
# Eng-review safety net #3: cap a single elaborate pass at 60s wall-clock by default.
# Past that we keep last-good (D7) and surface a synthetic diagnostic. A pathological
# Perl-style include cycle in a third-party RDL pack should NOT freeze the editor.
# Override per-workspace via systemrdl-pro.elaborationTimeoutMs (state.elaboration_timeout_s).
ELABORATION_TIMEOUT_SECONDS = 120.0
ELABORATION_TIMEOUT_SECONDS_MIN = 1.0
ELABORATION_TIMEOUT_SECONDS_MAX = 600.0


# Re-exports for backwards-compat — tests import several private helpers
# directly from this module. Listing them here keeps tooling (mypy, ruff)
# from reporting "imported but unused".
__all__ = [
    "DEBOUNCE_SECONDS",
    "ELABORATED_TREE_SCHEMA_VERSION",
    "ELABORATION_TIMEOUT_SECONDS",
    "SERVER_NAME",
    "SERVER_VERSION",
    "SYSTEMRDL_ONREAD_VALUES",
    "SYSTEMRDL_ONWRITE_VALUES",
    "SYSTEMRDL_PROPERTIES",
    "SYSTEMRDL_RW_VALUES",
    "SYSTEMRDL_TOP_KEYWORDS",
    "CachedElaboration",
    "CapturingPrinter",
    "CompilerMessage",
    "ElaborationCache",
    "ServerState",
    "_SimpleRef",
    "_address_conflict_diagnostics",
    "_build_range",
    "_canonicalize_for_skip",
    "_code_actions_for_range",
    "_code_lenses_for_addrmaps",
    "_comp_defs_from_cached",
    "_compile_text",
    "_completion_context",
    "_completion_items_for_context",
    "_completion_items_for_types",
    "_completion_items_static",
    "_definition_location",
    "_document_formatting_edits",
    "_document_links",
    "_document_symbols",
    "_elaborate",
    "_expand_include_vars",
    "_field_access_token",
    "_fingerprint_roots",
    "_folding_ranges_from_text",
    "_format_hex",
    "_format_text",
    "_hex",
    "_hover_for_word",
    "_hover_text_for_node",
    "_inlay_hints_for_addressables",
    "_iter_rdl_files",
    "_make_items",
    "_message_to_range",
    "_node_at_position",
    "_path_to_uri",
    "_peakrdl_toml_paths",
    "_publish_diagnostics",
    "_references_to_type",
    "_rename_locations",
    "_resolve_search_paths",
    "_safe_get_property",
    "_semantic_tokens_for_text",
    "_serialize_addressable",
    "_serialize_field",
    "_serialize_reg",
    "_serialize_root",
    "_severity_to_lsp",
    "_src_ref_to_dict",
    "_src_ref_to_range",
    "_unchanged_envelope",
    "_uri_to_path",
    "_word_at_position",
    "_workspace_symbols_for_uri",
    "build_server",
]


# ---------------------------------------------------------------------------
# Server state
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class ServerState:
    cache: ElaborationCache = dataclasses.field(default_factory=ElaborationCache)
    pending: dict[str, asyncio.Task] = dataclasses.field(default_factory=dict)
    include_paths: list[str] = dataclasses.field(default_factory=list)
    # T2-A: forward and reverse maps over the include graph, populated from
    # the set of source files the compiler actually opened on each successful
    # elaborate (see ``compile._harvest_consumed_files``). Used to proactively
    # re-elaborate any open consumer when a library file changes — the user
    # no longer has to close-and-reopen ``stress_25k.rdl`` after editing
    # ``types.rdl``. Both maps key on URIs.
    include_graph: dict[str, set[str]] = dataclasses.field(default_factory=dict)
    includee_to_includers: dict[str, set[str]] = dataclasses.field(default_factory=dict)
    # T2-C: per-URI elaboration mutex. Replaces the TODO(T2) marker that used
    # to live above ``_full_pass_async``. Same-URI didOpen+didSave races
    # serialize through this lock so the second call sees the first call's
    # cache.put before short-circuiting on buffer-equality. Different URIs
    # still elaborate concurrently (subject to GIL contention).
    uri_elab_locks: dict[str, asyncio.Lock] = dataclasses.field(default_factory=dict)
    # Test-only override that stands in for the pygls workspace. When
    # non-None, ``_is_open(uri)`` returns ``uri in self`` and
    # ``_read_buffer(uri)`` returns ``self.get(uri)`` instead of going
    # through ``server.workspace`` (which is not initialized in unit
    # tests). Production code never sets this. Map: URI → buffer text.
    test_open_buffers: dict[str, str] | None = None
    # Substitution map for ``$VAR`` / ``${VAR}`` inside ```include "..."`` paths.
    # Read from systemrdl-pro.includeVars; falls back to os.environ during expansion.
    include_vars: dict[str, str] = dataclasses.field(default_factory=dict)
    # URIs whose latest parse attempt failed but for which we still have a last-good
    # cache entry. The viewer renders a stale-bar when a URI is in this set (D7).
    stale_uris: set[str] = dataclasses.field(default_factory=set)
    # URIs that need to skip the buffer-equality / canonicalize-skip
    # short-circuits on the next elaborate (typically because a cascade
    # trigger needs to re-elaborate the file even though its own buffer
    # didn't change — an includee changed). Cleared the moment the
    # bypass actually fires. Separate from ``stale_uris`` so the stale-
    # transition logic in ``_apply_compile_result`` can detect a
    # genuine False → True (or T → F) move; if cascade overloaded
    # stale_uris like it used to, ``was_stale`` would always be True
    # on the cascade path and the viewer would miss the new failure.
    force_re_elaborate: set[str] = dataclasses.field(default_factory=set)
    # Forwarded to RDLCompiler(perl_safe_opcodes=...). Empty list keeps the
    # compiler's safe default. Power users adding `:base_io` for `print`-based
    # codegen go through this setting.
    perl_safe_opcodes: list[str] = dataclasses.field(default_factory=list)
    # One-shot guard for the "Perl is not on PATH" notification. The diagnostic
    # itself comes from systemrdl-compiler on every compile, so we only nag with
    # the modal banner once per session.
    perl_warning_shown: bool = False
    # Per-primary-URI snapshot of which cross-file URIs we last published
    # non-empty diagnostics to. Drives the clear-on-resolve cycle for
    # `\`include`d files (a fixed error in common.rdl publishes [] there next
    # compile so the stale squiggle disappears).
    diag_affected: dict[str, set[str]] = dataclasses.field(default_factory=dict)
    # Background pre-index toggles. workspace/symbol only sees files the user
    # has opened; pre-warming the cache fixes Ctrl+T against unfamiliar trees.
    # Default off — on a multi-window workspace each VSCode window starts its
    # own LSP and the parallel pre-indexes pegged the CPU. Users who want
    # workspace-wide search opt in via settings.json.
    preindex_enabled: bool = False
    preindex_max_files: int = 200
    # Per-workspace override for ELABORATION_TIMEOUT_SECONDS. systemrdl-pro.elaborationTimeoutMs
    # surfaces this so big chip designs (multi-subsystem aggregates in the 10-25k+ register
    # range) can lift the 10s cap when their elaboration legitimately takes longer.
    elaboration_timeout_s: float = ELABORATION_TIMEOUT_SECONDS
    # T1.4: lazy-tree capability gate. Set in INITIALIZED handler from
    # client.capabilities.experimental.systemrdlLazyTree. When True the LSP
    # serves spine envelopes (Reg.loadState='placeholder') and answers
    # rdl/expandNode requests; when False it serves full trees as before.
    lazy_supported: bool = False
    # T1.4: on-disk cache for spine envelopes. Survives window reload; second
    # window in a multi-root workspace shares the cache via content key.
    # Constructed lazily by the build_server() helper so tests can swap it.
    disk_cache: DiskCache = dataclasses.field(default_factory=DiskCache)
    # T3: ProcessPoolExecutor for cross-URI parallel elaborate. ``None``
    # means in-thread (legacy/fallback). Initialized on INITIALIZED based
    # on systemrdl-pro.elaborateInProcess; defaults to subprocess mode.
    # Workers pre-warm via _pool_warmup_noop so the first real elaborate
    # doesn't pay spawn + import cost on the user's typing latency.
    elaborate_pool: Any = None  # ProcessPoolExecutor when active
    # systemrdl-pro.elaborateInProcess setting. False (default) = use
    # the subprocess pool. True = run in the asyncio default
    # ThreadPoolExecutor as before T3 — kept as an escape hatch in
    # case a future systemrdl-compiler upgrade breaks RootNode pickle
    # compatibility, or for diagnosing pool-related issues in the field.
    elaborate_in_process: bool = False
    # Pool size cap. 2 covers the documented pain (small file behind
    # one big elaborate). Bumping higher is fine but each worker holds
    # ~150 MB resident at idle plus the in-flight tree, so 2-4 is the
    # sweet spot for a developer machine. Surfaced as a setting if a
    # future user reports needing more.
    elaborate_pool_workers: int = 2
    # T3-G: worker recycle threshold. Mitigates the upstream
    # systemrdl-compiler memory leak (~5 MB per elaborate of a
    # 40-reg fixture, observed in test_memory_growth_bounded). After
    # this many successful subprocess elaborates we tear down the
    # pool and spawn a fresh one — RSS comes back to baseline. 50
    # gives a few minutes of editing on big designs before the
    # next recycle, recycling itself is ~150 ms (worker spawn +
    # _pool_worker_init), barely visible during a typing pause.
    pool_max_elaborates: int = 50
    # Counts successful subprocess elaborates since the current pool
    # was spawned. Reset to 0 in ``_ensure_elaborate_pool``. When it
    # hits ``pool_max_elaborates`` the elaborate-finished bookkeeping
    # schedules a recycle and resets the counter.
    pool_elaborate_count: int = 0


# ---------------------------------------------------------------------------
# Server construction
# ---------------------------------------------------------------------------


def build_server() -> LanguageServer:
    server = LanguageServer(SERVER_NAME, SERVER_VERSION)
    state = ServerState()

    def _read_buffer(uri: str) -> str | None:
        if state.test_open_buffers is not None:
            return state.test_open_buffers.get(uri)
        try:
            doc = server.workspace.get_text_document(uri)
        except Exception:
            return None
        return doc.source

    def _ensure_elaborate_pool() -> None:
        """Lazy-init the ProcessPoolExecutor + pre-warm workers (T3).

        Workers spawn on first submit; we want spawn cost out of the
        first user-typing latency, so we kick a no-op per worker so
        each spawns + runs ``_pool_worker_init`` (imports
        systemrdl + systemrdl_lsp.compile + serialize). The warmup
        futures are intentionally not awaited — they complete in the
        background while the user is still configuring the editor.
        """
        if state.elaborate_in_process or state.elaborate_pool is not None:
            return
        try:
            ctx = mp.get_context("spawn")
            state.elaborate_pool = ProcessPoolExecutor(
                max_workers=state.elaborate_pool_workers,
                mp_context=ctx,
                initializer=_pool_worker_init,
            )
            for _ in range(state.elaborate_pool_workers):
                state.elaborate_pool.submit(_pool_warmup_noop)
            state.pool_elaborate_count = 0
            logger.info(
                "T3 elaborate pool: %d workers, spawn ctx, pre-warming "
                "(recycle every %d elaborates)",
                state.elaborate_pool_workers, state.pool_max_elaborates,
            )
        except Exception:
            logger.exception(
                "T3 elaborate pool init failed; falling back to in-thread"
            )
            state.elaborate_in_process = True

    def _shutdown_elaborate_pool() -> None:
        pool = state.elaborate_pool
        if pool is None:
            return
        state.elaborate_pool = None
        try:
            pool.shutdown(wait=False, cancel_futures=True)
        except Exception:
            logger.debug("elaborate pool shutdown raised", exc_info=True)

    # Best-effort cleanup if the LSP exits without an explicit shutdown
    # notification — VSCode kills the process abruptly on window close
    # and pygls' shutdown hook does not always fire. Without this, the
    # spawned worker processes can stick around as orphans.
    atexit.register(_shutdown_elaborate_pool)

    def _apply_stale_transition(
        uri: str, cached: CachedElaboration, target_stale: bool,
    ) -> int | None:
        """Toggle ``state.stale_uris[uri]`` to match ``target_stale``.

        On a real transition (False → True or True → False), bump
        ``cached.version`` and clear ``cached.serialized`` so the next
        ``rdl/elaboratedTree`` fetch builds a fresh envelope with the
        right stale flag, and return the new version so the caller
        can fire ``rdl/elaboratedTreeChanged``. Returns ``None`` when
        state was already correct (no client refresh needed).

        Centralises the stale state machine that used to live inline
        in three sites — last three field-reported stale-bar
        regressions all traced back to one of the copies drifting.
        """
        is_stale_now = uri in state.stale_uris
        if target_stale and not is_stale_now:
            state.stale_uris.add(uri)
        elif not target_stale and is_stale_now:
            state.stale_uris.discard(uri)
        else:
            return None
        cached.version += 1
        cached.serialized = None
        return cached.version

    def _update_include_graph(uri: str, consumed_files: set[pathlib.Path]) -> None:
        """Refresh forward + reverse include maps for ``uri`` (T2-A).

        Replaces this URI's includee set with the freshly observed
        ``consumed_files`` (converted to URIs). Old reverse-edges that
        no longer apply are pruned; new ones are added. Cheap — touches
        only the URIs that actually changed in the dep set.
        """
        new_includees: set[str] = set()
        for path in consumed_files:
            try:
                new_includees.add(_path_to_uri(path))
            except Exception:
                continue
        old_includees = state.include_graph.get(uri, set())
        state.include_graph[uri] = new_includees
        for stale in old_includees - new_includees:
            includers = state.includee_to_includers.get(stale)
            if includers is None:
                continue
            includers.discard(uri)
            if not includers:
                state.includee_to_includers.pop(stale, None)
        for added in new_includees - old_includees:
            state.includee_to_includers.setdefault(added, set()).add(uri)

    def _apply_compile_result(
        uri: str,
        buffer_text: str,
        messages: list[CompilerMessage],
        roots: list[RootNode],
        tmp_path: pathlib.Path,
        consumed_files: set[pathlib.Path],
        precomputed_conflicts: list[CompilerMessage] | None = None,
        precomputed_fingerprint: str | None = None,
        precomputed_node_index: dict[str, Any] | None = None,
        precomputed_spine: dict[str, Any] | None = None,
    ) -> bool:
        """Apply elaborate output to cache + diagnostics + viewer notification.

        Returns ``True`` when the AST genuinely changed (cascade-trigger
        consumers via reverse-dep map). ``False`` on semantic no-op
        (fingerprint match) or parse failure.

        Optional ``precomputed_*`` kwargs let callers hoist the heavy
        post-compile tree walks off the asyncio loop.
        """
        version_after: int | None = None
        ast_changed = False
        if roots:
            if precomputed_conflicts is not None:
                conflicts = precomputed_conflicts
            else:
                try:
                    conflicts = _address_conflict_diagnostics(roots, tmp_path)
                except Exception:
                    logger.debug("address-conflict scan failed", exc_info=True)
                    conflicts = []
            messages = list(messages) + conflicts

            # ``has_errors`` covers BOTH compile/parse errors AND post-
            # elaborate conflict diagnostics (address overlaps, alias
            # collisions, …). The compiler can elaborate a tree
            # successfully and still emit semantic ERROR severity for
            # things like duplicate addresses. The viewer needs to know:
            # field-reported as "alias и main с одинаковым адресом —  # noqa: RUF003
            # в Problems error, в webview ничего". We re-use the
            # ``stale_uris`` set + envelope ``stale`` flag for this
            # signal; the bar text on the viewer side already reads
            # broadly enough ("Showing last good elaboration · current
            # parse failed" → would benefit from rewording but that's
            # extension-side).
            has_errors = any(m.severity == Severity.ERROR for m in messages)
            new_fp = (
                precomputed_fingerprint
                if precomputed_fingerprint is not None
                else _fingerprint_roots(roots)
            )
            old = state.cache.get(uri)
            # T2-B fingerprint short-circuit: if the elaborated tree is byte-
            # identical (semantically) to the prior one, drop the new tmp_path
            # (we keep the prior entry's tmp_path so lazy SourceRefs keep
            # working) and skip the cache.put + notification. Diagnostics
            # still publish because they may differ across runs (e.g.,
            # severity tweaked by config) without an AST diff.
            if (
                old is not None
                and old.ast_fingerprint is not None
                and old.ast_fingerprint == new_fp
                and old.roots
            ):
                tmp_path.unlink(missing_ok=True)
                old.text = buffer_text
                old.text_canonical = _canonicalize_for_skip(buffer_text)
                old.elaborated_at = time.time()
                # symmetric stale transition (handles both T → F
                # and F → T). The latter fires when the user introduces
                # an address conflict in a buffer that produces the same
                # fingerprint as before — AST is the same but the
                # diagnostics changed.
                bumped = _apply_stale_transition(uri, old, has_errors)
                if bumped is not None:
                    version_after = bumped
                _update_include_graph(uri, consumed_files)
            else:
                # Cache takes ownership of the temp file (it backs lazy
                # src_ref reads for hover/documentSymbol). Old entry's
                # temp file is unlinked there.
                # Patch placeholder fields the pool worker couldn't know.
                # Compute target version inline so we can stuff index + spine
                # into the entry atomically through cache.put — without this,
                # an expand request landing between cache.put and the field
                # assignment sees node_index=None and triggers the slow
                # fallback walk on the main process loop.
                old = state.cache.get(uri)
                target_version = (old.version if old else 0) + 1
                if precomputed_spine is not None:
                    precomputed_spine["version"] = target_version
                    precomputed_spine["stale"] = has_errors
                state.cache.put(
                    uri, roots, buffer_text, tmp_path,
                    ast_fingerprint=new_fp,
                    node_index=precomputed_node_index or None,
                    serialized=precomputed_spine,
                )
                if has_errors:
                    state.stale_uris.add(uri)
                else:
                    state.stale_uris.discard(uri)
                cached = state.cache.get(uri)
                version_after = cached.version if cached is not None else None
                _update_include_graph(uri, consumed_files)
                ast_changed = True
        else:
            # Parse failed OR library file (no top-level addrmap). Both
            # produce empty roots; tell them apart by looking for ERROR
            # severity in messages.
            #
            # Library files (T2-A): we don't have an AST to fingerprint,
            # so we conservatively cascade to every open consumer. The
            # consumer-side buffer-equality + fingerprint short-circuits
            # absorb redundant work when the includee edit was a no-op
            # (whitespace, comment).
            #
            # Parse failure (D7): keep the previous cache entry intact
            # so hover/symbol still answer; mark stale so the next valid
            # buffer triggers a real re-elaborate. Skip cascade — we
            # don't want to ripple a known-broken state to consumers.
            tmp_path.unlink(missing_ok=True)
            had_error = any(m.severity == Severity.ERROR for m in messages)
            if had_error:
                # Tell open consumers to re-elaborate. They'll likely
                # fail too (broken includee → broken consumer parse),
                # which surfaces the stale-bar on every dependent
                # tab — without this, only the file the user typed
                # in shows stale, the consumers stay rendering their
                # last good tree with no visible warning.
                ast_changed = True
                prior_cached = state.cache.get(uri)
                if prior_cached is not None:
                    # stale F → T transition through the
                    # central helper. Originally inlined here as
                    # 9 lines of state-machine boilerplate.
                    bumped = _apply_stale_transition(uri, prior_cached, True)
                    if bumped is not None:
                        version_after = bumped
            else:
                # Library file: refresh the includee graph from this
                # buffer's perspective (it may itself include other
                # files) and signal cascade so open consumers re-pick.
                _update_include_graph(uri, consumed_files)
                ast_changed = True
        previously_affected = state.diag_affected.get(uri, set())
        state.diag_affected[uri] = _publish_diagnostics(
            server, uri, messages, previously_affected
        )

        # TODO-1 push: notify the client that a fresh elaborated tree is ready.
        # Payload is metadata-only ({uri, version}); the client decides whether
        # to fetch the full tree (rdl/elaboratedTree with sinceVersion). This
        # eliminates the user-perceptible refresh delay on save (extension
        # used to wait for didSaveTextDocument before pulling a fresh tree).
        # T2-B: only fire when ast_changed — a fingerprint-skip path leaves
        # cache.version untouched so the viewer's existing tree is current.
        if version_after is not None:
            try:
                server.protocol.notify(
                    "rdl/elaboratedTreeChanged",
                    {"uri": uri, "version": version_after},
                )
            except Exception:
                logger.debug("could not send elaboratedTreeChanged notification", exc_info=True)

        return ast_changed

    def _check_perl_pre_flight(buffer_text: str) -> None:
        """One-shot warning when source uses ``<%`` markers but ``perl`` is missing.

        systemrdl-compiler still emits its own fatal diagnostic on every compile,
        but a modal banner up front is much less confusing than a wall of "Perl
        macro expansion failed" errors with no remediation hint.
        """
        if state.perl_warning_shown:
            return
        if not _perl_in_source(buffer_text):
            return
        if _perl_available():
            return
        state.perl_warning_shown = True
        try:
            from lsprotocol.types import MessageType, ShowMessageParams
            server.window_show_message(
                ShowMessageParams(
                    type=MessageType.Warning,
                    message=(
                        "SystemRDL Perl preprocessor markers (`<% %>`) detected but "
                        "`perl` is not on PATH. Install Perl to enable preprocessor "
                        "expansion (clause 16.3 of the SystemRDL 2.0 spec)."
                    ),
                )
            )
        except Exception:
            logger.debug("could not surface perl-missing notification", exc_info=True)

    def _is_open(uri: str) -> bool:
        """True when the client has the document open in an editor.

        Used by the include-graph cascade (T2-A) to decide whether to
        proactively re-elaborate a consumer when its includee changes —
        we don't want to elaborate every workspace file for every typed
        character, only the ones the user is actively looking at.
        """
        if state.test_open_buffers is not None:
            return uri in state.test_open_buffers
        try:
            docs = getattr(server.workspace, "text_documents", None)
            if isinstance(docs, dict):
                return uri in docs
        except Exception:
            pass
        # Fallback: probe via get_text_document (may construct a phantom
        # for unopened files; if the source is non-empty assume open).
        try:
            doc = server.workspace.get_text_document(uri)
            return bool(getattr(doc, "source", None))
        except Exception:
            return False

    def _trigger_includer_cascade(changed_uri: str) -> None:
        """Re-elaborate every open includer of ``changed_uri`` (T2-A).

        Called after a successful AST-changing elaborate so an edit to
        a library file (``types.rdl``) ripples through to every open
        consumer (``stress_25k.rdl``) without the user closing and
        re-opening tabs. Marks each consumer ``stale`` so the buffer-
        equality short-circuit doesn't pre-empt the recursion. The
        cascade naturally terminates: each consumer's own elaborate
        either AST-changes (triggering its own consumers — finite
        DAG) or fingerprint-skips (no further cascade).
        """
        deps = state.includee_to_includers.get(changed_uri)
        if not deps:
            return
        for dep_uri in tuple(deps):
            if dep_uri == changed_uri or not _is_open(dep_uri):
                continue
            dep_text = _read_buffer(dep_uri)
            if dep_text is None:
                continue
            # ``force_re_elaborate`` (NOT ``stale_uris``) so the
            # buffer-equality / canonicalize-skip short-circuits know
            # to bypass — but the stale-transition detector in
            # ``_apply_compile_result`` still sees an honest False
            # before this elaborate runs, so a real new failure of
            # the consumer does push a notification.
            state.force_re_elaborate.add(dep_uri)
            asyncio.create_task(_full_pass_async(dep_uri, dep_text))

    async def _full_pass_async(uri: str, buffer_text: str | None) -> None:
        if buffer_text is None:
            try:
                buffer_text = _uri_to_path(uri).read_text(encoding="utf-8")
            except (OSError, ValueError):
                return

        # T2-C: per-URI mutex. Replaces the TODO(T2) marker that used to
        # warn about same-URI didOpen+didSave races. Different URIs are
        # not serialized through this lock — cross-URI throughput is still
        # subject to GIL contention (separate investigation).
        lock = state.uri_elab_locks.setdefault(uri, asyncio.Lock())
        t_acquire = time.monotonic()
        async with lock:
            t_locked = time.monotonic()
            if t_locked - t_acquire > 0.05:
                logger.debug(
                    "_full_pass_async %s: waited %.3fs for uri lock",
                    uri, t_locked - t_acquire,
                )

            # If a cascade trigger queued this elaborate, it left
            # ``force_re_elaborate`` set — bypass both short-circuits
            # below, then clear the marker so subsequent same-URI
            # passes resume normal short-circuit behaviour. Reading
            # the marker once + clearing immediately keeps the
            # bypass scope tight to "this one elaborate".
            forced = uri in state.force_re_elaborate
            state.force_re_elaborate.discard(uri)

            # Short-circuit 1 (T1): if the buffer text is byte-identical to
            # the last successful elaboration we already have a fresh tree
            # for this URI. Eliminates the GIL-pinning re-runs that pile up
            # when VSCode fires duplicate didOpens on workspace restore.
            # Costs one string equality check; saves seconds.
            prior = state.cache.get(uri)
            if (
                prior is not None
                and prior.text == buffer_text
                and uri not in state.stale_uris
                and not forced
            ):
                return

            # Short-circuit 2 (T2-D): if the buffer differs from the last
            # elaboration only in whitespace/comments, the elaborate would
            # produce identical AST. Skip the entire pipeline — no
            # elaboration_started notification, so the viewer banner never
            # flashes on a space character typed in stress_25k_multi.rdl.
            # Refresh ``prior.text`` so the next exact-equality check hits.
            if (
                prior is not None
                and prior.text_canonical is not None
                and uri not in state.stale_uris
                and not forced
            ):
                buffer_canon = _canonicalize_for_skip(buffer_text)
                if buffer_canon == prior.text_canonical:
                    prior.text = buffer_text
                    prior.elaborated_at = time.time()
                    logger.debug(
                        "_full_pass_async %s: canonical-equal short-circuit "
                        "(elaborate skipped, no banner)",
                        uri,
                    )
                    return

            _check_perl_pre_flight(buffer_text)

            # ``started_notified`` + try/finally below guarantee
            # ``rdl/elaborationFinished`` fires on EVERY exit path —
            # including ``CancelledError`` from LSP shutdown or a rapid
            # restart. Pre-T4-A cancellation propagated up
            # unconditionally and the viewer's "re-elaborating" spinner
            # stayed visible until window reload, plus the subprocess
            # tmp file was leaked because no done-callback was attached
            # in the cancel path.
            started_notified = False
            try:
                server.protocol.notify("rdl/elaborationStarted", {"uri": uri})
                started_notified = True
            except Exception:
                logger.debug("could not send elaborationStarted", exc_info=True)

            loop = asyncio.get_running_loop()
            # T3: route elaborate through the cross-process pool when
            # available, otherwise fall back to the asyncio default
            # ThreadPoolExecutor (legacy behaviour, preserved as an
            # escape hatch via systemrdl-pro.elaborateInProcess=true).
            # Pool path returns a zlib-compressed pickle bytes blob; we
            # decompress in this process. Wrap whichever future kind
            # we got with asyncio.shield so wait_for can time out
            # without cancelling the underlying compile work — the
            # done-callback cleans up the orphan tmp file when the
            # late result eventually arrives.
            t_submit = time.monotonic()
            # track the latest pool future at this scope so the
            # outer cancellation handler can register a tmp-cleanup
            # callback on it. Reassigned by ``_submit_compile`` on
            # every BrokenProcessPool retry.
            pool_fut: Any = None
            ast_changed = False

            def _register_late_cleanup(pf: Any) -> None:
                """Attach a done-callback that unlinks the subprocess's
                tmp file when the (still in-flight) future eventually
                finishes. Used for the cancellation path AND the
                timeout path — both leave the subprocess running past
                the point where the parent stopped waiting, with a
                tmp file that has no other owner. ``add_done_callback``
                fires immediately if ``pf`` is already done, so this
                also covers the rare race where cancellation and
                completion arrive in the same tick.
                """
                if pf is None:
                    return
                def _cleanup(f: Any) -> None:
                    try:
                        result = f.result()
                    except BaseException:
                        return
                    if isinstance(result, (bytes, bytearray)):
                        try:
                            decoded = _decompress_compile_result(result)
                        except Exception:
                            return
                        late_tmp = decoded[2] if len(decoded) >= 3 else None
                    else:
                        late_tmp = result[2] if len(result) >= 3 else None
                    if late_tmp is None:
                        return
                    try:
                        late_tmp.unlink(missing_ok=True)
                    except OSError:
                        pass
                try:
                    pf.add_done_callback(_cleanup)
                except Exception:
                    logger.debug("could not attach late-cleanup callback", exc_info=True)

            def _submit_compile() -> tuple[bool, asyncio.Future, Any]:
                """Submit one elaborate attempt; return
                ``(using_pool, awaitable, raw_pool_future)``. Pulled
                into a helper so the BrokenProcessPool recovery path
                can re-submit cleanly after respawning the pool."""
                up = (
                    not state.elaborate_in_process
                    and state.elaborate_pool is not None
                )
                pf: Any = None
                if up:
                    pf = state.elaborate_pool.submit(
                        _compile_text_compressed,
                        uri,
                        buffer_text,
                        state.include_paths,
                        state.include_vars,
                        state.perl_safe_opcodes,
                    )
                    awaitable = asyncio.wrap_future(pf)
                else:
                    awaitable = loop.run_in_executor(
                        None,
                        _compile_text,
                        uri,
                        buffer_text,
                        state.include_paths,
                        state.include_vars,
                        state.perl_safe_opcodes,
                    )
                return up, awaitable, pf

            # T3-E: BrokenProcessPool is a real production gap — a
            # subprocess that segfaults on a pathological RDL or gets
            # killed by an OOM reaper would otherwise poison every
            # subsequent elaborate until the user manually restarts
            # the LSP. The exception can fire at TWO points:
            #
            #   - ``ProcessPoolExecutor.submit()`` if the pool already
            #     knows it's broken (a previous worker died and the
            #     pool noticed before we arrived).
            #   - ``Future.result()`` (i.e. our ``await``) if the
            #     worker dies mid-elaborate.
            #
            # Both paths share the same recovery: tear down the dead
            # pool, spawn a fresh one, retry the whole elaborate ONCE.
            # If the retry also fails, surface the error normally so
            # we don't loop forever on a deterministically broken
            # input.
            recovery_attempted = False
            timeout_hit = False
            unexpected_exc = False
            broken_pool_excs = (BrokenProcessPool, BrokenPipeError, EOFError)
            while True:
                try:
                    using_pool, fut, pool_fut = _submit_compile()
                except broken_pool_excs as exc:
                    if recovery_attempted:
                        logger.exception(
                            "elaborate pool stuck broken after recovery; "
                            "giving up on %s", uri,
                        )
                        _emit_elaboration_finished(uri)
                        return
                    recovery_attempted = True
                    logger.warning(
                        "elaborate pool broken at submit (%s: %s); respawning",
                        type(exc).__name__, exc,
                    )
                    _shutdown_elaborate_pool()
                    _ensure_elaborate_pool()
                    continue
                try:
                    raw_result = await asyncio.wait_for(
                        asyncio.shield(fut), timeout=state.elaboration_timeout_s
                    )
                    if using_pool:
                        # Hoist zlib+pickle.loads off the main asyncio
                        # loop. On a 25k-reg blob (~6 MB compressed) the
                        # decode is ~2 s of pure-Python work; running it
                        # synchronously here pinned the GIL and made
                        # every other in-flight LSP request wait —
                        # observed in field traces as multi-second expand
                        # latency on small files concurrent with a big
                        # file's compile completion.
                        (
                            messages,
                            roots,
                            tmp_path,
                            consumed_files,
                            node_index,
                            spine_envelope,
                        ) = await asyncio.to_thread(
                            _decompress_compile_result, raw_result
                        )
                    else:
                        (
                            messages,
                            roots,
                            tmp_path,
                            consumed_files,
                            node_index,
                            spine_envelope,
                        ) = raw_result
                    break
                except asyncio.CancelledError:
                    # LSP shutdown / pygls task-cancel arrived
                    # mid-elaborate. The shielded subprocess is still
                    # running and will eventually emit its tmp file —
                    # register a callback so the tmp gets unlinked
                    # when it does. Also pair the elaborationStarted
                    # notification with Finished so the viewer's
                    # spinner clears (the next session reload would
                    # otherwise inherit a stale spinner state from
                    # the persisted webview). Re-raise so the
                    # cancellation propagates to whatever requested it.
                    _register_late_cleanup(pool_fut)
                    if started_notified:
                        _emit_elaboration_finished(uri)
                    raise
                except broken_pool_excs as exc:
                    if recovery_attempted or not using_pool:
                        logger.exception(
                            "elaborate pool dead after recovery attempt; "
                            "elaborate failing on %s", uri,
                        )
                        _emit_elaboration_finished(uri)
                        return
                    recovery_attempted = True
                    logger.warning(
                        "elaborate pool broken in flight (%s: %s); respawning + retry",
                        type(exc).__name__, exc,
                    )
                    _shutdown_elaborate_pool()
                    _ensure_elaborate_pool()
                    continue
                except asyncio.TimeoutError:
                    timeout_hit = True
                    break
                except Exception:
                    logger.exception(
                        "unexpected error during async full-pass for %s", uri
                    )
                    unexpected_exc = True
                    break
            if timeout_hit:
                logger.warning(
                    "elaborate timeout on %s after %.0fs; keeping last-good",
                    uri, state.elaboration_timeout_s,
                )

                def _drop_late_result(f: Any) -> None:
                    # The future may raise (compile error → no tmp produced)
                    # or be cancelled. We need to unlink the late tmp only
                    # when it actually exists. ``except BaseException``
                    # catches CancelledError on shutdown and any
                    # KeyboardInterrupt; the unlink is a fire-and-forget
                    # cleanup that must never escape this callback.
                    #
                    # T3: result shape depends on which executor produced
                    # it. ThreadPoolExecutor path returns the raw 4-tuple;
                    # ProcessPoolExecutor path returns a zlib+pickle bytes
                    # blob and the embedded tmp path lives inside the
                    # decompressed pickle. Detect by type so this works
                    # whichever path the timeout fired against.
                    try:
                        result = f.result()
                    except BaseException:
                        return
                    if isinstance(result, (bytes, bytearray)):
                        try:
                            decoded = _decompress_compile_result(result)
                        except Exception:
                            logger.debug(
                                "late-result decompress failed", exc_info=True,
                            )
                            return
                        late_tmp = decoded[2] if len(decoded) >= 3 else None
                    else:
                        late_tmp = result[2] if len(result) >= 3 else None
                    if late_tmp is None:
                        return
                    try:
                        late_tmp.unlink(missing_ok=True)
                    except OSError:
                        pass

                # asyncio.shield()'s result is an asyncio Future; the
                # original concurrent.futures Future from the pool is
                # in pool_fut. Attach to whichever is the underlying
                # work-tracking handle so the cleanup fires when the
                # subprocess actually completes (not when the shielded
                # asyncio handle thinks it did).
                if pool_fut is not None:
                    pool_fut.add_done_callback(_drop_late_result)
                else:
                    fut.add_done_callback(_drop_late_result)
                cached_for_timeout = state.cache.get(uri)
                if cached_for_timeout is not None:
                    bumped = _apply_stale_transition(
                        uri, cached_for_timeout, True,
                    )
                    if bumped is not None:
                        try:
                            server.protocol.notify(
                                "rdl/elaboratedTreeChanged",
                                {"uri": uri, "version": bumped},
                            )
                        except Exception:
                            logger.debug(
                                "stale-state notify failed (timeout)",
                                exc_info=True,
                            )
                try:
                    target_path = _uri_to_path(uri)
                except ValueError:
                    _emit_elaboration_finished(uri)
                    return
                timeout_msg = CompilerMessage(
                    severity=Severity.ERROR,
                    text=(
                        f"systemrdl-lsp: elaborate exceeded {state.elaboration_timeout_s:.0f}s — "
                        "viewer is showing last-good tree. Increase the cap with "
                        "systemrdl-pro.elaborationTimeoutMs if your design legitimately "
                        "needs longer."
                    ),
                    file_path=target_path,
                    line_1b=1,
                    col_start_1b=1,
                    col_end_1b=1,
                )
                previously_affected = state.diag_affected.get(uri, set())
                state.diag_affected[uri] = _publish_diagnostics(
                    server, uri, [timeout_msg], previously_affected
                )
                _emit_elaboration_finished(uri)
                return
            if unexpected_exc:
                _emit_elaboration_finished(uri)
                return
            t_compile_done = time.monotonic()
            # Run conflicts + fingerprint walks off the asyncio loop so
            # they don't block other URIs' apply on a 25k-reg tree.
            precomputed_conflicts: list[CompilerMessage] | None = None
            precomputed_fp: str | None = None
            if roots:
                def _heavy_walks() -> tuple[list[CompilerMessage], str]:
                    try:
                        conflicts_local = _address_conflict_diagnostics(
                            roots, tmp_path
                        )
                    except Exception:
                        logger.debug("address-conflict scan failed", exc_info=True)
                        conflicts_local = []
                    fp_local = _fingerprint_roots(roots)
                    return conflicts_local, fp_local
                try:
                    precomputed_conflicts, precomputed_fp = await asyncio.to_thread(
                        _heavy_walks
                    )
                except Exception:
                    logger.debug("post-compile walks failed", exc_info=True)
            t_apply_start = time.monotonic()
            ast_changed = _apply_compile_result(
                uri,
                buffer_text,
                messages,
                roots,
                tmp_path,
                consumed_files,
                precomputed_conflicts=precomputed_conflicts,
                precomputed_fingerprint=precomputed_fp,
                precomputed_node_index=node_index,
                precomputed_spine=spine_envelope,
            )
            t_apply_done = time.monotonic()
            logger.debug(
                "_full_pass_async %s: compile=%.3fs apply=%.3fs ast_changed=%s",
                uri,
                t_compile_done - t_submit,
                t_apply_done - t_apply_start,
                ast_changed,
            )
            # T3-G: count successful subprocess elaborates and recycle
            # the pool once we cross the leak-mitigation threshold.
            # Recycle is scheduled for AFTER this elaborate's
            # _emit_elaboration_finished — workers fully drained.
            recycle_pool_now = False
            if using_pool:
                state.pool_elaborate_count += 1
                if state.pool_elaborate_count >= state.pool_max_elaborates:
                    recycle_pool_now = True
            _emit_elaboration_finished(uri)
            if recycle_pool_now:
                logger.info(
                    "T3-G: recycling elaborate pool after %d elaborates "
                    "to release leaked memory",
                    state.pool_elaborate_count,
                )
                _shutdown_elaborate_pool()
                _ensure_elaborate_pool()
        # T2-A cascade fires *outside* the URI lock so we don't deadlock on
        # an A↔B include cycle (impossible at the SystemRDL level, but the
        # lock ordering shouldn't depend on it). Each consumer takes its own
        # lock; if one is currently elaborating, the cascade naturally
        # serializes there.
        if ast_changed:
            _trigger_includer_cascade(uri)

    def _emit_elaboration_finished(uri: str) -> None:
        """Notify clients that an in-flight elaborate completed (any outcome).

        Paired with rdl/elaborationStarted in :func:`_full_pass_async` so the
        viewer can clear its "re-elaborating" indicator regardless of whether
        the pass succeeded, timed out, or threw. Best-effort.
        """
        try:
            server.protocol.notify("rdl/elaborationFinished", {"uri": uri})
        except Exception:
            logger.debug("could not send elaborationFinished", exc_info=True)

    _PREINDEX_EXCLUDE_DIRS = {
        ".git", "node_modules", ".venv", "venv", "dist",
        "build", "out", "__pycache__",
    }

    async def _preindex_workspace() -> None:
        """Walk every workspace folder for ``.rdl`` files and pre-elaborate them.

        Disabled by default. Enable with ``systemrdl-pro.preindex.enabled``.

        Trade-off: ``workspace/symbol`` (Ctrl+T) only sees files the LSP has
        touched, so a cold workspace finds nothing. Pre-warming fixes that
        but parallel pre-indexes across multiple VSCode windows can peg the
        CPU. We default off; users who care about cross-file search opt in.

        When enabled: serial (one compile at a time), delayed by 5s after
        startup so it doesn't compete with the editor's own initial activity.
        """
        if not state.preindex_enabled:
            return
        await asyncio.sleep(5.0)  # let initial editor activity settle
        try:
            folders = list(server.workspace.folders.values()) if server.workspace.folders else []
        except Exception:
            folders = []
        if not folders:
            return

        rdl_paths: list[pathlib.Path] = []
        for folder in folders:
            try:
                root = _uri_to_path(folder.uri)
            except (ValueError, AttributeError):
                continue
            for path in _iter_rdl_files(root, _PREINDEX_EXCLUDE_DIRS):
                rdl_paths.append(path)
                if len(rdl_paths) >= state.preindex_max_files:
                    break
            if len(rdl_paths) >= state.preindex_max_files:
                logger.info(
                    "preindex hit cap %d files; remaining .rdl skipped",
                    state.preindex_max_files,
                )
                break

        logger.info("preindex: %d .rdl files queued (serial)", len(rdl_paths))
        for path in rdl_paths:
            uri = path.as_uri()
            if state.cache.get(uri) is not None:
                continue
            try:
                text = await asyncio.get_running_loop().run_in_executor(
                    None, lambda p=path: p.read_text(encoding="utf-8", errors="replace"),
                )
            except OSError:
                continue
            try:
                await _full_pass_async(uri, text)
            except Exception:
                logger.exception("preindex failed for %s", uri)
        logger.info("preindex: done (%d files)", len(rdl_paths))

    async def _debounced_full_pass(uri: str) -> None:
        try:
            await asyncio.sleep(DEBOUNCE_SECONDS)
        except asyncio.CancelledError:
            return
        try:
            await _full_pass_async(uri, _read_buffer(uri))
        finally:
            state.pending.pop(uri, None)

    async def _read_workspace_config() -> dict[str, Any] | None:
        """Fetch the systemrdl-pro section of the workspace config.

        Returns the dict on success, ``None`` if the client returned
        nothing or the response wasn't a dict. Raises only on the
        underlying transport failure — callers wrap in try/except.
        """
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
            return configs[0]
        return None

    def _apply_workspace_config(cfg: dict[str, Any], *, is_initial: bool) -> None:
        """Push values from a workspace-config dict into ServerState.

        Shared by ``_on_initialized`` (initial fetch) and
        ``_on_config_change`` (live update). Pre-T4-C the same
        ~30 lines of parsing lived in two places and drifted with
        every new setting.

        ``is_initial`` controls two things: log every value at INFO
        on first read (operators want a startup snapshot), and reload
        ``preindex.*`` only on the initial pass (mid-session preindex
        toggles need an explicit restart anyway since the cache state
        differs).
        """
        paths = cfg.get("includePaths") or []
        state.include_paths = [str(p) for p in paths if p]
        raw_vars = cfg.get("includeVars") or {}
        if isinstance(raw_vars, dict):
            state.include_vars = {str(k): str(v) for k, v in raw_vars.items()}
        raw_opcodes = cfg.get("perlSafeOpcodes") or []
        if isinstance(raw_opcodes, list):
            state.perl_safe_opcodes = [str(op) for op in raw_opcodes if op]
        if is_initial:
            preindex_cfg = cfg.get("preindex") or {}
            if isinstance(preindex_cfg, dict):
                if "enabled" in preindex_cfg:
                    state.preindex_enabled = bool(preindex_cfg["enabled"])
                max_files = preindex_cfg.get("maxFiles")
                if isinstance(max_files, int) and max_files > 0:
                    state.preindex_max_files = max_files
        timeout_ms = cfg.get("elaborationTimeoutMs")
        if isinstance(timeout_ms, (int, float)) and timeout_ms > 0:
            state.elaboration_timeout_s = max(
                ELABORATION_TIMEOUT_SECONDS_MIN,
                min(ELABORATION_TIMEOUT_SECONDS_MAX, timeout_ms / 1000.0),
            )
        if "elaborateInProcess" in cfg:
            new_in_proc = bool(cfg.get("elaborateInProcess"))
            if not is_initial and new_in_proc != state.elaborate_in_process:
                logger.info(
                    "elaborateInProcess change observed (was=%s now=%s); "
                    "restart the LSP for it to take effect "
                    "(SystemRDL: Restart Language Server)",
                    state.elaborate_in_process, new_in_proc,
                )
            state.elaborate_in_process = new_in_proc
        if is_initial:
            logger.info("includePaths: %s", state.include_paths)
            logger.info("includeVars: %s", list(state.include_vars))
            logger.info("perlSafeOpcodes override: %s", state.perl_safe_opcodes)
            logger.info(
                "preindex enabled=%s max=%d",
                state.preindex_enabled, state.preindex_max_files,
            )
            logger.info("elaboration timeout: %.1fs", state.elaboration_timeout_s)

    @server.feature(INITIALIZED)
    def _on_initialized(_ls: LanguageServer, _params: InitializedParams) -> None:
        # Detect lazy-tree client capability before answering any
        # rdl/elaboratedTree request. The capability lives under
        # `experimental.systemrdlLazyTree` per the design doc; clients that
        # don't set it get the v0.1-shaped full tree (backward compat).
        try:
            caps = server.client_capabilities
            exp = getattr(caps, "experimental", None)
            if isinstance(exp, dict):
                state.lazy_supported = bool(exp.get("systemrdlLazyTree", False))
        except Exception:
            logger.debug("could not read client capabilities", exc_info=True)
        logger.info("lazy tree supported by client: %s", state.lazy_supported)

        async def fetch() -> None:
            try:
                cfg = await _read_workspace_config()
                if cfg is not None:
                    _apply_workspace_config(cfg, is_initial=True)
            except Exception:
                logger.debug("initial workspace config read failed", exc_info=True)
            # Pool init AFTER config so the elaborateInProcess override
            # is honoured. Best-effort — falls back to in-thread on failure.
            _ensure_elaborate_pool()
            # Pre-index runs with whatever config we got (defaults on failure
            # are better than nothing — they still warm the cache).
            asyncio.ensure_future(_preindex_workspace())

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

    @server.feature(TEXT_DOCUMENT_DID_CLOSE)
    def _on_close(_ls: LanguageServer, params: DidCloseTextDocumentParams) -> None:
        """Per-URI cleanup when a document closes.

        - Cancel any pending debounce task so we don't elaborate against
          stale workspace state moments after close (the buffer might
          already be gone from pygls' workspace).
        - Drop the elaboration lock if not held. If currently held, leave
          it; the in-flight elaborate completes naturally and the next
          close cleans up.

        ``include_graph`` / ``includee_to_includers`` / ``stale_uris``
        are intentionally NOT cleared: a workspace-symbol search or
        re-opened consumer is still served correctly by stale-but-
        bounded data, and clearing would force redundant re-elaborates
        on every tab close.
        """
        uri = params.text_document.uri
        existing = state.pending.pop(uri, None)
        if existing is not None:
            existing.cancel()
        lock = state.uri_elab_locks.get(uri)
        if lock is not None and not lock.locked():
            state.uri_elab_locks.pop(uri, None)

    @server.feature("$/cancelRequest")
    def _on_cancel(_ls: LanguageServer, _params: Any) -> None:
        # pygls 2.x logs noisy warnings for cancel notifications that arrive after
        # the request already completed. A no-op handler keeps the channel quiet.
        return None

    @server.feature("workspace/didChangeWatchedFiles")
    def _on_watched_files_changed(_ls: LanguageServer, _params: Any) -> None:
        # VSCode fires this notification whenever any tracked file changes
        # on disk. We don't react to disk changes (the include cascade
        # already covers cross-file invalidation through the buffer-edit
        # path), but pygls 2.x logs an [WARNING] for every unhandled
        # notification. Empty handler suppresses the noise.
        return None

    @server.feature(WORKSPACE_DID_CHANGE_CONFIGURATION)
    def _on_config_change(
        _ls: LanguageServer, _params: DidChangeConfigurationParams
    ) -> None:
        async def refresh() -> None:
            try:
                cfg = await _read_workspace_config()
                if cfg is not None:
                    _apply_workspace_config(cfg, is_initial=False)
            except Exception:
                logger.debug("config refresh failed", exc_info=True)

        asyncio.ensure_future(refresh())

    @server.feature("textDocument/hover")
    def _on_hover(_ls: LanguageServer, params: Any) -> Any | None:
        from lsprotocol.types import Hover, MarkupContent, MarkupKind

        cached = state.cache.get(params.text_document.uri)
        if cached is None or not cached.roots:
            return None

        line, char = params.position.line, params.position.character

        # 1. Instance lookup first — gives the richest answer when it hits.
        # pass the LSP buffer-cache line reader through so
        # _property_origin_hint avoids the synchronous disk read on the
        # event loop. Falls back to disk inside hover.py if the reader
        # comes up empty.
        node = _node_at_position(cached.roots, line, char)
        markdown = (
            _hover_text_for_node(node, _file_line_reader)
            if node is not None else None
        )

        # 2-3. Word-based catalogue lookup for keywords / properties / access values / type names.
        if markdown is None:
            word = _word_at_position(cached.text, line, char)
            if word:
                markdown = _hover_for_word(word, cached.roots)

        if markdown is None:
            return None
        return Hover(contents=MarkupContent(kind=MarkupKind.Markdown, value=markdown))

    # NOTE: these handlers used to be sync `def`. Pygls calls sync handlers
    # directly on the asyncio event loop, blocking it for the full handler
    # duration. On a 25k-reg design that meant `documentSymbol`, `foldingRange`,
    # `inlayHint`, `codeLens`, and `workspace/symbol` each held the loop
    # for several seconds — and any concurrent request (including expand)
    # waited that whole time. Field-reported as "click reg in features
    # waits 8+ seconds because LSP is busy walking stress's tree". Async
    # wrappers + asyncio.to_thread offload the CPU walk so the loop stays
    # responsive.
    @server.feature("textDocument/documentSymbol")
    async def _on_document_symbol(_ls: LanguageServer, params: Any) -> list[Any]:
        cached = state.cache.get(params.text_document.uri)
        if cached is None or not cached.roots:
            return []
        return await asyncio.to_thread(_document_symbols, cached.roots)

    @server.feature("textDocument/foldingRange")
    async def _on_folding(_ls: LanguageServer, params: Any) -> list[FoldingRange]:
        cached = state.cache.get(params.text_document.uri)
        text = cached.text if cached is not None else _read_buffer(params.text_document.uri) or ""
        return await asyncio.to_thread(_folding_ranges_from_text, text)

    @server.feature("textDocument/inlayHint")
    async def _on_inlay_hint(_ls: LanguageServer, params: Any) -> list[InlayHint]:
        cached = state.cache.get(params.text_document.uri)
        if cached is None or not cached.roots:
            return []
        try:
            target_path = _uri_to_path(params.text_document.uri)
        except ValueError:
            return []
        path = cached.temp_path or target_path
        return await asyncio.to_thread(
            _inlay_hints_for_addressables, cached.roots, path, cached.text,
        )

    @server.feature("textDocument/codeLens")
    async def _on_code_lens(_ls: LanguageServer, params: Any) -> list[CodeLens]:
        cached = state.cache.get(params.text_document.uri)
        if cached is None or not cached.roots:
            return []
        path = cached.temp_path or _uri_to_path(params.text_document.uri)
        return await asyncio.to_thread(_code_lenses_for_addrmaps, cached.roots, path)

    @server.feature("workspace/symbol")
    async def _on_workspace_symbol(_ls: LanguageServer, params: Any) -> list[SymbolInformation]:
        query = getattr(params, "query", "") or ""
        entries = list(state.cache._entries.items())
        def collect() -> list[SymbolInformation]:
            out: list[SymbolInformation] = []
            for uri, entry in entries:
                if entry.roots:
                    out.extend(_workspace_symbols_for_uri(uri, entry.roots, query))
            return out
        return await asyncio.to_thread(collect)

    @server.feature("textDocument/completion")
    def _on_completion(_ls: LanguageServer, params: Any) -> Any:
        from lsprotocol.types import CompletionList

        cached = state.cache.get(params.text_document.uri)
        text = cached.text if cached is not None else _read_buffer(params.text_document.uri) or ""
        ctx = _completion_context(text, params.position.line, params.position.character)
        if ctx != "general":
            return CompletionList(is_incomplete=False, items=_completion_items_for_context(ctx))
        items = _completion_items_static()
        if cached is not None and cached.roots:
            items.extend(_completion_items_for_types(cached.roots))
            items.extend(_completion_items_for_user_properties(cached.roots))
        return CompletionList(is_incomplete=False, items=items)

    def _file_line_reader(path: pathlib.Path, line_idx: int) -> str | None:
        """Read line N from a workspace file, preferring the LSP buffer cache.

        If the user has the file open with unsaved edits, the LSP's text-document
        cache reflects those edits — use it so rename operates on what the user
        actually sees. Falls back to disk for files not currently open.
        """
        # Try LSP buffer cache first (active edit). The cache may carry the
        # primary URI for an in-flight compile, but other files we read from
        # disk; that's fine because rename's scope is single-document edits
        # plus cross-file edits to disk-resident `\\\`include`d files.
        target_uri = path.resolve().as_uri()
        text = _read_buffer(target_uri)
        if text is None:
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, ValueError):
                return None
        lines = text.splitlines()
        if 0 <= line_idx < len(lines):
            return lines[line_idx]
        return None

    @server.feature("textDocument/prepareRename")
    def _on_prepare_rename(_ls: LanguageServer, params: Any) -> Any | None:
        """Validate that the cursor is on a renameable identifier.

        We only rename top-level component type names — instance names and
        keywords return None so VSCode shows "You cannot rename this element".
        """
        from lsprotocol.types import Position
        from lsprotocol.types import Range as LSPRange

        uri = params.text_document.uri
        cached = state.cache.get(uri)
        if cached is None or not cached.roots:
            return None
        word = _word_at_position(cached.text, params.position.line, params.position.character)
        if not word:
            return None
        defs = _comp_defs_from_cached(cached.roots)
        if word not in defs:
            return None
        # Return the cursor-line range of the word so VSCode anchors its
        # rename input box correctly. The actual edits get computed in
        # textDocument/rename below.
        line = cached.text.splitlines()[params.position.line] if params.position.line < len(
            cached.text.splitlines()
        ) else ""
        import re as _re
        m = _re.search(rf"\b{_re.escape(word)}\b", line)
        if m is None:
            return None
        return LSPRange(
            start=Position(line=params.position.line, character=m.start()),
            end=Position(line=params.position.line, character=m.end()),
        )

    @server.feature("textDocument/rename")
    def _on_rename(_ls: LanguageServer, params: Any) -> Any | None:
        """Compute a workspace edit that renames a type across declaration + uses."""
        from lsprotocol.types import TextEdit, WorkspaceEdit

        uri = params.text_document.uri
        cached = state.cache.get(uri)
        if cached is None or not cached.roots:
            return None
        word = _word_at_position(cached.text, params.position.line, params.position.character)
        if not word:
            return None
        new_name = getattr(params, "new_name", None) or ""
        # SystemRDL identifiers: [A-Za-z_][A-Za-z0-9_]*. Reject anything else
        # so the user gets the standard VSCode "Can't rename" hint instead of
        # a corrupt buffer.
        if not new_name or not _is_valid_identifier(new_name):
            return None

        defs = _comp_defs_from_cached(cached.roots)
        if word not in defs:
            return None
        if new_name in defs:
            # Collision with another existing type — refuse rather than
            # silently shadow.
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
        locs = _rename_locations(word, cached.roots, translate, _file_line_reader)
        if not locs:
            return None

        # Bucket edits by URI; LSP WorkspaceEdit.changes is a map[uri, TextEdit[]].
        per_uri: dict[str, list[TextEdit]] = {}
        for loc in locs:
            per_uri.setdefault(loc.uri, []).append(
                TextEdit(range=loc.range, new_text=new_name)
            )
        return WorkspaceEdit(changes=per_uri)

    @server.feature("textDocument/documentHighlight")
    def _on_document_highlight(_ls: LanguageServer, params: Any) -> list[Any]:
        """Highlight every textual occurrence of the identifier under the cursor.

        Implementation: regex-find every `\\b<word>\\b` match in the buffer.
        Cheap and matches user expectation (highlight = same lexical token).
        """
        from lsprotocol.types import DocumentHighlight, DocumentHighlightKind

        uri = params.text_document.uri
        cached = state.cache.get(uri)
        text = cached.text if cached is not None else _read_buffer(uri) or ""
        word = _word_at_position(text, params.position.line, params.position.character)
        if not word:
            return []
        import re as _re
        pattern = _re.compile(rf"\b{_re.escape(word)}\b")
        out: list[DocumentHighlight] = []
        for line_idx, line in enumerate(text.splitlines()):
            for m in pattern.finditer(line):
                out.append(
                    DocumentHighlight(
                        range=Range(
                            start=Position(line=line_idx, character=m.start()),
                            end=Position(line=line_idx, character=m.end()),
                        ),
                        kind=DocumentHighlightKind.Text,
                    )
                )
        return out

    @server.feature("textDocument/selectionRange")
    def _on_selection_range(_ls: LanguageServer, params: Any) -> list[Any]:
        """Smart selection: expand cursor → word → enclosing `{...}` block(s) → file.

        Pure textual implementation walks outward through brace pairs, no
        elaboration dependency. Lets the user expand a selection from a
        field name through its containing reg, regfile, addrmap, etc. with
        Shift+Alt+Right.
        """
        from lsprotocol.types import SelectionRange as LSPSelectionRange

        uri = params.text_document.uri
        cached = state.cache.get(uri)
        text = cached.text if cached is not None else _read_buffer(uri) or ""
        if not text:
            return []
        lines = text.splitlines()
        out: list[Any] = []
        for pos in params.positions:
            ranges = _build_selection_ranges(text, lines, pos.line, pos.character)
            if not ranges:
                out.append(LSPSelectionRange(
                    range=Range(start=pos, end=pos),
                    parent=None,
                ))
                continue
            # Build LSP linked list: innermost first, parent pointers up.
            parent: Any = None
            for rng in ranges:
                parent = LSPSelectionRange(range=rng, parent=parent)
            out.append(parent)
        return out

    @server.feature(
        "textDocument/signatureHelp",
        SignatureHelpOptions(trigger_characters=["#", "("]),
    )
    def _on_signature_help(_ls: LanguageServer, params: Any) -> Any | None:
        """Signature help inside `#(...)` parametrized type instantiation.

        Lightweight: when the cursor is inside a ``#(...)`` after a known
        parametrized type, show its parameter names as the signature label.
        Returns None for non-matching contexts, no error.
        """
        try:
            uri = params.text_document.uri
            cached = state.cache.get(uri)
            text = cached.text if cached is not None else _read_buffer(uri) or ""
            if not text:
                return None
            split_lines = text.splitlines()
            line_text = (
                split_lines[params.position.line]
                if params.position.line < len(split_lines)
                else ""
            )
            prefix = line_text[: params.position.character]
            # Match `<TYPE> #(` immediately before the cursor (allowing stuff inside the parens).
            import re as _re
            m = _re.search(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*#\s*\([^)]*$", prefix)
            if not m:
                return None
            type_name = m.group(1)
            if cached is None or not cached.roots:
                return None
            defs = _comp_defs_from_cached(cached.roots)
            comp = defs.get(type_name)
            if comp is None:
                return None
            params_list = list(getattr(comp, "parameters", []) or [])
            if not params_list:
                return None
            sig_label = f"{type_name} #({', '.join(p.name for p in params_list)})"
            return SignatureHelp(
                signatures=[
                    SignatureInformation(
                        label=sig_label,
                        parameters=[
                            ParameterInformation(label=p.name)
                            for p in params_list
                        ],
                    )
                ],
                active_signature=0,
                active_parameter=0,
            )
        except Exception:
            logger.debug("signatureHelp handler failed", exc_info=True)
            return None

    @server.feature("textDocument/prepareTypeHierarchy")
    def _on_prepare_type_hierarchy(_ls: LanguageServer, params: Any) -> list[Any] | None:
        """Anchor for a type-hierarchy request — returns the item the user picked.

        SystemRDL has no inheritance chain, so we treat "subtypes" as
        "instantiations" (where this type is used). The anchor is the type's
        declaration; subtypes is the list of instance source locations.
        """
        from lsprotocol.types import SymbolKind, TypeHierarchyItem

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
        loc = _definition_location(comp, translate)
        if loc is None:
            return None
        kind_label = type(comp).__name__.lower()
        return [
            TypeHierarchyItem(
                name=word,
                kind=SymbolKind.Class,
                uri=loc.uri,
                range=loc.range,
                selection_range=loc.range,
                detail=kind_label,
                data={"typeName": word, "uri": uri},
            )
        ]

    @server.feature("typeHierarchy/subtypes")
    def _on_type_hierarchy_subtypes(_ls: LanguageServer, params: Any) -> list[Any]:
        """Subtypes ≡ instances of the type. Reuses `_references_to_type`."""
        from lsprotocol.types import SymbolKind, TypeHierarchyItem

        item = getattr(params, "item", None)
        if item is None:
            return []
        data = getattr(item, "data", None) or {}
        type_name = data.get("typeName") if isinstance(data, dict) else None
        cached_uri = data.get("uri") if isinstance(data, dict) else None
        if not type_name or not cached_uri:
            return []
        cached = state.cache.get(cached_uri)
        if cached is None or not cached.roots:
            return []
        try:
            original_path = _uri_to_path(cached_uri)
        except ValueError:
            original_path = None
        translate = (
            {cached.temp_path: original_path}
            if cached.temp_path is not None and original_path is not None
            else None
        )
        refs = _references_to_type(type_name, cached.roots, False, translate)
        return [
            TypeHierarchyItem(
                name=type_name,
                kind=SymbolKind.Variable,
                uri=ref.uri,
                range=ref.range,
                selection_range=ref.range,
                detail="instance",
            )
            for ref in refs
        ]

    @server.feature("typeHierarchy/supertypes")
    def _on_type_hierarchy_supertypes(_ls: LanguageServer, _params: Any) -> list[Any]:
        """SystemRDL has no inheritance — no supertypes ever."""
        return []

    @server.feature("textDocument/references")
    def _on_references(_ls: LanguageServer, params: Any) -> list[Location]:
        """Find all instantiation sites of the type under the cursor.

        Word-based: identifier at cursor → look up in comp_defs → walk every
        cached elaborated tree for instances whose ``original_def`` matches.
        Cross-file: instances in `\\`include`d files are reported with their
        true URIs (same path-translate logic as goto-def).
        """
        uri = params.text_document.uri
        cached = state.cache.get(uri)
        if cached is None or not cached.roots:
            return []
        word = _word_at_position(cached.text, params.position.line, params.position.character)
        if not word:
            return []
        defs = _comp_defs_from_cached(cached.roots)
        if word not in defs:
            return []
        try:
            original_path = _uri_to_path(uri)
        except ValueError:
            original_path = None
        translate = (
            {cached.temp_path: original_path}
            if cached.temp_path is not None and original_path is not None
            else None
        )
        include_decl = bool(getattr(params, "context", None) and params.context.include_declaration)
        return _references_to_type(word, cached.roots, include_decl, translate)

    @server.feature("textDocument/definition")
    def _on_definition(_ls: LanguageServer, params: Any) -> list[Location] | Location | None:
        uri = params.text_document.uri
        cached = state.cache.get(uri)
        if cached is None or not cached.roots:
            return None
        word = _word_at_position(cached.text, params.position.line, params.position.character)
        if not word:
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
        # Try multi-segment path first — `top.CTRL.enable` walks the elaborated
        # tree segment-by-segment. Falls through to single-word lookup when
        # the path has no dots.
        path = _path_at_position(cached.text, params.position.line, params.position.character)
        if path and "." in path:
            loc = _resolve_path(cached.roots, path, translate)
            if loc is not None:
                return loc
        defs = _comp_defs_from_cached(cached.roots)
        comp = defs.get(word)
        if comp is not None:
            return _definition_location(comp, translate)
        # Fallback: instance-name lookup. Catches signals (e.g. `resetsignal =
        # my_rst;` jumps to `signal { ... } my_rst;`) and named registers /
        # fields that aren't top-level type defs.
        return _find_instance_by_name(cached.roots, word, translate)

    @server.feature(
        TEXT_DOCUMENT_SEMANTIC_TOKENS_FULL,
        SemanticTokensLegend(
            token_types=TOKEN_TYPES,
            token_modifiers=TOKEN_MODIFIERS,
        ),
    )
    async def _on_semantic_tokens(_ls: LanguageServer, params: Any) -> SemanticTokens:
        """Compute semantic tokens for the buffer.

        ``params: Any`` (not ``SemanticTokensParams``) is deliberate — pygls
        2.x inspects the param annotation to decide whether to inject the
        server as the first argument. With a typed lsprotocol annotation it
        stripped the ``_ls`` slot and called us with only ``params``,
        producing ``TypeError: missing 1 required positional argument``
        on every keystroke (visible as editor-wide lag because VSCode
        retries the failing request constantly).

        Async + to_thread because the tokenizer walks the entire buffer
        text; on an 880KB file that pinned the asyncio loop for ~hundreds
        of ms, blocking concurrent requests like rdl/expandNode.
        """
        try:
            uri: str | None = None
            td = getattr(params, "text_document", None)
            if td is not None:
                uri = getattr(td, "uri", None)
            if uri is None and isinstance(params, dict):
                td_dict = params.get("textDocument") or params.get("text_document") or {}
                uri = td_dict.get("uri") if isinstance(td_dict, dict) else None
            if not uri:
                return SemanticTokens(data=[])
            text = _read_buffer(uri)
            if text is None:
                cached = state.cache.get(uri)
                text = cached.text if cached is not None else ""
            data = await asyncio.to_thread(_semantic_tokens_for_text, text)
            if len(data) % 5 != 0:
                logger.error(
                    "semanticTokens/full: encoder produced %d ints (not a multiple of 5)",
                    len(data),
                )
                return SemanticTokens(data=[])
            return SemanticTokens(data=[int(x) for x in data])
        except Exception:
            logger.exception("semanticTokens/full handler failed; returning empty")
            return SemanticTokens(data=[])

    @server.feature("textDocument/formatting")
    def _on_formatting(_ls: LanguageServer, params: Any) -> list[Any]:
        """Conservative whitespace formatter.

        Trims trailing whitespace, expands tabs to 4 spaces, ensures a single
        trailing newline. Skips opinionated changes (alignment, brace style)
        so the formatter never fights user style choices.
        """
        uri = params.text_document.uri
        text = _read_buffer(uri)
        if text is None:
            cached = state.cache.get(uri)
            text = cached.text if cached is not None else ""
        if not text:
            return []
        tab_size = 4
        opts = getattr(params, "options", None)
        if opts is not None:
            ts = getattr(opts, "tab_size", None)
            if isinstance(ts, int) and ts > 0:
                tab_size = ts
        return _document_formatting_edits(text, tab_size)

    @server.feature("textDocument/codeAction")
    def _on_code_action(_ls: LanguageServer, params: Any) -> list[Any]:
        """Surface quick fixes from the lightbulb.

        Currently: "Add `= 0` reset value" for field instantiations missing
        an explicit reset. Pure textual scan — no elaboration dependency, so
        the action shows up even on broken files where the user is mid-edit.
        """
        uri = params.text_document.uri
        text = _read_buffer(uri)
        if text is None:
            cached = state.cache.get(uri)
            text = cached.text if cached is not None else ""
        if not text:
            return []
        return _code_actions_for_range(uri, text, params.range)

    @server.feature("textDocument/documentLink")
    def _on_document_link(_ls: LanguageServer, params: Any) -> list[Any]:
        """Resolve `\\`include` paths into clickable documentLinks.

        Independent of elaboration — works even when the file has parse errors,
        so the user can navigate around a broken file to fix it.
        """
        uri = params.text_document.uri
        text = _read_buffer(uri)
        if not text:
            cached = state.cache.get(uri)
            text = cached.text if cached is not None else ""
        try:
            primary_path = _uri_to_path(uri)
        except ValueError:
            return []
        search_paths = [p for p, _src in _resolve_search_paths(uri, state.include_paths)]
        return _document_links(text, primary_path, search_paths, state.include_vars)

    @server.feature("rdl/includePaths")
    def _on_include_paths(_ls: LanguageServer, params: Any) -> dict[str, Any]:
        """Return the deduped, source-labeled include search path list for a URI.

        Powers the "SystemRDL: Show effective include paths" command. Lets the
        user see exactly which paths are in effect — formerly opaque, especially
        with multiple sources (settings.json + peakrdl.toml + sibling-dir).
        """
        uri = None
        if isinstance(params, dict):
            uri = params.get("uri") or params.get("textDocument", {}).get("uri")
        else:
            uri = getattr(params, "uri", None)
            if uri is None and hasattr(params, "text_document"):
                uri = params.text_document.uri
        if not uri:
            return {"uri": None, "paths": []}
        try:
            resolved = _resolve_search_paths(uri, state.include_paths)
        except (ValueError, OSError):
            resolved = []
        return {
            "uri": uri,
            "paths": [{"path": p, "source": src} for p, src in resolved],
        }

    def _disk_cache_key(uri: str) -> str | None:
        """Compute the on-disk cache key for ``uri``.

        Returns ``None`` if the URI doesn't correspond to a real file (in
        which case we just skip the disk cache entirely — the in-memory
        cache still works). Folds the systemrdl-compiler version so a
        compiler upgrade auto-invalidates every cached envelope.
        """
        try:
            path = _uri_to_path(uri)
        except ValueError:
            return None
        try:
            mtime_ns = path.stat().st_mtime_ns
        except OSError:
            return None
        try:
            import systemrdl as _systemrdl
            compiler_version = getattr(_systemrdl, "__version__", "unknown")
        except Exception:
            compiler_version = "unknown"
        # include_vars is part of the cache key. Same file with
        # a different $VAR substitution map produces a different include
        # graph and therefore a different elaborated tree.
        return make_key(
            path, mtime_ns, state.include_paths, compiler_version,
            include_vars=state.include_vars,
        )

    @server.feature("rdl/elaboratedTree")
    async def _on_elaborated_tree(_ls: LanguageServer, params: Any) -> dict[str, Any]:
        """Custom JSON-RPC: viewer fetches the latest elaborated tree for a URI.

        T1.4: now ``async`` and runs the (potentially multi-second) serialize
        in ``asyncio.to_thread`` so the LSP event loop is never blocked.
        Big designs no longer freeze hover / completion / diagnostics while
        the spine is being built.

        T1.4: when the client advertised ``experimental.systemrdlLazyTree``
        capability the server returns a spine envelope (``Reg.loadState =
        'placeholder'``, fields fetched on demand via ``rdl/expandNode``).
        Old clients keep getting the full tree.

        T1.4: spine envelopes are also persisted to ``DiskCache`` so a
        VSCode window reload skips parse + elaborate + serialize for an
        unchanged file. Cache key folds in mtime + include paths +
        compiler version. Disk cache is only consulted for the lazy path
        because spine envelopes are small (~10 MB even for 25k regs)
        while full envelopes can hit 200 MB and aren't worth caching.

        TODO-1 contract preserved:

        - Request may include ``sinceVersion: int``. If it matches the LSP's
          cached version, the response is a tiny ``{unchanged: true, version}``
          envelope and the client keeps its previously-rendered tree intact.
        - First request (no ``sinceVersion`` or stale value) returns the
          serialized tree, cached on the LSP side keyed by ``(uri, version)``.

        Schema: ``schemas/elaborated-tree.json`` v0.2.0. Returns the cached
        last-good tree when the current parse has failed (design D7).
        """
        uri = None
        since_version: int | None = None
        if isinstance(params, dict):
            uri = params.get("uri") or params.get("textDocument", {}).get("uri")
            raw = params.get("sinceVersion")
            if isinstance(raw, int):
                since_version = raw
        else:
            uri = getattr(params, "uri", None)
            if uri is None and hasattr(params, "text_document"):
                uri = params.text_document.uri
            raw = getattr(params, "sinceVersion", None)
            if isinstance(raw, int):
                since_version = raw

        if not uri:
            return _serialize_root([], stale=False, version=0)
        cached = state.cache.get(uri)
        if cached is None:
            return _serialize_root([], stale=False, version=0)

        # Version-gated fast path: client already has this version, skip both
        # serialization and transport of the (potentially huge) tree body.
        if since_version is not None and since_version == cached.version:
            return _unchanged_envelope(cached.version)

        # In-memory fast path: same-version re-fetch returns the memoized dict.
        if cached.serialized is not None:
            return cached.serialized

        # Disk cache fast path (lazy mode only — see docstring above).
        if state.lazy_supported:
            disk_key = _disk_cache_key(uri)
            if disk_key is not None:
                disk_envelope = await asyncio.to_thread(state.disk_cache.get, disk_key)
                # The cache is content-addressed (sha256 of abs path + mtime +
                # include paths + compiler version). A hit means the envelope's
                # *content* is byte-equivalent to what a fresh serialize would
                # produce — that's the whole point of the key. The envelope's
                # `version` field is a per-process monotonic counter for the
                # client's sinceVersion gating; the disk copy carries whatever
                # counter the previous LSP run had, which is rarely == the
                # current run's counter. Rewrite the field to the current
                # in-memory version so client sinceVersion checks stay
                # coherent, then return. Without this rewrite the disk cache
                # was effectively dead — every cold start re-serialized.
                if isinstance(disk_envelope, dict):
                    disk_envelope["version"] = cached.version
                    # Stale flag is per-process runtime state, NOT part
                    # of the content-addressed disk key — a hit may
                    # carry whatever ``stale`` value was true when the
                    # entry was written, which is unrelated to whether
                    # the latest parse succeeded. Override with the
                    # current state so a "valid → fetched → broken"
                    # flow doesn't keep serving the non-stale envelope
                    # from disk after the in-memory invalidation.
                    # Field-reported as "broke the file, editor shows
                    # error, webview shows nothing wrong".
                    disk_envelope["stale"] = uri in state.stale_uris
                    cached.serialized = disk_envelope
                    # Disk hit skips the spine build path that populates
                    # the expand-index. Build it in the background so
                    # first-click expand stays O(1).
                    if cached.node_index is None:
                        async def _bg_build_index(c: Any = cached) -> None:
                            try:
                                idx = await asyncio.to_thread(
                                    _build_node_index, c.roots
                                )
                                if c.node_index is None:
                                    c.node_index = idx
                            except Exception:
                                logger.debug(
                                    "background node-index build failed",
                                    exc_info=True,
                                )
                        asyncio.create_task(_bg_build_index())
                    return disk_envelope

        try:
            original_path = _uri_to_path(uri)
        except ValueError:
            original_path = None
        translate = (
            {cached.temp_path: original_path}
            if cached.temp_path is not None and original_path is not None
            else None
        )
        # Spine vs full per client capability. Threaded so the loop stays
        # responsive during the ~1s/~6s walk on 25k regs. Build the
        # expand-index on the same DFS pass when the cache doesn't have
        # one yet (zero extra walk cost; pool worker normally fills it).
        serialize_fn = _serialize_spine if state.lazy_supported else _serialize_root
        index_out: dict[str, Any] | None = (
            {} if state.lazy_supported and cached.node_index is None else None
        )
        envelope = await asyncio.to_thread(
            serialize_fn,
            cached.roots,
            uri in state.stale_uris,
            path_translate=translate,
            version=cached.version,
            out_index=index_out,
        )
        cached.serialized = envelope
        if index_out is not None:
            cached.node_index = index_out

        # Persist spine to disk (best-effort, fire-and-forget) for window-reload
        # speed. Full trees are too big to be worth caching to disk.
        if state.lazy_supported:
            disk_key = _disk_cache_key(uri)
            if disk_key is not None:
                # Don't await — disk write is independent of the response.
                asyncio.create_task(
                    asyncio.to_thread(state.disk_cache.put, disk_key, envelope)
                )
        return envelope

    @server.feature("rdl/expandNode")
    async def _on_expand_node(_ls: LanguageServer, params: Any) -> dict[str, Any]:
        """Custom JSON-RPC: lazy-mode client requests details for a placeholder Reg.

        T1.5. Request: ``{uri, version, nodeId}``. Response: a single ``Reg``
        dict with ``fields[]`` populated and ``loadState`` absent. The
        ``nodeId`` is the opaque base-36 string the client got from the
        spine; it's only valid within the (uri, version) pair the spine
        was emitted for.

        Errors raised as JSON-RPC error responses:
        - -32001 ``NodeNotFound`` — nodeId doesn't name any reg (or names
          a container, which can't be expanded — containers are always
          fully present in the spine).
        - -32002 ``VersionMismatch`` — version doesn't match the LSP's
          current cached version. Client should re-fetch the spine.
        """
        from pygls.exceptions import JsonRpcException

        uri = None
        version: int | None = None
        node_id: str | None = None
        if isinstance(params, dict):
            uri = params.get("uri") or params.get("textDocument", {}).get("uri")
            v = params.get("version")
            if isinstance(v, int):
                version = v
            n = params.get("nodeId")
            if isinstance(n, str):
                node_id = n
        else:
            uri = getattr(params, "uri", None)
            v = getattr(params, "version", None)
            if isinstance(v, int):
                version = v
            n = getattr(params, "nodeId", None)
            if isinstance(n, str):
                node_id = n

        if not uri or version is None or not node_id:
            raise JsonRpcException(
                code=-32602, message="invalid expandNode params"
            )
        cached = state.cache.get(uri)
        if cached is None:
            # Soft signal — viewer asked before first elaboration landed (or
            # after the cache was evicted). Returning a sentinel keeps this
            # off the JSON-RPC error channel: pygls auto-logs unhandled
            # JsonRpcException as [ERROR] with a traceback, and a normal race
            # shouldn't look like a server fault. The viewer's onTreeUpdate
            # will deliver a fresh tree and useEffect will retry.
            return {"outdated": True, "currentVersion": None}
        if cached.version != version:
            return {"outdated": True, "currentVersion": cached.version}

        # Memoize per node so repeat fetches of the same reg cost a dict ref.
        if cached.expanded is None:
            cached.expanded = {}
        if node_id in cached.expanded:
            return cached.expanded[node_id]

        try:
            original_path = _uri_to_path(uri)
        except ValueError:
            original_path = None
        translate = (
            {cached.temp_path: original_path}
            if cached.temp_path is not None and original_path is not None
            else None
        )
        # Fallback build — pool worker / spine path normally fills this.
        if cached.node_index is None:
            cached.node_index = await asyncio.to_thread(
                _build_node_index, cached.roots
            )
        result = await asyncio.to_thread(
            expand_node, cached.roots, node_id, translate, cached.node_index
        )
        if result is None:
            raise JsonRpcException(
                code=-32001, message=f"NodeNotFound (nodeId={node_id})"
            )
        cached.expanded[node_id] = result
        return result

    # Test hook: expose ServerState + the closures the integration suite
    # needs to drive elaborate paths without spinning up real stdio.
    # Production code never reads these attributes; private leading
    # underscore signals "do not depend on this from extension/CLI".
    server._systemrdl_state = state  # type: ignore[attr-defined]
    server._systemrdl_full_pass_async = _full_pass_async  # type: ignore[attr-defined]
    server._systemrdl_apply_compile_result = _apply_compile_result  # type: ignore[attr-defined]

    return server
