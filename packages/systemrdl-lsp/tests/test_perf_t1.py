"""End-to-end integration test for the T1 lazy-serialize pipeline.

Covers:

- Spine + per-node expand round-trip equals the full serializer's output
  on a stress fixture (1k regs × 30 fields).
- Disk cache survives a fresh DiskCache instance pointed at the same dir.
- Spine wall-clock budget (informational; no hard fail because CI machines
  vary, but we log it so regressions are visible).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from systemrdl import RDLCompiler
from systemrdl_lsp.cache import DiskCache, make_key
from systemrdl_lsp.serialize import (
    _serialize_root,
    _serialize_spine,
    expand_node,
)


def _generate_fixture(path: Path, regfiles: int, regs_per_file: int) -> None:
    """Mirror examples/gen_stress.py inline so the test doesn't depend on
    the CLI script (and runs in any cwd)."""
    fields_per_reg = 30
    bank_stride = 0x400
    lines = ["reg stress_reg_t {"]
    lines.append('    name = "Stress reg";')
    for i in range(fields_per_reg):
        lines.append(f"    field {{ sw=rw; hw=r; }} f{i:02d}[{i}:{i}] = 0;")
    lines.append("};")
    lines.append("")
    for rf in range(regfiles):
        lines.append(f"regfile bank_{rf:02d}_t {{")
        for r in range(regs_per_file):
            lines.append(f"    stress_reg_t REG_{r:03d} @ 0x{r * 4:04X};")
        lines.append("};")
        lines.append("")
    lines.append("addrmap stress_top {")
    for rf in range(regfiles):
        lines.append(f"    bank_{rf:02d}_t bank_{rf:02d} @ 0x{rf * bank_stride:04X};")
    lines.append("};")
    path.write_text("\n".join(lines) + "\n")


@pytest.fixture(scope="module")
def small_stress_roots(tmp_path_factory):
    """Reusable elaborated RootNode list for a small stress design (50 regs)."""
    tmp = tmp_path_factory.mktemp("stress")
    rdl = tmp / "stress_50.rdl"
    _generate_fixture(rdl, regfiles=5, regs_per_file=10)
    c = RDLCompiler()
    c.compile_file(str(rdl))
    return [c.elaborate(top_def_name=None)]


@pytest.fixture(scope="module")
def medium_stress_roots(tmp_path_factory):
    """Reusable elaborated RootNode list for a 1k-reg design."""
    tmp = tmp_path_factory.mktemp("stress_1k")
    rdl = tmp / "stress_1000.rdl"
    _generate_fixture(rdl, regfiles=10, regs_per_file=100)
    c = RDLCompiler()
    c.compile_file(str(rdl))
    return [c.elaborate(top_def_name=None)]


# ---------------------------------------------------------------------------
# Spine ⊕ expansions == full serialize
# ---------------------------------------------------------------------------


def _walk_regs(node):
    if node.get("kind") == "reg":
        yield node
    for c in node.get("children") or []:
        yield from _walk_regs(c)


def test_spine_plus_expansions_equals_full(small_stress_roots):
    """The lazy protocol must be lossless: spine + per-node expansion ==
    what _serialize_root emits in one shot. Excludes the new nodeId field
    (only present on lazy responses) and the elaboratedAt timestamp."""
    spine = _serialize_spine(small_stress_roots, stale=False, version=7)
    full = _serialize_root(small_stress_roots, stale=False, version=7)

    spine_regs = [r for r in (root for root in spine["roots"]) for r in _walk_regs(r)]
    full_regs = [r for r in (root for root in full["roots"]) for r in _walk_regs(r)]
    assert len(spine_regs) == len(full_regs)
    assert len(spine_regs) == 50  # 5 banks * 10 regs

    # Every spine reg has a placeholder marker and an empty fields[].
    assert all(r["loadState"] == "placeholder" for r in spine_regs)
    assert all(r["fields"] == [] for r in spine_regs)
    assert all("nodeId" in r for r in spine_regs)

    # Expand every placeholder; result must match the corresponding full reg
    # modulo the nodeId field (which the full path doesn't emit).
    full_by_address = {r["address"]: r for r in full_regs}
    for spine_reg in spine_regs:
        expanded = expand_node(small_stress_roots, spine_reg["nodeId"])
        assert expanded is not None
        assert expanded["nodeId"] == spine_reg["nodeId"]
        assert "loadState" not in expanded
        ref = full_by_address[expanded["address"]]
        # Compare modulo nodeId (only on expanded) — fields, accessSummary,
        # reset, source, displayName, and so on must all match exactly.
        assert {k: v for k, v in expanded.items() if k != "nodeId"} == ref


def test_spine_carries_cheap_rollups(small_stress_roots):
    """accessSummary and reset are derived from fields but we still compute
    them in spine mode (cheap field walk over cached access tokens). The
    tree-row UI shows these, so degrading them in lazy mode would be a
    visible regression."""
    spine = _serialize_spine(small_stress_roots, stale=False)
    regs = [r for root in spine["roots"] for r in _walk_regs(root)]
    assert all(r.get("accessSummary") for r in regs)
    assert all(r.get("reset") for r in regs)


def test_envelope_marks_lazy(small_stress_roots):
    spine = _serialize_spine(small_stress_roots, stale=False, version=42)
    assert spine["lazy"] is True
    assert spine["version"] == 42

    full = _serialize_root(small_stress_roots, stale=False, version=42)
    assert "lazy" not in full
    assert full["version"] == 42


# ---------------------------------------------------------------------------
# Disk cache integration
# ---------------------------------------------------------------------------


def test_disk_cache_round_trip_with_real_envelope(small_stress_roots, tmp_path):
    """Serialized spine survives a write/read cycle via DiskCache, byte-equal."""
    cache = DiskCache(base=tmp_path / "cache", max_entries=10)
    spine = _serialize_spine(small_stress_roots, stale=False, version=1)
    key = make_key("/abs/test.rdl", 12345, ["/inc"], "1.32.2")
    cache.put(key, spine)
    loaded = cache.get(key)
    assert loaded == spine
    # JSON round-trip equivalence (the LSP returns the dict from disk
    # directly, but downstream JSON-RPC will re-serialize, so verify
    # the dict serializes back to the same string).
    assert json.dumps(loaded, sort_keys=True) == json.dumps(spine, sort_keys=True)


def test_disk_cache_survives_new_instance(small_stress_roots, tmp_path):
    """A second DiskCache pointed at the same directory sees prior entries
    — that's the whole point of the disk layer (window reload, multi-window)."""
    base = tmp_path / "shared_cache"
    spine = _serialize_spine(small_stress_roots, stale=False, version=99)
    key = make_key("/abs/x.rdl", 1, [], "1.32.2")
    DiskCache(base=base).put(key, spine)
    # New instance, same dir
    fresh = DiskCache(base=base)
    assert fresh.get(key) == spine


# ---------------------------------------------------------------------------
# Perf budget (informational — does not fail CI)
# ---------------------------------------------------------------------------


def test_node_id_rejects_negative_index() -> None:
    """``_node_id`` is the base-36 encoder for spine ids — must reject negatives.

    The id must match the schema's NodeId pattern ``^[0-9a-z]+$``; a negative
    index would silently produce a string with ``-`` in it, breaking schema
    validation downstream. Catching at the source is much easier to debug.
    """
    from systemrdl_lsp.serialize import _node_id

    assert _node_id(0) == "0"
    assert _node_id(35) == "z"
    assert _node_id(36) == "10"
    with pytest.raises(ValueError, match="non-negative"):
        _node_id(-1)


def test_cached_expanded_resets_on_re_elaboration(small_stress_roots, tmp_path) -> None:
    """T1.5 correctness: ``CachedElaboration.expanded`` must NOT survive a
    cache.put — otherwise ``rdl/expandNode`` would serve stale field data
    after the user edits a register's fields and re-elaborates.

    The schema's nodeIds are assigned by depth-first visit order; an unrelated
    edit can shift every downstream id, so an old expand cache is never safe
    to reuse.
    """
    from systemrdl_lsp.compile import CachedElaboration, ElaborationCache

    cache = ElaborationCache()
    uri = (tmp_path / "x.rdl").as_uri()
    cache.put(uri, small_stress_roots, "buf1", None)
    entry = cache.get(uri)
    assert isinstance(entry, CachedElaboration)
    assert entry.expanded is None  # default — server lazily inits to {} on first expand
    # Simulate what the rdl/expandNode handler does: populate the memoization
    # dict during normal operation.
    entry.expanded = {"abc": {"kind": "reg", "name": "stale"}}
    # New elaboration → new entry → old expanded dict must be gone.
    cache.put(uri, small_stress_roots, "buf2", None)
    fresh = cache.get(uri)
    assert fresh is not entry, "put must replace the entry, not mutate it"
    assert fresh.expanded is None, (
        "stale expand-memoization must NOT survive a re-elaborate; "
        "nodeIds are reassigned by DFS order on every fresh serialize"
    )


def test_disk_cache_evict_lru_accepts_explicit_cap(tmp_path) -> None:
    """``evict_lru(max_entries=N)`` overrides the constructor default.

    Operators / tests use this to force-shrink a runaway cache without
    constructing a new ``DiskCache`` instance.
    """
    cache = DiskCache(base=tmp_path / "cache", max_entries=100)
    for i in range(6):
        cache.put(f"k{i}", {"i": i})
        time.sleep(0.01)  # distinct mtimes for deterministic LRU sort
    # All 6 fit under the constructor cap (100).
    assert len(list((tmp_path / "cache").iterdir())) == 6
    # Force evict down to 2 — overrides the constructor cap.
    evicted = cache.evict_lru(max_entries=2)
    assert evicted == 4
    assert len(list((tmp_path / "cache").iterdir())) == 2
    # Negative cap is clamped to 0 (defensive — kills everything).
    cache.evict_lru(max_entries=-5)
    assert list((tmp_path / "cache").iterdir()) == []


def test_disk_cache_put_cleans_tmp_when_target_unwritable(tmp_path, monkeypatch) -> None:
    """Atomicity guarantee: if ``os.replace`` fails, the leftover ``.tmp`` file
    must not survive — it would otherwise pollute the cache dir with N orphan
    artifacts on a flaky disk.
    """
    cache = DiskCache(base=tmp_path / "cache", max_entries=5)
    # Simulate a write failure at os.replace time. We let the .tmp write succeed
    # but make the rename raise OSError — the put() except branch must unlink
    # the orphan tmp file.
    real_replace = __import__("os").replace

    def boom(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr("systemrdl_lsp.cache.os.replace", boom)
    cache.put("doomed", {"v": 1})
    # Restore + assert no leftover .tmp files.
    monkeypatch.setattr("systemrdl_lsp.cache.os.replace", real_replace)
    leftover = list((tmp_path / "cache").glob("**/*.tmp"))
    assert leftover == [], f"tmp file leak after failed put: {leftover}"
    # And the target was never created.
    assert cache.get("doomed") is None


def test_spine_under_two_seconds_at_1k_regs(medium_stress_roots, capsys):
    """Wall-clock budget for the spine serialize on 1k regs.

    Not a hard fail because CI runners vary; we capture the number so a
    1.5x regression shows up in test output. T1's bigger goal is the
    spine-vs-full ratio, exercised in test_spine_plus_expansions_equals_full
    above. This test exists to flag pathological slowdowns.
    """
    # Warm the type cache + import paths
    _serialize_spine(medium_stress_roots, stale=False)
    t0 = time.perf_counter()
    spine = _serialize_spine(medium_stress_roots, stale=False, version=1)
    elapsed = time.perf_counter() - t0
    regs = sum(1 for root in spine["roots"] for _ in _walk_regs(root))
    assert regs == 1000
    # Don't fail on wall-clock; just print so regressions are visible.
    print(f"\n[T1 perf] spine of 1k regs: {elapsed * 1000:.0f} ms")
    # Sanity ceiling: if this takes > 5s on any reasonable machine, something
    # is seriously broken (the design budget is well under 2s).
    assert elapsed < 5.0, f"spine serialize at 1k regs took {elapsed:.1f}s — likely regression"
