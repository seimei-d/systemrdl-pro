"""LSP server for SystemRDL 2.0.

v0.1 implements only ``textDocument/publishDiagnostics``. The strategy is straightforward:
on every open and change event we run ``systemrdl-compiler`` against the on-disk file
content (or, when available, the in-memory document buffer from pygls). All compiler
messages are captured via a custom ``MessagePrinter`` and converted to LSP diagnostics.

Week 2 will add hover/outline/definition/completion. Week 4 adds the custom JSON-RPC
``rdl/elaboratedTree`` push (see schemas/elaborated-tree.json).
"""

from __future__ import annotations

import logging
import pathlib
import urllib.parse
from typing import TYPE_CHECKING

from lsprotocol.types import (
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
    Position,
    PublishDiagnosticsParams,
    Range,
)
from pygls.lsp.server import LanguageServer
from systemrdl import RDLCompiler, RDLCompileError
from systemrdl.messages import MessagePrinter, Severity

if TYPE_CHECKING:
    from systemrdl.messages import SourceRefBase

logger = logging.getLogger(__name__)

SERVER_NAME = "systemrdl-lsp"
SERVER_VERSION = "0.1.0"


class CapturingPrinter(MessagePrinter):
    """``MessagePrinter`` subclass that captures structured diagnostics instead of writing to stderr."""

    def __init__(self) -> None:
        super().__init__()
        self.captured: list[tuple[Severity, str, "SourceRefBase | None"]] = []

    # systemrdl-compiler 1.27+ API
    def print_message(self, severity, text, src_ref):  # type: ignore[override]
        self.captured.append((severity, text, src_ref))


def _severity_to_lsp(sev: Severity) -> DiagnosticSeverity:
    if sev == Severity.ERROR or sev == Severity.FATAL:
        return DiagnosticSeverity.Error
    if sev == Severity.WARNING:
        return DiagnosticSeverity.Warning
    if sev == Severity.INFO:
        return DiagnosticSeverity.Information
    return DiagnosticSeverity.Hint


def _src_ref_to_range(src_ref: "SourceRefBase | None") -> Range:
    """Convert a ``SourceRefBase`` (1-based line/col) to an LSP ``Range`` (0-based).

    ``systemrdl-compiler`` source refs may carry partial info (e.g., file-level errors with no
    column). We coerce to a sensible single-character range.
    """
    if src_ref is None:
        return Range(start=Position(line=0, character=0), end=Position(line=0, character=1))

    start_line = max(0, (getattr(src_ref, "start_line", 1) or 1) - 1)
    start_col = max(0, (getattr(src_ref, "start_col", 1) or 1) - 1)
    end_line_raw = getattr(src_ref, "end_line", None) or (start_line + 1)
    end_col_raw = getattr(src_ref, "end_col", None) or ((start_col + 1) + 1)
    end_line = max(0, end_line_raw - 1)
    end_col = max(0, end_col_raw - 1)

    if (end_line, end_col) <= (start_line, start_col):
        end_col = start_col + 1

    return Range(
        start=Position(line=start_line, character=start_col),
        end=Position(line=end_line, character=end_col),
    )


def _uri_to_path(uri: str) -> pathlib.Path:
    parsed = urllib.parse.urlparse(uri)
    if parsed.scheme not in ("file", ""):
        raise ValueError(f"Only file:// URIs are supported, got {uri!r}")
    return pathlib.Path(urllib.parse.unquote(parsed.path))


def _path_to_uri(p: pathlib.Path) -> str:
    return p.resolve().as_uri()


def _elaborate(path: pathlib.Path) -> list[tuple[Severity, str, "SourceRefBase | None"]]:
    """Run parse + elaboration on a single .rdl file, return captured messages.

    Errors are returned as messages, not raised. Behavior matches the design constraint:
    "Live elaboration requires a valid top-level addrmap. While the user types the file
    is often invalid. Strategy: keep last-good elaboration and surface diagnostics."
    """
    printer = CapturingPrinter()
    compiler = RDLCompiler(message_printer=printer)
    try:
        compiler.compile_file(str(path))
        # Elaboration may add more diagnostics. We attempt elaborate but tolerate failure.
        try:
            compiler.elaborate()
        except RDLCompileError:
            # Elaboration errors were already reported through the printer.
            pass
    except RDLCompileError:
        # Parse errors were already reported through the printer.
        pass
    except Exception as exc:  # defensive: never crash the server on a single file
        logger.exception("unexpected error while compiling %s", path)
        printer.captured.append((Severity.ERROR, f"internal: {exc}", None))
    return printer.captured


def _publish_for_uri(server: LanguageServer, uri: str) -> None:
    """Re-elaborate the file at ``uri`` and publish diagnostics."""
    try:
        path = _uri_to_path(uri)
    except ValueError as exc:
        logger.warning("ignoring non-file URI %s: %s", uri, exc)
        return

    if not path.exists():
        # The buffer may exist only in the editor; pygls already mirrors text via DidChange,
        # but for v0.1 we only re-read from disk. The user must save once before diagnostics.
        # Week 2: write the buffer to a temp file (or use systemrdl-compiler's compile_string).
        return

    messages = _elaborate(path)

    diagnostics: list[Diagnostic] = []
    for sev, text, src_ref in messages:
        # Only surface messages that point at this URI; cross-file errors will be handled
        # in Week 2 by publishing per-uri diagnostic batches.
        if src_ref is not None and getattr(src_ref, "filename", None):
            try:
                ref_uri = _path_to_uri(pathlib.Path(src_ref.filename))
            except (OSError, ValueError):
                ref_uri = uri
            if ref_uri != uri:
                continue
        diagnostics.append(
            Diagnostic(
                range=_src_ref_to_range(src_ref),
                severity=_severity_to_lsp(sev),
                source=SERVER_NAME,
                message=text,
            )
        )

    server.text_document_publish_diagnostics(
        PublishDiagnosticsParams(uri=uri, diagnostics=diagnostics)
    )


def build_server() -> LanguageServer:
    """Construct the configured ``LanguageServer``. Separated for ease of testing."""
    server = LanguageServer(SERVER_NAME, SERVER_VERSION)

    @server.feature(TEXT_DOCUMENT_DID_OPEN)
    def _on_open(ls: LanguageServer, params: DidOpenTextDocumentParams) -> None:
        _publish_for_uri(ls, params.text_document.uri)

    @server.feature(TEXT_DOCUMENT_DID_SAVE)
    def _on_save(ls: LanguageServer, params: DidSaveTextDocumentParams) -> None:
        _publish_for_uri(ls, params.text_document.uri)

    @server.feature(TEXT_DOCUMENT_DID_CHANGE)
    def _on_change(ls: LanguageServer, params: DidChangeTextDocumentParams) -> None:
        # v0.1 reads from disk only — Week 2 will switch to in-memory buffer or a temp file
        # and add 300ms debounce. For now, didChange just retriggers a disk-based pass.
        _publish_for_uri(ls, params.text_document.uri)

    @server.feature(WORKSPACE_DID_CHANGE_CONFIGURATION)
    def _on_config_change(ls: LanguageServer, params: DidChangeConfigurationParams) -> None:
        # No-op for v0.1: there are no settings the server reads yet. Week 2 wires
        # `systemrdl-pro.includePaths` into the RDLCompiler search path. Registering
        # a handler silences pygls's "Ignoring notification for unknown method" warning.
        del ls, params

    return server
