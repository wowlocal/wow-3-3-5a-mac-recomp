"""Verify the live raid comparison and measure tracked Metal submission time."""

import argparse
import json
from pathlib import Path
from runpy import run_path
from statistics import median


HERE = Path(__file__).resolve().parent
READ_WINDOW = run_path(str(HERE / "summarize-raid-shadow-reuse.py"))["read_window"]
READ_BOUNDS = run_path(str(HERE / "summarize-i386-unwind-hotspots.py"))["read_stack_window_bounds"]
DISTRIBUTION = run_path(str(HERE / "summarize-raid-frame-tail.py"))["distribution"]


def verify(folder, mode, logging, seconds):
    if (folder / "commit-mode.txt").read_text().split() != [str(mode), str(logging)]:
        raise ValueError(f"Incorrect submission flags at {folder}")
    if (folder / "map-shadow-mode.txt").read_text().split() != ["1", "0"]:
        raise ValueError(f"Incorrect shadow settings at {folder}")
    if not json.loads((folder / "legacy_witness.json").read_text())["legacy_paused"]:
        raise ValueError(f"Legacy process was not paused at {folder}")
    return READ_WINDOW(folder, seconds)


def diagnostics(folder, mode):
    window = verify(folder, mode, 1, 25)
    _, first, cutoff = READ_BOUNDS(folder)
    main = {}
    worker = {}
    with (folder / "commits.ns").open() as source:
        for line in source:
            if not line.endswith("\n"):
                break
            kind, queue, serial, metadata, start, end = line.split()
            queue, serial, metadata, start, end = map(int, (queue, serial, metadata, start, end))
            begin = start if kind == "main" else metadata
            if not first <= begin < cutoff:
                continue
            key = queue, serial, begin
            if kind == "main" and metadata == mode and end >= start and key not in main:
                main[key] = end - start
            elif kind == "worker" and mode == 1 and end >= start >= begin and key not in worker:
                worker[key] = start - begin, end - start
            else:
                raise ValueError(f"Invalid or duplicate Metal commit timing at {folder}")
    if not main or (mode and main.keys() != worker.keys()) or (not mode and worker):
        raise ValueError(f"Incomplete main/worker event pairing at {folder}")
    result = {
        "deferred_commit": bool(mode),
        "diagnostic_gpu_window": window,
        "tracked_submissions_inside_measured_window": len(main),
        "submissions_by_distinct_queues": len({entry[0] for entry in main}),
        "main_submission_call_ns": DISTRIBUTION(list(main.values())),
        "main_submission_ns_divided_by_verified_outputs": sum(main.values()) / window["verified_completed_renders"],
    }
    if worker:
        result["worker_begin_after_main_begin_ns"] = DISTRIBUTION([delay for delay, _ in worker.values()])
        result["worker_metal_commit_call_ns"] = DISTRIBUTION([cost for _, cost in worker.values()])
    return result


def summarize(root):
    logs = [diagnostics(root / "diagnostic" / str(mode), mode) for mode in (0, 1)]
    runs = []
    for index, mode in enumerate((0, 1, 1, 0), 1):
        folder = root / "timed" / f"{index}-{mode}"
        if (folder / "commits.ns").exists():
            raise ValueError(f"Submission logging was enabled in timed capture {folder}")
        runs.append({"order": index, "deferred_commit": bool(mode), **verify(folder, mode, 0, 60)})
    modes = {}
    for mode, name in ((False, "inline"), (True, "deferred")):
        rows = [r for r in runs if r["deferred_commit"] == mode]
        modes[name] = {
            "median_completed_fps": median(row["average_completed_fps"] for row in rows),
            "median_p99_gap_ns": median(row["p99_gpu_completion_gap_ns"] for row in rows),
        }
    return {
        "diagnostics": logs,
        "timed_runs": runs,
        "comparison": modes,
        "paired_deferred_minus_inline": [
            {
                "completed_fps": runs[t]["average_completed_fps"] - runs[c]["average_completed_fps"],
                "p99_gap_ns": runs[t]["p99_gpu_completion_gap_ns"] - runs[c]["p99_gpu_completion_gap_ns"],
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
