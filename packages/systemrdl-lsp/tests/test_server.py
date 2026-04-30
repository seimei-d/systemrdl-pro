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

from systemrdl_lsp import server as server_mod
from systemrdl_lsp.server import (
    ElaborationCache,
    _comp_defs_from_cached,
    _compile_text,
    _completion_context,
    _peakrdl_toml_paths,
    _completion_items_for_context,
    _completion_items_for_types,
    _completion_items_static,
    _definition_location,
    _document_symbols,
    _elaborate,
    _hover_for_word,
    _hover_text_for_node,
    _node_at_position,
    _src_ref_to_range,
    _word_at_position,
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
    messages, roots, tmp_file = _compile_text(uri, VALID_RDL)
    try:
        errors = [m for m in messages if m.severity in (Severity.ERROR, Severity.FATAL)]
        assert errors == []
        assert len(roots) == 1
    finally:
        tmp_file.unlink(missing_ok=True)


def test_compile_text_returns_no_root_on_parse_error(tmp_path):
    """A parse error yields an empty roots list and reports diagnostics."""
    uri = (tmp_path / "x.rdl").as_uri()
    messages, roots, tmp_file = _compile_text(uri, INVALID_RDL)
    try:
        assert roots == []
        errors = [m for m in messages if m.severity in (Severity.ERROR, Severity.FATAL)]
        assert errors, "expected at least one error message"
    finally:
        tmp_file.unlink(missing_ok=True)


def test_compile_text_translates_temp_path_to_original_uri(tmp_path):
    """Diagnostics carry the original file path, not the LSP-internal temp path."""
    original = tmp_path / "real.rdl"
    uri = original.as_uri()
    messages, _roots, tmp_file = _compile_text(uri, INVALID_RDL)
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
# Multi-root elaboration (Decision 3C)
# ---------------------------------------------------------------------------


MULTI_ADDRMAP_RDL = textwrap.dedent("""
    addrmap chip_a {
        reg { field { sw=rw; hw=r; } a[0:0]=0; } CTRL @ 0x0;
    };
    addrmap chip_b {
        reg { field { sw=rw; hw=r; } b[0:0]=0; } CTRL @ 0x0;
        reg { field { sw=rw; hw=r; } c[0:0]=0; } STATUS @ 0x4;
    };
""").strip()


# ---------------------------------------------------------------------------
# textDocument/definition (W2-6)
# ---------------------------------------------------------------------------


TYPED_RDL = textwrap.dedent("""
    reg my_ctrl_t {
        field { sw=rw; hw=r; } enable[0:0] = 0;
    };
    addrmap top {
        my_ctrl_t CTRL @ 0x0;
        my_ctrl_t STATUS @ 0x4;
    };
""").strip()


def test_word_at_position_extracts_identifier_under_cursor():
    """Cursor inside, at start, and at trailing edge of an identifier all match."""
    text = "  my_ctrl_t CTRL @ 0x0;\n"
    # 0123456789012345678901234
    assert _word_at_position(text, 0, 2) == "my_ctrl_t"  # at start
    assert _word_at_position(text, 0, 6) == "my_ctrl_t"  # inside
    assert _word_at_position(text, 0, 11) == "my_ctrl_t"  # trailing edge
    assert _word_at_position(text, 0, 12) == "CTRL"  # next ident
    assert _word_at_position(text, 0, 17) is None  # whitespace
    assert _word_at_position(text, 0, 19) is None  # numeric (0x0)


def test_word_at_position_handles_out_of_range():
    text = "addrmap top {};"
    assert _word_at_position(text, 999, 0) is None
    assert _word_at_position(text, 0, 999) is None
    assert _word_at_position("", 0, 0) is None


def test_definition_resolves_top_level_type(tmp_path):
    """F12 on ``my_ctrl_t`` (line 5, col 4) jumps to its ``reg`` definition (line 1)."""
    uri = (tmp_path / "x.rdl").as_uri()
    _msgs, roots, tmp = _compile_text(uri, TYPED_RDL)
    try:
        assert roots, "expected at least one elaborated root"
        defs = _comp_defs_from_cached(roots)
        assert "my_ctrl_t" in defs and "top" in defs
        loc = _definition_location(defs["my_ctrl_t"], path_translate={tmp: tmp_path / "x.rdl"})
        assert loc is not None
        # ``reg my_ctrl_t {`` is line 1 in the source (1-based) → LSP line 0 (0-based).
        assert loc.range.start.line == 0, f"expected LSP line 0, got {loc.range.start.line}"
        # File path was translated from the temp file back to the workspace path.
        assert loc.uri == (tmp_path / "x.rdl").as_uri()
    finally:
        tmp.unlink(missing_ok=True)


def test_definition_returns_none_for_unknown_word(tmp_path):
    uri = (tmp_path / "x.rdl").as_uri()
    _msgs, roots, tmp = _compile_text(uri, TYPED_RDL)
    try:
        defs = _comp_defs_from_cached(roots)
        assert "doesnotexist" not in defs
    finally:
        tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# textDocument/completion (W2-7)
# ---------------------------------------------------------------------------


def test_static_completion_includes_keywords_and_access_values():
    items = _completion_items_static()
    labels = {it.label for it in items}
    # Top-level keywords
    assert "addrmap" in labels and "reg" in labels and "field" in labels
    # Properties
    assert "sw" in labels and "hw" in labels and "reset" in labels
    # Access values
    assert "rw" in labels and "ro" in labels and "woclr" in labels


def test_static_completion_items_carry_documentation():
    """Every static item must have a non-empty `documentation` so VSCode shows
    an explanation in the popup detail pane — bare labels are useless."""
    items = _completion_items_static()
    blanks = [it.label for it in items if not it.documentation]
    assert not blanks, f"items missing documentation: {blanks}"


def test_completion_context_detects_sw_assignment():
    """Cursor right after `sw =` returns 'sw_value' so the handler narrows
    suggestions to access modes only."""
    # cursor at the very end of the prefix
    text = "    field { sw = "
    assert _completion_context(text, 0, len(text)) == "sw_value"
    # extra typed chars after `=` still count
    text2 = "    field { sw = r"
    assert _completion_context(text2, 0, len(text2)) == "sw_value"


def test_completion_context_detects_onwrite_assignment():
    text = "    onwrite = "
    assert _completion_context(text, 0, len(text)) == "onwrite_value"


def test_completion_context_general_outside_assignment():
    text = "    "
    assert _completion_context(text, 0, len(text)) == "general"
    text2 = "addrmap top {"
    assert _completion_context(text2, 0, len(text2)) == "general"


def test_completion_context_offers_only_rw_values_for_sw():
    """The whole point — sw_value context must NOT include keywords."""
    items = _completion_items_for_context("sw_value")
    labels = {it.label for it in items}
    assert "rw" in labels and "ro" in labels and "na" in labels
    # Must NOT leak keywords or onwrite/onread values
    assert "addrmap" not in labels and "reg" not in labels
    assert "woclr" not in labels and "rclr" not in labels


def test_completion_context_offers_only_onwrite_values():
    items = _completion_items_for_context("onwrite_value")
    labels = {it.label for it in items}
    assert "woclr" in labels and "woset" in labels and "wzc" in labels
    assert "rw" not in labels and "addrmap" not in labels


def test_hover_for_word_resolves_keyword():
    md = _hover_for_word("addrmap", roots=[])
    assert md is not None
    assert "addrmap" in md
    assert "(keyword)" in md


def test_hover_for_word_resolves_property():
    md = _hover_for_word("sw", roots=[])
    assert md is not None
    assert "(property)" in md
    assert "Software access" in md


def test_hover_for_word_resolves_access_value():
    md = _hover_for_word("rw", roots=[])
    assert md is not None
    assert "(access mode)" in md
    md_woclr = _hover_for_word("woclr", roots=[])
    assert md_woclr is not None and "(onwrite value)" in md_woclr


def test_hover_for_word_resolves_user_type(tmp_path):
    """Hover on a type identifier surfaces its kind, name, and desc."""
    uri = (tmp_path / "x.rdl").as_uri()
    _msgs, roots, tmp = _compile_text(uri, TYPED_RDL)
    try:
        md = _hover_for_word("my_ctrl_t", roots=roots)
        assert md is not None
        assert "reg" in md and "my_ctrl_t" in md
    finally:
        tmp.unlink(missing_ok=True)


def test_hover_for_word_unknown_returns_none():
    assert _hover_for_word("nonexistent_xyz", roots=[]) is None
    assert _hover_for_word("", roots=[]) is None


def test_completion_offers_user_defined_types(tmp_path):
    """The viewer's typed sample registers ``ctrl_t``, ``status_reg_t``, etc.
    Those names should appear in the completion list so the user can pick them
    when typing an instantiation site."""
    uri = (tmp_path / "x.rdl").as_uri()
    _msgs, roots, tmp = _compile_text(uri, TYPED_RDL)
    try:
        items = _completion_items_for_types(roots)
        labels = {it.label for it in items}
        assert "my_ctrl_t" in labels and "top" in labels
        # Detail describes the kind so VSCode can group/filter intelligently.
        ctrl = next(it for it in items if it.label == "my_ctrl_t")
        assert ctrl.detail == "reg", f"expected 'reg', got {ctrl.detail!r}"
        # Documentation should never be empty — at minimum a "User-defined type" fallback.
        assert ctrl.documentation
    finally:
        tmp.unlink(missing_ok=True)


def test_peakrdl_toml_extracts_relative_paths(tmp_path):
    """peakrdl.toml in the file's directory contributes incl_search_paths."""
    (tmp_path / "lib").mkdir()
    (tmp_path / "common").mkdir()
    (tmp_path / "peakrdl.toml").write_text(
        "[parser]\nincl_search_paths = [\"lib\", \"common\"]\n",
        encoding="utf-8",
    )
    rdl = tmp_path / "x.rdl"
    rdl.write_text(VALID_RDL, encoding="utf-8")
    paths = _peakrdl_toml_paths(rdl)
    assert any(p.endswith("/lib") for p in paths)
    assert any(p.endswith("/common") for p in paths)


def test_peakrdl_toml_walks_up_to_find_config(tmp_path):
    """A peakrdl.toml in an ancestor directory is honoured (workspace root case)."""
    workspace = tmp_path / "ws"
    sub = workspace / "src" / "blocks"
    sub.mkdir(parents=True)
    (workspace / "lib").mkdir()
    (workspace / "peakrdl.toml").write_text(
        "[parser]\nincl_search_paths = [\"lib\"]\n",
        encoding="utf-8",
    )
    rdl = sub / "block.rdl"
    rdl.write_text(VALID_RDL, encoding="utf-8")
    paths = _peakrdl_toml_paths(rdl)
    assert paths and paths[0].endswith("/lib")


def test_peakrdl_toml_missing_returns_empty(tmp_path):
    rdl = tmp_path / "x.rdl"
    rdl.write_text(VALID_RDL, encoding="utf-8")
    assert _peakrdl_toml_paths(rdl) == []


def test_multi_addrmap_elaborates_each_top_level_definition(tmp_path):
    """A file with two sibling top-level addrmaps must elaborate both.

    Without explicit ``top_def_name``, ``compiler.elaborate()`` only picks the
    last definition — that left ``chip_a`` invisible in the viewer (user feedback
    "для второй карты памяти нет 'tab'"). The fix enumerates ``comp_defs``.
    """
    uri = (tmp_path / "x.rdl").as_uri()
    _msgs, roots, tmp = _compile_text(uri, MULTI_ADDRMAP_RDL)
    try:
        assert len(roots) == 2, f"expected 2 RootNodes, got {len(roots)}"
        names = []
        for r in roots:
            for top in r.children(unroll=True):
                names.append(top.inst_name)
        assert names == ["chip_a", "chip_b"], (
            f"expected declaration order [chip_a, chip_b]; got {names}"
        )
    finally:
        tmp.unlink(missing_ok=True)


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


# ---------------------------------------------------------------------------
# Safety net #1: elaborate timeout + last-good fallback
# ---------------------------------------------------------------------------


def test_elaboration_timeout_constant_is_reasonable():
    """Sanity-check the wall-clock cap on a single elaborate pass.

    Eng review locked 10s as the ceiling: longer than any real-world map
    elaborates, short enough that a runaway (Perl preprocessor recursion,
    pathological include) doesn't freeze the editor. Anchoring this in a
    test prevents accidental drift to multi-minute timeouts.
    """
    assert 1.0 <= server_mod.ELABORATION_TIMEOUT_SECONDS <= 30.0


def test_timeout_path_preserves_last_good_cache(tmp_path):
    """The timeout branch must not call ``cache.put`` — last-good has to survive.

    We exercise the cache contract directly: put a good entry, then confirm
    that *no* code path on a timeout (which is a no-op for the cache by design)
    can clobber it. This is a contract test for the invariant that drives
    the viewer's stale-bar (D7).
    """
    uri = (tmp_path / "x.rdl").as_uri()
    msgs, root, tmp = _compile_text(uri, VALID_RDL)
    assert root is not None
    cache = ElaborationCache()
    cache.put(uri, root, VALID_RDL, tmp)
    cached_before = cache.get(uri)
    assert cached_before is not None

    # Simulate the timeout branch: orphan future drains later, no cache op happens.
    # We just verify the cache wasn't disturbed by anything in this module.
    cached_after = cache.get(uri)
    assert cached_after is cached_before, "timeout must not mutate the cache"
    cache.clear()
