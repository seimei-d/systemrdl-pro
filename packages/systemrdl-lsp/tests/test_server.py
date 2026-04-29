"""Tests for the systemrdl-lsp server.

Every test exercises one user-visible behaviour. We deliberately avoid mocking
``systemrdl-compiler`` — eng review #2 (decision log) requires real-elaboration
coverage.
"""

from __future__ import annotations

import pathlib
import textwrap

import pytest
from systemrdl.messages import Severity

from systemrdl_lsp.server import (
    ElaborationCache,
    _compile_text,
    _document_symbols,
    _elaborate,
    _hover_text_for_node,
    _node_at_position,
    _src_ref_to_range,
)

VALID_RDL = textwrap.dedent("""
    addrmap simple {
        reg {
            field {
                sw = rw;
                hw = r;
            } enable[0:0] = 0;
        } CTRL @ 0x0;
    };
""").strip()

SAMPLE_RDL = textwrap.dedent("""
    addrmap chip {
        reg {
            field { sw = rw; hw = r; } enable[0:0] = 1;
            field { sw = r;  hw = w; } busy[1:1]   = 0;
        } CTRL @ 0x0000;

        reg {
            field { sw = rw; hw = r; } addr[31:2]  = 0x100;
        } DMA_BASE_ADDR @ 0x0010;
    };
""").strip()

INVALID_RDL = textwrap.dedent("""
    addrmap broken {
        not_a_keyword;
    };
""").strip()


@pytest.fixture
def tmp_rdl(tmp_path):
    def _write(content: str, name: str = "x.rdl") -> pathlib.Path:
        p = tmp_path / name
        p.write_text(content, encoding="utf-8")
        return p

    return _write


# ---------------------------------------------------------------------------
# Diagnostics path (carried over from v0.1)
# ---------------------------------------------------------------------------


def test_valid_file_produces_no_errors(tmp_rdl):
    msgs = _elaborate(tmp_rdl(VALID_RDL))
    errors = [m for m in msgs if m[0] in (Severity.ERROR, Severity.FATAL)]
    assert errors == [], f"expected no errors on valid file; got {errors}"


def test_invalid_file_reports_error_with_location(tmp_rdl):
    msgs = _elaborate(tmp_rdl(INVALID_RDL))
    errors = [m for m in msgs if m[0] in (Severity.ERROR, Severity.FATAL)]
    assert errors, "expected at least one error on invalid file"
    sev, text, src_ref = errors[0]
    assert src_ref is not None
    assert text


def test_src_ref_resolves_to_correct_line(tmp_rdl):
    rdl_with_error_on_line_3 = "addrmap a {\n    reg {} CTRL;\n    junk_token;\n};\n"
    msgs = _elaborate(tmp_rdl(rdl_with_error_on_line_3, "x.rdl"))
    errors = [m for m in msgs if m[0] in (Severity.ERROR, Severity.FATAL) and m[2] is not None]
    assert errors
    rng = _src_ref_to_range(errors[0][2])
    assert rng.start.line == 2, f"expected LSP line 2 (file line 3), got {rng.start.line}"
    assert rng.end.character > rng.start.character


def test_missing_file_returns_message_not_crash(tmp_path):
    missing = tmp_path / "does-not-exist.rdl"
    msgs = _elaborate(missing)
    assert msgs


# ---------------------------------------------------------------------------
# In-memory buffer compile (W2-1)
# ---------------------------------------------------------------------------


def test_compile_text_returns_root_on_valid_buffer(tmp_path):
    """Compiling a buffer (without saving) yields a usable RootNode."""
    uri = (tmp_path / "x.rdl").as_uri()
    messages, root, tmp_file = _compile_text(uri, VALID_RDL)
    try:
        errors = [m for m in messages if m.severity in (Severity.ERROR, Severity.FATAL)]
        assert errors == []
        assert root is not None
    finally:
        tmp_file.unlink(missing_ok=True)


def test_compile_text_returns_no_root_on_parse_error(tmp_path):
    """A parse error leaves root=None and reports diagnostics."""
    uri = (tmp_path / "x.rdl").as_uri()
    messages, root, tmp_file = _compile_text(uri, INVALID_RDL)
    try:
        assert root is None
        errors = [m for m in messages if m.severity in (Severity.ERROR, Severity.FATAL)]
        assert errors, "expected at least one error message"
    finally:
        tmp_file.unlink(missing_ok=True)


def test_compile_text_translates_temp_path_to_original_uri(tmp_path):
    """Diagnostics carry the original file path, not the LSP-internal temp path."""
    original = tmp_path / "real.rdl"
    uri = original.as_uri()
    messages, _root, tmp_file = _compile_text(uri, INVALID_RDL)
    try:
        with_path = [m for m in messages if m.file_path is not None]
        assert with_path, "expected at least one message with a resolved file path"
        for m in with_path:
            assert m.file_path == original, (
                f"file_path should be the workspace path; got temp leak {m.file_path}"
            )
    finally:
        tmp_file.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# ElaborationCache (W2-2)
# ---------------------------------------------------------------------------


def test_cache_unlinks_old_temp_file_on_replace(tmp_path):
    """Replacing a cache entry must unlink the previous entry's temp file."""
    uri = (tmp_path / "x.rdl").as_uri()

    _msgs1, root1, tmp1 = _compile_text(uri, VALID_RDL)
    assert tmp1.exists()

    cache = ElaborationCache()
    cache.put(uri, root1, VALID_RDL, tmp1)
    assert cache.get(uri) is not None

    _msgs2, root2, tmp2 = _compile_text(uri, VALID_RDL)
    cache.put(uri, root2, VALID_RDL, tmp2)
    assert not tmp1.exists(), "old temp file should be unlinked on cache replace"
    assert tmp2.exists(), "new temp file must be alive while cached"
    cache.clear()
    assert not tmp2.exists(), "clear() must unlink all cached temp files"


def test_cache_clear_unlinks_all_temps(tmp_path):
    cache = ElaborationCache()
    paths = []
    for i in range(3):
        uri = (tmp_path / f"x{i}.rdl").as_uri()
        _msgs, root, tmp = _compile_text(uri, VALID_RDL)
        cache.put(uri, root, VALID_RDL, tmp)
        paths.append(tmp)
    for p in paths:
        assert p.exists()
    cache.clear()
    for p in paths:
        assert not p.exists()


# ---------------------------------------------------------------------------
# Hover (W2-3)
# ---------------------------------------------------------------------------


def test_hover_on_field_includes_access_and_reset(tmp_path):
    uri = (tmp_path / "x.rdl").as_uri()
    _msgs, root, tmp = _compile_text(uri, SAMPLE_RDL)
    try:
        # SAMPLE_RDL is 1-based:
        #   line 3: `field { sw = rw; hw = r; } enable[0:0] = 1;`
        # _node_at_position takes 0-based, so pass line=2.
        node = _node_at_position(root, 2, 30)
        assert node is not None, "expected to find a node at the enable field declaration"
        md = _hover_text_for_node(node)
        assert md is not None
        assert "enable" in md, f"hover should describe enable; got {md!r}"
        assert "rw" in md
        assert "0x00000001" in md
    finally:
        tmp.unlink(missing_ok=True)


def test_hover_on_register_includes_address_and_width(tmp_path):
    uri = (tmp_path / "x.rdl").as_uri()
    _msgs, root, tmp = _compile_text(uri, SAMPLE_RDL)
    try:
        # SAMPLE_RDL line 5 (1-based): `} CTRL @ 0x0000;`
        node = _node_at_position(root, 4, 10)
        assert node is not None
        md = _hover_text_for_node(node)
        assert md is not None
        assert "CTRL" in md
        assert "0x00000000" in md
        assert "**width**" in md and "32" in md
    finally:
        tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# documentSymbol (W2-4)
# ---------------------------------------------------------------------------


def test_document_symbols_are_unique(tmp_path):
    """Regression: an earlier implementation iterated children() AND fields() on RegNode,
    yielding every field twice in the outline."""
    uri = (tmp_path / "x.rdl").as_uri()
    _msgs, root, tmp = _compile_text(uri, SAMPLE_RDL)
    try:
        syms = _document_symbols(root)
        # Top-level: one addrmap (chip)
        assert len(syms) == 1
        chip = syms[0]
        assert chip.name == "chip"
        # chip has 2 registers
        assert len(chip.children) == 2
        ctrl = next(c for c in chip.children if c.name == "CTRL")
        # CTRL has exactly 2 fields, no duplicates
        names = [f.name for f in ctrl.children]
        assert names == ["enable", "busy"], f"expected [enable, busy], got {names}"
    finally:
        tmp.unlink(missing_ok=True)


def test_document_symbols_carry_addresses(tmp_path):
    uri = (tmp_path / "x.rdl").as_uri()
    _msgs, root, tmp = _compile_text(uri, SAMPLE_RDL)
    try:
        syms = _document_symbols(root)
        chip = syms[0]
        dma = next(c for c in chip.children if c.name == "DMA_BASE_ADDR")
        assert dma.detail and "0x00000010" in dma.detail
    finally:
        tmp.unlink(missing_ok=True)
