"""Custom ``rdl/*`` request handlers — extracted from server.py.

These three handlers carry the bulk of viewer-facing wire traffic; pulling
them out keeps server.py focused on lifecycle wiring.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from ._uri import _uri_to_path
from .cache import make_key
from .serialize import (
    _build_node_index,
    _serialize_root,
    _serialize_spine,
    _unchanged_envelope,
    expand_node,
)

if TYPE_CHECKING:
    from ._state import ServerState

logger = logging.getLogger(__name__)


def handle_include_paths(state: "ServerState", params: Any) -> dict[str, Any]:
    """Return the deduped, source-labeled include search path list for a URI.

    Powers the "SystemRDL: Show effective include paths" command. Lets the
    user see exactly which paths are in effect — formerly opaque, especially
    with multiple sources (settings.json + peakrdl.toml + sibling-dir).
    """
    from .compile import _resolve_search_paths

    uri = None
    if isinstance(params, dict):
        uri = params.get("uri") or params.get("textDocument", {}).get("uri")
    else:
        uri = getattr(params, "uri", None)
        if uri is None and hasattr(params, "text_document"):
            uri = params.text_document.uri
    if not uri:
        return {"uri": None, "paths": []}
    try:
        resolved = _resolve_search_paths(uri, state.include_paths)
    except (ValueError, OSError):
        resolved = []
    return {
        "uri": uri,
        "paths": [{"path": p, "source": src} for p, src in resolved],
    }


def _disk_cache_key(state: "ServerState", uri: str) -> str | None:
    """On-disk cache key for ``uri``, or ``None`` if path/mtime unreadable.

    Folds the systemrdl-compiler version so a compiler upgrade auto-
    invalidates every cached envelope; folds ``include_vars`` so the same
    file with a different ``$VAR`` substitution map produces a different
    elaborated tree and therefore a different key.
    """
    try:
        path = _uri_to_path(uri)
    except ValueError:
        return None
    try:
        mtime_ns = path.stat().st_mtime_ns
    except OSError:
        return None
    try:
        import systemrdl as _systemrdl
        compiler_version = getattr(_systemrdl, "__version__", "unknown")
    except Exception:
        compiler_version = "unknown"
    return make_key(
        path, mtime_ns, state.include_paths, compiler_version,
        include_vars=state.include_vars,
    )


async def handle_elaborated_tree(state: "ServerState", params: Any) -> dict[str, Any]:
    """Custom JSON-RPC: viewer fetches the latest elaborated tree for a URI.

    The (potentially multi-second) serialize runs in ``asyncio.to_thread``
    so the LSP event loop is never blocked. Big designs no longer freeze
    hover / completion / diagnostics while the spine is being built.

    When the client advertised ``experimental.systemrdlLazyTree`` capability
    the server returns a spine envelope (``Reg.loadState = 'placeholder'``,
    fields fetched on demand via ``rdl/expandNode``). Old clients keep
    getting the full tree.

    Spine envelopes are also persisted to ``DiskCache`` so a VSCode window
    reload skips parse + elaborate + serialize for an unchanged file.
    Cache key folds in mtime + include paths + compiler version. Disk
    cache is only consulted for the lazy path because spine envelopes are
    small (~10 MB even for 25k regs) while full envelopes can hit 200 MB.

    Caching contract:

    - Request may include ``sinceVersion: int``. If it matches the LSP's
      cached version, the response is a tiny ``{unchanged: true, version}``
      envelope and the client keeps its previously-rendered tree intact.
    - First request (no ``sinceVersion`` or stale value) returns the
      serialized tree, cached on the LSP side keyed by ``(uri, version)``.

    Schema: ``schemas/elaborated-tree.json`` v0.2.0. Returns the cached
    last-good tree when the current parse has failed (design D7).
    """
    uri = None
    since_version: int | None = None
    if isinstance(params, dict):
        uri = params.get("uri") or params.get("textDocument", {}).get("uri")
        raw = params.get("sinceVersion")
        if isinstance(raw, int):
            since_version = raw
    else:
        uri = getattr(params, "uri", None)
        if uri is None and hasattr(params, "text_document"):
            uri = params.text_document.uri
        raw = getattr(params, "sinceVersion", None)
        if isinstance(raw, int):
            since_version = raw

    if not uri:
        return _serialize_root([], stale=False, version=0)
    cached = state.cache.get(uri)
    if cached is None:
        return _serialize_root([], stale=False, version=0)

    # Version-gated fast path: client already has this version, skip both
    # serialization and transport of the (potentially huge) tree body.
    if since_version is not None and since_version == cached.version:
        return _unchanged_envelope(cached.version)

    # In-memory fast path: same-version re-fetch returns the memoized dict.
    if cached.serialized is not None:
        return cached.serialized

    if state.lazy_supported:
        disk_key = _disk_cache_key(state, uri)
        if disk_key is not None:
            disk_envelope = await asyncio.to_thread(state.disk_cache.get, disk_key)
            # The cache is content-addressed; a hit means the envelope's
            # *content* is byte-equivalent to a fresh serialize. The
            # envelope's `version` field is a per-process monotonic counter
            # for sinceVersion gating; the disk copy carries whatever counter
            # the previous LSP run had, which is rarely == the current run's
            # counter. Rewrite to the current in-memory version. Without this
            # rewrite the disk cache was effectively dead — every cold start
            # re-serialized.
            if isinstance(disk_envelope, dict):
                disk_envelope["version"] = cached.version
                # Stale flag is per-process runtime state, NOT part of the
                # content-addressed disk key — override with the current
                # state so a "valid → fetched → broken" flow doesn't keep
                # serving the non-stale envelope from disk after the
                # in-memory invalidation.
                disk_envelope["stale"] = uri in state.stale_uris
                cached.serialized = disk_envelope
                # Disk hit skips the spine build path that populates the
                # expand-index. Build it in the background so first-click
                # expand stays O(1).
                if cached.node_index is None:
                    async def _bg_build_index(c: Any = cached) -> None:
                        try:
                            idx = await asyncio.to_thread(
                                _build_node_index, c.roots
                            )
                            if c.node_index is None:
                                c.node_index = idx
                        except Exception:
                            logger.debug(
                                "background node-index build failed",
                                exc_info=True,
                            )
                    asyncio.create_task(_bg_build_index())
                return disk_envelope

    try:
        original_path = _uri_to_path(uri)
    except ValueError:
        original_path = None
    translate = (
        {cached.temp_path: original_path}
        if cached.temp_path is not None and original_path is not None
        else None
    )
    # Spine vs full per client capability. Threaded so the loop stays
    # responsive during the ~1s/~6s walk on 25k regs. Build the expand-
    # index on the same DFS pass when the cache doesn't have one yet
    # (zero extra walk cost; pool worker normally fills it).
    serialize_fn = _serialize_spine if state.lazy_supported else _serialize_root
    index_out: dict[str, Any] | None = (
        {} if state.lazy_supported and cached.node_index is None else None
    )
    envelope = await asyncio.to_thread(
        serialize_fn,
        cached.roots,
        uri in state.stale_uris,
        path_translate=translate,
        version=cached.version,
        out_index=index_out,
    )
    cached.serialized = envelope
    if index_out is not None:
        cached.node_index = index_out

    if state.lazy_supported:
        disk_key = _disk_cache_key(state, uri)
        if disk_key is not None:
            asyncio.create_task(
                asyncio.to_thread(state.disk_cache.put, disk_key, envelope)
            )
    return envelope


async def handle_expand_node(state: "ServerState", params: Any) -> dict[str, Any]:
    """Custom JSON-RPC: lazy-mode client requests details for a placeholder Reg.

    Request: ``{uri, version, nodeId}``. Response: a single ``Reg`` dict with
    ``fields[]`` populated and ``loadState`` absent. The ``nodeId`` is the
    opaque base-36 string the client got from the spine; it's only valid
    within the (uri, version) pair the spine was emitted for.

    Errors raised as JSON-RPC error responses:

    - -32001 ``NodeNotFound`` — nodeId doesn't name any reg (or names a
      container, which can't be expanded — containers are always fully
      present in the spine).
    - -32002 ``VersionMismatch`` — version doesn't match the LSP's current
      cached version. Client should re-fetch the spine.
    """
    from pygls.exceptions import JsonRpcException

    uri = None
    version: int | None = None
    node_id: str | None = None
    if isinstance(params, dict):
        uri = params.get("uri") or params.get("textDocument", {}).get("uri")
        v = params.get("version")
        if isinstance(v, int):
            version = v
        n = params.get("nodeId")
        if isinstance(n, str):
            node_id = n
    else:
        uri = getattr(params, "uri", None)
        v = getattr(params, "version", None)
        if isinstance(v, int):
            version = v
        n = getattr(params, "nodeId", None)
        if isinstance(n, str):
            node_id = n

    if not uri or version is None or not node_id:
        raise JsonRpcException(code=-32602, message="invalid expandNode params")
    cached = state.cache.get(uri)
    if cached is None:
        # Soft signal — viewer asked before first elaboration landed (or
        # after the cache was evicted). Returning a sentinel keeps this off
        # the JSON-RPC error channel.
        return {"outdated": True, "currentVersion": None}
    if cached.version != version:
        return {"outdated": True, "currentVersion": cached.version}

    if cached.expanded is None:
        cached.expanded = {}
    if node_id in cached.expanded:
        return cached.expanded[node_id]

    try:
        original_path = _uri_to_path(uri)
    except ValueError:
        original_path = None
    translate = (
        {cached.temp_path: original_path}
        if cached.temp_path is not None and original_path is not None
        else None
    )
    if cached.node_index is None:
        cached.node_index = await asyncio.to_thread(
            _build_node_index, cached.roots
        )
    result = await asyncio.to_thread(
        expand_node, cached.roots, node_id, translate, cached.node_index
    )
    if result is None:
        raise JsonRpcException(
            code=-32001, message=f"NodeNotFound (nodeId={node_id})"
        )
    cached.expanded[node_id] = result
    return result
