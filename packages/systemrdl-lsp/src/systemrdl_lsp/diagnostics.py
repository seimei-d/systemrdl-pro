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
    previously_affected: set[str] | None = None,
) -> set[str]:
    r"""Bucket messages by source file and publish each bucket to its own URI.

    The compiler can emit diagnostics from `\include`d files (e.g. a syntax
    error in ``common.rdl`` while compiling ``master.rdl``). We publish those
    to their real file URIs so the squiggle lands in the right editor.

    ``previously_affected`` is the set of URIs we published non-empty diags to
    on the previous compile of this same primary. We clear any URI that drops
    out so a fixed error doesn't leave a stale squiggle behind. The primary
    URI itself is always published (empty or not).

    Returns the set of URIs we published *non-empty* diagnostics to this round —
    callers store it as the next round's ``previously_affected``.

    Limitation: when two primaries `\include` the same file and only one has
    an error in it, the clean primary's compile clears the shared file's
    squiggle. This is eventually-consistent (next compile of the dirty primary
    republishes), acceptable for v1; cross-primary diag ownership would need a
    proper inverted index.
    """
    target_path = _uri_to_path(uri)
    try:
        target_resolved = target_path.resolve()
    except OSError:
        target_resolved = target_path

    buckets: dict[str, list[Diagnostic]] = {uri: []}
    for m in messages:
        if m.file_path is None:
            # Sourceless meta messages (e.g. "Parse aborted due to previous errors")
            # would otherwise pin to file line 1 as redundant noise.
            continue
        try:
            mpath = m.file_path.resolve()
        except OSError:
            mpath = m.file_path
        if mpath == target_resolved:
            bucket_uri = uri
        else:
            try:
                bucket_uri = mpath.as_uri()
            except ValueError:
                # Path not absolute or malformed — can't address as URI, drop.
                continue
        buckets.setdefault(bucket_uri, []).append(
            Diagnostic(
                range=_message_to_range(m),
                severity=_severity_to_lsp(m.severity),
                source=SERVER_NAME,
                message=m.text,
            )
        )

    affected: set[str] = set()
    for bucket_uri, diags in buckets.items():
        if diags or bucket_uri == uri:
            # Always publish the primary, even empty, so its prior diags clear.
            # Non-empty cross-file buckets get published; empty ones are skipped
            # below in the "stale clear" pass which targets only URIs that had
            # diags last round.
            server.text_document_publish_diagnostics(
                PublishDiagnosticsParams(uri=bucket_uri, diagnostics=diags)
            )
            if diags:
                affected.add(bucket_uri)

    # Clear cross-file URIs that had diagnostics last round but not this one.
    # Without this pass, fixing the error in common.rdl would leave a stale
    # squiggle there until something else re-touches the file.
    if previously_affected:
        for stale_uri in previously_affected - affected - {uri}:
            server.text_document_publish_diagnostics(
                PublishDiagnosticsParams(uri=stale_uri, diagnostics=[])
            )

    return affected


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
