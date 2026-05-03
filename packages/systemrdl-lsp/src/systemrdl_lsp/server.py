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
    WORKSPACE_DID_CHANGE_CONFIGURATION,
    DidChangeConfigurationParams,
    DidChangeTextDocumentParams,
    DidCloseTextDocumentParams,
    DidOpenTextDocumentParams,
    DidSaveTextDocumentParams,
    InitializedParams,
)
from pygls.lsp.server import LanguageServer
from systemrdl.messages import Severity

from ._handlers_lsp import register as register_lsp_handlers
from ._handlers_rdl import (
    handle_elaborated_tree,
    handle_expand_node,
    handle_include_paths,
)
from ._state import (
    ELABORATION_TIMEOUT_SECONDS,
    ELABORATION_TIMEOUT_SECONDS_MAX,
    ELABORATION_TIMEOUT_SECONDS_MIN,
    ServerState,
)
from ._text_utils import (
    _iter_rdl_files,
)
from ._uri import _path_to_uri, _uri_to_path
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
    _completion_items_static,
    _make_items,
)
from .definition import (
    _comp_defs_from_cached,
    _definition_location,
    _references_to_type,
    _rename_locations,
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
    _semantic_tokens_for_text,
)
from .serialize import (
    ELABORATED_TREE_SCHEMA_VERSION,
    _field_access_token,
    _hex,
    _serialize_addressable,
    _serialize_field,
    _serialize_reg,
    _serialize_root,
    _src_ref_to_dict,
    _unchanged_envelope,
)

if TYPE_CHECKING:
    from systemrdl.node import RootNode

logger = logging.getLogger(__name__)

from systemrdl_lsp import __version__ as SERVER_VERSION  # noqa: E402  pulled from package __init__

DEBOUNCE_SECONDS = 0.3


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
            # AST fingerprint short-circuit: byte-identical elaborated tree
            # vs the prior one. Don't bump version (no
            # ``elaboratedTreeChanged`` push), but DO swap in the fresh
            # roots — their src_refs carry the new line numbers, so
            # codeLens / inlayHint / hover-link overlays land in the
            # right place after a formatter run or whitespace edit.
            # Stale serialized envelopes get cleared so the next
            # rdl/elaboratedTree fetch re-emits with up-to-date positions.
            if (
                old is not None
                and old.ast_fingerprint is not None
                and old.ast_fingerprint == new_fp
                and old.roots
            ):
                if old.temp_path is not None and old.temp_path != tmp_path:
                    old.temp_path.unlink(missing_ok=True)
                old.roots = roots
                old.temp_path = tmp_path
                old.text = buffer_text
                old.text_canonical = _canonicalize_for_skip(buffer_text)
                old.elaborated_at = time.time()
                old.serialized = None
                old.expanded = None
                old.node_index = precomputed_node_index or None
                bumped = _apply_stale_transition(uri, old, has_errors)
                if bumped is not None:
                    version_after = bumped
                _update_include_graph(uri, consumed_files)
                # Force VSCode to re-pull anything tied to source positions
                # — the AST is the same shape but lines moved. pygls'
                # refresh helpers all take a single ``None`` params arg
                # (LSP-spec shape) and return a coroutine.
                for refresh in (
                    server.workspace_code_lens_refresh_async,
                    server.workspace_inlay_hint_refresh_async,
                    server.workspace_semantic_tokens_refresh_async,
                    server.workspace_diagnostic_refresh_async,
                ):
                    try:
                        asyncio.create_task(refresh(None))
                    except Exception:
                        logger.warning(
                            "workspace refresh %s failed",
                            refresh.__name__, exc_info=True,
                        )
            else:
                # Cache takes ownership of the temp file (it backs lazy
                # src_ref reads for hover/documentSymbol). Old entry's
                # temp file is unlinked there.
                # Compute target version inline so we can stuff index + spine
                # into the entry atomically through cache.put — without this,
                # an expand request landing between cache.put and the field
                # assignment sees node_index=None and triggers the slow
                # fallback walk on the main process loop.
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

        # Server-push of "fresh tree ready". Metadata-only payload; the client
        # decides whether to fetch the body (rdl/elaboratedTree with
        # sinceVersion). Skip when the fingerprint short-circuit kept
        # ``cache.version`` untouched — there is nothing new to fetch.
        if version_after is not None:
            try:
                server.protocol.notify(
                    "rdl/elaboratedTreeChanged",
                    {"uri": uri, "version": version_after},
                )
            except Exception:
                logger.debug("could not send elaboratedTreeChanged notification", exc_info=True)
            # Refresh server→client requests (LSP 3.16/3.17) for overlays
            # tied to source-line positions. Fire-and-forget. Each helper
            # takes a single ``None`` params arg per pygls' signature.
            for refresh in (
                server.workspace_code_lens_refresh_async,
                server.workspace_inlay_hint_refresh_async,
                server.workspace_semantic_tokens_refresh_async,
                server.workspace_diagnostic_refresh_async,
            ):
                try:
                    asyncio.create_task(refresh(None))
                except Exception:
                    logger.warning("workspace refresh %s failed", refresh.__name__, exc_info=True)

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
                    state.inflight_pool_futures[uri] = pf
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
        state.inflight_pool_futures.pop(uri, None)
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
        if "formatOnSave" in cfg:
            state.format_on_save = bool(cfg["formatOnSave"])
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
        # Cascade cancellation into the pool worker if the previous
        # elaborate hadn't started yet — at least frees a queued slot.
        # Workers already running keep going (Python's ProcessPool can't
        # safely kill them), but the awaiter is dropped on parent cancel.
        prev_pool_fut = state.inflight_pool_futures.pop(uri, None)
        if prev_pool_fut is not None:
            try:
                prev_pool_fut.cancel()
            except Exception:
                pass
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

    @server.feature("shutdown")
    async def _on_shutdown(_ls: LanguageServer, _params: Any) -> None:
        """Drain in-flight work before exit so we don't orphan elaborates.

        Cancels every queued debounce, waits up to 2s for the in-flight
        compiles to finish, and then shuts the subprocess pool with
        ``wait=True`` so its workers exit cleanly. Without this VSCode's
        process kill on window close left worker subprocesses briefly
        stranded.
        """
        for task in list(state.pending.values()):
            task.cancel()
        if state.pending:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*state.pending.values(), return_exceptions=True),
                    timeout=2.0,
                )
            except (asyncio.TimeoutError, Exception):
                pass
        pool = state.elaborate_pool
        if pool is not None:
            state.elaborate_pool = None
            try:
                pool.shutdown(wait=True, cancel_futures=True)
            except Exception:
                logger.debug("pool drain failed", exc_info=True)

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

    def _file_line_reader(path: pathlib.Path, line_idx: int) -> str | None:
        """Read line N from a workspace file, preferring the LSP buffer cache.

        If the user has the file open with unsaved edits, the LSP's text-
        document cache reflects those edits — use it so rename operates on
        what the user actually sees. Falls back to disk for files not
        currently open.
        """
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

    register_lsp_handlers(
        server, state,
        read_buffer=_read_buffer,
        file_line_reader=_file_line_reader,
    )

    @server.feature("rdl/includePaths")
    def _on_include_paths(_ls: LanguageServer, params: Any) -> dict[str, Any]:
        return handle_include_paths(state, params)

    @server.feature("rdl/elaboratedTree")
    async def _on_elaborated_tree(_ls: LanguageServer, params: Any) -> dict[str, Any]:
        return await handle_elaborated_tree(state, params)

    @server.feature("rdl/expandNode")
    async def _on_expand_node(_ls: LanguageServer, params: Any) -> dict[str, Any]:
        return await handle_expand_node(state, params)

    # Test hook: expose ServerState + the closures the integration suite
    # needs to drive elaborate paths without spinning up real stdio.
    # Production code never reads these attributes; private leading
    # underscore signals "do not depend on this from extension/CLI".
    server._systemrdl_state = state  # type: ignore[attr-defined]
    server._systemrdl_full_pass_async = _full_pass_async  # type: ignore[attr-defined]
    server._systemrdl_apply_compile_result = _apply_compile_result  # type: ignore[attr-defined]
    # Shared dict consumed by the pull-diagnostics handler in _handlers_lsp.
    # Updated as a side-effect of every _publish_diagnostics call.
    server._systemrdl_last_diagnostics = state.last_diagnostics  # type: ignore[attr-defined]

    return server
