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

### Surprise 1: `RootNode` IS picklable

Predicted "almost certainly not". Reality: `pickle.dumps(root)` succeeds
and `pickle.loads` round-trips the tree intact. Measured:

| Fixture | Elaborate | Pickle | Unpickle | Raw size |
| --- | ---: | ---: | ---: | ---: |
| `examples/sample.rdl` (3 roots) | <0.1s | <0.01s | <0.01s | 53 KB |
| `examples/stress_25k_multi.rdl` (1 root, ~25k regs, 52 includes) | 34.5s | 2.7s | 1.9s | **174 MB** |

### Surprise 2: pickle is wildly compressible — the payload problem evaporates

The 174 MB raw pickle was the original concern that pushed me toward
"JSON-only" as the alternative. Then I tried compression on a hunch.
On the same `stress_25k_multi.rdl` fixture:

| Format | Size | Encode | Decode | vs raw |
| --- | ---: | ---: | ---: | ---: |
| `pickle` raw (proto 5) | 174.3 MB | 2.71s | 1.96s | 1× |
| `pickle + zlib` (level 1, fastest) | 5.4 MB | 2.60s | 2.11s | **32×** |
| `pickle + zlib` (level 6, default) | 2.1 MB | 2.85s | 2.09s | **83×** |
| `pickle + lz4` (default) | 4.2 MB | 2.42s | 2.00s | **41×** |
| `pickle + zstd` (level 3) | 0.8 MB | 2.44s | 1.86s | **218×** |
| `pickle + zstd` (level 9) | 0.7 MB | 2.56s | 2.34s | 249× |

The pickle format has massive redundancy on a tree of similar nodes —
SystemRDL fixtures typify this (50 banks × 500 regs × 30 fields, mostly
identical metadata). General-purpose compressors crush it without
breaking a sweat, and the encode time barely moves because compression
is dominated by `pickle.dumps` itself.

**Net consequence**: the original payload-size argument for going
JSON-only is dead. `pickle + zlib` (in the stdlib) gives us the same
wire size (~5 MB on 25k) as a hypothetical JSON-only contract, with
the entire `RootNode` arriving on the parent side intact. `zstd`
shrinks it another 7× if we accept an optional dep.

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

## Architecture options after the compression finding

### Option 1 — Pickle full `RootNode` + zlib (recommended)

Subprocess elaborates, pickles the `RootNode` to a `zlib`-compressed
buffer, ships back. Parent decompresses, unpickles, caches as today.
All feature handlers keep working identically — they walk the live
`RootNode` tree they always have.

- ✅ Zero behavioural change to `hover.py`, `outline.py`, `definition.py`,
  `completion.py`, `code_actions.py`, `links.py`.
- ✅ `zlib` is in the stdlib — no new runtime dependency.
- ✅ Wire size on `stress_25k_multi`: ~5 MB compressed (was 174 MB raw).
- ✅ Encode/decode time within ~5% of raw pickle — compression is
  near-free vs the underlying `pickle.dumps` cost.
- ✅ Subprocess crashes don't poison parent state (parent only ever sees
  the pickled bytes; bad subprocess → re-spawn, retry).
- ❌ ~5s pickle+compress on 25k still adds ~15% to a 34s elaborate. For
  smaller designs (where most users live) this is microseconds.
- ❌ Per-elaborate memory spike: subprocess holds tree, then pickle buffer,
  then compressed buffer; parent decompresses then unpickles. Briefly
  3-4× tree size in RAM during the handoff.

### Option 1+ — same as Option 1 but `zstd` instead of `zlib`

Optional runtime dependency. Wire size drops another 7× (5 MB → 0.8 MB
on 25k). Same encode/decode time. Worth it if a user reports IPC
showing up in profiles, but `zlib` already crushes the original concern
so this is a "nice to have" tier.

### Option 2 — JSON-only contract — DROPPED

Originally proposed because the JSON spine (~5 MB on 25k) seemed much
smaller than the assumed "raw pickle" cost of 174 MB. With compression,
pickle is the same size (~5 MB) and **doesn't require rewriting every
feature module to read envelope dicts instead of calling
`node.get_property(...)`**. The complexity-to-payoff ratio inverted —
JSON-only is dominated by Option 1+zlib on every axis.

If Option 1 ever falls apart in the field (e.g. memory pressure during
the pickle handoff bites users with smaller machines), JSON-only is
still on the shelf — the PoC's `_serialize_in_subprocess` function
shows the contract works.

### Recommendation

**Ship Option 1 with `zlib` (level 1).** Single PR, no new runtime deps,
no feature rewrites, ~5 MB IPC on the worst fixture we have. Add a
config knob (`systemrdl-pro.elaborateInProcess`) defaulting to `false`
(use subprocess) but lettable to `true` to fall back if a user hits a
pickle-failure regression after a future `systemrdl-compiler` upgrade.

## What the next PR should do (Option 1 sketch)

1. Replace `loop.run_in_executor(None, _compile_text, ...)` in
   `_full_pass_async` with a `ProcessPoolExecutor` shared via
   `ServerState`. Pool size = `min(2, cpu_count)` to keep RAM bounded.
   `ProcessPoolExecutor` already pickles arguments + return value, so
   nothing extra is needed — but for the 25k case we want `zlib` on
   top of that. Two ways:
   a. Wrap the worker function: `def _worker_compile(...)` returns
      `zlib.compress(pickle.dumps(_compile_text(...)))`; parent calls
      `pickle.loads(zlib.decompress(...))`. Avoids `ProcessPoolExecutor`'s
      built-in pickle entirely.
   b. Custom `multiprocessing.connection` reducer registered via
      `dispatch_table` to compress everything. Cleaner but more code.
   Recommend (a) — single function, easy to reason about.
2. Wire cancellation: when a new elaborate for the same URI arrives,
   kill the in-flight subprocess via `Future.cancel()` (if pending) or
   `process.terminate()` (if running). Replace the pending tmp file
   cleanup callback with one that handles `CancelledError` from the
   new process-level cancel.
3. Pre-warm one worker on `INITIALIZED` so the first elaborate doesn't
   pay spawn cost.
4. Keep the per-URI lock in the *parent* — subprocess is just the
   work target, the lock is still about ordering parent-side state
   mutations (`cache.put`, notification, cascade trigger).
5. Pickle-version compatibility: include `pickle.HIGHEST_PROTOCOL` and
   the `systemrdl_compiler` version in a header on every IPC payload.
   On version mismatch, parent falls back to in-process elaborate
   (the `elaborateInProcess: true` config knob).
6. Tests: add `test_perf_t3.py` with timing-style probes from this PoC,
   a unit test that the subprocess call returns a `RootNode`-typed
   value (pins pickle compatibility against `systemrdl-compiler`
   upgrades), and a memory-pressure spike test that runs 100
   elaborates in the same pool and asserts RSS doesn't grow without
   bound.

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

## Security note on pickle

The Python docs warn that pickle is unsafe for untrusted data because a
crafted pickle stream can execute arbitrary code at deserialize time.
**That warning does not apply to this design.** Reasoning:

- The pickle stream flows between an LSP parent process we wrote and a
  subprocess we spawned, both running our own code under the same uid
  on the same machine.
- The transport is an OS pipe / `multiprocessing.Connection` —
  attacker with write access to that pipe already has process-level
  access to the LSP, at which point pickle is irrelevant.
- We do not read pickle from disk; the disk cache (`cache.py`) stores
  JSON envelopes. We do not accept pickle over the network.
- `concurrent.futures.ProcessPoolExecutor` uses pickle internally to
  ship arguments and return values across the pipe regardless of what
  data you give it. Avoiding pickle would mean abandoning the standard
  multiprocessing primitives.

If a future requirement does introduce an untrusted source — e.g. a
shared-cache feature that serializes elaborated trees to a network
share — switch that path to a safer format (the JSON contract from
this PoC, or a schema-driven binary like Cap'n Proto) and gate the
unpickler with `pickle.Unpickler.find_class` overrides as a
defense-in-depth measure. None of that applies to local same-uid
subprocess IPC.

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
