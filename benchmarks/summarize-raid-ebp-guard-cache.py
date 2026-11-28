"""Verify the live raid comparison for cached i386 EBP bounds checks."""

import argparse
import json
from pathlib import Path
from runpy import run_path
from statistics import median
import struct


HERE = Path(__file__).resolve().parent
READ_WINDOW = run_path(str(HERE / "summarize-raid-shadow-reuse.py"))["read_window"]


def verify(capture, mode, counts, duration, legacy_paused):
    if (capture / "ebp-cache-mode.txt").read_text().split() != [str(mode), str(counts)]:
        raise ValueError(f"Unexpected cache flags in {capture}")
    if (capture / "map-shadow-mode.txt").read_text().split() != ["1", "0"]:
        raise ValueError(f"Unexpected shadow settings in {capture}")
    if json.loads((capture / "legacy_witness.json").read_text())["legacy_paused"] != legacy_paused:
        raise ValueError(f"Different background test state in {capture}")
    if (capture / "ebp-guard-counts.bin").exists() != bool(counts):
        raise ValueError(f"Unexpected runtime counter file in {capture}")
    return READ_WINDOW(capture, duration)


def counters(hits, misses):
    if min(hits, misses) < 0 or hits + misses == 0:
        raise ValueError("Expected guarded EBP lookup counts")
    return {"hits": hits, "misses": misses, "hit_fraction": hits / (hits + misses)}


def summarize(root):
    diagnostic = root / "diagnostic"
    paused = json.loads((diagnostic / "legacy_witness.json").read_text())["legacy_paused"]
    diag = verify(diagnostic, 1, 1, 25, paused)
    hits, misses = struct.unpack("<QQ", (diagnostic / "ebp-guard-counts.bin").read_bytes())
    counter_samples = {"whole_launch": counters(hits, misses)}
    before = diagnostic / "counts-world_ready.json"
    after = diagnostic / "counts-gpu_window_complete.json"
    if before.exists() and after.exists():
        first, last = (json.loads(path.read_text()) for path in (before, after))
        if last["observed_monotonic_ns"] <= first["observed_monotonic_ns"] or last["hits"] > hits or last["misses"] > misses:
            raise ValueError("Invalid live counter snapshots")
        counter_samples["between_world_and_window_markers"] = counters(
            last["hits"] - first["hits"], last["misses"] - first["misses"],
        )
        counter_samples["marker_snapshot_span_ns"] = last["observed_monotonic_ns"] - first["observed_monotonic_ns"]
    timed = []
    for order, mode in enumerate((0, 1, 1, 0), 1):
        folder = root / "timed" / f"{order}-{mode}"
        timed.append({"order": order, "ebp_guard_cache": bool(mode), **verify(folder, mode, 0, 60, paused)})
    groups = {}
    for name, mode in (("guard_each_entry", False), ("reuse_verified_address", True)):
        active = [run for run in timed if run["ebp_guard_cache"] == mode]
        groups[name] = {
            "median_completed_fps": median(row["average_completed_fps"] for row in active),
            "median_p99_gap_ns": median(row["p99_gpu_completion_gap_ns"] for row in active),
        }
    return {
        "legacy_paused_in_all_launches": paused,
        "diagnostic_gpu_window": diag,
        "diagnostic_counters": counter_samples,
        "timed_runs": timed,
        "comparison": groups,
        "paired_trial_minus_control": [
            {
                "completed_fps": timed[t]["average_completed_fps"] - timed[c]["average_completed_fps"],
                "p99_gap_ns": timed[t]["p99_gpu_completion_gap_ns"] - timed[c]["p99_gpu_completion_gap_ns"],
            }
            for c, t in ((0, 1), (3, 2))
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_root", type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize(args.capture_root), indent=2))


if __name__ == "__main__":
    main()
