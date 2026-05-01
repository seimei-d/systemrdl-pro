"""Diagnostic conversion + LSP publishing + cross-tree address-conflict scan."""

from __future__ import annotations

import logging
import pathlib
from typing import TYPE_CHECKING, Any

from lsprotocol.types import (
    Diagnostic,
    DiagnosticSeverity,
    Position,
    PublishDiagnosticsParams,
    Range,
)
from pygls.lsp.server import LanguageServer
from systemrdl.messages import Severity

from ._uri import _uri_to_path
from .compile import CompilerMessage

if TYPE_CHECKING:
    from systemrdl.node import RootNode

logger = logging.getLogger(__name__)

SERVER_NAME = "systemrdl-lsp"


def _severity_to_lsp(sev: Severity) -> DiagnosticSeverity:
    if sev in (Severity.ERROR, Severity.FATAL):
        return DiagnosticSeverity.Error
    if sev == Severity.WARNING:
        return DiagnosticSeverity.Warning
    if sev == Severity.INFO:
        return DiagnosticSeverity.Information
    return DiagnosticSeverity.Hint


def _build_range(line_1b: int, col_start_1b: int, col_end_1b: int) -> Range:
    line_0b = max(0, line_1b - 1)
    start_0b = max(0, col_start_1b - 1)
    end_0b = max(start_0b + 1, col_end_1b)
    return Range(
        start=Position(line=line_0b, character=start_0b),
        end=Position(line=line_0b, character=end_0b),
    )


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


def _address_conflict_diagnostics(
    roots: list[RootNode], path: pathlib.Path
) -> list[CompilerMessage]:
    """Detect overlapping register address ranges within the same parent and
    surface them as warning diagnostics. systemrdl-compiler usually catches
    this, but only for direct sibling overlaps; we extend the check across
    the full elaborated tree for defence-in-depth.
    """
    from systemrdl.node import RegNode

    from .serialize import _hex

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
