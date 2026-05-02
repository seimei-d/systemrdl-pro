"""T3 ProcessPool tests — cross-URI parallel elaborate.

Covers:

- ``_compile_text_compressed`` round-trips through ``ProcessPoolExecutor``
  with the pickle+zlib contract (the wire-format that lets us ship the
  ``RootNode`` across processes for ~5 MB on 25k regs).
- ``_full_pass_async`` routes through the pool when enabled and falls
  back to in-thread when ``elaborate_in_process=True``.
- The fingerprint computed inside the subprocess matches one computed
  in the main process (proves the cross-process tree is semantically
  identical, not just syntactically deserialized).

Spawning a real ``ProcessPoolExecutor`` adds ~1-2s per test on Linux
because of the spawn+import cost; tests share a single pool via a
module-scope fixture to amortize.
"""

from __future__ import annotations

import asyncio
import multiprocessing as mp
import pickle
from concurrent.futures import ProcessPoolExecutor

import pytest
from systemrdl_lsp.compile import (
    _compile_text,
    _compile_text_compressed,
    _decompress_compile_result,
    _fingerprint_roots,
    _pool_warmup_noop,
    _pool_worker_init,
)
from systemrdl_lsp.server import build_server

SAMPLE_RDL = """\
addrmap top {
    reg {
        field { sw=rw; hw=r; } a[0:0] = 0;
        field { sw=rw; hw=r; } b[1:1] = 1;
    } CTRL @ 0x00;
};
"""


@pytest.fixture(scope="module")
def real_pool():
    """One real ProcessPoolExecutor per module so tests amortize the
    ~1-2s spawn cost. Workers share the same initializer the LSP uses."""
    ctx = mp.get_context("spawn")
    pool = ProcessPoolExecutor(
        max_workers=2,
        mp_context=ctx,
        initializer=_pool_worker_init,
    )
    # Pre-warm both workers so the first real submit doesn't pay spawn.
    for _ in range(2):
        pool.submit(_pool_warmup_noop).result(timeout=30)
    yield pool
    pool.shutdown(wait=False, cancel_futures=True)


# ---------------------------------------------------------------------------
# Wire-format unit tests
# ---------------------------------------------------------------------------


def test_compressed_round_trip_locally(tmp_path):
    """``_compile_text_compressed`` then ``_decompress_compile_result``
    in the SAME process — proves the pickle+zlib contract preserves
    every field of the result tuple, independent of subprocess noise."""
    rdl = tmp_path / "a.rdl"
    rdl.write_text(SAMPLE_RDL)
    blob = _compile_text_compressed(rdl.as_uri(), SAMPLE_RDL)
    assert isinstance(blob, bytes)
    msgs, roots, tmp, consumed = _decompress_compile_result(blob)
    try:
        assert isinstance(msgs, list)
        assert len(roots) == 1
        assert isinstance(consumed, set)
        # Tree itself is functional after decompress — fingerprint can
        # walk it. If pickle dropped some non-trivial state the
        # fingerprint walk would crash on a missing attribute.
        fp = _fingerprint_roots(roots)
        assert isinstance(fp, str) and len(fp) == 64
    finally:
        tmp.unlink(missing_ok=True)


def test_compression_actually_shrinks(tmp_path):
    """Sanity: zlib ratio is meaningful even on tiny fixtures.
    Catches a regression where someone sets the level to 0 or skips
    compression entirely."""
    rdl = tmp_path / "a.rdl"
    rdl.write_text(SAMPLE_RDL)
    blob = _compile_text_compressed(rdl.as_uri(), SAMPLE_RDL)
    msgs, roots, tmp, consumed = _decompress_compile_result(blob)
    try:
        raw = pickle.dumps((msgs, roots, tmp, consumed), protocol=5)
        # zlib L=1 typically gets at least 2x on this fixture; allow
        # generous slack so a future systemrdl-compiler version that
        # tweaks the in-memory shape doesn't break the test.
        assert len(blob) < len(raw), (
            f"compressed blob should be smaller than raw pickle: "
            f"compressed={len(blob)} raw={len(raw)}"
        )
    finally:
        tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Subprocess round-trip — actually goes through ProcessPoolExecutor
# ---------------------------------------------------------------------------


def test_subprocess_round_trip_via_pool(real_pool, tmp_path):
    """End-to-end: submit ``_compile_text_compressed`` to a real
    subprocess pool, decompress in the parent, verify the tree
    equals one elaborated locally."""
    rdl = tmp_path / "a.rdl"
    rdl.write_text(SAMPLE_RDL)

    # Subprocess
    fut = real_pool.submit(_compile_text_compressed, rdl.as_uri(), SAMPLE_RDL)
    blob = fut.result(timeout=60)
    msgs_p, roots_p, tmp_p, consumed_p = _decompress_compile_result(blob)

    # Local
    _msgs_l, roots_l, tmp_l, _consumed_l = _compile_text(rdl.as_uri(), SAMPLE_RDL)

    try:
        assert _fingerprint_roots(roots_p) == _fingerprint_roots(roots_l), (
            "subprocess-elaborated tree must be semantically identical "
            "to the locally-elaborated tree"
        )
        assert isinstance(roots_p[0], type(roots_l[0]))
    finally:
        tmp_p.unlink(missing_ok=True)
        tmp_l.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# _full_pass_async routing: pool path vs in-thread fallback
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_full_pass_uses_pool_when_enabled(real_pool, tmp_path):
    """elaborate_in_process=False (T3 default) → results land in the
    cache via the pool path. We bolt the test pool onto the server
    state directly (no INITIALIZED handshake in unit tests)."""
    s = build_server()
    state = s._systemrdl_state
    full_pass = s._systemrdl_full_pass_async
    state.elaborate_in_process = False
    state.elaborate_pool = real_pool

    rdl = tmp_path / "a.rdl"
    rdl.write_text(SAMPLE_RDL)
    uri = rdl.as_uri()

    _run(full_pass(uri, SAMPLE_RDL))
    cached = state.cache.get(uri)
    assert cached is not None
    assert cached.version == 1
    assert cached.ast_fingerprint is not None
    assert len(cached.roots) == 1


def test_full_pass_falls_back_to_thread_when_in_process(tmp_path):
    """elaborate_in_process=True → bypasses the pool entirely.
    Same observable result (tree in cache, version bumped) but
    no subprocess involved."""
    s = build_server()
    state = s._systemrdl_state
    full_pass = s._systemrdl_full_pass_async
    state.elaborate_in_process = True
    state.elaborate_pool = None

    rdl = tmp_path / "a.rdl"
    rdl.write_text(SAMPLE_RDL)
    uri = rdl.as_uri()

    _run(full_pass(uri, SAMPLE_RDL))
    cached = state.cache.get(uri)
    assert cached is not None
    assert cached.version == 1
    assert len(cached.roots) == 1


def test_full_pass_pool_path_preserves_includes(real_pool, tmp_path):
    """Pool path returns includee list intact — proves the consumed_files
    set survived pickle. Drives the T2-A include-graph cascade across
    the new architecture."""
    types = tmp_path / "types.rdl"
    types.write_text("reg t_r { field { sw=rw; hw=r; } f[0:0] = 0; };\n")
    main = tmp_path / "main.rdl"
    main_text = f'`include "{types.name}"\naddrmap top {{ t_r R @ 0; }};\n'
    main.write_text(main_text)

    s = build_server()
    state = s._systemrdl_state
    full_pass = s._systemrdl_full_pass_async
    state.elaborate_in_process = False
    state.elaborate_pool = real_pool

    _run(full_pass(main.as_uri(), main_text))
    deps = state.include_graph.get(main.as_uri(), set())
    assert types.as_uri() in deps, (
        f"include reverse-dep map should include types.rdl after pool "
        f"elaborate; got {deps}"
    )
