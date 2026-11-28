"""Verify the deferred Metal finalization trial on a jointly trained PGO build."""

import argparse
import json
from pathlib import Path
from runpy import run_path
from statistics import median


HERE = Path(__file__).resolve().parent
END = run_path(str(HERE / "summarize-raid-render-end.py"))
QUEUED = END["QUEUED"]
MODES = ("reference", "control", "deferred")
ROLES = ("reference", "training", "candidate")
FPS = "average_completed_fps"
P99 = "p99_gpu_completion_gap_ns"


def verify_run(root, name, role, flag, logged, artifacts):
    capture = root / name
    expected = {
        "runtime_unsigned_sha256": artifacts["runtime_unsigned_sha256"],
        "runtime_signed_sha256": artifacts["runtime_signed_sha256"],
        "renderer_role": role,
        "renderer_sha256": artifacts["renderer_sha256_by_role"][role],
    }
    if (json.loads((capture / "artifact-sha256.json").read_text()) != expected or
            (capture / "render-end-mode.txt").read_text().split() != [flag, str(int(logged))] or
            (capture / "render-finalize.tsv").is_file() != logged or
            (capture / "render-worker.tsv").is_file() != logged):
        raise ValueError(f"Wrong signed runtime, renderer or logging in {capture}")
    return QUEUED["TIMING"]["verify"](capture, role, seconds=25 if logged else 60)


def summary(root):
    previous = json.loads((HERE / "results/raid-winemetalgl-renderer-pgo-ab.json").read_text())
    close_trial = json.loads((HERE / "results/raid-winemetalgl-defer-render-end-ab.json").read_text())
    artifacts = json.loads((root / "artifact-sha256.json").read_text())
    sha = QUEUED["sha"]
    if (sha(root / "artifacts/candidate/ocerz") != artifacts["runtime_unsigned_sha256"] or
            artifacts["runtime_unsigned_sha256"] != close_trial["artifact_sha256"]["runtime_unsigned_sha256"] or
            artifacts["renderer_sha256_by_role"]["reference"] != previous["renderer_artifacts"]["candidate"]["sha256"]):
        raise ValueError("The accepted runtime or reference PGO renderer differs")
    for role in ROLES:
        if sha(root / f"artifacts/{role}/libwinemetalgl.dylib") != artifacts["renderer_sha256_by_role"][role]:
            raise ValueError(f"The {role} renderer differs")
    flags = json.loads((root / "build-flags.json").read_text())
    if (sha(root / "raid.profdata") != flags["merged_profile_sha256"] or
            not flags["matched_core_compile_commands"] or flags["release_optimization"] != "-O3" or
            not flags["thin_lto"] or flags["deferred_commit_profile_counter_sum"]["control"] != 0 or
            flags["deferred_commit_profile_counter_sum"]["deferred"] <= 0):
        raise ValueError("The merged profile lacks the required training paths")
    training = [
        {"mode": mode, **verify_run(root, f"training-{mode}", "training", mode, False, artifacts)}
        for mode in ("control", "deferred")
    ]
    diagnostic = verify_run(root, "diagnostic-deferred", "candidate", "deferred", True, artifacts)
    finalization = END["finalize_profile"](root / "diagnostic-deferred", deferred=True)
    runs = []
    for index, mode in enumerate(MODES + MODES[::-1], 1):
        role = "reference" if mode == "reference" else "candidate"
        flag = "control" if mode == "reference" else mode
        runs.append({"order": index, "mode": mode, "renderer_role": role,
                     **verify_run(root, f"timed-{index}-{mode}", role, flag, False, artifacts)})
    by_mode = {mode: [row for row in runs if row["mode"] == mode] for mode in MODES}
    comparison = {
        mode: {"median_gpu_completed_fps": median(row[FPS] for row in samples),
               "median_p99_completion_gap_ns": median(row[P99] for row in samples)}
        for mode, samples in by_mode.items()
    }
    differences = {}
    for before, after in (("reference", "control"), ("control", "deferred"), ("reference", "deferred")):
        differences[f"{before}_to_{after}"] = [
            {"completed_fps": right[FPS] - left[FPS], "p99_gap_ns": right[P99] - left[P99]}
            for left, right in zip(by_mode[before], by_mode[after])
        ]
    settings = ("hardware", "macos", "viewport", "vsync", "caster_count", "boss_count", "camera")
    return {**{key: previous[key] for key in settings}, "artifact_sha256": artifacts,
            "renderer_build_validation": flags, "training_captures": training,
            "trained_deferred_diagnostic": {"verified_gpu_window": diagnostic, "finalization": finalization},
            "gpu_tests_per_mode": 343, "indexed_replay": END["REUSE"]["replay_pixels"](root, MODES),
            "timed_runs": runs, "comparison": comparison, "paired_mode_differences": differences}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_root", type=Path)
    print(json.dumps(summary(parser.parse_args().capture_root), indent=2))
