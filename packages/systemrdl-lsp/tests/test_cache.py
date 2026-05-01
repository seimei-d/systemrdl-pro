"""Unit tests for systemrdl_lsp.cache.DiskCache (T1.3)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from systemrdl_lsp.cache import DiskCache, default_cache_dir, make_key


@pytest.fixture
def tmp_cache(tmp_path: Path) -> DiskCache:
    return DiskCache(base=tmp_path / "cache", max_entries=5)


# ---------------------------------------------------------------------------
# Key derivation
# ---------------------------------------------------------------------------


def test_make_key_is_deterministic() -> None:
    k1 = make_key("/abs/path/file.rdl", 12345, ["/inc/a", "/inc/b"], "1.29.0")
    k2 = make_key("/abs/path/file.rdl", 12345, ["/inc/a", "/inc/b"], "1.29.0")
    assert k1 == k2


def test_make_key_changes_on_mtime() -> None:
    k1 = make_key("/abs/path/file.rdl", 100, [], "1.29.0")
    k2 = make_key("/abs/path/file.rdl", 200, [], "1.29.0")
    assert k1 != k2


def test_make_key_changes_on_compiler_version() -> None:
    k1 = make_key("/abs/path/file.rdl", 100, [], "1.29.0")
    k2 = make_key("/abs/path/file.rdl", 100, [], "1.30.0")
    assert k1 != k2


def test_make_key_include_paths_order_invariant() -> None:
    """Include paths come from sets/dicts whose iteration order is unstable —
    sorting them before hashing is what makes the cache hit reliably."""
    k1 = make_key("/abs/file.rdl", 100, ["/a", "/b", "/c"], "1.29.0")
    k2 = make_key("/abs/file.rdl", 100, ["/c", "/a", "/b"], "1.29.0")
    assert k1 == k2


def test_make_key_is_32_hex_chars() -> None:
    k = make_key("/abs/x.rdl", 1, [], "1.0.0")
    assert len(k) == 32
    assert all(c in "0123456789abcdef" for c in k)


# ---------------------------------------------------------------------------
# Default cache dir
# ---------------------------------------------------------------------------


def test_default_cache_dir_honors_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYSTEMRDL_LSP_CACHE_DIR", "/custom/cache/dir")
    assert default_cache_dir() == Path("/custom/cache/dir")


def test_default_cache_dir_falls_back_to_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SYSTEMRDL_LSP_CACHE_DIR", raising=False)
    expected = Path.home() / ".cache" / "systemrdl-pro"
    assert default_cache_dir() == expected


# ---------------------------------------------------------------------------
# Get / put round-trip
# ---------------------------------------------------------------------------


def test_get_returns_none_on_miss(tmp_cache: DiskCache) -> None:
    assert tmp_cache.get("nonexistent_key_aaa") is None


def test_put_then_get_round_trip(tmp_cache: DiskCache) -> None:
    envelope = {"schemaVersion": "0.2.0", "version": 1, "lazy": True, "roots": []}
    tmp_cache.put("k1", envelope)
    assert tmp_cache.get("k1") == envelope


def test_put_does_not_create_dir_on_construction(tmp_path: Path) -> None:
    """Importing/constructing must not have filesystem side-effects."""
    DiskCache(base=tmp_path / "lazy_dir")
    assert not (tmp_path / "lazy_dir").exists()


def test_get_returns_none_on_corrupted_json(tmp_cache: DiskCache) -> None:
    # Manually plant a bad file
    (tmp_cache.base / "bad").mkdir(parents=True, exist_ok=True)
    (tmp_cache.base / "bad" / "spine.json").write_text("not json {{{", encoding="utf-8")
    assert tmp_cache.get("bad") is None


def test_get_returns_none_on_non_dict_payload(tmp_cache: DiskCache) -> None:
    (tmp_cache.base / "list").mkdir(parents=True, exist_ok=True)
    (tmp_cache.base / "list" / "spine.json").write_text("[1, 2, 3]", encoding="utf-8")
    assert tmp_cache.get("list") is None


def test_put_atomic_via_tmp_then_rename(tmp_cache: DiskCache) -> None:
    """After put, no .tmp file should remain."""
    tmp_cache.put("atomic", {"k": "v"})
    leftover = list(tmp_cache.base.glob("**/*.tmp"))
    assert leftover == []


def test_put_overwrites_existing(tmp_cache: DiskCache) -> None:
    tmp_cache.put("ow", {"v": 1})
    tmp_cache.put("ow", {"v": 2})
    assert tmp_cache.get("ow") == {"v": 2}


# ---------------------------------------------------------------------------
# LRU eviction
# ---------------------------------------------------------------------------


def test_eviction_at_max_entries(tmp_path: Path) -> None:
    cache = DiskCache(base=tmp_path / "cache", max_entries=3)
    for i in range(5):
        cache.put(f"key{i}", {"i": i})
        # Force distinct mtimes so the LRU sort is deterministic
        time.sleep(0.01)
    # max_entries=3 means after 5 puts, 2 oldest evicted
    remaining = list((tmp_path / "cache").iterdir())
    assert len(remaining) == 3
    # The newest 3 should remain
    assert cache.get("key0") is None
    assert cache.get("key1") is None
    assert cache.get("key2") == {"i": 2}
    assert cache.get("key4") == {"i": 4}


def test_eviction_no_op_under_cap(tmp_path: Path) -> None:
    cache = DiskCache(base=tmp_path / "cache", max_entries=10)
    for i in range(3):
        cache.put(f"k{i}", {"i": i})
    evicted = cache.evict_lru()
    assert evicted == 0


def test_eviction_no_op_when_dir_missing(tmp_path: Path) -> None:
    cache = DiskCache(base=tmp_path / "never_created", max_entries=3)
    assert cache.evict_lru() == 0


# ---------------------------------------------------------------------------
# clear()
# ---------------------------------------------------------------------------


def test_clear_wipes_everything(tmp_cache: DiskCache) -> None:
    tmp_cache.put("a", {"x": 1})
    tmp_cache.put("b", {"x": 2})
    assert tmp_cache.base.exists()
    tmp_cache.clear()
    assert not tmp_cache.base.exists()


def test_clear_no_op_when_dir_missing(tmp_path: Path) -> None:
    cache = DiskCache(base=tmp_path / "never_created")
    cache.clear()  # Must not raise
