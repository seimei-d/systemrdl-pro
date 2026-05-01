r"""On-disk cache for serialized spine envelopes.

T1.3 of the LSP perf overhaul. Lets a VSCode window reload (or a second
window in a multi-root workspace) skip the parse + elaborate + serialize
pipeline for an unchanged source file. The cache is keyed by content,
not path, so renames/moves don't poison entries — and so the same
.rdl file in two different workspaces shares one cache entry.

Layout::

    ~/.cache/systemrdl-pro/<key>/spine.json

Where ``<key>`` is the first 32 hex chars of
``sha256(abs_path + mtime_ns + sorted_include_paths + compiler_version)``.
The compiler version is folded in so a ``pip install -U systemrdl-compiler``
auto-invalidates everything (output schema may have shifted in subtle ways).

Known limitation: changes to ``\`include``d files do not invalidate the
cache because their mtimes are not in the key. Acceptable for T1 since the
primary pain point is window reload of unchanged files. T2-A (per-file
parse cache) addresses this properly via include-graph hashing.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import pathlib
import shutil
from typing import Any

logger = logging.getLogger(__name__)

CACHE_DIR_ENV = "SYSTEMRDL_LSP_CACHE_DIR"
DEFAULT_MAX_ENTRIES = 50


def default_cache_dir() -> pathlib.Path:
    """Return the default cache directory.

    Honors ``SYSTEMRDL_LSP_CACHE_DIR`` for tests / CI / power users. Falls
    back to ``~/.cache/systemrdl-pro``. The dir is created lazily on first
    write, never on construction — important so importing this module
    doesn't have a filesystem side-effect.
    """
    override = os.environ.get(CACHE_DIR_ENV)
    if override:
        return pathlib.Path(override)
    return pathlib.Path.home() / ".cache" / "systemrdl-pro"


def make_key(
    abs_path: pathlib.Path | str,
    mtime_ns: int,
    include_paths: list[str] | tuple[str, ...],
    compiler_version: str,
) -> str:
    """Compute the content-addressed cache key.

    Inputs are concatenated with a NUL separator (which can't appear in
    filenames or version strings) and hashed with SHA-256. Truncated to
    the first 32 hex chars — 128 bits, far below collision probability
    for any plausible cache size.
    """
    abs_str = str(pathlib.Path(abs_path).resolve()) if abs_path else ""
    inc_str = "\0".join(sorted(include_paths))
    raw = f"{abs_str}\0{mtime_ns}\0{inc_str}\0{compiler_version}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


class DiskCache:
    """File-backed spine envelope cache. Thread-safe for the single-process LSP.

    The LSP only ever has one writer per key (the elaboration finalizer)
    and many readers (every ``rdl/elaboratedTree`` request). Writes are
    atomic via tmp-file + rename so a partially-written file is impossible.
    Reads tolerate corrupted JSON (returns None and logs).

    Eviction is LRU by directory mtime — every successful ``put`` runs
    ``evict_lru`` to keep the cache bounded. No TTL: an unchanged file
    can sit cached forever and still be valid.
    """

    def __init__(
        self,
        base: pathlib.Path | None = None,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        self.base = base if base is not None else default_cache_dir()
        self.max_entries = max_entries

    # ------------------------------------------------------------------
    # Read / write
    # ------------------------------------------------------------------

    def _path_for(self, key: str) -> pathlib.Path:
        return self.base / key / "spine.json"

    def get(self, key: str) -> dict[str, Any] | None:
        """Return the cached envelope or None on miss / corruption.

        Touches the directory mtime on hit so the LRU walk treats this
        as recently-used. Logs a warning on JSON decode failure (so
        operators can spot a corrupted entry in the LSP log) but never
        raises — the LSP must always be able to fall back to elaboration.
        """
        p = self._path_for(key)
        try:
            data = p.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            logger.warning("DiskCache.get(%s): %s", key, exc)
            return None
        try:
            envelope = json.loads(data)
        except json.JSONDecodeError as exc:
            logger.warning("DiskCache.get(%s): corrupted JSON: %s", key, exc)
            return None
        if not isinstance(envelope, dict):
            logger.warning("DiskCache.get(%s): not a dict, skipping", key)
            return None
        # LRU touch — mtime on the dir, not the file, so the eviction walk
        # (which sorts directories) sees the right ordering.
        try:
            os.utime(p.parent, None)
        except OSError:
            pass
        return envelope

    def put(self, key: str, envelope: dict[str, Any]) -> None:
        """Atomically write the envelope to ``<base>/<key>/spine.json``.

        Writes to a sibling ``.tmp`` file then renames; partial writes
        cannot pollute a successful prior cache entry. After a successful
        put, runs ``evict_lru`` to keep the cache bounded.
        """
        target = self._path_for(key)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("DiskCache.put(%s): cannot mkdir: %s", key, exc)
            return
        tmp = target.with_suffix(target.suffix + ".tmp")
        try:
            tmp.write_text(json.dumps(envelope), encoding="utf-8")
            os.replace(tmp, target)
        except OSError as exc:
            logger.warning("DiskCache.put(%s): write failed: %s", key, exc)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return
        self.evict_lru()

    def evict_lru(self, max_entries: int | None = None) -> int:
        """Delete oldest cache directories beyond ``max_entries``.

        Returns the number of entries evicted (0 if under the cap or if
        the cache dir doesn't exist yet). Errors during deletion are
        logged but never raised — eviction is best-effort.
        """
        cap = max_entries if max_entries is not None else self.max_entries
        if cap < 0:
            cap = 0
        if not self.base.exists():
            return 0
        try:
            entries = [d for d in self.base.iterdir() if d.is_dir()]
        except OSError as exc:
            logger.warning("DiskCache.evict_lru: cannot list base: %s", exc)
            return 0
        if len(entries) <= cap:
            return 0
        # Sort by directory mtime ascending — oldest first, evict from the front.
        try:
            entries.sort(key=lambda d: d.stat().st_mtime)
        except OSError as exc:
            logger.warning("DiskCache.evict_lru: stat failed: %s", exc)
            return 0
        evicted = 0
        for d in entries[: len(entries) - cap]:
            try:
                shutil.rmtree(d)
                evicted += 1
            except OSError as exc:
                logger.warning("DiskCache.evict_lru: rmtree %s failed: %s", d, exc)
        return evicted

    def clear(self) -> None:
        """Wipe the entire cache. Test/maintenance hook; not used by the LSP."""
        if not self.base.exists():
            return
        try:
            shutil.rmtree(self.base)
        except OSError as exc:
            logger.warning("DiskCache.clear: %s", exc)
