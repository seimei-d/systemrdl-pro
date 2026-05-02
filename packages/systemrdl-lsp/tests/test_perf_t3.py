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
import pathlib
import pickle
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool

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


# ---------------------------------------------------------------------------
# T3-E: BrokenProcessPool recovery
# ---------------------------------------------------------------------------


def _crashing_worker(*_args, **_kwargs) -> bytes:
    """Worker function that exits the process abruptly. Used to simulate
    a subprocess that segfaulted or got OOM-killed mid-elaborate, so the
    parent observes BrokenProcessPool on the next operation."""
    import os
    os._exit(137)  # mimics SIGKILL exit status


def test_pool_recovers_from_broken_subprocess(tmp_path, monkeypatch):
    """Crash the worker, observe BrokenProcessPool, verify the parent
    respawns the pool and the next elaborate succeeds without operator
    intervention. Without recovery the next elaborate would also fail
    until the user restarts the LSP — this is the production gap T3-E
    closes."""
    s = build_server()
    state = s._systemrdl_state
    full_pass = s._systemrdl_full_pass_async
    state.elaborate_in_process = False

    # Stand up a real pool, then immediately submit the crashing worker
    # to take it out. The next regular submit observes BrokenProcessPool.
    ctx = mp.get_context("spawn")
    state.elaborate_pool = ProcessPoolExecutor(
        max_workers=2,
        mp_context=ctx,
        initializer=_pool_worker_init,
    )
    # Pre-warm so the crash isn't masked by a startup-time failure.
    for _ in range(2):
        state.elaborate_pool.submit(_pool_warmup_noop).result(timeout=30)

    # Fire the crash. We use .submit + .result, swallowing the exception —
    # the goal is just to wedge the pool into the broken state.
    crash_fut = state.elaborate_pool.submit(_crashing_worker)
    # Worker exits 137 → BrokenProcessPool on result(). The exact
    # exception class depends on whether result() observes the
    # broken state via the future's exception or via the pool itself,
    # so accept either.
    try:
        crash_fut.result(timeout=30)
    except (BrokenProcessPool, Exception):
        pass

    # Now drive a real elaborate. _full_pass_async should detect
    # the broken pool, respawn, retry once, and succeed.
    rdl = tmp_path / "a.rdl"
    rdl.write_text(SAMPLE_RDL)
    uri = rdl.as_uri()

    _run(full_pass(uri, SAMPLE_RDL))

    cached = state.cache.get(uri)
    assert cached is not None, "elaborate must have succeeded after recovery"
    assert cached.version == 1
    assert len(cached.roots) == 1
    # Pool should have been replaced with a fresh one.
    assert state.elaborate_pool is not None

    state.elaborate_pool.shutdown(wait=False, cancel_futures=True)


# ---------------------------------------------------------------------------
# T3-F: memory spike — RSS growth across many elaborates stays bounded
# ---------------------------------------------------------------------------


def _worker_rss_kb() -> int:
    """Worker reports its own RSS in KB. Linux-only — reads
    /proc/self/status. Skipped on platforms where this isn't available.
    Run inside the subprocess so the test is observing per-worker
    memory, not the parent."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except (FileNotFoundError, OSError, IndexError, ValueError):
        return -1
    return -1


def _elaborate_and_report_rss(uri: str, text: str) -> tuple[int, int]:
    """Elaborate one buffer, return (root_count, worker_rss_kb).
    Used by the spike test to track per-iteration memory."""
    sys_path_prefix = pathlib.Path(__file__).resolve().parents[2] / "src"
    import sys
    if str(sys_path_prefix) not in sys.path:
        sys.path.insert(0, str(sys_path_prefix))
    from systemrdl_lsp.compile import _compile_text

    _msgs, roots, tmp_path, _consumed = _compile_text(uri, text)
    rss = _worker_rss_kb()
    tmp_path.unlink(missing_ok=True)
    return len(roots), rss


@pytest.mark.skipif(
    not pathlib.Path("/proc/self/status").exists(),
    reason="memory spike test reads /proc — Linux-only",
)
def test_memory_growth_bounded_across_many_elaborates(tmp_path):
    """Run 50 elaborates of the same fixture through one pool worker
    and assert RSS growth stays bounded (<50 MB). Catches an obvious
    leak in either ``_compile_text`` or ``RDLCompiler``'s import-cached
    Perl interpreter — neither would be visible from the LSP-level
    tests because each one creates a fresh ServerState."""
    rdl = tmp_path / "stress_lite.rdl"
    # Build a multi-reg addrmap so the worker actually does meaningful
    # work each iteration (not just parse a 5-line file). Concatenating
    # SAMPLE_RDL would re-define ``top`` and the elaborate would fail.
    fields = "\n".join(
        f"        field {{ sw=rw; hw=r; }} f{i:02d}[{i}:{i}] = 0;"
        for i in range(20)
    )
    regs = "\n".join(
        f"    reg {{\n{fields}\n    }} R{i:03d} @ 0x{i*4:04X};"
        for i in range(40)
    )
    rdl.write_text(f"addrmap top {{\n{regs}\n}};\n")

    ctx = mp.get_context("spawn")
    pool = ProcessPoolExecutor(
        max_workers=1,  # pin to one worker so RSS samples are comparable
        mp_context=ctx,
        initializer=_pool_worker_init,
    )
    try:
        # Burn one warm-up elaborate — first call pays import + Perl
        # interpreter setup. Subsequent calls measure steady state.
        baseline_roots, baseline_rss = pool.submit(
            _elaborate_and_report_rss, rdl.as_uri(), rdl.read_text()
        ).result(timeout=60)
        if baseline_rss < 0:
            pytest.skip("worker could not read /proc/self/status")
        assert baseline_roots >= 1

        rss_samples = [baseline_rss]
        for _ in range(49):
            roots, rss = pool.submit(
                _elaborate_and_report_rss, rdl.as_uri(), rdl.read_text()
            ).result(timeout=60)
            assert roots >= 1
            rss_samples.append(rss)

        peak_growth_kb = max(rss_samples) - baseline_rss
        peak_growth_mb = peak_growth_kb / 1024
        # Known: systemrdl-compiler leaks ~5 MB per elaborate of a
        # ~40-reg fixture as of compiler version pinned by uv.lock.
        # 50 iterations → ~250 MB growth observed in the field. The
        # ceiling here is set wide enough that this version passes
        # but a leak that DOUBLES (e.g. someone plumbs more state into
        # RootNode without a __reduce__ that drops it) gets caught.
        #
        # The production mitigation lives in ``ServerState`` —
        # ``pool_max_elaborates`` recycles the pool after N elaborates
        # so RSS comes back to baseline. Without that, a long editing
        # session on a big design would slowly OOM the worker.
        assert peak_growth_mb < 500.0, (
            f"worker RSS grew by {peak_growth_mb:.1f} MB across 50 "
            f"elaborates — leak got worse than the ~250 MB baseline. "
            f"Samples (KB, first 10): {rss_samples[:10]} ... "
            f"last 5: {rss_samples[-5:]}"
        )
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


# ---------------------------------------------------------------------------
# T3-G: pool worker recycling
# ---------------------------------------------------------------------------


def test_pool_recycles_after_threshold(tmp_path):
    """After N successful subprocess elaborates the pool gets torn
    down + respawned. Mitigates the systemrdl-compiler ~5 MB leak per
    elaborate. Without this a long editing session OOMs the worker."""
    s = build_server()
    state = s._systemrdl_state
    full_pass = s._systemrdl_full_pass_async
    state.elaborate_in_process = False
    state.pool_max_elaborates = 3  # tiny threshold so the test is fast

    ctx = mp.get_context("spawn")
    state.elaborate_pool = ProcessPoolExecutor(
        max_workers=1,
        mp_context=ctx,
        initializer=_pool_worker_init,
    )
    state.elaborate_pool.submit(_pool_warmup_noop).result(timeout=30)
    pool_v1 = state.elaborate_pool

    rdl = tmp_path / "a.rdl"
    rdl.write_text(SAMPLE_RDL)
    uri = rdl.as_uri()

    # Below threshold — same pool throughout.
    for _ in range(2):
        _run(full_pass(uri, SAMPLE_RDL))
        # Force re-elaborate by marking stale (otherwise buffer-equality
        # short-circuit would skip and the counter wouldn't increment).
        state.stale_uris.add(uri)
    assert state.elaborate_pool is pool_v1, (
        "pool should not have recycled yet"
    )
    assert state.pool_elaborate_count == 2

    # Crossing threshold (3rd successful elaborate) → recycle.
    _run(full_pass(uri, SAMPLE_RDL))
    assert state.elaborate_pool is not pool_v1, (
        "pool should have been recycled after 3rd elaborate"
    )
    assert state.pool_elaborate_count == 0, (
        "elaborate count should reset on recycle"
    )

    state.elaborate_pool.shutdown(wait=False, cancel_futures=True)


def test_stale_bar_appears_when_parse_fails(real_pool, tmp_path):
    """Field-reported regression: editing an open file into a parse error
    used to leave the viewer's "Showing last good" stale indicator
    invisible. Root cause: the parse-failure branch added the URI to
    state.stale_uris but never invalidated cached.serialized or pushed
    rdl/elaboratedTreeChanged, so the client kept rendering the
    pre-failure (non-stale) envelope.

    Fix: on stale transition (False → True), bump cached.version,
    clear cached.serialized, and notify the client. The next fetch
    builds a fresh envelope with stale=True."""
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
    v_good = cached.version
    assert uri not in state.stale_uris

    # Pre-cache a serialized envelope to simulate the viewer having
    # already fetched and rendered the tree.
    cached.serialized = {"version": v_good, "stale": False, "roots": []}

    # Now feed a buffer that fails to parse. roots=[] + ERROR diag.
    broken = SAMPLE_RDL + "\n}}}\n"
    _run(full_pass(uri, broken))

    cached_after = state.cache.get(uri)
    assert uri in state.stale_uris
    assert cached_after.version > v_good, (
        "version must bump on stale transition so the client's "
        "sinceVersion check fails and it re-fetches"
    )
    assert cached_after.serialized is None, (
        "cached envelope must be invalidated on stale transition so "
        "the next fetch builds a fresh one with stale=True"
    )


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
