"""Standard LSP feature handlers — hover/completion/navigation/view group.

Extracted from server.py so the wiring file stays focused on lifecycle and
custom rdl/* handlers. Each handler closes over ``state`` and a small set of
build_server-local helpers passed via the register() entry point.

The handler set covers everything that primarily delegates to the themed
module helpers (``hover``, ``completion``, ``definition``, ``outline``,
``semantic``, ``formatting``, ``links``, ``code_actions``) — i.e. the
read-only feature surface. Lifecycle (didOpen/didSave/didChange/didClose),
configuration, and the elaborate orchestration stay in server.py because
they own the in-flight task graph.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
import re as _re
from typing import TYPE_CHECKING, Any, Callable

from lsprotocol.types import (
    TEXT_DOCUMENT_DIAGNOSTIC,
    TEXT_DOCUMENT_SEMANTIC_TOKENS_FULL,
    TEXT_DOCUMENT_SEMANTIC_TOKENS_FULL_DELTA,
    TEXT_DOCUMENT_SEMANTIC_TOKENS_RANGE,
    CodeLens,
    CodeLensOptions,
    CompletionOptions,
    DiagnosticOptions,
    FoldingRange,
    InlayHint,
    Location,
    ParameterInformation,
    Position,
    Range,
    RelatedFullDocumentDiagnosticReport,
    SemanticTokens,
    SemanticTokensDelta,
    SemanticTokensEdit,
    SemanticTokensLegend,
    SignatureHelp,
    SignatureHelpOptions,
    SignatureInformation,
    SymbolInformation,
)
from pygls.lsp.server import LanguageServer

from ._text_utils import _build_selection_ranges, _is_valid_identifier
from ._uri import _uri_to_path
from .code_actions import _code_actions_for_range
from .completion import (
    _completion_context,
    _completion_items_for_context,
    _completion_items_for_instances,
    _completion_items_for_members,
    _completion_items_for_properties_of,
    _completion_items_for_types,
    _completion_items_for_user_properties,
    _completion_items_static,
    _enclosing_instance_scope,
)
from .compile import _resolve_search_paths
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
from .formatting import _document_formatting_edits
from .hover import _hover_for_word, _hover_text_for_node, _node_at_position
from .links import _document_links
from .outline import (
    _code_lenses_for_addrmaps,
    _document_symbols,
    _folding_ranges_from_text,
    _inlay_hints_for_addressables,
    _resolve_code_lens_for,
    _workspace_symbols_for_uri,
)
from .semantic import TOKEN_MODIFIERS, TOKEN_TYPES, _semantic_tokens_for_text

if TYPE_CHECKING:
    from ._state import ServerState

logger = logging.getLogger(__name__)


def _no_members_sentinel(replace: Range) -> Any:
    """Single throwaway item used to evict a stale popup when the resolved
    node has no member completions.

    VSCode's completion widget keeps the previously-shown popup open when a
    new request comes back with an empty list (microsoft/vscode#13735). One
    item is enough to force a repaint; we make it a no-op accept (zero-text
    edit covering the partial identifier) and mark it lowest-rank so it
    doesn't outrank anything when the user starts typing again.
    """
    from lsprotocol.types import CompletionItem, CompletionItemKind, TextEdit

    return CompletionItem(
        label="(no members)",
        kind=CompletionItemKind.Text,
        detail="leaf node — nothing to complete",
        sort_text="￿(no members)",
        filter_text="",  # never matches user-typed chars → hidden the moment typing resumes
        insert_text="",
        text_edit=TextEdit(range=replace, new_text=""),
    )


def register(
    server: LanguageServer,
    state: "ServerState",
    *,
    read_buffer: Callable[[str], str | None],
    file_line_reader: Callable[[pathlib.Path, int], str | None],
) -> None:
    """Register the standard LSP feature handlers on ``server``.

    The two callable kwargs are deliberate: ``read_buffer`` reaches into
    pygls' workspace which only exists once the server is constructed, and
    ``file_line_reader`` prefers the open-buffer cache over disk so rename
    sees unsaved edits. Both are defined inside ``build_server`` and passed
    in here.
    """

    # -- hover -----------------------------------------------------------
    @server.feature("textDocument/hover")
    async def _on_hover(_ls: LanguageServer, params: Any) -> Any | None:
        from lsprotocol.types import Hover, MarkupContent, MarkupKind

        uri = params.text_document.uri
        cached = state.cache.get(uri)
        if cached is None or not cached.roots:
            return None

        line, char = params.position.line, params.position.character
        roots = cached.roots
        text = cached.text

        # Map the compiler's content-addressed tmp file back to the user's
        # real workspace path so source links in hover are openable.
        translate: dict[pathlib.Path, pathlib.Path] | None = None
        if cached.temp_path is not None:
            try:
                translate = {cached.temp_path: _uri_to_path(uri)}
            except ValueError:
                translate = None

        def compute() -> str | None:
            node = _node_at_position(roots, line, char)
            md = (
                _hover_text_for_node(node, file_line_reader, translate)
                if node is not None else None
            )
            if md is None:
                word = _word_at_position(text, line, char)
                if word:
                    md = _hover_for_word(word, roots)
            return md

        markdown = await asyncio.to_thread(compute)
        if markdown is None:
            return None
        return Hover(contents=MarkupContent(kind=MarkupKind.Markdown, value=markdown))

    # -- view group: documentSymbol / foldingRange / inlayHint / codeLens / workspace symbol ---
    @server.feature("textDocument/documentSymbol")
    async def _on_document_symbol(_ls: LanguageServer, params: Any) -> list[Any]:
        cached = state.cache.get(params.text_document.uri)
        if cached is None or not cached.roots:
            return []
        return await asyncio.to_thread(_document_symbols, cached.roots)

    @server.feature("textDocument/foldingRange")
    async def _on_folding(_ls: LanguageServer, params: Any) -> list[FoldingRange]:
        cached = state.cache.get(params.text_document.uri)
        text = cached.text if cached is not None else read_buffer(params.text_document.uri) or ""
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

    @server.feature(
        "textDocument/codeLens",
        CodeLensOptions(resolve_provider=True),
    )
    async def _on_code_lens(_ls: LanguageServer, params: Any) -> list[CodeLens]:
        cached = state.cache.get(params.text_document.uri)
        if cached is None or not cached.roots:
            return []
        path = cached.temp_path or _uri_to_path(params.text_document.uri)
        lenses = await asyncio.to_thread(_code_lenses_for_addrmaps, cached.roots, path)
        # Round-trip the URI through `data` so resolve doesn't need a second
        # workspace probe to find which document the lens belongs to.
        for lens in lenses:
            if isinstance(lens.data, dict):
                lens.data["uri"] = params.text_document.uri
        return lenses

    @server.feature("codeLens/resolve")
    async def _on_code_lens_resolve(_ls: LanguageServer, params: Any) -> Any:
        data = getattr(params, "data", None)
        if not isinstance(data, dict):
            return params
        uri = data.get("uri")
        addrmap = data.get("addrmapName")
        if not uri or not addrmap:
            return params
        cached = state.cache.get(uri)
        if cached is None or not cached.roots:
            return params
        cmd = await asyncio.to_thread(_resolve_code_lens_for, cached.roots, addrmap)
        if cmd is not None:
            params.command = cmd
        return params

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

    # -- completion ------------------------------------------------------
    @server.feature(
        "textDocument/completion",
        # Trigger chars:
        # - `.` member access (`WIDE_REG.`)
        # - `>` second char of `->` (dynamic property assignment)
        # - `=` RHS value popup right after the assignment (`sw=`)
        # - ` ` space — value-popup only; needed because typing `sw = `
        #   (with trailing space) otherwise drops the popup that opened on
        #   `=`, and the user has to manually re-invoke. The handler short-
        #   circuits to `null` for spaces outside an assignment so this is
        #   not a noisy "fire-on-every-space" trigger.
        CompletionOptions(trigger_characters=[".", ">", "=", " "], resolve_provider=False),
    )
    async def _on_completion(_ls: LanguageServer, params: Any) -> Any:
        from lsprotocol.types import CompletionItemDefaults, CompletionList, TextEdit

        # LSP 3.17 itemDefaults capability — when the client supports it we
        # ship a single shared edit_range on the CompletionList instead of
        # repeating it on every CompletionItem (10k items × identical range
        # was the dominant payload bloat on 25k-reg general-ctx dumps).
        # Falls back to per-item text_edit when the client is older.
        item_defaults_supported = False
        try:
            cc = server.client_capabilities
            cl_caps = getattr(getattr(cc, "text_document", None), "completion", None)
            cli_caps = getattr(cl_caps, "completion_list", None) if cl_caps else None
            defaults = getattr(cli_caps, "item_defaults", None) if cli_caps else None
            item_defaults_supported = isinstance(defaults, list) and "editRange" in defaults
        except Exception:
            pass

        cached = state.cache.get(params.text_document.uri)
        # Live buffer first, cached.text only as a fallback. ``cached.text``
        # reflects the last SUCCESSFUL parse, so when the user is mid-typing
        # (and the buffer is temporarily broken) it lags by exactly the
        # characters that matter for context detection — `WIDE_REG.addr.`
        # would be missing the trailing dot, so we'd see `general` instead
        # of `member:WIDE_REG.addr`. read_buffer pulls the LIVE document
        # contents from pygls' workspace.
        live = read_buffer(params.text_document.uri)
        text = live if live is not None else (cached.text if cached is not None else "")
        ctx = _completion_context(text, params.position.line, params.position.character)
        roots = cached.roots if cached is not None else None
        line_idx = params.position.line
        char_idx = params.position.character
        # Diagnostic markers — grep "[COMPL]" in the LSP Output channel to
        # find every step of one completion request.
        line_text = text.splitlines()[line_idx] if line_idx < len(text.splitlines()) else ""
        prefix_so_far = line_text[:char_idx]
        trig = getattr(getattr(params, "context", None), "trigger_character", None)
        logger.warning(
            "[COMPL] === request === pos=%d:%d trigger=%r live=%s prefix=%r",
            line_idx, char_idx, trig, "yes" if live is not None else "NO",
            prefix_so_far[-60:],
        )
        logger.warning(
            "[COMPL] context=%s cache.roots=%s",
            ctx, (len(roots) if roots else 0),
        )

        # Word-replace range: covers the partial identifier the user is
        # mid-typing AFTER the last `.` / `>` separator. Mature LSPs
        # (typescript-language-server, rust-analyzer, gopls) attach an
        # explicit textEdit to every item so VSCode replaces precisely this
        # span when the user accepts a suggestion — without it, VSCode
        # falls back to its word-boundary heuristic, which doesn't always
        # honour `.` as a separator and can leave stale popup state.
        def _replace_range_for_partial() -> Range:
            lines = text.splitlines()
            line_text = lines[line_idx] if line_idx < len(lines) else ""
            # Walk back from cursor while we're on `\w` chars — that's the
            # partial member name being typed (empty right after `.`).
            start = char_idx
            while start > 0 and (line_text[start - 1].isalnum() or line_text[start - 1] == "_"):
                start -= 1
            return Range(
                start=Position(line=line_idx, character=start),
                end=Position(line=line_idx, character=char_idx),
            )

        def _attach_text_edits(items: list[Any], replace: Range) -> list[Any]:
            for it in items:
                it.text_edit = TextEdit(range=replace, new_text=it.label)
            return items

        def _build_list(items: list[Any], replace: Range, *, is_incomplete: bool = False) -> Any:
            """CompletionList with shared editRange when client supports it,
            per-item text_edit otherwise. Either path produces the same
            client-visible behaviour; the shared form is just lighter."""
            if item_defaults_supported:
                return CompletionList(
                    is_incomplete=is_incomplete,
                    items=items,
                    item_defaults=CompletionItemDefaults(edit_range=replace),
                )
            return CompletionList(
                is_incomplete=is_incomplete,
                items=_attach_text_edits(items, replace),
            )

        # Member / property access — narrow popup to children of the resolved node.
        if ctx.startswith("member:"):
            path = ctx[len("member:"):]
            items = (
                await asyncio.to_thread(_completion_items_for_members, roots, path)
                if roots else []
            )
            logger.warning(
                "[COMPL] member path=%r → %d items: %s",
                path, len(items), [i.label for i in items[:10]],
            )
            if not items:
                logger.warning("[COMPL] → returning null (no members)")
                return None
            replace = _replace_range_for_partial()
            logger.warning("[COMPL] → returning %d items (itemDefaults=%s)", len(items), item_defaults_supported)
            return _build_list(items, replace)
        if ctx.startswith("property:"):
            path = ctx[len("property:"):]
            items = (
                await asyncio.to_thread(_completion_items_for_properties_of, roots, path)
                if roots else []
            )
            logger.warning(
                "[COMPL] property path=%r → %d items: %s",
                path, len(items), [i.label for i in items[:10]],
            )
            if not items:
                logger.warning("[COMPL] → returning null (no properties)")
                return None
            replace = _replace_range_for_partial()
            logger.warning("[COMPL] → returning %d items (itemDefaults=%s)", len(items), item_defaults_supported)
            return _build_list(items, replace)

        if ctx != "general":
            ctx_items = _completion_items_for_context(ctx)
            logger.warning(
                "[COMPL] RHS-value ctx=%s → %d items", ctx, len(ctx_items),
            )
            if not ctx_items:
                # `value:foo` for a property without a closed-set catalogue
                # (e.g. ``regwidth = ``) — return null so VSCode dismisses
                # the popup instead of falling through to other providers.
                return None
            replace = _replace_range_for_partial()
            return _build_list(ctx_items, replace)

        # `general` context but the user explicitly typed `=` or space as
        # the trigger — they're in an assignment RHS slot we don't
        # recognise (unknown property, or in an expression). Returning
        # the full keyword/type/instance dump here floods the popup with
        # junk; suppressing keeps the editor quiet. ` ` is also a space
        # outside any property-RHS context (e.g. `addrmap top {<space>`),
        # where popping a 130-item list would be noise.
        if trig in ("=", " "):
            logger.warning("[COMPL] %r trigger in general ctx → null (suppressed)", trig)
            return None
        # `general` ctx walks the elaborated tree to harvest type names,
        # user properties, and instance names. Off-load — VSCode fires
        # completion on every keystroke; on 25k-reg fixtures the instance
        # walk is the dominant cost.
        scope = _enclosing_instance_scope(text, params.position.line, params.position.character)
        def collect() -> list[Any]:
            items = _completion_items_static()
            if roots:
                items.extend(_completion_items_for_types(roots))
                items.extend(_completion_items_for_user_properties(roots))
                items.extend(_completion_items_for_instances(roots, scope_prefix=scope))
            return items
        items = await asyncio.to_thread(collect)
        # `is_incomplete` mirrors whether we still expect new items to land.
        # When the cache is empty the first elaborate hasn't finished — we
        # only have static keywords; tell VSCode to re-query so the user
        # sees `WIDE_REG` etc. as soon as the tree is ready.
        is_incomplete = roots is None
        logger.warning(
            "[COMPL] general scope=%r → %d items (incomplete=%s itemDefaults=%s)",
            scope, len(items), is_incomplete, item_defaults_supported,
        )
        replace = _replace_range_for_partial()
        return _build_list(items, replace, is_incomplete=is_incomplete)

    # -- rename ----------------------------------------------------------
    @server.feature("textDocument/prepareRename")
    def _on_prepare_rename(_ls: LanguageServer, params: Any) -> Any | None:
        """Validate that the cursor is on a renameable identifier.

        We only rename top-level component type names — instance names and
        keywords return None so VSCode shows "You cannot rename this element".
        """
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
        # Anchor the rename input on the word's range so VSCode positions it
        # correctly. Actual edits get computed in textDocument/rename below.
        line = cached.text.splitlines()[params.position.line] if params.position.line < len(
            cached.text.splitlines()
        ) else ""
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
        if not new_name or not _is_valid_identifier(new_name):
            return None

        defs = _comp_defs_from_cached(cached.roots)
        if word not in defs:
            return None
        if new_name in defs:
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
        locs = _rename_locations(word, cached.roots, translate, file_line_reader)
        if not locs:
            return None

        per_uri: dict[str, list[TextEdit]] = {}
        for loc in locs:
            per_uri.setdefault(loc.uri, []).append(
                TextEdit(range=loc.range, new_text=new_name)
            )
        return WorkspaceEdit(changes=per_uri)

    # -- documentHighlight + selectionRange + signatureHelp --------------
    @server.feature("textDocument/documentHighlight")
    async def _on_document_highlight(_ls: LanguageServer, params: Any) -> list[Any]:
        """Highlight every textual occurrence of the identifier under the cursor.

        Implementation: regex-find every `\\b<word>\\b` match in the buffer.
        Cheap and matches user expectation (highlight = same lexical token).
        """
        from lsprotocol.types import DocumentHighlight, DocumentHighlightKind

        uri = params.text_document.uri
        cached = state.cache.get(uri)
        text = cached.text if cached is not None else read_buffer(uri) or ""
        word = _word_at_position(text, params.position.line, params.position.character)
        if not word:
            return []

        # 880KB buffer × `splitlines` + per-line regex iter is measurable CPU.
        # Off-load — VSCode dispatches highlight on every cursor stop.
        def scan() -> list[DocumentHighlight]:
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

        return await asyncio.to_thread(scan)

    @server.feature("textDocument/selectionRange")
    async def _on_selection_range(_ls: LanguageServer, params: Any) -> list[Any]:
        """Smart selection: expand cursor → word → enclosing `{...}` block(s) → file.

        Pure textual implementation walks outward through brace pairs, no
        elaboration dependency. Lets the user expand a selection from a
        field name through its containing reg, regfile, addrmap, etc. with
        Shift+Alt+Right.
        """
        from lsprotocol.types import SelectionRange as LSPSelectionRange

        uri = params.text_document.uri
        cached = state.cache.get(uri)
        text = cached.text if cached is not None else read_buffer(uri) or ""
        if not text:
            return []

        # `_build_selection_ranges` does a char-by-char scan to locate brace
        # pairs — linear in buffer size, off-load so a 1MB SystemRDL file
        # doesn't pause the loop on every Shift+Alt+Right.
        positions = list(params.positions)
        def build() -> list[Any]:
            lines = text.splitlines()
            result: list[Any] = []
            for pos in positions:
                ranges = _build_selection_ranges(text, lines, pos.line, pos.character)
                if not ranges:
                    result.append(LSPSelectionRange(
                        range=Range(start=pos, end=pos),
                        parent=None,
                    ))
                    continue
                parent: Any = None
                for rng in ranges:
                    parent = LSPSelectionRange(range=rng, parent=parent)
                result.append(parent)
            return result

        return await asyncio.to_thread(build)

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
            text = cached.text if cached is not None else read_buffer(uri) or ""
            if not text:
                return None
            split_lines = text.splitlines()
            line_text = (
                split_lines[params.position.line]
                if params.position.line < len(split_lines)
                else ""
            )
            prefix = line_text[: params.position.character]
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

    # -- type hierarchy --------------------------------------------------
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
        """Subtypes ≡ instances of the type. Reuses ``_references_to_type``."""
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
        return []

    # -- references + definition -----------------------------------------
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
        # Multi-segment path first — `top.CTRL.enable` walks the elaborated
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
        # Fallback: instance-name lookup. Catches signals (`resetsignal =
        # my_rst;` jumps to `signal { ... } my_rst;`) and named registers /
        # fields that aren't top-level type defs.
        return _find_instance_by_name(cached.roots, word, translate)

    # -- semantic tokens / formatting / code action / document link ------
    # Per-URI cache of the last full token array, keyed by result_id, so
    # `semanticTokens/full/delta` can compute edits against the prior
    # request. ``result_id`` is the buffer hash so identical text always
    # produces the same id (and clients see "no change" cheaply).
    _semtok_cache: dict[str, tuple[str, list[int]]] = {}

    def _semtok_result_id(text: str) -> str:
        import hashlib
        return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()[:16]

    def _diff_semantic_tokens(prev: list[int], cur: list[int]) -> list[Any]:
        """Return a single SemanticTokensEdit covering the changed slice.

        Cheap LCS-like collapse: trim equal prefix and suffix, encode the
        diff as one edit. Loses some compression vs a multi-edit diff but
        is cheap to compute and still way smaller than re-shipping the
        entire token stream on every keystroke.
        """
        n = min(len(prev), len(cur))
        # Token entries are 5 ints — only crop on multiple-of-5 boundaries
        # so we don't split a single token across the edit.
        head = 0
        while head + 5 <= n and prev[head:head + 5] == cur[head:head + 5]:
            head += 5
        tail = 0
        while (
            tail + 5 <= n - head
            and prev[len(prev) - tail - 5:len(prev) - tail]
                == cur[len(cur) - tail - 5:len(cur) - tail]
        ):
            tail += 5
        delete_count = len(prev) - head - tail
        new_data = cur[head:len(cur) - tail]
        if delete_count == 0 and not new_data:
            return []
        return [SemanticTokensEdit(start=head, delete_count=delete_count, data=[int(x) for x in new_data])]

    def _filter_tokens_in_range(data: list[int], rng: Range) -> list[int]:
        """Reduce a full token stream to entries overlapping ``rng``.

        Tokens are encoded as relative deltas (deltaLine, deltaStartChar,
        length, type, mods) — we walk and re-base each kept token's deltas
        so the trimmed stream is still self-consistent.
        """
        out: list[int] = []
        line = 0
        col = 0
        last_emitted_line = 0
        last_emitted_col = 0
        for i in range(0, len(data), 5):
            dl, dc, length, ttype, mods = data[i:i + 5]
            if dl == 0:
                col += dc
            else:
                line += dl
                col = dc
            in_range = (
                rng.start.line <= line <= rng.end.line
                and not (line == rng.start.line and col + length <= rng.start.character)
                and not (line == rng.end.line and col >= rng.end.character)
            )
            if in_range:
                if not out:
                    out.extend([line, col, length, ttype, mods])
                    last_emitted_line, last_emitted_col = line, col
                else:
                    rel_line = line - last_emitted_line
                    rel_col = col if rel_line > 0 else col - last_emitted_col
                    out.extend([rel_line, rel_col, length, ttype, mods])
                    last_emitted_line, last_emitted_col = line, col
        return out

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
        2.x strips the ``_ls`` slot when it sees a typed lsprotocol param,
        producing ``TypeError: missing 1 required positional argument`` on
        every keystroke. Async + to_thread because the tokenizer walks the
        entire buffer.
        """
        try:
            uri = _semtok_uri_from(params)
            if not uri:
                return SemanticTokens(data=[])
            text = _semtok_text_for(uri)
            data = await asyncio.to_thread(_semantic_tokens_for_text, text)
            if len(data) % 5 != 0:
                logger.error(
                    "semanticTokens/full: encoder produced %d ints (not a multiple of 5)",
                    len(data),
                )
                return SemanticTokens(data=[])
            data = [int(x) for x in data]
            rid = _semtok_result_id(text)
            _semtok_cache[uri] = (rid, data)
            return SemanticTokens(result_id=rid, data=data)
        except Exception:
            logger.exception("semanticTokens/full handler failed; returning empty")
            return SemanticTokens(data=[])

    def _semtok_uri_from(params: Any) -> str | None:
        td = getattr(params, "text_document", None)
        if td is not None:
            return getattr(td, "uri", None)
        if isinstance(params, dict):
            td_dict = params.get("textDocument") or params.get("text_document") or {}
            return td_dict.get("uri") if isinstance(td_dict, dict) else None
        return None

    def _semtok_text_for(uri: str) -> str:
        text = read_buffer(uri)
        if text is None:
            cached = state.cache.get(uri)
            text = cached.text if cached is not None else ""
        return text

    @server.feature(TEXT_DOCUMENT_SEMANTIC_TOKENS_RANGE)
    async def _on_semantic_tokens_range(_ls: LanguageServer, params: Any) -> SemanticTokens:
        """Tokens overlapping a viewport range — VSCode requests this for the
        visible region instead of the full file when the file is large.
        """
        try:
            uri = _semtok_uri_from(params)
            rng = getattr(params, "range", None)
            if not uri or rng is None:
                return SemanticTokens(data=[])
            text = _semtok_text_for(uri)
            full = await asyncio.to_thread(_semantic_tokens_for_text, text)
            if len(full) % 5 != 0:
                return SemanticTokens(data=[])
            data = _filter_tokens_in_range([int(x) for x in full], rng)
            return SemanticTokens(data=data)
        except Exception:
            logger.exception("semanticTokens/range failed")
            return SemanticTokens(data=[])

    @server.feature(TEXT_DOCUMENT_SEMANTIC_TOKENS_FULL_DELTA)
    async def _on_semantic_tokens_delta(_ls: LanguageServer, params: Any) -> Any:
        """Edit-encoded diff against the prior result. Falls back to a full
        SemanticTokens response when no usable previous snapshot exists.
        """
        try:
            uri = _semtok_uri_from(params)
            prev_id = getattr(params, "previous_result_id", None)
            if not uri:
                return SemanticTokens(data=[])
            text = _semtok_text_for(uri)
            new_data = await asyncio.to_thread(_semantic_tokens_for_text, text)
            if len(new_data) % 5 != 0:
                return SemanticTokens(data=[])
            new_data = [int(x) for x in new_data]
            new_id = _semtok_result_id(text)
            cached = _semtok_cache.get(uri)
            _semtok_cache[uri] = (new_id, new_data)
            if cached is None or cached[0] != prev_id:
                return SemanticTokens(result_id=new_id, data=new_data)
            edits = _diff_semantic_tokens(cached[1], new_data)
            return SemanticTokensDelta(result_id=new_id, edits=edits)
        except Exception:
            logger.exception("semanticTokens/full/delta failed")
            return SemanticTokens(data=[])

    @server.feature(
        TEXT_DOCUMENT_DIAGNOSTIC,
        DiagnosticOptions(
            identifier="systemrdl",
            inter_file_dependencies=True,  # `\include` graph crosses files.
            workspace_diagnostics=False,
        ),
    )
    def _on_diagnostic(_ls: LanguageServer, params: Any) -> Any:
        """LSP 3.17 pull-model diagnostics: client requests current
        diagnostics for a document on demand.

        We continue to push via ``publishDiagnostics`` from elaborate; this
        handler just serves the cached snapshot back. Saves a synchronous
        re-elaborate when the client (e.g. VSCode 1.85+) chooses pull.
        """
        uri = params.text_document.uri
        items = state.last_diagnostics.get(uri, [])
        return RelatedFullDocumentDiagnosticReport(items=list(items))

    @server.feature("textDocument/willSaveWaitUntil")
    def _on_will_save_wait_until(_ls: LanguageServer, params: Any) -> list[Any]:
        """Format-on-save — opt-in via ``systemrdl-pro.formatOnSave`` (default
        off). When enabled, returns the same edits as
        ``textDocument/formatting`` so VSCode applies them on save.
        """
        if not getattr(state, "format_on_save", False):
            return []
        uri = params.text_document.uri
        text = read_buffer(uri)
        if text is None:
            cached = state.cache.get(uri)
            text = cached.text if cached is not None else ""
        if not text:
            return []
        return _document_formatting_edits(text, 4)

    @server.feature("textDocument/formatting")
    def _on_formatting(_ls: LanguageServer, params: Any) -> list[Any]:
        """Conservative whitespace formatter.

        Trims trailing whitespace, expands tabs to 4 spaces, ensures a single
        trailing newline. Skips opinionated changes (alignment, brace style)
        so the formatter never fights user style choices.
        """
        uri = params.text_document.uri
        text = read_buffer(uri)
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
        text = read_buffer(uri)
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
        text = read_buffer(uri)
        if not text:
            cached = state.cache.get(uri)
            text = cached.text if cached is not None else ""
        try:
            primary_path = _uri_to_path(uri)
        except ValueError:
            return []
        search_paths = [p for p, _src in _resolve_search_paths(uri, state.include_paths)]
        return _document_links(text, primary_path, search_paths, state.include_vars)
