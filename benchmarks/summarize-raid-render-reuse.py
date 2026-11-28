"""Verify Metal batch reuse and compare it with the original queued encoder."""

import argparse
from collections import Counter
import json
from pathlib import Path
from runpy import run_path
from statistics import median


HERE = Path(__file__).resolve().parent
QUEUED = run_path(str(HERE / "summarize-raid-queued-render.py"))
MODES = ("control", "queued", "reuse")
FPS = "average_completed_fps"
P99 = "p99_gpu_completion_gap_ns"


def verify_run(root, name, mode, logged, artifacts):
    capture = root / name
    if (json.loads((capture / "artifact-sha256.json").read_text()) != artifacts or
            (capture / "render-reuse-mode.txt").read_text().split() != [mode, str(int(logged))] or
            (capture / "queued-encoder.tsv").is_file() != logged):
        raise ValueError(f"Wrong renderer, runtime or logging in {capture}")
    return QUEUED["TIMING"]["verify"](capture, "candidate", seconds=25 if logged else 60)


def batch_counters(path, mode):
    columns = ("queued_commands", "batches", "copied_payload_bytes", "synchronous_forwarded_calls",
               "reused_batches", "unpooled_batches")
    totals = Counter()
    pass_count = 0
    for line in path.read_text().splitlines():
        fields = list(map(int, line.split()))
        if len(fields) != len(columns) or min(fields) < 0:
            raise ValueError(f"Invalid encoder row in {path}")
        commands, batches, _, _, reused, unpooled = fields
        if commands > 96 * batches or reused + unpooled > batches:
            raise ValueError(f"Inconsistent batch ownership counts in {path}")
        totals.update(dict(zip(columns, fields)))
        pass_count += 1
    if not pass_count or not totals["batches"]:
        raise ValueError(f"Missing queued Metal work in {path}")
    if mode == "queued" and (totals["reused_batches"] or totals["unpooled_batches"] != totals["batches"]):
        raise ValueError(f"The original queue used the pool in {path}")
    if mode == "reuse" and not totals["reused_batches"]:
        raise ValueError(f"The reuse arm did not recycle batches in {path}")
    counts = {column: totals[column] for column in columns}
    return {"logged_passes": pass_count, **counts,
            "batch_allocations": totals["batches"] - totals["reused_batches"]}


def replay_pixels(root, modes=MODES):
    pixels = {}
    for mode in modes:
        if not (root / f"gpu-tests-{mode}.log").read_text().endswith("343 tests, 0 failed checks\n"):
            raise ValueError(f"The {mode} GPU suite failed")
        row = QUEUED["PIXELS"].search((root / f"replay-{mode}.log").read_text())
        if not row or int(row[1]) <= 0:
            raise ValueError(f"The {mode} replay did not produce pixels")
        pixels[mode] = {"outputs": int(row[1]), "rgba_hash": row[2], "colored_pixels": int(row[3])}
    for key in ("rgba_hash", "colored_pixels"):
        if len({values[key] for values in pixels.values()}) != 1:
            raise ValueError("Reusing a render batch changed the framebuffer")
    return pixels


def summary(root):
    artifacts = json.loads((root / "artifact-sha256.json").read_text())
    sha = QUEUED["sha"]
    if (sha(root / "artifacts/candidate/ocerz") != artifacts["runtime_unsigned_sha256"] or
            sha(root / "artifacts/candidate/libwinemetalgl.dylib") != artifacts["renderer_sha256"]):
        raise ValueError("The timed artifacts changed")
    diagnostics = {}
    for mode in ("queued", "reuse"):
        name = f"diagnostic-{mode}"
        capture = root / name
        window = verify_run(root, name, mode, True, artifacts)
        pid = json.loads((capture / "window.json").read_text())["pid"]
        diagnostics[mode] = {
            "diagnostic_gpu_window": window,
            "pass_counters": batch_counters(capture / "queued-encoder.tsv", mode),
            "cpu_time_profile": QUEUED["cpu_samples"](capture / "encoder-cpu.xml", pid),
        }
    runs = [{"order": index, "mode": mode,
             **verify_run(root, f"timed-{index}-{mode}", mode, False, artifacts)}
            for index, mode in enumerate(MODES + MODES[::-1], 1)]
    by_mode = {mode: [row for row in runs if row["mode"] == mode] for mode in MODES}
    comparison = {
        mode: {"median_gpu_completed_fps": median(row[FPS] for row in measurements),
               "median_p99_completion_gap_ns": median(row[P99] for row in measurements)}
        for mode, measurements in by_mode.items()
    }
    differences = {}
    for before, after in (("control", "queued"), ("queued", "reuse"), ("control", "reuse")):
        differences[f"{before}_to_{after}"] = [
            {"completed_fps": right[FPS] - left[FPS], "p99_gap_ns": right[P99] - left[P99]}
            for left, right in zip(by_mode[before], by_mode[after])
        ]
    return {"artifact_sha256": artifacts, "gpu_tests_per_mode": 343, "indexed_replay": replay_pixels(root),
            "diagnostics": diagnostics, "timed_runs": runs, "comparison": comparison,
            "paired_mode_differences": differences}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_root", type=Path)
    print(json.dumps(summary(parser.parse_args().capture_root), indent=2))
