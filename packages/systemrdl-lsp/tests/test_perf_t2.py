"""T2 incremental-elaborate tests.

Covers the three Lean pieces shipped in 0.17.0:

- ``_fingerprint_roots`` — viewer-stable AST hash that ignores
  whitespace / comments / source positions.
- ``_harvest_consumed_files`` — set of source files the compiler
  actually opened, drives the include reverse-dep map.
- ``_apply_compile_result`` — fingerprint short-circuit (no version
  bump on semantic no-op) + include-graph maintenance.
- ``_full_pass_async`` — per-URI lock serializes same-URI races,
  cascade re-elaborates open includers when an includee changes.
"""

from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path

import pytest
from systemrdl_lsp.compile import (
    _compile_text,
    _fingerprint_roots,
    _harvest_consumed_files,
)
from systemrdl_lsp.server import build_server

# ---------------------------------------------------------------------------
# Fingerprint unit tests
# ---------------------------------------------------------------------------


SIMPLE_RDL = """\
addrmap top {
    reg {
        field { sw=rw; hw=r; } a[0:0] = 0;
        field { sw=rw; hw=r; } b[1:1] = 1;
    } CTRL @ 0x00;
};
"""


def _roots(rdl_text: str, tmp_path: Path, name: str = "x.rdl"):
    p = tmp_path / name
    p.write_text(rdl_text)
    msgs, roots, tmp, consumed = _compile_text(p.as_uri(), rdl_text)
    return roots, tmp, consumed, msgs


def test_fingerprint_stable_across_runs(tmp_path):
    """Two compiles of the same buffer → identical fingerprint."""
    r1, t1, _c1, _m1 = _roots(SIMPLE_RDL, tmp_path, "a.rdl")
    r2, t2, _c2, _m2 = _roots(SIMPLE_RDL, tmp_path, "b.rdl")
    try:
        assert _fingerprint_roots(r1) == _fingerprint_roots(r2)
    finally:
        t1.unlink(missing_ok=True)
        t2.unlink(missing_ok=True)


def test_fingerprint_ignores_comments(tmp_path):
    """Adding a comment is a semantic no-op."""
    with_comment = SIMPLE_RDL.replace(
        "addrmap top {", "// docstring\naddrmap top {"
    )
    r1, t1, _c, _m = _roots(SIMPLE_RDL, tmp_path, "a.rdl")
    r2, t2, _c, _m = _roots(with_comment, tmp_path, "b.rdl")
    try:
        assert _fingerprint_roots(r1) == _fingerprint_roots(r2)
    finally:
        t1.unlink(missing_ok=True)
        t2.unlink(missing_ok=True)


def test_fingerprint_ignores_whitespace(tmp_path):
    """Reformatting with extra blank lines is a semantic no-op."""
    reformatted = SIMPLE_RDL.replace("} CTRL", "}\n\n\nCTRL")
    r1, t1, _c, _m = _roots(SIMPLE_RDL, tmp_path, "a.rdl")
    r2, t2, _c, _m = _roots(reformatted, tmp_path, "b.rdl")
    try:
        assert _fingerprint_roots(r1) == _fingerprint_roots(r2)
    finally:
        t1.unlink(missing_ok=True)
        t2.unlink(missing_ok=True)


def test_fingerprint_changes_on_reset_value(tmp_path):
    """Changing a field reset value is a real semantic change."""
    bumped = SIMPLE_RDL.replace("a[0:0] = 0", "a[0:0] = 1")
    r1, t1, _c, _m = _roots(SIMPLE_RDL, tmp_path, "a.rdl")
    r2, t2, _c, _m = _roots(bumped, tmp_path, "b.rdl")
    try:
        assert _fingerprint_roots(r1) != _fingerprint_roots(r2)
    finally:
        t1.unlink(missing_ok=True)
        t2.unlink(missing_ok=True)


def test_fingerprint_changes_on_field_width(tmp_path):
    """Widening a field is a real semantic change (msb/lsb hashed)."""
    widened = SIMPLE_RDL.replace("a[0:0]", "a[3:0]")
    r1, t1, _c, _m = _roots(SIMPLE_RDL, tmp_path, "a.rdl")
    r2, t2, _c, _m = _roots(widened, tmp_path, "b.rdl")
    try:
        assert _fingerprint_roots(r1) != _fingerprint_roots(r2)
    finally:
        t1.unlink(missing_ok=True)
        t2.unlink(missing_ok=True)


def test_fingerprint_changes_on_reg_address(tmp_path):
    """Moving a reg to a new address is a real semantic change."""
    moved = SIMPLE_RDL.replace("@ 0x00", "@ 0x10")
    r1, t1, _c, _m = _roots(SIMPLE_RDL, tmp_path, "a.rdl")
    r2, t2, _c, _m = _roots(moved, tmp_path, "b.rdl")
    try:
        assert _fingerprint_roots(r1) != _fingerprint_roots(r2)
    finally:
        t1.unlink(missing_ok=True)
        t2.unlink(missing_ok=True)


def test_fingerprint_empty_roots():
    """Empty list (parse failure) hashes to a stable sentinel, not crash."""
    fp = _fingerprint_roots([])
    assert isinstance(fp, str) and len(fp) == 64  # sha256 hex


# ---------------------------------------------------------------------------
# Consumed-files unit tests
# ---------------------------------------------------------------------------


def test_consumed_files_includes_includee(tmp_path):
    """Compiler-opened includee shows up in the consumed-file set."""
    types = tmp_path / "types.rdl"
    types.write_text(textwrap.dedent("""\
        reg my_reg_t {
            field { sw=rw; hw=r; } f[0:0] = 0;
        };
    """))
    main = tmp_path / "main.rdl"
    main_text = textwrap.dedent(f"""\
        `include "{types.name}"
        addrmap top {{
            my_reg_t REG @ 0x00;
        }};
    """)
    main.write_text(main_text)
    msgs, roots, tmp, consumed = _compile_text(main.as_uri(), main_text)
    try:
        assert roots, f"compile failed: {[m.text for m in msgs]}"
        # Resolve symlinks/realpath like _harvest does.
        assert types.resolve() in consumed
    finally:
        tmp.unlink(missing_ok=True)


def test_consumed_files_excludes_temp_path(tmp_path):
    """The buffer's own tmp file must not appear in consumed_files (drives
    include reverse-dep tracking; including self would loop the cascade)."""
    main = tmp_path / "main.rdl"
    main.write_text(SIMPLE_RDL)
    msgs, roots, tmp, consumed = _compile_text(main.as_uri(), SIMPLE_RDL)
    try:
        assert roots
        assert tmp.resolve() not in consumed
    finally:
        tmp.unlink(missing_ok=True)


def test_harvest_handles_empty_roots(tmp_path):
    """Parse failure path: harvest returns whatever messages reference,
    minus tmp; never crashes on empty roots."""
    bogus_tmp = tmp_path / "bogus.rdl"
    bogus_tmp.write_text("")
    consumed = _harvest_consumed_files([], [], bogus_tmp)
    assert consumed == set()


# ---------------------------------------------------------------------------
# Server-level integration: fingerprint skip + include cascade + lock
# ---------------------------------------------------------------------------


@pytest.fixture
def server_state():
    """Build a fresh LanguageServer + expose its private state hooks."""
    s = build_server()
    return s, s._systemrdl_state, s._systemrdl_full_pass_async


def _run(coro):
    """Drive an awaitable to completion in a fresh event loop."""
    return asyncio.new_event_loop().run_until_complete(coro)


def test_apply_skip_keeps_version_when_fingerprint_matches(server_state, tmp_path):
    """Two _full_pass_async on identical-AST buffers → version bumps once."""
    s, state, full_pass = server_state
    rdl_path = tmp_path / "a.rdl"
    rdl_path.write_text(SIMPLE_RDL)
    uri = rdl_path.as_uri()

    _run(full_pass(uri, SIMPLE_RDL))
    cached1 = state.cache.get(uri)
    assert cached1 is not None and cached1.ast_fingerprint is not None
    v1 = cached1.version

    # Edit that doesn't change semantics — extra blank lines.
    semantic_no_op = SIMPLE_RDL.replace("} CTRL", "}\n\n\nCTRL")
    _run(full_pass(uri, semantic_no_op))
    cached2 = state.cache.get(uri)
    assert cached2 is not None
    assert cached2.version == v1, "fingerprint match should NOT bump version"
    assert cached2.text == semantic_no_op, "text refresh on skip path"


def test_apply_bumps_version_on_real_change(server_state, tmp_path):
    """A reset-value edit must bump the cache version (cascade signal)."""
    s, state, full_pass = server_state
    rdl_path = tmp_path / "a.rdl"
    rdl_path.write_text(SIMPLE_RDL)
    uri = rdl_path.as_uri()

    _run(full_pass(uri, SIMPLE_RDL))
    v1 = state.cache.get(uri).version

    bumped = SIMPLE_RDL.replace("a[0:0] = 0", "a[0:0] = 1")
    _run(full_pass(uri, bumped))
    v2 = state.cache.get(uri).version
    assert v2 == v1 + 1


def test_include_graph_populated(server_state, tmp_path):
    """After elaborate, state.include_graph[uri] holds the includee URIs."""
    s, state, full_pass = server_state
    types = tmp_path / "types.rdl"
    types.write_text("reg t_r { field { sw=rw; hw=r; } f[0:0] = 0; };\n")
    main = tmp_path / "main.rdl"
    main_text = f'`include "{types.name}"\naddrmap top {{ t_r R @ 0x00; }};\n'
    main.write_text(main_text)
    main_uri = main.as_uri()
    types_uri = types.as_uri()

    _run(full_pass(main_uri, main_text))
    assert types_uri in state.include_graph[main_uri]
    assert main_uri in state.includee_to_includers[types_uri]


def test_per_uri_lock_serializes_concurrent_calls(server_state, tmp_path):
    """Two concurrent _full_pass_async on the same URI run sequentially."""
    s, state, full_pass = server_state
    rdl_path = tmp_path / "a.rdl"
    rdl_path.write_text(SIMPLE_RDL)
    uri = rdl_path.as_uri()

    async def both() -> None:
        await asyncio.gather(
            full_pass(uri, SIMPLE_RDL),
            full_pass(uri, SIMPLE_RDL),
        )

    _run(both())
    # Same buffer twice → first elaborate populates cache, second hits the
    # buffer-equality short-circuit *because* the lock made the put visible.
    # Without the lock the second call could start before the first stored
    # its result and we'd see version=2.
    assert state.cache.get(uri).version == 1


def test_includee_change_triggers_includer_cascade(server_state, tmp_path):
    """Cascade: editing types.rdl re-elaborates the open includer."""
    s, state, full_pass = server_state
    types = tmp_path / "types.rdl"
    types.write_text("reg t_r { field { sw=rw; hw=r; } f[0:0] = 0; };\n")
    main = tmp_path / "main.rdl"
    main_text = f'`include "{types.name}"\naddrmap top {{ t_r R @ 0x00; }};\n'
    main.write_text(main_text)
    main_uri = main.as_uri()
    types_uri = types.as_uri()

    # Stand up the main file first so the include graph knows about types.rdl.
    _run(full_pass(main_uri, main_text))
    v1 = state.cache.get(main_uri).version

    # Stand in for pygls workspace via the test-only override on state.
    # Production wires _is_open / _read_buffer through server.workspace,
    # which the standalone unit test cannot initialize.
    state.test_open_buffers = {main_uri: main_text}

    # Now edit the includee. The cascade should also re-elaborate main.
    new_types_text = (
        "reg t_r { field { sw=rw; hw=r; } f[0:0] = 1; };\n"  # reset bumped
    )
    types.write_text(new_types_text)

    async def edit_then_settle() -> None:
        await full_pass(types_uri, new_types_text)
        # Cascade fires create_task() outside the parent lock; the spawned
        # task awaits a thread-pool elaborate (tens of ms for a tiny RDL),
        # so we explicitly drain every still-pending task. Plain
        # ``sleep(0)`` only yields once and isn't enough.
        pending = [
            t for t in asyncio.all_tasks() if t is not asyncio.current_task()
        ]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    _run(edit_then_settle())
    v2 = state.cache.get(main_uri).version
    assert v2 == v1 + 1, (
        f"includer should have re-elaborated; v1={v1} v2={v2}"
    )
