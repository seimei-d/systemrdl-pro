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
    SemanticTokens,
    SemanticTokensLegend,
    SymbolInformation,
)
from pygls.lsp.server import LanguageServer
from systemrdl.messages import Severity

from ._uri import _path_to_uri, _uri_to_path
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
    _src_ref_to_dict,
    _unchanged_envelope,
)

if TYPE_CHECKING:
    from systemrdl.node import RootNode

logger = logging.getLogger(__name__)

SERVER_VERSION = "0.14.5"


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


def _is_valid_identifier(name: str) -> bool:
    """Match SystemRDL identifier syntax: ``[A-Za-z_][A-Za-z0-9_]*``.

    Used by rename to reject input that would corrupt the buffer.
    """
    import re as _re
    return bool(_re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name))


DEBOUNCE_SECONDS = 0.3
# Eng-review safety net #3: cap a single elaborate pass at 10s wall-clock.
# Past that we keep last-good (D7) and surface a synthetic diagnostic. A pathological
# Perl-style include cycle in a third-party RDL pack should NOT freeze the editor.
ELABORATION_TIMEOUT_SECONDS = 10.0


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
                server.send_notification(
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

        _check_perl_pre_flight(buffer_text)

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
            previously_affected = state.diag_affected.get(uri, set())
            state.diag_affected[uri] = _publish_diagnostics(
                server, uri, [timeout_msg], previously_affected
            )
            return
        except Exception:
            logger.exception("unexpected error during async full-pass for %s", uri)
            return
        _apply_compile_result(uri, buffer_text, messages, roots, tmp_path)

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
                    logger.info("includePaths from initial config: %s", state.include_paths)
                    logger.info("includeVars from initial config: %s", list(state.include_vars))
                    logger.info("perlSafeOpcodes override: %s", state.perl_safe_opcodes)
                    logger.info(
                        "preindex enabled=%s max=%d",
                        state.preindex_enabled, state.preindex_max_files,
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

    @server.feature("rdl/elaboratedTree")
    def _on_elaborated_tree(_ls: LanguageServer, params: Any) -> dict[str, Any]:
        """Custom JSON-RPC: viewer fetches the latest elaborated tree for a URI.

        TODO-1 contract:

        - Request may include ``sinceVersion: int``. If it matches the LSP's
          cached version, the response is a tiny ``{unchanged: true, version}``
          envelope and the client keeps its previously-rendered tree intact.
        - First request (no ``sinceVersion`` or stale value) returns the full
          serialized tree, cached on the LSP side keyed by ``(uri, version)`` so
          repeat fetches at the same version don't re-serialize.

        Schema: ``schemas/elaborated-tree.json`` v0.1.0. Returns the cached
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

        # Reuse the previously-serialized JSON when version hasn't advanced —
        # multiple fetches at the same version (e.g. tab focus changes,
        # re-mount) cost a dict reference rather than a full tree walk.
        if cached.serialized is not None:
            return cached.serialized

        try:
            original_path = _uri_to_path(uri)
        except ValueError:
            original_path = None
        translate = (
            {cached.temp_path: original_path}
            if cached.temp_path is not None and original_path is not None
            else None
        )
        envelope = _serialize_root(
            cached.roots,
            stale=uri in state.stale_uris,
            path_translate=translate,
            version=cached.version,
        )
        cached.serialized = envelope
        return envelope

    return server
