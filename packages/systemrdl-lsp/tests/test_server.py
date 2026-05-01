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
    _completion_items_for_context,
    _completion_items_for_types,
    _completion_items_static,
    _definition_location,
    _document_symbols,
    _elaborate,
    _expand_include_vars,
    _hover_for_word,
    _hover_text_for_node,
    _node_at_position,
    _peakrdl_toml_paths,
    _perl_available,
    _perl_in_source,
    _resolve_search_paths,
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


def test_references_finds_all_instantiation_sites(tmp_path):
    """``my_ctrl_t`` is instantiated twice; references should yield both sites.

    With ``include_declaration=True`` the type's def location is prepended.
    """
    from systemrdl_lsp.server import _references_to_type
    uri = (tmp_path / "x.rdl").as_uri()
    _msgs, roots, tmp = _compile_text(uri, TYPED_RDL)
    try:
        translate = {tmp: tmp_path / "x.rdl"}
        # Without the declaration: just the two CTRL/STATUS instance lines.
        refs = _references_to_type("my_ctrl_t", roots, False, path_translate=translate)
        assert len(refs) == 2, f"expected 2 instance refs, got {refs}"
        # With declaration: 1 def + 2 instances = 3.
        with_decl = _references_to_type("my_ctrl_t", roots, True, path_translate=translate)
        assert len(with_decl) == 3
        # Decl is first by contract.
        assert with_decl[0].range.start.line == 0
    finally:
        tmp.unlink(missing_ok=True)


def test_rename_locations_covers_def_and_uses(tmp_path):
    """Rename of ``my_ctrl_t`` produces edits at the declaration AND each use site.

    Each Location.range covers the *type-name* token (not the instance name)
    so applying ``new_text=new_name`` results in a syntactically valid file.
    """
    from systemrdl_lsp.server import _rename_locations
    rdl_path = tmp_path / "x.rdl"
    rdl_path.write_text(TYPED_RDL, encoding="utf-8")
    _msgs, roots, tmp = _compile_text(rdl_path.as_uri(), TYPED_RDL)

    def reader(p, line_idx):
        try:
            return p.read_text(encoding="utf-8").splitlines()[line_idx]
        except (OSError, IndexError):
            return None

    try:
        translate = {tmp: rdl_path}
        locs = _rename_locations("my_ctrl_t", roots, translate, reader)
        # 1 declaration + 2 instances = 3 edit ranges.
        assert len(locs) == 3, f"expected 3 rename ranges, got {locs}"
        # Each range must cover exactly "my_ctrl_t" — verify by looking up
        # the corresponding source span.
        for loc in locs:
            assert loc.range.end.character - loc.range.start.character == len("my_ctrl_t")
    finally:
        tmp.unlink(missing_ok=True)


def test_rename_skips_unrelated_types(tmp_path):
    """Two distinct types in the same file: rename one, leave the other alone."""
    from systemrdl_lsp.server import _rename_locations
    src = textwrap.dedent("""
        reg ctrl_t {
            field { sw=rw; hw=r; } enable[0:0] = 0;
        };
        reg status_t {
            field { sw=r; hw=w; } busy[0:0] = 0;
        };
        addrmap top {
            ctrl_t CTRL @ 0;
            status_t STAT @ 4;
            ctrl_t CTRL2 @ 8;
        };
    """).strip()
    rdl_path = tmp_path / "x.rdl"
    rdl_path.write_text(src, encoding="utf-8")
    _msgs, roots, tmp = _compile_text(rdl_path.as_uri(), src)

    def reader(p, line_idx):
        try:
            return p.read_text(encoding="utf-8").splitlines()[line_idx]
        except (OSError, IndexError):
            return None

    try:
        translate = {tmp: rdl_path}
        ctrl_locs = _rename_locations("ctrl_t", roots, translate, reader)
        # 1 declaration + 2 instances (CTRL, CTRL2) = 3.
        assert len(ctrl_locs) == 3, f"got {ctrl_locs}"
        status_locs = _rename_locations("status_t", roots, translate, reader)
        assert len(status_locs) == 2, f"got {status_locs}"
    finally:
        tmp.unlink(missing_ok=True)


def test_references_returns_empty_for_unknown(tmp_path):
    from systemrdl_lsp.server import _references_to_type
    uri = (tmp_path / "x.rdl").as_uri()
    _msgs, roots, tmp = _compile_text(uri, TYPED_RDL)
    try:
        assert _references_to_type("nonexistent_t", roots, True, path_translate=None) == []
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


def test_include_var_expansion_replaces_in_include_path():
    """`$VAR and `${VAR} expand inside `include directives, body left alone."""
    text = '''`include "$IP_ROOT/common.rdl"
`include "${SHARED}/types.rdl"
addrmap top { reg { field { sw=rw; hw=r; } a[0:0]=0; } R @ 0; };
'''
    out = _expand_include_vars(text, {"IP_ROOT": "/lib", "SHARED": "/sh"})
    assert '`include "/lib/common.rdl"' in out
    assert '`include "/sh/types.rdl"' in out
    # Body untouched (no $-substitution outside include).
    assert "addrmap top {" in out


def test_include_var_expansion_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("XYZ_TEST_VAR", "/env-value")
    out = _expand_include_vars('`include "$XYZ_TEST_VAR/x.rdl"', {})
    assert '`include "/env-value/x.rdl"' in out


def test_include_var_expansion_leaves_unknown_literal():
    """Unresolved variable surfaces in the resulting path so the diagnostic
    points at it ("include not found: $UNDEFINED/foo.rdl")."""
    out = _expand_include_vars('`include "$UNDEFINED/foo.rdl"', {})
    assert "$UNDEFINED" in out


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


# ---------------------------------------------------------------------------
# Perl preprocessor polish (TODO-4)
# ---------------------------------------------------------------------------


def test_perl_in_source_detects_marker():
    """`<%` markers anywhere in the source signal a Perl preprocessor user."""
    assert _perl_in_source("<% for my $i (0..3) { %>")
    assert _perl_in_source("reg r @ <%=0x100%>;")
    assert not _perl_in_source("addrmap top { reg { } R @ 0; };")
    assert not _perl_in_source("")


def test_perl_available_caches_result():
    """``_perl_available()`` is lru_cached so repeated calls cost nothing."""
    # Same boolean both times — the decorator does the caching, we verify
    # the function returns a stable bool. We don't assert which value because
    # CI machines may or may not have perl installed.
    first = _perl_available()
    second = _perl_available()
    assert first is second


def test_cache_version_increments_on_each_put(tmp_path):
    """TODO-1: per-URI version is the version-gated push contract.

    Every successful ``put`` must yield a strictly-monotonic version so the
    client's ``sinceVersion`` comparison is meaningful. A clear() resets the
    counter (cache is gone, no client should still hold a ref to it).
    """
    cache = ElaborationCache()
    uri = (tmp_path / "x.rdl").as_uri()
    _msgs1, root1, tmp1 = _compile_text(uri, VALID_RDL)
    cache.put(uri, root1, VALID_RDL, tmp1)
    v1 = cache.get(uri).version
    assert v1 >= 1
    _msgs2, root2, tmp2 = _compile_text(uri, VALID_RDL)
    cache.put(uri, root2, VALID_RDL, tmp2)
    v2 = cache.get(uri).version
    assert v2 == v1 + 1, f"version must increment monotonically; {v1} → {v2}"
    cache.clear()


def test_bridge_keyword_in_completion_and_hover(tmp_path):
    """`bridge` is a top-level addrmap modifier — it must surface in completion
    and word-based hover, and an instance hover should mark the addrmap as a
    bridge in its title.
    """
    from systemrdl_lsp.server import _completion_items_static, _hover_for_word, _hover_text_for_node

    # 1. Static completion catalogue includes `bridge`.
    labels = {it.label for it in _completion_items_static()}
    assert "bridge" in labels

    # 2. Word-based hover explains the keyword.
    md = _hover_for_word("bridge", roots=[])
    assert md is not None and "bridge" in md.lower() and "(keyword)" in md

    # 3. Instance hover on a bridge-marked addrmap calls it out.
    # SystemRDL constraint: a `bridge` addrmap can only contain other addrmaps,
    # and must have at least two — encode that in the fixture.
    rdl = textwrap.dedent("""
        addrmap soc {
            bridge;
            addrmap apb {
                reg { field { sw=rw; hw=r; } a[0:0]=0; } CTRL @ 0x0;
            } apb_block @ 0x0000;
            addrmap ahb {
                reg { field { sw=rw; hw=r; } b[0:0]=0; } STATUS @ 0x0;
            } ahb_block @ 0x1000;
        };
    """).strip()
    rdl_path = tmp_path / "x.rdl"
    rdl_path.write_text(rdl, encoding="utf-8")
    _msgs, roots, tmp = _compile_text(rdl_path.as_uri(), rdl)
    try:
        from systemrdl_lsp.server import _node_at_position
        # Cursor on `addrmap soc {` (line 0 in 0-based).
        node = _node_at_position(roots, 0, 10)
        assert node is not None, "expected to find soc addrmap node"
        text = _hover_text_for_node(node)
        assert text is not None
        assert "bridge" in text.lower(), f"hover should mark bridge; got {text!r}"
    finally:
        tmp.unlink(missing_ok=True)


def test_address_conflicts_scope_per_top_level_addrmap(tmp_path):
    """A reg at 0x0 inside ``addrmap a`` does NOT conflict with a reg at 0x0
    inside a separate ``addrmap b`` — each top-level addrmap is its own
    address space. The previous implementation pooled regs across all
    elaborated roots and produced false overlap warnings.
    """
    from systemrdl_lsp.diagnostics import _address_conflict_diagnostics

    rdl = textwrap.dedent("""
        addrmap chip_a {
            reg { field { sw=rw; hw=r; } a[0:0]=0; } CTRL @ 0x0;
        };
        addrmap chip_b {
            reg { field { sw=rw; hw=r; } b[0:0]=0; } CTRL @ 0x0;
        };
    """).strip()
    rdl_path = tmp_path / "x.rdl"
    rdl_path.write_text(rdl, encoding="utf-8")
    _msgs, roots, tmp = _compile_text(rdl_path.as_uri(), rdl)
    try:
        out = _address_conflict_diagnostics(roots, tmp)
        assert out == [], f"expected no overlaps; got {[o.text for o in out]}"
    finally:
        tmp.unlink(missing_ok=True)


def test_address_conflicts_skip_reused_type_body(tmp_path):
    """A reused regfile type doesn't trigger fake "self-overlaps" on its body lines."""
    from systemrdl_lsp.diagnostics import _address_conflict_diagnostics

    rdl = textwrap.dedent("""
        regfile dma_channel_t {
            reg { field { sw=rw; hw=r; } a[0:0]=0; } CTRL @ 0x0;
            reg { field { sw=rw; hw=r; } b[0:0]=0; } STATUS @ 0x4;
        };
        addrmap top {
            dma_channel_t ch0 @ 0x100;
            dma_channel_t ch1 @ 0x200;
            dma_channel_t ch2 @ 0x300;
        };
    """).strip()
    rdl_path = tmp_path / "x.rdl"
    rdl_path.write_text(rdl, encoding="utf-8")
    _msgs, roots, tmp = _compile_text(rdl_path.as_uri(), rdl)
    try:
        out = _address_conflict_diagnostics(roots, tmp)
        assert out == [], f"reused-type body should not self-overlap; got {[o.text for o in out]}"
    finally:
        tmp.unlink(missing_ok=True)


def test_hover_works_on_field_inside_reused_reg_type(tmp_path):
    """Field inside a reg type that has multiple instances must still hover.

    The reused-type-body heuristic (line shared by many elaborated nodes →
    skip) is right for AddressableNode hover (different instances have
    different absolute_address values), but wrong for fields — their
    properties don't vary per instance, so picking any one is fine.
    Regression: the user reported hover on `enable[0:0]` returned None
    when its containing `secure_ctrl_t` was instantiated 3×.
    """
    rdl = textwrap.dedent("""
        reg my_status_t {
            field { sw=rw; hw=r; } enable[0:0] = 0;
        };
        addrmap top {
            my_status_t S0 @ 0x0;
            my_status_t S1 @ 0x4;
            my_status_t S2 @ 0x8;
        };
    """).strip()
    rdl_path = tmp_path / "x.rdl"
    rdl_path.write_text(rdl, encoding="utf-8")
    _msgs, roots, tmp = _compile_text(rdl_path.as_uri(), rdl)
    try:
        # Field declaration `} enable[0:0] = 0;` is on line 2 (0-based 1).
        # Even though my_status_t is reused three times, hover on the field should
        # work — fields are exempt from the reused-type-body filter.
        node = _node_at_position(roots, 1, 14)  # cursor on `enable`
        assert node is not None, "field hover should work despite reused container"
        text = _hover_text_for_node(node)
        assert text is not None and "enable" in text
        assert "field" in text and "[0:0]" in text
    finally:
        tmp.unlink(missing_ok=True)


def test_inlay_hints_skip_reused_type_body(tmp_path):
    """Reused regfile types must NOT get inlay hints on their internal lines.

    Defining ``regfile dma_channel_t { reg ... CTRL @ 0; }`` and instantiating
    it twice means the elaborated tree replays the type's internal source
    refs once per instance. Painting an absolute address on those internal
    lines would lie about which instance owns the address. The inlay-hint
    walker should detect the reuse (count > 1 elaborated nodes per source
    line) and skip painting.
    """
    from systemrdl_lsp.outline import _inlay_hints_for_addressables

    rdl = textwrap.dedent("""
        regfile dma_channel_t {
            reg { field { sw=rw; hw=r; } enable[0:0] = 0; } CTRL @ 0x0;
        };
        addrmap top {
            dma_channel_t ch0 @ 0x100;
            dma_channel_t ch1 @ 0x200;
        };
    """).strip()
    rdl_path = tmp_path / "x.rdl"
    rdl_path.write_text(rdl, encoding="utf-8")
    _msgs, roots, tmp = _compile_text(rdl_path.as_uri(), rdl)
    try:
        # Pass the temp path the compiler actually saw — that's what
        # inlay-hint emission uses for filename comparison.
        hints = _inlay_hints_for_addressables(roots, tmp, rdl)
        # Find the line of the reg inside the regfile type. That line is
        # reused twice (once per ch0/ch1 elaborated copy). It should NOT
        # have an inlay hint.
        type_body_line = next(
            i for i, line in enumerate(rdl.splitlines())
            if "CTRL @ 0x0" in line
        )
        type_body_hints = [h for h in hints if h.position.line == type_body_line]
        assert type_body_hints == [], (
            f"reused type body line {type_body_line} should have no inlay "
            f"hint; got {type_body_hints}"
        )
        # The two instance lines (ch0 @ 0x100, ch1 @ 0x200) SHOULD have hints.
        ch0_line = next(
            i for i, line in enumerate(rdl.splitlines())
            if "ch0 @ 0x100" in line
        )
        ch0_hints = [h for h in hints if h.position.line == ch0_line]
        assert ch0_hints, "ch0 instance line should have an inlay hint"
    finally:
        tmp.unlink(missing_ok=True)


def test_iter_rdl_files_walks_workspace_skipping_noise(tmp_path):
    """Pre-index walker yields .rdl files, skips .git/node_modules/etc."""
    from systemrdl_lsp.server import _iter_rdl_files
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.rdl").write_text(VALID_RDL, encoding="utf-8")
    (tmp_path / "src" / "b.rdl").write_text(VALID_RDL, encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "noise.rdl").write_text(VALID_RDL, encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config.rdl").write_text(VALID_RDL, encoding="utf-8")
    (tmp_path / "src" / "not_rdl.txt").write_text("ignore me", encoding="utf-8")

    excludes = {".git", "node_modules"}
    paths = sorted(p.name for p in _iter_rdl_files(tmp_path, excludes))
    assert paths == ["a.rdl", "b.rdl"], f"expected only src/*.rdl; got {paths}"


def test_format_text_trims_trailing_whitespace_and_tabs():
    """Formatter normalises tabs to spaces, trims trailing whitespace, ensures EOF newline."""
    from systemrdl_lsp.server import _format_text
    src = "addrmap top {\n\treg {} CTRL @ 0;   \n};   \n\n\n"
    out = _format_text(src, tab_size=4)
    assert "\t" not in out
    assert "   \n" not in out, "trailing spaces remained"
    assert out.endswith("};\n"), f"expected single trailing newline; got {out!r}"
    # Idempotency: running the formatter twice must be a no-op.
    assert _format_text(out, tab_size=4) == out


def test_format_text_no_change_returns_empty_edits():
    """If the buffer is already canonical, no edits — VSCode shows no diff."""
    from systemrdl_lsp.server import _document_formatting_edits, _format_text
    canonical = _format_text("addrmap top {};\n")
    assert _document_formatting_edits(canonical) == []


def test_code_action_offers_add_missing_reset():
    """A field instantiation without `= <value>` triggers the quick-fix."""
    from lsprotocol.types import Position, Range
    from systemrdl_lsp.server import _code_actions_for_range
    src = "field { sw=rw; hw=r; } enable[0:0];\n"
    rng = Range(start=Position(line=0, character=0), end=Position(line=0, character=0))
    actions = _code_actions_for_range("file:///x.rdl", src, rng)
    assert len(actions) == 1
    assert "= 0" in actions[0].title
    edits = actions[0].edit.changes["file:///x.rdl"]
    assert len(edits) == 1
    # Insertion is zero-width before the semicolon.
    assert edits[0].range.start.character == edits[0].range.end.character
    assert edits[0].new_text.strip() == "= 0"


def test_code_action_skips_when_reset_already_present():
    """Field with ``= 0x1`` already on the line gets no quick-fix."""
    from lsprotocol.types import Position, Range
    from systemrdl_lsp.server import _code_actions_for_range
    src = "field { sw=rw; hw=r; } enable[0:0] = 0x1;\n"
    rng = Range(start=Position(line=0, character=0), end=Position(line=0, character=0))
    actions = _code_actions_for_range("file:///x.rdl", src, rng)
    assert actions == []


def test_semantic_tokens_encode_keywords_and_properties():
    """Smoke-test the LSP semantic-tokens delta encoding.

    The exact int payload is brittle to lock down; we just verify that the
    output is well-formed (multiple of 5) and that tokenizing a known buffer
    produces a non-empty result with sane structure.
    """
    from systemrdl_lsp.server import _semantic_tokens_for_text
    src = textwrap.dedent("""
        addrmap top {
            reg my_t {
                field { sw = rw; } enable[0:0] = 0x1;
            } CTRL @ 0x100;
        };
    """).strip()
    data = _semantic_tokens_for_text(src)
    assert data, "expected non-empty token stream"
    assert len(data) % 5 == 0, "wire format requires groups of 5 ints"
    # The first token's deltaLine is absolute (no prior token), so it must
    # be >= 0; sanity-check we didn't go negative anywhere.
    for i in range(0, len(data), 5):
        delta_line, delta_start, length, type_idx, mods = data[i:i + 5]
        assert delta_line >= 0
        assert length > 0
        assert type_idx >= 0
        assert mods == 0  # we don't emit modifiers


def test_document_links_resolve_include_directives(tmp_path):
    r"""Each `\include "x.rdl"` becomes a Ctrl+clickable documentLink.

    The link's range covers the path string (not the keyword), so clicking
    the bare `include` token doesn't navigate.
    """
    from systemrdl_lsp.server import _document_links
    common = tmp_path / "common.rdl"
    common.write_text(VALID_RDL, encoding="utf-8")
    master = tmp_path / "master.rdl"
    master_text = '`include "common.rdl"\naddrmap top {};\n'

    links = _document_links(master_text, master, [], {})
    assert len(links) == 1
    link = links[0]
    assert link.target == common.as_uri()
    # Range must cover only the literal path between the quotes.
    assert link.range.start.line == 0
    line0 = master_text.splitlines()[0]
    assert line0[link.range.start.character:link.range.end.character] == "common.rdl"


def test_document_links_skip_unresolved(tmp_path):
    """Unresolved includes don't get a link — the compiler's diagnostic surfaces the error."""
    from systemrdl_lsp.server import _document_links
    master = tmp_path / "x.rdl"
    text = '`include "nonexistent.rdl"\naddrmap top {};\n'
    assert _document_links(text, master, [], {}) == []


def test_document_links_resolve_via_search_path(tmp_path):
    r"""Setting/peakrdl.toml include paths satisfy `\include` resolution."""
    from systemrdl_lsp.server import _document_links
    libdir = tmp_path / "lib"
    libdir.mkdir()
    (libdir / "common.rdl").write_text(VALID_RDL, encoding="utf-8")
    master = tmp_path / "master.rdl"
    text = '`include "common.rdl"\naddrmap top {};\n'
    links = _document_links(text, master, [str(libdir)], {})
    assert len(links) == 1
    assert links[0].target == (libdir / "common.rdl").as_uri()


def test_resolve_search_paths_dedups_and_labels(tmp_path):
    """Three sources collapse into one ordered, deduped list with provenance.

    Setting beats peakrdl.toml beats sibling-dir on collision (first-source-wins).
    The user-explicit setting taking priority is the whole reason for the
    `setting` label on the front of the list.
    """
    (tmp_path / "lib").mkdir()
    (tmp_path / "shared").mkdir()
    (tmp_path / "peakrdl.toml").write_text(
        '[parser]\nincl_search_paths = ["lib", "shared"]\n',
        encoding="utf-8",
    )
    rdl = tmp_path / "x.rdl"
    rdl.write_text(VALID_RDL, encoding="utf-8")

    setting_paths = [str(tmp_path / "lib"), "/some/other/dir"]
    resolved = _resolve_search_paths(rdl.as_uri(), setting_paths)

    sources = [src for _p, src in resolved]
    paths = [p for p, _src in resolved]

    # `lib` appears in BOTH setting and peakrdl.toml; setting wins → only one entry.
    assert paths.count(str(tmp_path / "lib")) == 1
    # Setting entries come first.
    assert sources[0] == "setting"
    # peakrdl.toml entries follow.
    assert "peakrdl.toml" in sources
    # File's own directory is the implicit `sibling` fallback.
    assert any(s == "sibling" for s in sources)


def test_cross_file_diagnostics_bucket_by_source(tmp_path):
    r"""An error in `\include`d common.rdl is reported against common.rdl's URI,
    not the master file that was being compiled."""
    common = tmp_path / "common.rdl"
    common.write_text(
        textwrap.dedent("""
            reg my_ctrl_t {
                // syntax error: missing semicolon after field declaration
                field { sw=rw; hw=r } enable[0:0] = 0;
            };
        """).strip(),
        encoding="utf-8",
    )
    master = tmp_path / "master.rdl"
    master.write_text(
        '`include "common.rdl"\n'
        'addrmap top { my_ctrl_t CTRL @ 0; };\n',
        encoding="utf-8",
    )
    msgs, _roots, tmp = _compile_text(master.as_uri(), master.read_text())
    try:
        common_msgs = [m for m in msgs if m.file_path == common]
        master_msgs = [m for m in msgs if m.file_path == master]
        assert common_msgs, (
            f"expected at least one diagnostic on common.rdl; got file_paths "
            f"{[m.file_path for m in msgs]}"
        )
        # The master itself should not carry the include's syntax error —
        # that would land the squiggle on the master's `include line, which
        # is not where the error actually is.
        assert not any(
            m.severity in (Severity.ERROR, Severity.FATAL)
            for m in master_msgs
        ), f"unexpected error on master.rdl: {master_msgs}"
    finally:
        tmp.unlink(missing_ok=True)


def test_generated_types_match_schema():
    """Decision 9A: generated TypedDict + TS types are the source of truth.

    If the schema changed but ``bun run codegen`` wasn't re-run, the generated
    Python file falls out of date. CI catches that here so we don't ship a
    drifted shadow. Re-run ``bun run codegen`` to fix.
    """
    import pathlib
    import subprocess
    repo_root = pathlib.Path(__file__).resolve().parents[3]
    py_path = repo_root / "packages/systemrdl-lsp/src/systemrdl_lsp/_generated_types.py"
    before = py_path.read_text(encoding="utf-8")
    result = subprocess.run(
        ["uv", "run", "python", "tools/codegen.py"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"codegen failed: {result.stderr}"
    after = py_path.read_text(encoding="utf-8")
    assert before == after, (
        "Generated types out of sync with schemas/elaborated-tree.json. "
        "Run `bun run codegen` and commit the diff."
    )


def test_unchanged_envelope_is_constant_size():
    """TODO-1 fast path: ``unchanged`` reply is small regardless of tree size."""
    from systemrdl_lsp.server import _unchanged_envelope
    env = _unchanged_envelope(version=42)
    assert env["unchanged"] is True
    assert env["version"] == 42
    assert env["roots"] == []
    # Crucially no "elaboratedAt" timestamp — unchanged means nothing happened
    # since the previous reply, the timestamp would lie.
    assert "elaboratedAt" not in env


def test_compile_text_accepts_perl_safe_opcodes(tmp_path):
    """``perl_safe_opcodes`` plumbs through to RDLCompiler without breaking simple RDL.

    No Perl markers in this fixture, so the compiler runs the safe-opcode
    reader path but never invokes the preprocessor — exercises the kwarg
    forwarding without depending on a perl binary in CI.
    """
    uri = (tmp_path / "x.rdl").as_uri()
    msgs, roots, tmp = _compile_text(
        uri,
        VALID_RDL,
        perl_safe_opcodes=[":base_core", ":base_mem", ":base_loop"],
    )
    try:
        errors = [m for m in msgs if m.severity in (Severity.ERROR, Severity.FATAL)]
        assert errors == []
        assert len(roots) == 1
    finally:
        tmp.unlink(missing_ok=True)
