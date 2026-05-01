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
    SymbolInformation,
)
from pygls.lsp.server import LanguageServer
from systemrdl.messages import Severity

from ._uri import _path_to_uri, _uri_to_path
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
from .hover import (
    _format_hex,
    _hover_for_word,
    _hover_text_for_node,
    _node_at_position,
)
from .outline import (
    _code_lenses_for_addrmaps,
    _document_symbols,
    _folding_ranges_from_text,
    _inlay_hints_for_addressables,
    _workspace_symbols_for_uri,
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

SERVER_VERSION = "0.11.0"
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
    "_code_lenses_for_addrmaps",
    "_comp_defs_from_cached",
    "_compile_text",
    "_completion_context",
    "_completion_items_for_context",
    "_completion_items_for_types",
    "_completion_items_static",
    "_definition_location",
    "_document_symbols",
    "_elaborate",
    "_expand_include_vars",
    "_field_access_token",
    "_folding_ranges_from_text",
    "_format_hex",
    "_hex",
    "_hover_for_word",
    "_hover_text_for_node",
    "_inlay_hints_for_addressables",
    "_make_items",
    "_message_to_range",
    "_node_at_position",
    "_path_to_uri",
    "_peakrdl_toml_paths",
    "_publish_diagnostics",
    "_safe_get_property",
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
        _publish_diagnostics(server, uri, messages)

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
                    raw_opcodes = configs[0].get("perlSafeOpcodes") or []
                    if isinstance(raw_opcodes, list):
                        state.perl_safe_opcodes = [str(op) for op in raw_opcodes if op]
                    logger.info("includePaths from initial config: %s", state.include_paths)
                    logger.info("includeVars from initial config: %s", list(state.include_vars))
                    logger.info("perlSafeOpcodes override: %s", state.perl_safe_opcodes)
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
