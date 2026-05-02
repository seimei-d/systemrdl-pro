# PoC report — `ProcessPoolExecutor` for cross-URI parallel elaborate

**Branch**: `perf/process-pool-poc`
**Driver**: `packages/systemrdl-lsp/scripts/poc_process_pool.py`
**Date**: 2026-05-02
**Status**: PoC complete — investigation only, no LSP wiring.

## Why

T2 ([PR #2][pr-t2]) closed three of four invalidation/concurrency gaps but left
one explicitly deferred: **a small file's elaborate gets blocked behind a big
file's** because all elaborate work runs on the asyncio default
`ThreadPoolExecutor` and the GIL serializes CPU-bound Python.

Field-confirmed during T2 manual verification: opening `alias_demo.rdl` while
`stress_25k_multi.rdl` was elaborating made the user wait. Same scenario was
the original case for adding the buffer-equality short-circuit in T1, which
helped re-opens but not first-opens.

The hypothesized fix was `ProcessPoolExecutor`. The big open question was
whether `RootNode` (the elaborated tree the LSP caches and the spine
serializer walks) survives crossing a process boundary — it holds references
to `RDLEnvironment`, lazy `SourceRef` file handles, and various compiler
internals.

[pr-t2]: https://github.com/seimei-d/systemrdl-pro/pull/2

## Findings

### Surprise: `RootNode` IS picklable

Predicted "almost certainly not". Reality: `pickle.dumps(root)` succeeds
and `pickle.loads` round-trips the tree intact. Measured:

| Fixture | Elaborate | Pickle | Unpickle | Payload size |
| --- | ---: | ---: | ---: | ---: |
| `examples/sample.rdl` (3 roots) | <0.1s | <0.01s | <0.01s | 53 KB |
| `examples/stress_25k_multi.rdl` (1 root, ~25k regs, 52 includes) | 34.5s | 2.7s | 1.9s | **174 MB** |

The 174 MB payload size kills "just send the whole tree" as the contract
for big designs — IPC bandwidth would dominate the elaborate cost on a
saturated machine, and the parent process's memory footprint would
double on every cross-process round-trip.

### JSON-only contract is small and total

A subprocess can return a 4-tuple of plain JSON:

```python
{
    "messages": [...],            # CompilerMessage as dict
    "spine_envelopes": [...],     # _serialize_spine output, one per root
    "full_envelopes": [...],      # _serialize_root output, one per root
    "consumed_files": [...],      # absolute paths as strings
    "ast_fingerprint": "...",     # SHA-256 hex
    "tmp_path": "...",            # parent unlinks
}
```

Round-trip on `sample.rdl`: 97 ms for spawn + serialize + return. Payload
size 791 bytes for that fixture, low MB range for `stress_25k_multi`
(spine is intentionally lazy; full envelope grows but the LSP only ever
serializes spine for lazy-mode clients, which is everyone since T1).

### Cross-URI parallelism: 4.5x small-file responsiveness gain

Three timed scenarios on `alias_demo.rdl` (small, ~2 ms baseline) +
`stress_25k_multi.rdl` (big, ~34s):

| Scenario | small wall | big wall | small slowdown vs alone |
| --- | ---: | ---: | ---: |
| A. Small alone (in-process) | 1.7 ms | — | 1.0× |
| B. Both in `ThreadPoolExecutor` (current LSP) | 20 ms | 34.8s | **11.3×** |
| C. Both in `ProcessPoolExecutor` (proposed) | 4.4 ms | 34.4s | 2.5× |

Net: **4.5× responsiveness gain on the small file** when the big file is
running concurrently. The big file's wall-clock is unchanged either way
(still GIL-bound inside its own process).

The remaining 2.5× slowdown vs alone is the IPC + spawn cost; could be
shaved further with worker pre-warm, but that's optimization on top of
"works."

## Two viable architectures

### Option 1 — Pickle full `RootNode` from subprocess

Subprocess elaborates and pickles the `RootNode` tree back. Parent caches
it as today. All hover / symbol / completion features keep working
identically (they walk the live `RootNode`).

- ✅ Zero behavioural change to feature handlers.
- ✅ Smallest diff against the current architecture.
- ❌ 174 MB payload across the pipe per 25k elaborate. ~5s pickle/unpickle
  overhead added to a 34s elaborate (15% overhead).
- ❌ Memory pressure: parent and subprocess each hold the full tree at the
  moment of return.
- ❌ Doesn't scale gracefully — large SoC tops would blow up the wire size.

### Option 2 — JSON-only contract, hover/symbol read from envelope

Subprocess returns only the JSON payload. Parent stores the envelope, no
`RootNode` lives in the parent process at all. Hover / outline / symbol
features get rewritten to read the envelope dict instead of calling
`node.get_property("desc")` / `node.children(...)` etc.

- ✅ Small payload (low MB even for 25k).
- ✅ Subprocess can be killed/replaced at any time without losing parent
  state — natural cancellation story for "user kept typing while
  elaborate was running."
- ✅ Cleaner separation: parent never touches `systemrdl-compiler`.
- ❌ Touches every feature module that currently reads `cached.roots`:
  `hover.py`, `outline.py`, `definition.py`, `completion.py`,
  `code_actions.py`, `links.py`. Each needs an envelope-walking
  equivalent.
- ❌ Risk that the envelope misses a property some hover path needs;
  surfacing it adds a serialize step.

### Recommendation

**Option 1 first, Option 2 if Option 1 doesn't hold up.** Option 1 is one
PR (~3-5 sessions) instead of a multi-PR refactor across every feature
module. The 15% IPC overhead on 25k is a real cost but it buys the
cross-URI win we wanted; smaller designs (where most users live) pay
microseconds.

If Option 1 ships and a real user complains about IPC time, Option 2 is
the next move and the PoC contract from this report (`_serialize_in_subprocess`)
already gives us the envelope-only interface to evolve into.

## What the next PR should do (Option 1 sketch)

1. Replace `loop.run_in_executor(None, _compile_text, ...)` in
   `_full_pass_async` with a `ProcessPoolExecutor` shared via `ServerState`.
   Pool size = `min(2, cpu_count)` to keep RAM bounded.
2. Submit `_compile_text(uri, text, ...)` to the pool; result is a
   pickled tuple returned across the boundary.
3. Wire cancellation: when a new elaborate for the same URI arrives, kill
   the in-flight subprocess via `Future.cancel()` (if pending) or
   `process.terminate()` (if running). Replace the pending tmp file
   cleanup callback with one that handles `CancelledError` from the new
   process-level cancel.
4. Pre-warm one worker on `INITIALIZED` so the first elaborate doesn't
   pay spawn cost.
5. Keep the per-URI lock in the *parent* — subprocess is just the work
   target, the lock is still about ordering parent-side state mutations
   (cache.put, notification, cascade trigger).
6. Tests: add a `test_perf_t3.py` with the timing-style probes from this
   PoC, plus a unit test that the subprocess call returns a
   `RootNode`-typed value (proves pickle still works after future
   `systemrdl-compiler` upgrades).

## Estimated complexity

- Initial wiring + pool lifecycle: 1-2 sessions.
- Cancellation + tmp-file lifecycle on `terminate()`: 1 session (this is
  the gnarly part — `_compile_text` writes a tmp file before forking the
  compiler; killing the subprocess can leave the tmp behind on disk).
- Tests + manual verification on `stress_25k_multi`: 1 session.
- Documentation + CHANGELOG + release: <1 session.

**Total: 3-5 sessions.** Risk is concentrated in step 3 (cancellation).
Worst-case fallback: ship without aggressive cancellation, accept that a
user typing fast will have elaborate runs queue up until they pause —
strictly better than today since they're at least running in parallel.

## What this PoC does NOT cover

- **Memory growth across many elaborates.** ProcessPoolExecutor reuses
  workers; need to confirm `RDLCompiler` doesn't leak via the import-
  cached Perl interpreter or similar. Spike test: elaborate 100 times
  in the same worker, watch RSS.
- **Worker crash recovery.** A pathological RDL that segfaults the
  subprocess should not crash the LSP. `ProcessPoolExecutor` returns
  `BrokenProcessPool` in that case; need to handle and respawn.
- **Windows behaviour.** PoC tested on Linux (WSL2). `multiprocessing`
  on Windows uses `spawn` by default which has different import semantics;
  the script forces `spawn` everywhere to match.
- **Pre-existing tests.** No production code changes in this PoC; the
  T2 test suite still passes against `main`.
