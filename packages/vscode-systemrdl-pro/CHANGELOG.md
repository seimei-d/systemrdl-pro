# Changelog

All notable changes to **SystemRDL Pro** are documented here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project uses [SemVer](https://semver.org/).

## [0.28.0] — 2026-05-02

T4-C/T4-D — architectural cleanups (Tier-3) + performance fixes
(Tier-4) from the post-T3 multi-agent code review. No new features;
~20 reliability/correctness/performance items landed across all four
packages. Bigger architectural splits (server.py / extension.ts /
viewer-core hooks; viewer virtualisation) deferred to dedicated PRs
where the regression risk warrants standalone review.

### Architecture (T4-C)

- **A3** — `_apply_stale_transition` helper consolidates the stale
  state machine that used to live inline in three sites
  (`_apply_compile_result` success + parse-failure, `_full_pass_async`
  timeout). The last three field-reported stale-bar regressions all
  traced back to drift between the copies.
- **A5** — `_read_workspace_config` + `_apply_workspace_config`
  helpers dedup the config fetch/refresh flow that was copy-pasted
  between `_on_initialized` and `_on_config_change`.
- **A7** — `_format_hex` moved to `compile.py`; `outline.py` no
  longer imports from `hover.py` (inverted dependency cleanup).
- **A10** — `_children_safe` helper kills the four-times-copy-pasted
  `node.children(skip_not_present=...)` try/except dance in
  `_harvest_consumed_files` and `_fingerprint_roots`.

### Performance (T4-D)

In-LSP:

- **P6** — `DiskCache.evict_lru` no longer issues `iterdir + stat`
  on every put. Counter gate runs the actual eviction every
  `max_entries / 4` puts.
- **P7** — `_harvest_consumed_files` dedups raw paths before
  `pathlib.resolve()` (~50–250 ms saved on 25k regs; the harvest
  visited 50k src_refs but only K unique filenames).
- **P8** — `_on_close` cancels the pending debounce task and
  drops the elaboration lock (was leaving locks alive forever for
  files closed during a debounce window).
- **P10** — `_peakrdl_toml_paths` cached via `lru_cache(128)` —
  pre-T4-D walked ancestor dirs and read the toml on every
  `_resolve_search_paths` call (every didOpen / didSave).
- **P11** — `_build_selection_ranges` builds a prefix-sum of line
  start offsets so `pos_of` is O(log n) (binary search) instead of
  O(n) per call. Deep nesting in 10k-line files used to be O(K·N).

In viewer-core:

- **P2** — `TreeRow` wrapped in `React.memo` so a selection change
  re-renders only the two rows whose `selected` flipped, not all
  500–25k visible rows.
- **P3** — `findRegByKey` wrapped in `useMemo` so the O(n) DFS
  doesn't run in the render body on every keystroke / scroll /
  toast.
- **P4** — Filter input debounced 150 ms. Pre-T4-D fast typing
  triggered 200k field comparisons per keystroke on a 25k-reg
  tree.
- **P5** — `Detail` wrapped in `React.memo` so filter keystrokes
  don't re-render the heavy bit-grid + field rows when the active
  reg hasn't changed.

In extension:

- **P12** — `onDidChangeDiagnostics` debounced 200 ms. Status-bar
  diag refresh used to fire per LSP publish (multiple per keystroke
  during catchup), each iterating the entire DiagnosticCollection.
- **P13** — `cachedRegCount` + `cachedRootNames` populated once on
  `refreshMemoryMap`. Status-bar updates no longer re-walk the tree
  on every tab switch / debounced diag tick.
- **P14** — `refreshMemoryMap` coalesces concurrent calls via a
  per-panel inflight promise. Drag/resize used to fire multiple LSP
  round-trips for the same effective work.

In rdl-viewer-cli:

- **P15** — Tree pre-serialised once per refresh (`latestTreeJson`)
  instead of being re-`JSON.stringify`-d on every `/tree` request
  and every SSE connect.
- **P16** — `JSON.parse` accepts the dump's `Buffer` directly,
  avoiding the intermediate UTF-8 string allocation (saves ~half
  the peak memory transient on 25k-reg dumps).
- **P17** — `staticAsset` caches asset bytes in a `Map<string,
  Buffer>` after the first load. Pre-T4-D every request did
  `existsSync + readFileSync` syscalls.

### Deferred to follow-up PRs

These are real items from the review but warrant standalone PR
review because they restructure or add risk:

- **A1** — split `server.py` build_server() factory.
- **A2** — `ServerState` sub-types (T2 / T3 / config clusters).
- **A4** — separate test-hooks module + shrink `__all__`.
- **A8** — move `_canonicalize_for_skip` + `_fingerprint_roots` to `cache.py`.
- **A9** — split `_full_pass_async` into named phases.
- **A11** — viewer-core `useTransport` + `useExpandNode` hooks.
- **A12** — extension.ts module split.
- **A13/A14** — typed transport factory in viewer-core, used by
  extension and CLI inline scripts.
- **A15/A16** — shared python-probe module + CLI server-state factory.
- **P1** — viewer virtualisation (largest UX win; needs careful
  perf benchmarks before/after to justify).
- **P9** — `cached.serialized` non-lazy memory release.

### Tests

138 pass (no test changes — these are reliability/perf fixes
without new behaviour surface). All typechecks pass.

## [0.27.1] — 2026-05-02

T4-B — eleven Tier-2 hardening fixes from the post-T3 multi-agent
code review. No new features; reliability + performance + correctness
improvements that prevent classes of bugs from emerging.

### Fixed

- **H1 — `pendingExpansions` `useState` → `useRef`.** Pre-T4-B used
  `useState<Set<string>>` with the setter discarded — the Set was
  mutated in place. Under React 18 StrictMode the effect double-
  invokes; the second invocation found the key already in the Set
  from the first run and silently dropped the expand request,
  leaving the placeholder spinner stuck in dev. Real `useRef` now.
- **H5 — Per-panel cursor sync state.** `cursorSyncTimer` and
  `suppressNextCursorSync` were module-globals. With two panels
  open (multi-root, Decision 3C), a reveal in panel A would
  suppress the next cursor sync for panel B too, and a cursor move
  in panel A's editor would clear panel B's pending debounce.
  Both fields now live on `PanelEntry` per panel.
- **H6 — Restart-server subscription leak.** Each call to
  `startServer` (initial + every `restartServer`) used to push a
  fresh `{ dispose: () => client?.stop() }` into
  `context.subscriptions` and register a fresh trio of
  `client.onNotification` handlers without cleaning up prior
  registrations. After N restarts every push notification fired
  N×`refreshMemoryMap`. Tracked lifecycle aggregate now disposed
  before re-creating.
- **H7 — `clientReady` hung forever on early-return.** `startServer`
  returned without resolving the readiness promise when Python was
  not found or the LSP module was missing. Deserialized panels
  awaiting `clientReady` blocked on it indefinitely, leaving the
  viewer permanently blank with no error message even after a
  restart fixed the underlying issue. `signalClientReady()` now
  fires on every exit path including the catch arm of
  `client.start()`.
- **H8 — CSPRNG nonce.** `makeNonce()` used `Math.random()` —
  category-error vs the CSP nonce mechanism. Switched to
  `crypto.randomBytes(18).toString('base64url')`.
- **H9 — Watcher + decoration disposal.** `createFileSystemWatcher`
  was leaked by `LanguageClient.synchronize.fileEvents` on every
  restart (one inotify/fd handle per restart). `flashDecoration`
  was a module-load-time singleton with no disposal path. Both now
  tracked and disposed correctly.

### Fixed (in `systemrdl-lsp` 0.19.1)

- **H2 — `include_vars` in disk cache key.** `make_key` hashed
  `(abs_path, mtime, include_paths, compiler_version)` but NOT the
  `include_vars` substitution map. Two compiles of the same file
  with different `$IP_ROOT` values would hit the same disk-cache
  entry and the second would receive the first's spine envelope —
  silent cross-workspace cache poisoning. Vars now in the key
  (sorted key=value pairs, NUL-separated).
- **H3 — `_fingerprint_roots` type cache.** Pre-T4-B did 200k+
  uncached `node.get_property()` calls per fingerprint on a 25k-
  reg fixture (~1s wasted per elaborate). Mirrors `serialize.py`'s
  `_TypeCache` pattern: keyed by `(id(original_def), prop_name)`,
  bypassed for instance-level overrides.
- **H4 — Hover off the event loop + single tree walk.**
  `_property_origin_hint` did a synchronous `Path.read_text` on
  every hover; the hover handler runs on the asyncio event loop, so
  a slow NFS / spinning-disk read would stall diagnostics,
  completion, and every other LSP response for hundreds of
  milliseconds. New optional `line_reader` parameter consults the
  LSP buffer cache first, falls back to disk only when not
  provided. Plus `_node_at_position` collapsed from two full tree
  walks (line-counter + best-match) to one, and the `fields()`
  double-visit (children(unroll=True) already yields fields)
  was removed — roughly 2× speedup on every hover.

### Fixed (in `rdl-viewer` 0.1.0+)

- **H10 — SIGTERM handler.** Pre-T4-B only `SIGINT` was handled.
  Docker, systemd, plain `kill <pid>` all default to SIGTERM and
  took the default behaviour (immediate exit, no cleanup) —
  leaking the watcher's inotify handle and orphaning any
  in-flight Python dump child as a zombie until the kernel
  reaped it. Mirror SIGINT handler.
- **H11 — `--port` / `--python` argv bounds check.** Pre-T4-B did
  bare `Number(argv[++i])` for `--port` (forgot value →
  `Number(undefined)=NaN` → Bun.serve assigned a random port,
  silent confusion) and bare `argv[++i]` for `--python`
  (`undefined` propagates into spawnSync, throws uncaught). Now
  validates + exits with an actionable error.

### Tests

138 pass (no test changes — these are reliability/perf fixes
without new behavior surface).

## [0.27.0] — 2026-05-02

T4-A — six Tier-1 critical bug fixes from the post-T3 multi-agent
code review (`docs/code-review-2026-05-02.md`, local-only). Each one
was a real bug nobody had reported yet but every one would surface
eventually.

### Fixed

- **C1 — Decoder showed `0x0` for fields above bit 31 of 64-bit
  registers.** JavaScript `>>>` is a 32-bit shift; a 64-bit reg with
  a field at `lsb >= 32` silently truncated to zero regardless of
  input. Decoder now routes >32-bit registers through `BigInt`
  arithmetic; ≤32-bit registers stay on the cheaper `Number` path.
- **C2 — Lazy expand left placeholder spinner stuck on screen.**
  `spliceExpandedReg` mutated the parent's `children` array in place
  and only spread the top-level `tree` envelope, so `useMemo([root])`
  callers (`buildFlatList` → `flatRows`) didn't recompute and the
  tree kept rendering the placeholder row even though the reg's data
  was already in memory. Splice now returns a fresh tree with every
  container on the root → reg path cloned (immutable update path).
- **C3 — `expandNode` race lost responses on rapid clicks.**
  `expandResolvers` was keyed on `nodeId` only — a second click for
  the same node (or a v2 elaborate landing while v1's expand was in
  flight) overwrote the resolver, leaking the prior promise so the
  spinner never cleared. Resolvers now keyed by `version+":"+nodeId`,
  and rapid duplicate clicks short-circuit to the existing in-flight
  promise instead of posting a second LSP request and overwriting
  the resolver.
- **C4 — `getTree()` rejection silently hung the viewer.** The
  `.catch(() => {})` swallowed every failure mode (LSP startup
  failure, transport timeout). The viewer now renders an error pane
  with the message + a Retry button; live tree updates clear the
  error automatically when the transport recovers.

### Fixed (in `systemrdl-lsp` 0.19.0)

- **C5 — Standalone CLI binary served HTTP 500 on every static
  asset.** `VIEWER_CORE_DIST` was hardcoded to a path relative to
  the source tree; after `bun build` produced `dist/rdl-viewer.js`
  the path no longer resolved. Build now copies viewer assets into
  `dist/viewer/` next to the binary, and `VIEWER_CORE_DIST` prefers
  that location with the dev-source path as fallback.
- **C6 — LSP shutdown / cancellation leaked tmp files and stuck the
  "Re-elaborating" spinner.** `_full_pass_async` sent
  `rdl/elaborationStarted` then awaited the shielded subprocess
  future; `CancelledError` propagated out without sending `Finished`
  (spinner stuck) and without registering a tmp-file cleanup
  callback (subprocess kept running, wrote tmp, no-one unlinked it).
  New explicit `except CancelledError` catches the case, registers
  a done-callback that unlinks the tmp when the late result arrives,
  emits `Finished`, then re-raises so the cancellation still
  propagates to whatever requested it.

### Tests

138 pass (137 baseline + 1 new in `tests/test_perf_t3.py` covering
the cancellation path).

## [0.26.4] — 2026-05-02

### Fixed (in `systemrdl-lsp` 0.18.4)

- **Stale-bar surfaces semantic ERRORs that survive a successful
  elaborate.** Field-reported case: alias and main register sharing
  the same address — Problems panel showed the conflict but the
  webview rendered the tree with no warning. `systemrdl-compiler`
  treats most semantic conflicts as FATAL (handled by the existing
  parse-failure branch), but the success path now also marks the
  URI stale when post-elaborate diagnostics carry any ERROR
  severity. Defensive against permissive-compiler futures and
  custom diagnostic plugins like the address-conflict scan.
- **Stale-bar text reworded.** "Showing last good elaboration ·
  current parse failed" was misleading when the trigger was a
  semantic ERROR (the tree IS current, it just has issues). New
  text: "Errors detected — see Problems panel · viewer may show
  last good or pre-error state". Same visual indicator.

## [0.26.3] — 2026-05-02

### Fixed (in `systemrdl-lsp` 0.18.3)

- **Stale-bar still missing on broken file when disk cache hit (third
  field-reported attempt at this).** Editing a file invalid + asking
  the viewer for the tree returned the cached-on-disk envelope (with
  `stale=False` from the prior successful fetch) because the disk
  cache key is content-addressed by mtime. The buffer differs from
  disk while unsaved → mtime unchanged → same key → hit → wrong
  stale value rendered.

  Fix: the disk-cache fast path in `rdl/elaboratedTree` now overrides
  `envelope["stale"]` from `state.stale_uris` instead of trusting the
  value frozen at disk-write time. Symmetric to the existing
  `version` override that's been there since T1.4.

  This is the ACTUAL root cause of the "broke the file, editor shows
  error, webview shows nothing wrong" pattern. The 0.26.1 + 0.26.2
  fixes addressed in-memory cache invalidation but the disk cache
  was a third source serving a non-stale envelope.

## [0.26.2] — 2026-05-02

Two stale-bar correctness gaps caught in field testing of 0.26.1.

### Fixed (in `systemrdl-lsp` 0.18.2)

- **Stale-bar no longer sticks on a recovered file.** Editing a file
  invalid → fixing it back to a byte-identical AST left the
  "Showing last good" indicator visible because the fingerprint-skip
  path discarded `state.stale_uris` but didn't bump the cache
  version or invalidate `cached.serialized`. The viewer kept
  rendering the envelope it fetched while the parse was failing.
  Fix: stale True → False transition in the fingerprint-skip path
  bumps version + clears serialized + notifies. Symmetric with the
  False → True fix from 0.26.1.
- **Cascade-failure now surfaces the stale-bar on every consumer.**
  Editing a library file (e.g. `types.rdl`) into a parse error
  cascade-re-elaborates every open consumer. Each consumer's
  elaborate fails too — but the cascade trigger used to overload
  `state.stale_uris` to bypass the buffer-equality short-circuit,
  so by the time `_apply_compile_result` ran the False → True
  transition detector saw `was_stale = True` and skipped the
  notification. Consumers kept their last good render with no
  stale indicator.

  Fix: cascade now uses a separate `state.force_re_elaborate` set
  for the bypass. `state.stale_uris` is reserved for actual stale
  state, so the transition detector sees an honest False before
  this elaborate marks it True. Plus: the parse-failure branch now
  also sets `ast_changed = True` so the cascade actually fires when
  a library file breaks (without it, only the file the user typed
  in showed stale, the consumers stayed silent).

## [0.26.1] — 2026-05-02

Patch on top of 0.26.0. Three field-reported gaps in T3 + a leak
mitigation that turned out to be production-critical.

### Fixed (in `systemrdl-lsp` 0.18.1)

- **"Showing last good" stale indicator now appears on broken RDL
  again.** Editing a file into a parse error used to leave the
  viewer rendering the prior (non-stale) tree with no visible signal
  that the LSP had seen the breakage. Root cause: the parse-failure
  branch added the URI to `state.stale_uris` but never invalidated
  the cached spine envelope or pushed `rdl/elaboratedTreeChanged`,
  so the client kept its outdated render. Fix: on stale transition
  (`False → True`) we bump `cached.version`, drop
  `cached.serialized`, and notify. Same fix applied on the elaborate
  timeout path. Regression test pinned in `test_perf_t3.py`.
- **Pool worker recycling (T3-G).** The PoC's standalone memory
  spike test surfaced a real upstream leak in `systemrdl-compiler`
  — about 5 MB per elaborate of a 40-reg fixture, never released.
  Without intervention the worker process would slowly grow until
  it OOM-killed itself in a long editing session on a big design.
  Mitigation: after `pool_max_elaborates` (default 50) successful
  subprocess elaborates, the pool is torn down and respawned. RSS
  comes back to baseline; recycle itself is ~150 ms (worker spawn
  + import warm-up), barely visible during a typing pause.
- **`BrokenProcessPool` recovery (T3-E).** A subprocess that
  segfaults on a pathological RDL or gets killed by the OOM reaper
  used to poison every subsequent elaborate until the user manually
  restarted the LSP. The exception fires at two points (submit-time
  if the pool already noticed, await-time if the worker died
  in-flight) — both are now caught, the dead pool is torn down, a
  fresh one is spawned, and the elaborate is retried once. If the
  retry also fails the original exception surfaces normally.

### Tests

- 4 new T3 tests covering crash recovery, recycle threshold, the
  stale-bar regression, and the upstream memory leak (documents
  current behaviour at ~250 MB growth across 50 elaborates;
  ceiling at 500 MB so a doubling regression gets caught).
- Suite goes 129 → 133.

## [0.26.0] — 2026-05-02

T3 perf release. Closes the cross-URI blocking gap that was the only
remaining T2 limitation: editing a small `.rdl` file while a 25k-register
design is still elaborating used to make the small file wait. Now they
elaborate in parallel.

### Added (in `systemrdl-lsp` 0.18.0)

- **`ProcessPoolExecutor` for elaborate.** Each `.rdl` elaborate now
  runs in a dedicated subprocess instead of sharing a Python thread
  with the rest of the LSP. Two pre-warmed workers per LSP process
  by default. Wire format is `pickle` + `zlib` (level 1) — on
  `stress_25k_multi.rdl` the IPC payload comes in at ~5 MB compressed
  vs 174 MB raw; encode time is identical because compression is
  near-free on the redundant tree shape. PoC report and reproducible
  bench script in [`docs/perf-poc-processpool.md`][poc] and
  [`packages/systemrdl-lsp/scripts/poc_process_pool.py`][script].
- **New setting `systemrdl-pro.elaborateInProcess`** (default `false`).
  Escape hatch — set to `true` to fall back to the pre-T3 in-thread
  path. Useful if a future `systemrdl-compiler` upgrade breaks
  `RootNode` pickle compatibility, or for diagnosing pool-related
  issues. Restart the language server for the change to take effect.

### Performance

Measured on `examples/stress_25k_multi.rdl` (~25k regs, 52 includes,
deep Perl preprocessing):

| scenario | small-file wall | small-file slowdown vs alone |
| --- | ---: | ---: |
| small alone | 1.7 ms | 1× |
| small + big in threads (pre-T3) | 20–60 ms | 11–30× |
| small + big in processes (T3) | 4.4 ms | 2.5× |

Net: **4–13× responsiveness gain on the small file** while the big
file is mid-elaborate. The big file's wall-clock is unchanged
either way. Encoded IPC overhead (~5s on 25k) is amortized — a fresh
elaborate of a 25k design pays it once vs the editor staying
unresponsive every time the user touches a different file.

### Security note

The pickle wire format is safe in this context because the IPC stays
between an LSP parent process and a subprocess we spawn under the
same uid on the same machine. `concurrent.futures.ProcessPoolExecutor`
uses pickle internally regardless of what we ship through it. We do
not read pickle from disk (the disk cache stays JSON) or accept
pickle from any external source. See `docs/perf-poc-processpool.md`
for the full threat-model writeup.

[poc]: https://github.com/seimei-d/systemrdl-pro/blob/main/docs/perf-poc-processpool.md
[script]: https://github.com/seimei-d/systemrdl-pro/blob/main/packages/systemrdl-lsp/scripts/poc_process_pool.py

## [0.25.1] — 2026-05-02

Patch on top of 0.25.0 — fixes three field-reported gaps from the T2
manual test pass.

### Fixed (in `systemrdl-lsp` 0.17.1)

- **Editing `desc` / `name` properties now updates the viewer.** The
  AST fingerprint hashed only access semantics + reset + bit ranges,
  missing `name` (display label), `desc` (description text), `counter`,
  `encode`, and the addrmap/regfile-level `bridge` property — so
  edits to those silently no-op'd: the LSP saw "identical AST", didn't
  bump `cache.version`, and the Memory Map kept showing the old text.
  Fingerprint now mirrors every property `serialize.py` actually
  reads. Regression tests pinned per property.
- **No more banner flash on whitespace edits.** A new
  pre-elaborate canonicalize-skip pass strips comments and collapses
  non-string whitespace, then compares against the cached canonical
  form. If they match, `_full_pass_async` returns *before* sending
  `rdl/elaborationStarted`, so the "Re-elaborating in background"
  banner never appears for a typed space or a comment edit on
  `stress_25k_multi.rdl`. Only edits that actually change tokens
  (identifiers, addresses, strings, Perl sections) trigger the
  compiler now.
- **`elaborationTimeoutMs` default raised 60s → 120s** (cap raised
  from 5min to 10min). The 60s cap was timing out on
  `stress_25k_multi.rdl` (52 included files with deep Perl
  preprocessing). 120s clears the field-reported case with headroom.
- **Silent handler for `workspace/didChangeWatchedFiles`.** pygls 2.x
  was logging `[WARNING] Ignoring notification for unknown method` on
  every disk change VSCode reported. We don't react to disk-side
  changes (the include cascade covers it via the buffer-edit path),
  but the warning was noise in the trace channel.

### Known limitation

- **Cross-URI cooperative scheduling still GIL-bound.** Opening a
  small file while `stress_25k_multi.rdl` is mid-elaborate still
  serializes behind it because Python threads share the GIL on CPU-
  bound work. Field-confirmed: "Не сработало, пришлось ждать". Real
  fix needs `ProcessPoolExecutor` (next perf PR — needs PoC for
  `RootNode` pickle-viability).

## [0.25.0] — 2026-05-02

Server-driven release: extension binaries unchanged, ships against
`systemrdl-lsp >= 0.17.0`. The user-visible payoff is the **T2 Lean**
incremental-elaborate trio in the LSP backend.

### Changed (in `systemrdl-lsp` 0.17.0)

- **Cross-file invalidation now works.** Editing `types.rdl` (or any
  library file) automatically re-elaborates every open consumer
  (`stress_25k.rdl`, etc.). Previously you had to close-and-reopen the
  consumer tab to see new reset values, type renames, or field
  reshapes. Driven by a per-elaborate include reverse-dep map.
- **No-op edits no longer churn the viewer.** Whitespace,
  reformatting, comment changes, and dead-code identifier rewrites
  produce an identical AST — the LSP now SHA-256-fingerprints the
  elaborated tree and skips the cache version bump + the
  `rdl/elaboratedTreeChanged` push when the fingerprint matches.
  Memory Map stops flickering on cosmetic edits.
- **Per-URI elaboration mutex.** Replaces the TODO(T2) marker that
  warned about a benign race between `didOpen + didSave` for the
  same URI. Different URIs still elaborate concurrently.
- **`_full_pass_async` timing logs.** Behind `trace.server: messages`,
  every elaborate now logs `compile=<s> apply=<s> ast_changed=<bool>`
  so future investigation of cross-URI blocking has a baseline.

### Out of scope (deferred)

- **`ProcessPoolExecutor` for true cross-URI parallelism.** GIL
  contention still serializes CPU-bound elaborates across different
  files. This is the next perf PR — it requires confirming
  pickle-viability of `RootNode` and the spine envelope, separate
  scope.
- **Delta-push protocol for the viewer.** TODO-1 in ROADMAP. Spine is
  still re-fetched whole on version change.
- **Patching `systemrdl-compiler`** for incremental elaborate. Out of
  scope per project policy — the upstream library stays untouched.

### Install

```sh
pip install --upgrade systemrdl-lsp
```

The extension auto-detects the new behavior — no settings change
needed.

## [0.24.0] — 2026-05-02

### Added

- **Re-elaborate progress indicator.** When you edit a large `.rdl`
  file (~10s elaborate at 25k registers), the Memory Map now shows a
  "Re-elaborating in background" banner instead of leaving you guessing.
  The previous tree stays interactive throughout — the banner clears
  when the fresh tree arrives. Driven by new `rdl/elaborationStarted`
  / `rdl/elaborationFinished` LSP notifications.
- **Default `elaborationTimeoutMs` raised from 10s to 60s.** 25k-register
  designs need ~10s to elaborate cold, with no headroom under the old
  cap. The 60s default covers aggregated multi-subsystem designs out of
  the box; smaller IPs still finish in well under a second.

### Fixed

- **Decoder panel now updates after switching registers.** The lazy-tree
  splice was mutating the placeholder reg in place, so React's
  `useMemo([reg, decoderInput])` saw a referentially-equal `reg` and
  reused stale decoded values from the previous register. Splice now
  replaces the reg in its parent's `children` array, giving downstream
  memos a real ref change.
- **Soft handling of `expandNode` version-mismatch races.** When the LSP
  re-elaborates while a placeholder expand is in flight (common at 25k:
  edit → 10s elaborate → click in old tree), the server now returns a
  `{outdated: true}` sentinel instead of raising `JsonRpcException`,
  and the viewer transparently retries against the new `tree.version`.
  Eliminates the noisy `[ERROR] VersionMismatch` traceback.
- **Skip re-elaborate when buffer is byte-identical to last pass.** VSCode
  fires duplicate `didOpen` on workspace restore, and various editor
  extensions touch the buffer without producing a real diff. Previously
  every event triggered a full elaborate; on a 25k file this would pin
  the GIL and stall opens of small files. Now we short-circuit on a
  string equality check.
- **LSP custom notifications now actually fire.** Pre-existing
  `rdl/elaboratedTreeChanged` (and the new `rdl/elaboration{Started,
  Finished}`) were calling `server.send_notification(...)` — a method
  that doesn't exist on `pygls.lsp.server.LanguageServer` in pygls 2.x.
  The call raised `AttributeError`, swallowed by an outer `try/except`,
  so the notification silently never reached the client. Switched to
  the supported `server.protocol.notify(...)` API. Side effects beyond
  the new banner: edits to `.rdl` files now refresh the Memory Map
  automatically (renames, reset-value tweaks, etc.) without needing
  to switch tabs to force a refresh.
- **No more `Error: Webview is disposed` in the host log.** When a late
  LSP notification (expand result, tree update) arrives just after the
  user closed a Memory Map tab, `panel.webview.postMessage` returns a
  Thenable that rejects with that error. We now swallow it explicitly
  via `.then(undefined, () => {})` since it's a normal close-race.
- **Loading state during initial elaborate.** Opening a `.rdl` file
  used to flash the "No top-level addrmap found" pane for ~10s on a
  25k design while the LSP was still working on the first elaborate.
  The viewer now shows "Loading…" until the LSP reports a real version
  (`>= 1`); after that the addrmap-less pane only shows for files that
  truly contain no addrmap.
- **On-disk spine cache actually used on cold start.** The
  version-equality guard before reading from `~/.cache/systemrdl-pro/`
  was rejecting every disk hit because `version` is a per-process
  monotonic counter that resets to 1 on each LSP boot — the disk
  envelope's recorded counter was almost never equal. The cache key
  is content-addressed (sha256 of abs path + mtime + include paths +
  compiler version), so a hit *is* authoritative; we now rewrite the
  envelope's `version` field to the current process's counter on read
  instead of gating on equality. Window reload of a 25k file is now
  the documented "skip parse + elaborate + serialize" path.
- **`pendingExpansions` keyed by `version:nodeId`.** The viewer's
  in-flight expand tracking was using the raw `nodeId` string, so
  when a fresh elaboration produced a new tree with the same DFS
  shape (same nodeIds), an in-flight v1 request blocked the v2
  retry until the v1 resolved as `outdated`. Result: the spinner
  stayed up for an extra round-trip. Version-prefixed keys remove
  the cross-tree blocking.
- **Per-process nonce on `DiskCache` `.tmp` filenames.** Two LSP
  instances on the same workspace (second VSCode window) hashed the
  same key and both staged to `spine.json.tmp`, racing the final
  `os.replace`. The .tmp is now per-pid; the rename target is still
  the shared name so the cache stays content-addressed.
- **Orphan `.rdl` tmp files no longer leak on elaboration timeouts.**
  The `_drop_late_result` callback was using `except Exception`,
  swallowing `CancelledError` on shutdown without unlinking the late
  tmp. Long-lived LSP sessions hitting frequent 60s timeouts on
  large designs would slowly fill `/tmp`. Fixed with `BaseException`
  catch and a separate unlink try-block.

## [0.23.0] — 2026-05-01

### Added

- **Lazy memory-map viewer (LSP perf overhaul T1).** For aggregated
  multi-subsystem designs in the 10-25k+ register range, the viewer
  now receives a "spine" envelope (addrmaps + regfiles + reg shells
  with empty fields) and fetches per-register field detail on demand
  via a new `rdl/expandNode` RPC. Spine is 17-18x smaller and 4-5x
  faster to build than the legacy full tree. The LSP elaboratedTree
  handler is now async and runs serialization in a thread pool, so
  diagnostics / hover / completion no longer freeze while the viewer
  is loading.
- **On-disk spine cache** at `~/.cache/systemrdl-pro/<key>/spine.json`.
  Keyed by absolute path + mtime + include paths + compiler version.
  Window reload of an unchanged file skips parse + elaborate +
  serialize entirely.
- **Lazy capability negotiation.** Old extensions / non-VSCode LSP
  clients keep getting full trees; only clients that advertise
  `experimental.systemrdlLazyTree` see the new spine + expand flow.

### Internal

- Schema bumped to v0.2.0 (`Reg.loadState`, `Reg.nodeId`, envelope
  `lazy` flag — all optional / additive).
- See `feat/lsp-perf` branch for the 7-commit history.

## [0.22.18] — 2026-05-01

### Fixed

- **Demo GIF now renders on Open VSX listing.** v0.22.16 bundled the
  demo into the `.vsix` and referenced it with the relative path
  `media/demo.gif`, which VSCode Marketplace rewrites to a GitHub raw
  URL automatically — but Open VSX renders the README as-is and
  relative paths to bundled assets don't resolve there. Switched the
  README to point at the canonical `docs/demo.gif` via absolute GitHub
  raw URL — works on both registries, single source of truth for the
  demo asset, and `.vsix` size is back from 3.9 MB to 163 KB (the
  duplicate `media/demo.gif` was dropped).

## [0.22.17] — 2026-05-01

### Changed

- **Marketplace ID renamed.** Package `name` field went from
  `vscode-systemrdl-pro` to `systemrdl-pro`, so the new full ID is
  `seimei-d.systemrdl-pro` (was `seimei-d.vscode-systemrdl-pro`). The
  display name "SystemRDL Pro" and language id `systemrdl-pro` are
  unchanged. The old listing on Marketplace and Open VSX stays as a
  zombie pointing at v0.22.16; future updates ship under the new ID.
  - **CLI install command:** `code --install-extension seimei-d.systemrdl-pro`
  - **Old listing:** ignore — install the new one.

## [0.22.16] — 2026-05-01

### Added

- **Demo GIF in Marketplace listing.** Marketplace and Open VSX render
  `packages/vscode-systemrdl-pro/README.md`, which previously had no
  visuals. Bundled `demo.gif` into the extension under `media/` and
  embedded it in the README so the listing now shows the 30-second
  tour (live diagnostics, hover, F12 goto-def, viewer click-to-reveal,
  binary decode, theme follow). `.vsix` size grew from 163 KB to 3.9 MB.

## [0.22.15] — 2026-05-01

### Added

- **Extension icon** — replaces the 69-byte placeholder. 2×2 grid of
  access-mode bit-cells (RW green, RO blue-grey, W1C amber, WO purple)
  on a dark slate background. Renders crisply from 32 px sidebar tile
  to 256 px Marketplace tile. SVG source kept alongside the PNG in
  `media/` for future re-renders.
- **GitHub social-preview image** — 1280×640 card shown when the repo
  URL is shared in Slack, Discord, X, LinkedIn. Stored at
  `docs/social-preview.png`; uploaded via repo Settings → Social
  preview.
- **README icon header** — root README now leads with the new icon.

## [0.21.0] — 2026-05-01

### Added (viewer)

- **Memory map overview strip.** A new horizontal pane above the tree
  shows every direct child of the active addrmap as a clickable tile.
  Tiles flex-grow by `log²(size)` so multi-MB regfiles take more visual
  space than 4-byte registers but the smallest items never disappear.
  Reserved gaps render as dashed empty tiles between named children.
  - **Click on a regfile/addrmap tile** drills into it; a breadcrumb at
    the top tracks the stack and lets you climb back up.
  - **Click on a register tile** reveals it in the editor and selects the
    matching node in the tree below.
  - Hover any tile for full address + size + access summary tooltip.
  - Toggle button in the tabs row hides/shows the overview pane.
- Tiles are colour-accented by access mode (left-border stripe — RW
  green, RO blue, W1C amber, etc.) without overpowering the chrome
  background.

### Fixed

- **`textDocument/semanticTokens/full` failure resolved.** Diagnosed
  via the user-shared traceback as a pygls signature-introspection
  edge case: `from __future__ import annotations` + `get_type_hints`
  evaluating a return-type annotation imported only in a local scope
  returned NameError, which `has_ls_param_or_annotation`'s try/except
  swallowed, so pygls thought the handler didn't take an `ls` arg
  and called it with one positional instead of two — `TypeError` on
  every keystroke, visible as editor lag. Fix: import
  `SemanticTokens`, `SemanticTokensLegend`, and the method constant
  at module level.

## [0.20.1] — 2026-05-01

Hot-fixes for the four issues reported on 0.20.0:

### Fixed

- **Semantic tokens request failure caused editor lag.** The handler
  was throwing on some buffers, and VSCode retries failing
  `semanticTokens/full` on every keystroke — that's where the
  "the bigger the window the more it lags" came from. Switched
  registration to the simpler `SemanticTokensLegend` form (was
  `SemanticTokensRegistrationOptions`) and wrapped the handler in
  defensive try/except so a future bug returns empty tokens instead
  of looping forever.
- **Workspace pre-index defaulted to ON.** Multi-window setups had
  every VSCode window racing its own pre-index walk, pegging CPU.
  Default flipped to OFF; users who want workspace-wide search opt
  in via `systemrdl-pro.preindex.enabled`. When enabled, the walker
  is now serial (1 file at a time) with a 5 s startup delay so it
  doesn't compete with initial editor activity.

### Changed (viewer)

- **Bit-field grid: multi-line names + no duplicate bit indices.**
  Names like `TRANSMIT_BUFFER_FULL` were truncating to "f…" because
  cells were locked single-line. They now wrap (`word-break`,
  `overflow-wrap: anywhere`) and cells are taller. Bit ranges no
  longer appear inside cells — the header row above already shows
  every index, datasheet-style.
- **Scroll-to-top button redesigned.** The pulsing blue circle was
  too loud for a navigation aid. Replaced with a small chip-style
  button (28×28, panel background, accent border on hover) carrying
  an SVG chevron. Same position, much quieter.

## [0.20.0] — 2026-05-01

Seven LSP features in one release. Hardware register-map editing now has
the table stakes most language servers offer (rename, find references,
formatter, etc.).

### Added (LSP)

- **Document links** on `\` `include "..."` directives. Ctrl+click jumps
  to the included file. Resolves through the same search-path chain the
  compiler uses, including `$VAR` substitution and peakrdl.toml.
- **Find references** (`Shift+F12`). Identifier under cursor →
  every instantiation site, cross-file. Optional declaration in the result.
- **Rename refactoring** (`F2`). Renames a top-level type
  identifier across its declaration and every instantiation. Validates
  the new name as a SystemRDL identifier and refuses to shadow existing
  types.
- **Semantic tokens** (`textDocument/semanticTokens/full`).
  Distinguishes properties (`sw`, `hw`, `reset`) from access values
  (`rw`, `ro`, `woclr`) at the LSP level — TextMate alone can't tell
  them apart. Works on broken files (no elaboration dependency).
- **Code action: "Add `= 0` reset value"**. Lightbulb on field
  declarations missing an explicit reset. Inserts ` = 0` before the
  semicolon.
- **Document formatting** (`Shift+Alt+F`). Conservative: trims trailing
  whitespace, normalises tabs to spaces (respecting editor tabSize),
  ensures a single trailing newline. Idempotent. No opinionated
  alignment.
- **Workspace pre-index**. On first launch, walks every `.rdl` file in
  the workspace and pre-elaborates it in the background (4-way
  concurrent, capped at 200 files by default). `workspace/symbol`
  (`Ctrl+T`) now finds symbols across files the user hasn't yet
  opened.

### Added (settings)

- `systemrdl-pro.preindex.enabled` — toggle the pre-index walker.
- `systemrdl-pro.preindex.maxFiles` — cap on files visited (default 200).

## [0.19.0] — 2026-05-01

Three more cleanups: types are now schema-driven, cross-file diagnostics
land in the right editor, and include-paths UX is no longer opaque.

### Added

- **New command `SystemRDL: Show effective include paths`.** Quick-pick
  of every directory the LSP will search for `\` `include`d files,
  labeled by source (`setting` / `peakrdl.toml` / `sibling`). Press Enter
  on a row to reveal it in the OS file manager.
- **Cross-file diagnostics.** A syntax error inside an `\` `include`d
  file is now reported against that file's URI, not silently dropped.
  Fixing the error clears the squiggle (clear-on-resolve cycle).

### Internal

- **Codegen for elaborated-tree types** (Decision 9A). `bun run codegen`
  walks `schemas/elaborated-tree.json` and emits Python TypedDicts +
  TypeScript types. The hand-written shadow types in `extension.ts`,
  `viewer-core/types.ts`, etc. now re-export the generated copies.
  Drift detection: a CI test asserts the generated file matches the
  committed one.
- **Include-path resolution unified.** `_resolve_search_paths(uri)`
  returns one deduped, source-labeled list. Setting > peakrdl.toml >
  sibling-dir on collision (first-source-wins).

## [0.18.0] — 2026-05-01

Backlog cleanup: four TODOs closed in one batch.

### Added

- **`systemrdl-pro.perlSafeOpcodes` setting.** Override the Perl `Safe`
  opcode set (defaults are conservative — bans `print` and most I/O).
  Add `:base_io` to allow `print`-based code generation in `<% … %>`.
- **Perl pre-flight check.** When a buffer contains `<%` markers but
  `perl` is missing from PATH, surface a one-time warning notification
  instead of letting the compiler's fatal diagnostic fire on every save.
- **Push-driven Memory Map updates.** LSP now sends an
  `rdl/elaboratedTreeChanged` notification on every successful
  elaboration; the extension refreshes proactively without waiting for
  `didSaveTextDocument`. Open the panel, type — tree updates live.

### Changed

- **Version-gated tree fetches.** `rdl/elaboratedTree` accepts
  `sinceVersion`. If the LSP's cached version matches, the response is
  a constant-size `{unchanged: true, version}` envelope — skip
  serialization + transport on no-op refreshes (e.g. focus changes,
  panel re-mount). Same-version repeat fetches reuse a cached
  serialized dict on the LSP side.
- **Polished caret-toggle button.** Tree expand/collapse glyph was a
  text `<span>` with hover-only background, reading as a glyph rather
  than an affordance. Replaced with a real `<button>` (proper a11y),
  22×22 click target, persistent subtle background, SVG chevron at
  10×10 / 1.6 px stroke (sharper at HiDPI than `▼/▶`).

### Internal

- **`server.py` refactored** from a 1900-line monolith into seven
  themed modules (`compile`, `diagnostics`, `hover`, `completion`,
  `definition`, `serialize`, `outline`) plus a ~470-line LSP wiring
  shim. All 44 tests pass unchanged — the existing test surface
  imports through `systemrdl_lsp.server` re-exports.

### Docs

- README: new **Perl preprocessor** section documents the `perl` PATH
  requirement, the `<%=$i%>` no-leading-whitespace gotcha, and the new
  opcode-override setting.

## [0.17.0] — 2026-04-30

### Changed

- **Multi-tab Memory Map.** One panel per `.rdl` file (markdown-preview-style)
  instead of a single shared panel. Open `chip_a.rdl`, run Show Memory Map,
  switch to `chip_b.rdl`, run again — both tabs now coexist. Re-running on a
  file that already has a panel just brings it forward.
- **Status bar follows the active editor.** When you switch between two
  `.rdl` files with open panels, the reg/error count tracks the focused file.
- **Inlay hints moved to end-of-line** with `→ 0xADDR` glyph. Earlier
  position broke names mid-word (`CTR (0x...)L`); end-of-line never collides.

### Removed

- **`📋 Open in Memory Map` CodeLens** — redundant with `Ctrl+Shift+V`
  shortcut and the `📊 N regs · 0x..0x` summary lens stays.

### Fixed

- **Bit-field grid redesign.** Fields now span their full width as one cell
  (was: one cell per bit, name clipped to 1 letter). Datasheet-style row
  with bit indices on top, colored field cells underneath, gaps render as
  reserved cells.

## [0.16.0] — 2026-04-30

Major UX upgrade across editor and viewer.

### Added (editor)

- **Snippets** — `addrmap`, `regfile`, `reg`, `regtyped`, `field`, `fieldw1c`,
  `fieldcounter`, `include`, `perlloop` expansions with tab-stops.
- **Folding ranges** — collapsible `{...}` blocks via dedicated LSP provider
  (more reliable than indent-based folding on irregular formatting).
- **Inlay hints** — resolved absolute address shown ghost-grey after each
  register name (e.g. `} CTRL @ 0x0   (0x0000_0010)` for nested instances).
- **CodeLens** above every `addrmap` declaration — `📊 N regs · 0x0..0xN`
  summary + `📋 Open in Memory Map` clickable link.
- **Workspace symbols** (`Ctrl+T`) — search registers across every `.rdl`
  file the LSP has touched.
- **Address conflict warnings** — overlapping reg ranges anywhere in the
  elaborated tree now emit a warning diagnostic (defence-in-depth on top of
  systemrdl-compiler's direct sibling check).
- **Onboarding walkthrough** — first-run "Get Started" page with 4 cards.
- **Status bar diagnostics counter** — current file's `$(error) N`
  / `$(warning) M` count appended next to the reg/root summary; updates
  on every diagnostic change.

### Added (viewer)

- **Bit-field grid** — visual `[width-1..0]` cell strip in the detail pane
  with colour-coded RW / RO / W1C / etc. fields and field names overlaid
  inside their bit ranges.

## [0.15.1] — 2026-04-30

### Added

- Keybinding **Ctrl+Shift+V** (Cmd+Shift+V on macOS) opens the Memory Map
  panel when a `.rdl` file is focused in the editor. Mirrors the markdown
  preview shortcut so the muscle memory carries over.

## [0.15.0] — 2026-04-30

First public Marketplace release. Walking skeleton ➝ feature-complete viewer.

### LSP

- `textDocument/diagnostics` — live, 300 ms debounce, 10 s elaborate timeout
  with last-good fallback
- `textDocument/hover` — resolved address/width/access on instances; markdown
  docs on every keyword / property / access value / user-defined type
- `textDocument/documentSymbol` — outline of `addrmap → regfile → reg → field`
- `textDocument/definition` — goto-def on type identifiers (cross-file)
- `textDocument/completion` — context-aware narrowing after `sw =` /
  `onwrite =` / `onread =`; user-defined types surface their `name` + `desc`
- `incl_search_paths` — explicit setting + auto-discovery from `peakrdl.toml`
- `systemrdl-pro.includeVars` — `$VAR` / `${VAR}` substitution in `include`
  paths (lightweight fallback for projects without `perl`; full Perl
  preprocessor works upstream when `perl` is on PATH)
- Auto-restart up to 3× in 60 s on LSP crash; manual `Restart LSP` command
- Multi-root elaboration — one tab per top-level `addrmap` definition

### Viewer

- Tree + detail-pane layout; tabs for multi-root files
- Collapsible containers (▼/▶) with caret-only toggle (body click reveals
  in editor)
- Cmd-F filter with scope selector (Name / Address / Field / All)
- Click register → editor scroll + 200 ms flash; cursor in editor → tree
  auto-selects matching node
- Right-click context menu: Copy Name / Address / Type / Reveal in Editor
- Stale-bar when current parse fails; viewer keeps last good tree
- Auto dark / light theme tokens via `prefers-color-scheme`
- Pulsing scroll-to-top button on long trees
- WAI-ARIA tree roles + tabindex for screen-reader navigation

### Architecture

- Renderer extracted from inline JS into shared
  [`@systemrdl-pro/viewer-core`](https://github.com/seimei-d/systemrdl-pro/tree/main/packages/rdl-viewer-core)
  React bundle, consumed by both the VSCode webview and the standalone
  `rdl-viewer` CLI

### Removed

- Arrow-key navigation in the tree — too easily disrupted by VSCode's editor
  focus model. ARIA roles + Tab-into still work for screen readers.
