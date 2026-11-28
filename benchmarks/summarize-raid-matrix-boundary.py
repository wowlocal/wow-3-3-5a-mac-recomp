"""Separate the i386 matrix block split, callout and epilogue costs."""

import argparse
import json
from pathlib import Path
import re
from runpy import run_path
from statistics import median


HERE = Path(__file__).resolve().parent
TIMING = run_path(str(HERE / "summarize-raid-renderer-pgo.py"))
MATRIX = run_path(str(HERE / "summarize-raid-matrix.py"))
MODES = ("control", "split", "decline", "native", "chain")
FPS = "average_completed_fps"
P99 = "p99_gpu_completion_gap_ns"


def verify_capture(root, name, mode, logging, artifacts):
    capture = root / name
    if (json.loads((capture / "artifact-sha256.json").read_text()) != artifacts or
            (capture / "matrix-boundary-mode.txt").read_text().split() != [mode, str(int(logging))]):
        raise ValueError(f"Wrong runtime, renderer or launch mode in {capture}")
    text = (capture / "client.log").read_text()
    has_ring = (capture / "matrix.bin").is_file()
    has_witness = "ocerz: raid matrix native calls=" in text
    has_link = "ocerz: raid matrix chained to guest epilogue" in text
    if (has_ring, has_witness, has_link) != (logging, logging, logging and mode == "chain"):
        raise ValueError(f"Incorrect tracing or missing native link in {capture}")
    return TIMING["verify"](capture, "candidate", seconds=25 if logging else 60)


def summary(root):
    artifacts = json.loads((root / "artifact-sha256.json").read_text())
    if MATRIX["REPLAY"]["digest"](root / "artifacts/candidate/ocerz") != artifacts["runtime_unsigned_sha256"]:
        raise ValueError("The instrumented runtime artifact changed")
    diagnostic = verify_capture(root, "diagnostic", "chain", True, artifacts)
    replay = MATRIX["replay"](root, "matrix-export.json", "matrix-replay.txt",
                              "matrix-sanitized.txt", "matrix-cases.bin")
    if replay["diagnostic_artifacts"] != artifacts or replay["captures"][0]["verified_gpu_window"] != diagnostic:
        raise ValueError("The replay did not come from the verified chain diagnostic")
    log = (root / "diagnostic/client.log").read_text()
    hit_counts = [int(count) for count in re.findall(r"^ocerz: raid matrix native calls=(\d+)$",
                                                      log, re.MULTILINE)]
    if not hit_counts or max(hit_counts) < 32768:
        raise ValueError("The native helper did not execute during the chain diagnostic")

    runs = []
    for order, mode in enumerate(MODES + MODES[::-1], 1):
        runs.append({"order": order, "mode": mode,
                     **verify_capture(root, f"timed-{order}-{mode}", mode, False, artifacts)})
    by_mode = {mode: [row for row in runs if row["mode"] == mode] for mode in MODES}
    comparison = {
        mode: {"median_gpu_completed_fps": median(row[FPS] for row in samples),
               "median_p99_completion_gap_ns": median(row[P99] for row in samples)}
        for mode, samples in by_mode.items()
    }
    contrasts = {}
    for before, after in zip(MODES, (*MODES[1:], "control")):
        contrasts[f"{before}_to_{after}"] = [
            {"completed_fps": right[FPS] - left[FPS], "p99_gap_ns": right[P99] - left[P99],
             "mean_output_interval_delta_ns": 1e9 / right[FPS] - 1e9 / left[FPS]}
            for left, right in zip(by_mode[before], by_mode[after])
        ]
    if not (root / "m32-tests.log").read_text().endswith("test_m32: 805 checks, 0 failed\n"):
        raise ValueError("General i386 checks failed")
    return {"chained_matrix_replay": replay, "native_calls_at_last_largest_witness": max(hit_counts),
            "general_i386_checks": 805, "timed_runs": runs, "comparison": comparison,
            "paired_mode_differences": contrasts}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_root", type=Path)
    print(json.dumps(summary(parser.parse_args().capture_root), indent=2))
