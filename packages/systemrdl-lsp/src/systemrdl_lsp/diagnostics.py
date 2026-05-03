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
    # Mirror to the per-URI cache so the LSP 3.17 pull-model
    # ``textDocument/diagnostic`` handler can answer cheaply.
    diag_cache = getattr(server, "_systemrdl_last_diagnostics", None)
    for bucket_uri, diags in buckets.items():
        if diags or bucket_uri == uri:
            server.text_document_publish_diagnostics(
                PublishDiagnosticsParams(uri=bucket_uri, diagnostics=diags)
            )
            if diags:
                affected.add(bucket_uri)
            if isinstance(diag_cache, dict):
                diag_cache[bucket_uri] = list(diags)

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
    """Detect overlapping register address ranges WITHIN ONE addrmap and
    surface them as warning diagnostics.

    Each top-level addrmap is its own address space — a register at 0x0 in
    ``dma_engine`` doesn't conflict with one at 0x0 in ``uart``. We therefore
    scope the overlap check per top-level addrmap, never pooling across.

    Reused-type bodies (lines where multiple elaborated instances share the
    same source ref) are skipped entirely: a regfile type instantiated 6×
    would otherwise spam each internal reg line with a "self-overlap" that
    is just an artifact of the elaboration replay, not a real conflict.
    """
    from collections import Counter

    from systemrdl.node import AddressableNode, RegNode

    from .serialize import _hex

    # Pre-pass: count elaborated nodes per (filename, line) — same heuristic
    # used by hover/inlay-hints to detect reused-type bodies. Walk only via
    # ``children(unroll=True)`` (no separate fields() pass) so RegNode fields
    # aren't double-counted.
    line_uses: Counter[tuple[str, int]] = Counter()

    def collect(node: Any) -> None:
        if isinstance(node, AddressableNode):
            inst = getattr(node, "inst", None)
            sr = getattr(inst, "inst_src_ref", None) or getattr(inst, "def_src_ref", None)
            if sr is not None:
                line_1b = getattr(sr, "line", None)
                fn = getattr(sr, "filename", None)
                if line_1b is not None and fn:
                    line_uses[(str(fn), line_1b)] += 1
        if hasattr(node, "children"):
            try:
                for c in node.children(unroll=True):
                    collect(c)
            except Exception:
                pass

    for r in roots:
        collect(r)

    out: list[CompilerMessage] = []

    def collect_regs_in(node: Any, into: list[tuple[int, int, RegNode]]) -> None:
        """Walk one top-level addrmap subtree, collecting (start, end, reg) tuples."""
        if isinstance(node, RegNode):
            try:
                a = node.absolute_address
                size = max(1, int(getattr(node, "size", 1)))
                into.append((a, a + size, node))
            except Exception:
                pass
        if hasattr(node, "children"):
            for c in node.children(unroll=True):
                collect_regs_in(c, into)

    # Per top-level addrmap: own flat list, own overlap check.
    for r in roots:
        for top in r.children(unroll=True):
            flat: list[tuple[int, int, RegNode]] = []
            collect_regs_in(top, flat)
            flat.sort(key=lambda t: t[0])
            for i in range(1, len(flat)):
                prev_start, prev_end, prev_node = flat[i - 1]
                cur_start, cur_end, cur_node = flat[i]
                if cur_start >= prev_end:
                    continue
                inst = getattr(cur_node, "inst", None)
                src_ref = getattr(inst, "inst_src_ref", None) or getattr(
                    inst, "def_src_ref", None
                )
                ref_filename = getattr(src_ref, "filename", None)
                line_1b = getattr(src_ref, "line", None)
                if not (line_1b and ref_filename and pathlib.Path(ref_filename) == path):
                    continue
                # Skip reused-type body lines — the warning would land on a
                # line that isn't actually responsible for the address.
                if line_uses.get((str(ref_filename), line_1b), 0) > 1:
                    continue
                sel = getattr(src_ref, "line_selection", None) or (1, 1)
                try:
                    cs, ce = sel
                except (TypeError, ValueError):
                    cs = ce = 1
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
