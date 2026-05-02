"""ProcessPool PoC for cross-URI parallel elaboration.

Three steps, run in order:

  1. ``probe_pickle_rootnode()`` — does ``RootNode`` survive pickling?
     Almost certainly NO. Capture the exact failure mode so the contract
     decision in step 2 is grounded.
  2. ``probe_json_contract()`` — can a subprocess return only the JSON
     pieces the LSP needs (messages, spine envelope, consumed files)?
  3. ``measure_parallelism()`` — does spawning subprocesses actually let
     a small file's elaborate finish while a 25k file is still running?
     Three scenarios timed: small alone, small+big in threads (current
     GIL behaviour), small+big in processes.

Run from the repo root:

    uv run python packages/systemrdl-lsp/scripts/poc_process_pool.py
"""

from __future__ import annotations

import json
import multiprocessing as mp
import pathlib
import pickle
import sys
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor


REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
EXAMPLES = REPO_ROOT / "examples"

# Make the package importable when run via `uv run python ...`
sys.path.insert(0, str(REPO_ROOT / "packages" / "systemrdl-lsp" / "src"))


def _section(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


# ---------------------------------------------------------------------------
# Step 1 — try direct pickle
# ---------------------------------------------------------------------------


def probe_pickle_rootnode() -> dict:
    """Attempt to pickle a RootNode produced by elaborating a simple file."""
    _section("STEP 1 — direct pickle of RootNode")

    from systemrdl_lsp.compile import _compile_text

    rdl_path = EXAMPLES / "sample.rdl"
    text = rdl_path.read_text()
    _msgs, roots, tmp_path, _consumed = _compile_text(rdl_path.as_uri(), text)
    print(f"elaborated {len(roots)} roots from {rdl_path.name}")
    if not roots:
        return {"ok": False, "reason": "no roots elaborated"}

    root = roots[0]
    print(f"root type: {type(root).__name__}")

    try:
        blob = pickle.dumps(root)
        # Round-trip
        restored = pickle.loads(blob)
        print(f"OK — pickled to {len(blob)} bytes; restored type {type(restored).__name__}")
        return {"ok": True, "size": len(blob)}
    except Exception as exc:
        print(f"FAILED: {type(exc).__name__}: {exc}")
        traceback.print_exc(limit=4)
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
    finally:
        tmp_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Step 2 — JSON-only contract
# ---------------------------------------------------------------------------


def _serialize_in_subprocess(uri: str, text: str) -> dict:
    """Run inside the subprocess: elaborate + serialize spine + harvest
    consumed files. Return ONLY plain Python (JSON-serializable) data
    so the parent can receive it without holding any compiler refs."""
    sys.path.insert(0, str(REPO_ROOT / "packages" / "systemrdl-lsp" / "src"))
    from systemrdl_lsp.compile import (
        _canonicalize_for_skip,
        _compile_text,
        _fingerprint_roots,
    )
    from systemrdl_lsp.serialize import _serialize_root, _serialize_spine

    msgs, roots, tmp_path, consumed = _compile_text(uri, text)
    spine_envelopes = []
    full_envelopes = []
    for r in roots:
        try:
            full_envelopes.append(_serialize_root(r, version=1))
        except Exception as exc:
            full_envelopes.append({"error": f"{type(exc).__name__}: {exc}"})
        try:
            spine_envelopes.append(_serialize_spine(r, version=1))
        except Exception as exc:
            spine_envelopes.append({"error": f"{type(exc).__name__}: {exc}"})
    fp = _fingerprint_roots(roots)
    canonical = _canonicalize_for_skip(text)

    payload = {
        "messages": [
            {
                "severity": int(m.severity),
                "text": m.text,
                "file": str(m.file_path) if m.file_path else None,
                "line": m.line_1b,
                "col_start": m.col_start_1b,
                "col_end": m.col_end_1b,
            }
            for m in msgs
        ],
        "spine_envelopes": spine_envelopes,
        "full_envelopes": full_envelopes,
        "consumed_files": [str(p) for p in consumed],
        "ast_fingerprint": fp,
        "text_canonical_len": len(canonical),
        "tmp_path": str(tmp_path),  # parent unlinks after use
    }
    # Test that the entire payload survives JSON round-trip — proves
    # nothing snuck in that wouldn't survive process boundary.
    json.dumps(payload)
    return payload


def probe_json_contract() -> dict:
    """Run elaborate in a subprocess via ProcessPoolExecutor; verify
    the JSON-only payload arrives intact."""
    _section("STEP 2 — JSON-only subprocess contract")

    rdl_path = EXAMPLES / "sample.rdl"
    text = rdl_path.read_text()

    t0 = time.monotonic()
    with ProcessPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(_serialize_in_subprocess, rdl_path.as_uri(), text)
        payload = fut.result(timeout=60)
    elapsed = time.monotonic() - t0

    print(f"subprocess round-trip OK in {elapsed:.3f}s")
    print(f"  messages: {len(payload['messages'])}")
    print(f"  spine envelopes: {len(payload['spine_envelopes'])}")
    print(f"  full envelopes: {len(payload['full_envelopes'])}")
    print(f"  consumed files: {len(payload['consumed_files'])}")
    print(f"  fingerprint: {payload['ast_fingerprint'][:16]}...")
    print(f"  canonical text length: {payload['text_canonical_len']}")
    payload_json_size = len(json.dumps(payload))
    print(f"  payload JSON size: {payload_json_size:,} bytes")

    # Cleanup the temp the subprocess left behind.
    pathlib.Path(payload["tmp_path"]).unlink(missing_ok=True)
    return {"ok": True, "elapsed": elapsed, "json_size": payload_json_size}


# ---------------------------------------------------------------------------
# Step 3 — measure cross-URI parallelism
# ---------------------------------------------------------------------------


SMALL_FILE = EXAMPLES / "alias_demo.rdl"
BIG_FILE = EXAMPLES / "stress_25k_multi.rdl"


def _elaborate_only(uri: str, text: str) -> tuple[float, int]:
    """Return (wall-clock seconds, root count) for an elaborate."""
    sys.path.insert(0, str(REPO_ROOT / "packages" / "systemrdl-lsp" / "src"))
    from systemrdl_lsp.compile import _compile_text

    t0 = time.monotonic()
    _msgs, roots, tmp_path, _consumed = _compile_text(uri, text)
    elapsed = time.monotonic() - t0
    tmp_path.unlink(missing_ok=True)
    return elapsed, len(roots)


def measure_parallelism() -> dict:
    """Three scenarios, each timed:

    A. small alone (baseline cost of the small file by itself)
    B. small + big in threads (current LSP behaviour — GIL-bound)
    C. small + big in processes (target architecture)

    What we want: in scenario C the small file should finish in roughly
    its scenario-A time, NOT close to the big file's elapsed. Scenario
    B will show the small file effectively waiting for the big one.
    """
    _section("STEP 3 — small+big parallelism (threads vs processes)")

    if not SMALL_FILE.exists() or not BIG_FILE.exists():
        print(f"missing fixture(s): small={SMALL_FILE.exists()} big={BIG_FILE.exists()}")
        return {"ok": False, "reason": "fixtures missing"}

    small_text = SMALL_FILE.read_text()
    big_text = BIG_FILE.read_text()
    small_uri = SMALL_FILE.as_uri()
    big_uri = BIG_FILE.as_uri()

    # A — small alone
    print()
    print("[A] small alone (in-process baseline)")
    small_alone, small_roots = _elaborate_only(small_uri, small_text)
    print(f"    {SMALL_FILE.name}: {small_alone:.2f}s, roots={small_roots}")

    # B — small + big in THREADS (current behaviour)
    print()
    print("[B] small + big in threads (current LSP path, GIL-bound)")
    with ThreadPoolExecutor(max_workers=2) as pool:
        t0 = time.monotonic()
        fut_big = pool.submit(_elaborate_only, big_uri, big_text)
        # tiny stagger so big definitely starts first
        time.sleep(0.05)
        fut_small = pool.submit(_elaborate_only, small_uri, small_text)
        small_b, _ = fut_small.result(timeout=300)
        big_b, _ = fut_big.result(timeout=300)
        wall_b = time.monotonic() - t0
    print(f"    big   {BIG_FILE.name}: {big_b:.2f}s")
    print(f"    small {SMALL_FILE.name}: {small_b:.2f}s")
    print(f"    wall: {wall_b:.2f}s")
    print(f"    small slowdown vs A: {small_b / max(small_alone, 0.001):.1f}x")

    # C — small + big in PROCESSES (target)
    print()
    print("[C] small + big in processes (ProcessPoolExecutor)")
    with ProcessPoolExecutor(max_workers=2) as pool:
        t0 = time.monotonic()
        fut_big = pool.submit(_elaborate_only, big_uri, big_text)
        time.sleep(0.05)
        fut_small = pool.submit(_elaborate_only, small_uri, small_text)
        small_c, _ = fut_small.result(timeout=300)
        big_c, _ = fut_big.result(timeout=300)
        wall_c = time.monotonic() - t0
    print(f"    big   {BIG_FILE.name}: {big_c:.2f}s")
    print(f"    small {SMALL_FILE.name}: {small_c:.2f}s")
    print(f"    wall: {wall_c:.2f}s")
    print(f"    small slowdown vs A: {small_c / max(small_alone, 0.001):.1f}x")

    print()
    print("VERDICT:")
    win = small_b / max(small_c, 0.001)
    print(f"  small-file responsiveness gain (B → C): {win:.1f}x")
    if small_c < small_b * 0.5:
        print("  ProcessPool DOES help cross-URI responsiveness")
    else:
        print("  ProcessPool does NOT meaningfully help — investigate")

    return {
        "ok": True,
        "small_alone": small_alone,
        "threads": {"small": small_b, "big": big_b, "wall": wall_b},
        "processes": {"small": small_c, "big": big_c, "wall": wall_c},
    }


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def main() -> None:
    mp.set_start_method("spawn", force=True)
    results = {}
    results["step1"] = probe_pickle_rootnode()
    results["step2"] = probe_json_contract()
    results["step3"] = measure_parallelism()

    _section("SUMMARY")
    print(json.dumps(results, indent=2, default=str))


if __name__ == "__main__":
    main()
