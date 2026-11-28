"""Compare synchronous render finalization with close-and-commit on the Metal worker."""

import argparse
import json
from pathlib import Path
from runpy import run_path
from statistics import median


HERE = Path(__file__).resolve().parent
REUSE = run_path(str(HERE / "summarize-raid-render-reuse.py"))
QUEUED = REUSE["QUEUED"]
DISTRIBUTION = run_path(str(HERE / "summarize-raid-frame-tail.py"))["distribution"]
MODES = ("control", "reuse", "deferred")
FPS = "average_completed_fps"
P99 = "p99_gpu_completion_gap_ns"


def verify_run(root, name, mode, logged, artifacts):
    capture = root / name
    if (json.loads((capture / "artifact-sha256.json").read_text()) != artifacts or
            (capture / "render-end-mode.txt").read_text().split() != [mode, str(int(logged))] or
            (capture / "render-finalize.tsv").is_file() != logged or
            (capture / "render-worker.tsv").is_file() != (logged and mode == "deferred")):
        raise ValueError(f"Wrong signed runtime, renderer or logging in {capture}")
    return QUEUED["TIMING"]["verify"](capture, "candidate", seconds=25 if logged else 60)


def completion_bounds(capture):
    window = json.loads((capture / "window.json").read_text())
    pid, stream = window["pid"], window["stream"]
    first, last = window["first_sequence"], window["last_sequence"]
    width, height = window["window_size"]
    dimensions = (width << 32) | height
    times = []
    for line in (capture / "gpu.ns").read_text().splitlines():
        fields = line.split()
        if len(fields) == 6:
            process, identifier, sequence, timestamp, _, size = map(int, fields)
            if (process, identifier, size) == (pid, stream, dimensions) and first <= sequence <= last:
                if sequence != first + len(times) or (times and times[-1] >= timestamp):
                    raise ValueError("Unexpected GPU sequence in the profiling window")
                times.append(timestamp)
    if len(times) != window["verified_completed_renders"]:
        raise ValueError("GPU completion bounds are incomplete")
    return times[0], times[-1]


def finalize_profile(capture, deferred):
    first, last = completion_bounds(capture)
    records = {}
    for line in (capture / "render-finalize.tsv").read_text().splitlines():
        mode, queue, serial, start, returned = map(int, line.split())
        if mode != int(deferred) or not 0 < start <= returned or not serial or not queue:
            raise ValueError("Invalid producer finalization timestamp")
        if first <= start < last:
            key = queue, serial
            if key in records:
                raise ValueError("Repeated render buffer finalization")
            records[key] = start, returned
    if not records:
        raise ValueError("No render finalization in the verified GPU interval")
    result = {"finalized_render_buffers": len(records),
              "producer_wall_time_ns": DISTRIBUTION([end - start for start, end in records.values()])}
    if deferred:
        wait, close, commit, total = [], [], [], []
        worker_started_after_return = 0
        joined = set()
        for line in (capture / "render-worker.tsv").read_text().splitlines():
            queue, serial, reserve, start, closed, committed = map(int, line.split())
            key = queue, serial
            if not 0 < reserve <= start <= closed <= committed or not queue or not serial:
                raise ValueError("Invalid worker timestamp order")
            if key not in records:
                continue
            producer_start, producer_returned = records[key]
            if key in joined or not producer_start <= reserve <= producer_returned:
                raise ValueError("Worker record did not match its producer reservation")
            joined.add(key)
            wait.append(start - reserve)
            close.append(closed - start)
            commit.append(committed - closed)
            total.append(committed - reserve)
            worker_started_after_return += start > producer_returned
        if joined != records.keys():
            raise ValueError("A deferred close or commit is missing from the GPU interval")
        result.update(reserve_to_worker_ns=DISTRIBUTION(wait),
                      worker_last_batch_and_end_ns=DISTRIBUTION(close),
                      worker_commit_ns=DISTRIBUTION(commit),
                      reserve_to_commit_return_ns=DISTRIBUTION(total),
                      worker_started_after_producer_return=worker_started_after_return)
    return result


def summary(root):
    artifacts = json.loads((root / "artifact-sha256.json").read_text())
    if (QUEUED["sha"](root / "artifacts/candidate/ocerz") != artifacts["runtime_unsigned_sha256"] or
            QUEUED["sha"](root / "artifacts/candidate/libwinemetalgl.dylib") != artifacts["renderer_sha256"]):
        raise ValueError("The timed binaries changed")
    diagnostics = {}
    for mode in ("reuse", "deferred"):
        name = f"diagnostic-{mode}"
        diagnostics[mode] = {
            "verified_gpu_window": verify_run(root, name, mode, True, artifacts),
            "finalization": finalize_profile(root / name, deferred=mode == "deferred"),
        }
    runs = [{"order": index, "mode": mode,
             **verify_run(root, f"timed-{index}-{mode}", mode, False, artifacts)}
            for index, mode in enumerate(MODES + MODES[::-1], 1)]
    by_mode = {mode: [row for row in runs if row["mode"] == mode] for mode in MODES}
    comparison = {
        mode: {"median_gpu_completed_fps": median(row[FPS] for row in samples),
               "median_p99_completion_gap_ns": median(row[P99] for row in samples)}
        for mode, samples in by_mode.items()
    }
    differences = {}
    for before in ("control", "reuse"):
        differences[f"{before}_to_deferred"] = [
            {"completed_fps": right[FPS] - left[FPS], "p99_gap_ns": right[P99] - left[P99]}
            for left, right in zip(by_mode[before], by_mode["deferred"])
        ]
    return {"artifact_sha256": artifacts, "gpu_tests_per_mode": 343,
            "indexed_replay": REUSE["replay_pixels"](root, MODES), "diagnostics": diagnostics,
            "timed_runs": runs, "comparison": comparison, "paired_mode_differences": differences}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_root", type=Path)
    print(json.dumps(summary(parser.parse_args().capture_root), indent=2))
