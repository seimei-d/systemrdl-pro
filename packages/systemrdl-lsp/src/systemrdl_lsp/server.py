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
import dataclasses
import logging
import pathlib
from typing import TYPE_CHECKING, Any

from lsprotocol.types import (
    INITIALIZED,
    TEXT_DOCUMENT_DID_CHANGE,
    TEXT_DOCUMENT_DID_OPEN,
    TEXT_DOCUMENT_DID_SAVE,
    TEXT_DOCUMENT_SEMANTIC_TOKENS_FULL,
    WORKSPACE_DID_CHANGE_CONFIGURATION,
    CodeLens,
    DidChangeConfigurationParams,
    DidChangeTextDocumentParams,
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
    _compile_text,
    _elaborate,
    _expand_include_vars,
    _peakrdl_toml_paths,
    _perl_available,
    _perl_in_source,
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
    expand_node,
)

if TYPE_CHECKING:
    from systemrdl.node import RootNode

logger = logging.getLogger(__name__)

SERVER_VERSION = "0.16.0"


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

    # Convert (line, char) → absolute offset.
    def offset_of(li: int, co: int) -> int:
        return sum(len(ln) + 1 for ln in lines[:li]) + co

    def pos_of(off: int) -> Position:
        # Inverse of offset_of.
        seen = 0
        for li, ln in enumerate(lines):
            if seen + len(ln) >= off:
                return Position(line=li, character=off - seen)
            seen += len(ln) + 1
        return Position(line=len(lines), character=0)

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
ELABORATION_TIMEOUT_SECONDS = 60.0
ELABORATION_TIMEOUT_SECONDS_MIN = 1.0
ELABORATION_TIMEOUT_SECONDS_MAX = 300.0


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
    # Substitution map for ``$VAR`` / ``${VAR}`` inside ```include "..."`` paths.
    # Read from systemrdl-pro.includeVars; falls back to os.environ during expansion.
    include_vars: dict[str, str] = dataclasses.field(default_factory=dict)
    # URIs whose latest parse attempt failed but for which we still have a last-good
    # cache entry. The viewer renders a stale-bar when a URI is in this set (D7).
    stale_uris: set[str] = dataclasses.field(default_factory=set)
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
        version_after: int | None = None
        if roots:
            # Cache takes ownership of the temp file (it backs lazy src_ref reads
            # for hover/documentSymbol). Old entry's temp file is unlinked there.
            state.cache.put(uri, roots, buffer_text, tmp_path)
            state.stale_uris.discard(uri)
            cached = state.cache.get(uri)
            version_after = cached.version if cached is not None else None
            try:
                conflicts = _address_conflict_diagnostics(roots, tmp_path)
                messages = list(messages) + conflicts
            except Exception:
                logger.debug("address-conflict scan failed", exc_info=True)
        else:
            # Parse failed — keep the previous cache entry intact (D7).
            tmp_path.unlink(missing_ok=True)
            if state.cache.get(uri) is not None:
                state.stale_uris.add(uri)
        previously_affected = state.diag_affected.get(uri, set())
        state.diag_affected[uri] = _publish_diagnostics(
            server, uri, messages, previously_affected
        )

        # TODO-1 push: notify the client that a fresh elaborated tree is ready.
        # Payload is metadata-only ({uri, version}); the client decides whether
        # to fetch the full tree (rdl/elaboratedTree with sinceVersion). This
        # eliminates the user-perceptible refresh delay on save (extension
        # used to wait for didSaveTextDocument before pulling a fresh tree).
        if version_after is not None:
            try:
                server.protocol.notify(
                    "rdl/elaboratedTreeChanged",
                    {"uri": uri, "version": version_after},
                )
            except Exception:
                logger.debug("could not send elaboratedTreeChanged notification", exc_info=True)

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

    async def _full_pass_async(uri: str, buffer_text: str | None) -> None:
        if buffer_text is None:
            try:
                buffer_text = _uri_to_path(uri).read_text(encoding="utf-8")
            except (OSError, ValueError):
                return

        # Short-circuit: if the buffer text is byte-identical to the last
        # successful elaboration we already have a fresh tree for this URI.
        # This eliminates the GIL-pinning re-runs that pile up when VSCode
        # fires duplicate didOpens on workspace restore, when format-on-save
        # or trim-trailing-whitespace touches the buffer without producing a
        # real diff, and when small files get queued behind a 25k re-elaborate
        # for no reason. Costs one string equality check; saves seconds.
        prior = state.cache.get(uri)
        if (
            prior is not None
            and prior.text == buffer_text
            and uri not in state.stale_uris
        ):
            return

        _check_perl_pre_flight(buffer_text)

        # Tell the client elaboration is starting so it can paint a
        # "re-elaborating" indicator while keeping the existing tree
        # interactive. Paired with rdl/elaborationFinished in the finally
        # block below so the indicator clears on every termination path
        # (success, timeout, exception). Best-effort: a missed notification
        # only means a slightly stale UI, never broken state.
        try:
            server.protocol.notify("rdl/elaborationStarted", {"uri": uri})
        except Exception:
            logger.debug("could not send elaborationStarted", exc_info=True)

        loop = asyncio.get_running_loop()
        # Run the synchronous compiler off the event loop so a pathological elaborate
        # can't block hover/cancel/etc. wait_for() can't actually kill the worker thread
        # on timeout, so we attach a cleanup callback that unlinks the orphan temp file
        # whenever the late result eventually arrives.
        fut: asyncio.Future = loop.run_in_executor(
            None,
            _compile_text,
            uri,
            buffer_text,
            state.include_paths,
            state.include_vars,
            state.perl_safe_opcodes,
        )
        try:
            messages, roots, tmp_path = await asyncio.wait_for(
                asyncio.shield(fut), timeout=state.elaboration_timeout_s
            )
        except asyncio.TimeoutError:
            logger.warning(
                "elaborate timeout on %s after %.0fs; keeping last-good",
                uri, state.elaboration_timeout_s,
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
        except Exception:
            logger.exception("unexpected error during async full-pass for %s", uri)
            _emit_elaboration_finished(uri)
            return
        _apply_compile_result(uri, buffer_text, messages, roots, tmp_path)
        _emit_elaboration_finished(uri)

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

    @server.feature(INITIALIZED)
    def _on_initialized(_ls: LanguageServer, _params: InitializedParams) -> None:
        # T1.4: detect lazy-tree client capability before answering any
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
                    cfg = configs[0]
                    paths = cfg.get("includePaths") or []
                    state.include_paths = [str(p) for p in paths if p]
                    raw_vars = cfg.get("includeVars") or {}
                    if isinstance(raw_vars, dict):
                        state.include_vars = {str(k): str(v) for k, v in raw_vars.items()}
                    raw_opcodes = cfg.get("perlSafeOpcodes") or []
                    if isinstance(raw_opcodes, list):
                        state.perl_safe_opcodes = [str(op) for op in raw_opcodes if op]
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
                    logger.info("includePaths from initial config: %s", state.include_paths)
                    logger.info("includeVars from initial config: %s", list(state.include_vars))
                    logger.info("perlSafeOpcodes override: %s", state.perl_safe_opcodes)
                    logger.info(
                        "preindex enabled=%s max=%d",
                        state.preindex_enabled, state.preindex_max_files,
                    )
                    logger.info(
                        "elaboration timeout: %.1fs", state.elaboration_timeout_s,
                    )
            except Exception:
                logger.debug("could not fetch initial workspace configuration", exc_info=True)
            # Workspace pre-index runs after config so it picks up the user's
            # configured preindex limit (skip-on-disable + file-count cap).
            # Even on config failure we still try with defaults — pre-warming
            # the cache with default limits is preferable to no index.
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

    @server.feature("$/cancelRequest")
    def _on_cancel(_ls: LanguageServer, _params: Any) -> None:
        # pygls 2.x logs noisy warnings for cancel notifications that arrive after
        # the request already completed. A no-op handler keeps the channel quiet.
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
                    raw_opcodes = configs[0].get("perlSafeOpcodes") or []
                    if isinstance(raw_opcodes, list):
                        state.perl_safe_opcodes = [str(op) for op in raw_opcodes if op]
                    timeout_ms = configs[0].get("elaborationTimeoutMs")
                    if isinstance(timeout_ms, (int, float)) and timeout_ms > 0:
                        state.elaboration_timeout_s = max(
                            ELABORATION_TIMEOUT_SECONDS_MIN,
                            min(ELABORATION_TIMEOUT_SECONDS_MAX, timeout_ms / 1000.0),
                        )
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
        node = _node_at_position(cached.roots, line, char)
        markdown = _hover_text_for_node(node) if node is not None else None

        # 2-3. Word-based catalogue lookup for keywords / properties / access values / type names.
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
        for uri, entry in state.cache._entries.items():
            if entry.roots:
                out.extend(_workspace_symbols_for_uri(uri, entry.roots, query))
        return out

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
    def _on_semantic_tokens(_ls: LanguageServer, params: Any) -> SemanticTokens:
        """Compute semantic tokens for the buffer.

        ``params: Any`` (not ``SemanticTokensParams``) is deliberate — pygls
        2.x inspects the param annotation to decide whether to inject the
        server as the first argument. With a typed lsprotocol annotation it
        stripped the ``_ls`` slot and called us with only ``params``,
        producing ``TypeError: missing 1 required positional argument``
        on every keystroke (visible as editor-wide lag because VSCode
        retries the failing request constantly).
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
            data = _semantic_tokens_for_text(text)
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
        return make_key(path, mtime_ns, state.include_paths, compiler_version)

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
                # Trust the on-disk envelope only if its `version` field equals
                # what we just elaborated to. Mtime in the key should make this
                # impossible to mismatch in practice, but be defensive.
                if (
                    isinstance(disk_envelope, dict)
                    and disk_envelope.get("version") == cached.version
                ):
                    cached.serialized = disk_envelope
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
        # Pick spine vs full based on the client's advertised capability and
        # run in a thread so the event loop stays responsive during the
        # ~1s (spine) or ~6s (full) wall-clock at 25k regs.
        serialize_fn = _serialize_spine if state.lazy_supported else _serialize_root
        envelope = await asyncio.to_thread(
            serialize_fn,
            cached.roots,
            uri in state.stale_uris,
            translate,
            cached.version,
        )
        cached.serialized = envelope

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
        result = await asyncio.to_thread(
            expand_node, cached.roots, node_id, translate
        )
        if result is None:
            raise JsonRpcException(
                code=-32001, message=f"NodeNotFound (nodeId={node_id})"
            )
        cached.expanded[node_id] = result
        return result

    return server
