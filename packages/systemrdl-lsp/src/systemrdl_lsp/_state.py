"""ServerState dataclass + the timeout constants it depends on.

Extracted from server.py so the bag-of-state isn't woven into the same
file as the feature-handler wiring.
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

from .cache import DiskCache
from .compile import ElaborationCache


# Eng-review safety net #3: cap a single elaborate pass at the configured
# wall-clock limit. Past that we keep last-good (D7) and surface a synthetic
# diagnostic. A pathological Perl-style include cycle in a third-party RDL
# pack should NOT freeze the editor. Override per-workspace via
# systemrdl-pro.elaborationTimeoutMs (state.elaboration_timeout_s).
ELABORATION_TIMEOUT_SECONDS = 120.0
ELABORATION_TIMEOUT_SECONDS_MIN = 1.0
ELABORATION_TIMEOUT_SECONDS_MAX = 600.0


@dataclasses.dataclass
class ServerState:
    cache: ElaborationCache = dataclasses.field(default_factory=ElaborationCache)
    pending: dict[str, asyncio.Task] = dataclasses.field(default_factory=dict)
    include_paths: list[str] = dataclasses.field(default_factory=list)
    # T2-A: forward and reverse maps over the include graph, populated from
    # the set of source files the compiler actually opened on each successful
    # elaborate (see ``compile._harvest_consumed_files``). Used to proactively
    # re-elaborate any open consumer when a library file changes — the user
    # no longer has to close-and-reopen ``stress_25k.rdl`` after editing
    # ``types.rdl``. Both maps key on URIs.
    include_graph: dict[str, set[str]] = dataclasses.field(default_factory=dict)
    includee_to_includers: dict[str, set[str]] = dataclasses.field(default_factory=dict)
    # T2-C: per-URI elaboration mutex. Same-URI didOpen+didSave races
    # serialize through this lock so the second call sees the first call's
    # cache.put before short-circuiting on buffer-equality. Different URIs
    # still elaborate concurrently (subject to GIL contention).
    uri_elab_locks: dict[str, asyncio.Lock] = dataclasses.field(default_factory=dict)
    # In-flight ProcessPool futures per URI — when a rapid edit cancels the
    # parent asyncio task we also try to ``.cancel()`` the pool future, which
    # frees a not-yet-started worker slot. Workers that have already started
    # keep running (Python's ProcessPool can't kill them safely), but their
    # result is discarded because the awaiter is gone.
    inflight_pool_futures: dict[str, Any] = dataclasses.field(default_factory=dict)
    # Last published diagnostics per URI, for the LSP 3.17 pull model
    # (``textDocument/diagnostic``). Push (``publishDiagnostics``) stays
    # the primary delivery; this cache makes ``pull`` cheap.
    last_diagnostics: dict[str, list[Any]] = dataclasses.field(default_factory=dict)
    # Test-only override that stands in for the pygls workspace. When
    # non-None, ``_is_open(uri)`` returns ``uri in self`` and
    # ``_read_buffer(uri)`` returns ``self.get(uri)`` instead of going
    # through ``server.workspace`` (which is not initialized in unit
    # tests). Production code never sets this. Map: URI → buffer text.
    test_open_buffers: dict[str, str] | None = None
    # Substitution map for ``$VAR`` / ``${VAR}`` inside ```include "..."`` paths.
    # Read from systemrdl-pro.includeVars; falls back to os.environ during expansion.
    include_vars: dict[str, str] = dataclasses.field(default_factory=dict)
    # URIs whose latest parse attempt failed but for which we still have a last-good
    # cache entry. The viewer renders a stale-bar when a URI is in this set (D7).
    stale_uris: set[str] = dataclasses.field(default_factory=set)
    # URIs that need to skip the buffer-equality / canonicalize-skip
    # short-circuits on the next elaborate (typically because a cascade
    # trigger needs to re-elaborate the file even though its own buffer
    # didn't change — an includee changed). Cleared the moment the
    # bypass actually fires. Separate from ``stale_uris`` so the stale-
    # transition logic in ``_apply_compile_result`` can detect a
    # genuine False → True (or T → F) move; if cascade overloaded
    # stale_uris like it used to, ``was_stale`` would always be True
    # on the cascade path and the viewer would miss the new failure.
    force_re_elaborate: set[str] = dataclasses.field(default_factory=set)
    # Forwarded to RDLCompiler(perl_safe_opcodes=...). Empty list keeps the
    # compiler's safe default. Power users adding `:base_io` for `print`-based
    # codegen go through this setting.
    perl_safe_opcodes: list[str] = dataclasses.field(default_factory=list)
    # One-shot guard for the "Perl is not on PATH" notification. The diagnostic
    # itself comes from systemrdl-compiler on every compile, so we only nag with
    # the modal banner once per session.
    perl_warning_shown: bool = False
    # Per-primary-URI snapshot of which cross-file URIs we last published
    # non-empty diagnostics to. Drives the clear-on-resolve cycle for
    # `\`include`d files (a fixed error in common.rdl publishes [] there next
    # compile so the stale squiggle disappears).
    diag_affected: dict[str, set[str]] = dataclasses.field(default_factory=dict)
    # Background pre-index toggles. workspace/symbol only sees files the user
    # has opened; pre-warming the cache fixes Ctrl+T against unfamiliar trees.
    # Default off — on a multi-window workspace each VSCode window starts its
    # own LSP and the parallel pre-indexes pegged the CPU. Users who want
    # workspace-wide search opt in via settings.json.
    preindex_enabled: bool = False
    preindex_max_files: int = 200
    # Per-workspace override for ELABORATION_TIMEOUT_SECONDS. systemrdl-pro.elaborationTimeoutMs
    # surfaces this so big chip designs (multi-subsystem aggregates in the 10-25k+ register
    # range) can lift the 10s cap when their elaboration legitimately takes longer.
    elaboration_timeout_s: float = ELABORATION_TIMEOUT_SECONDS
    # T1.4: lazy-tree capability gate. Set in INITIALIZED handler from
    # client.capabilities.experimental.systemrdlLazyTree. When True the LSP
    # serves spine envelopes (Reg.loadState='placeholder') and answers
    # rdl/expandNode requests; when False it serves full trees as before.
    lazy_supported: bool = False
    # T1.4: on-disk cache for spine envelopes. Survives window reload; second
    # window in a multi-root workspace shares the cache via content key.
    # Constructed lazily by the build_server() helper so tests can swap it.
    disk_cache: DiskCache = dataclasses.field(default_factory=DiskCache)
    # T3: ProcessPoolExecutor for cross-URI parallel elaborate. ``None``
    # means in-thread (legacy/fallback). Initialized on INITIALIZED based
    # on systemrdl-pro.elaborateInProcess; defaults to subprocess mode.
    # Workers pre-warm via _pool_warmup_noop so the first real elaborate
    # doesn't pay spawn + import cost on the user's typing latency.
    elaborate_pool: Any = None  # ProcessPoolExecutor when active
    # systemrdl-pro.elaborateInProcess setting. False (default) = use
    # the subprocess pool. True = run in the asyncio default
    # ThreadPoolExecutor as before T3 — kept as an escape hatch in
    # case a future systemrdl-compiler upgrade breaks RootNode pickle
    # compatibility, or for diagnosing pool-related issues in the field.
    elaborate_in_process: bool = False
    # Pool size cap. 2 covers the documented pain (small file behind
    # one big elaborate). Bumping higher is fine but each worker holds
    # ~150 MB resident at idle plus the in-flight tree, so 2-4 is the
    # sweet spot for a developer machine. Surfaced as a setting if a
    # future user reports needing more.
    elaborate_pool_workers: int = 2
    # T3-G: worker recycle threshold. Mitigates the upstream
    # systemrdl-compiler memory leak (~5 MB per elaborate of a
    # 40-reg fixture, observed in test_memory_growth_bounded). After
    # this many successful subprocess elaborates we tear down the
    # pool and spawn a fresh one — RSS comes back to baseline. 50
    # gives a few minutes of editing on big designs before the
    # next recycle, recycling itself is ~150 ms (worker spawn +
    # _pool_worker_init), barely visible during a typing pause.
    pool_max_elaborates: int = 50
    # Counts successful subprocess elaborates since the current pool
    # was spawned. Reset to 0 in ``_ensure_elaborate_pool``. When it
    # hits ``pool_max_elaborates`` the elaborate-finished bookkeeping
    # schedules a recycle and resets the counter.
    pool_elaborate_count: int = 0
