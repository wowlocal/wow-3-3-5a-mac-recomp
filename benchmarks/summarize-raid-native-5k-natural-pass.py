"""Compare the original and natural-boundary submission policies at native 5K."""

import argparse
import json
from pathlib import Path
import re
from runpy import run_path
from statistics import median


HERE = Path(__file__).resolve().parent
VERIFY = run_path(str(HERE / "verify-raid-native-5k.py"))["verify"]
REFERENCE, NATURAL = 64, 2048
FPS, P99 = "average_completed_fps", "p99_gpu_completion_gap_ns"


def read_run(case, budget, baseline):
    run = VERIFY(case, case / "graphics-settings.lua")
    if (run["graphics"]["issues"] or run["graphics"]["actual"] != baseline["graphics"]["actual"]
            or run["artifact_sha256"] != baseline["artifact_sha256"]):
        raise ValueError(f"{case.name} differs from the verified native 5K baseline")
    if ((case / "pass-mode.txt").read_text().split() != [str(budget), "0"]
            or (case / "render-end-mode.txt").read_text().split() != ["deferred", "0"]
            or (case / "submissions.ns").exists()):
        raise ValueError(f"{case.name} differs from the requested policy or timing instrumentation")
    return {"budget": budget, "gpu_window": run["verified_gpu_window"], "verified_cast_rows": run["verified_cast_rows"],
            "world_readback_sha256": run["world_readback_sha256"]}


def check_replay(root, budget):
    summary = (root / f"native-5k-replay-{budget}.log").read_text()
    gpu_tests = (root / f"native-5k-gpu-tests-{budget}.log").read_text().splitlines()[-1]
    replay = re.search(r"^outputs (\d+), RGBA ([0-9a-f]{16}), colored (\d+)$", summary, re.M)
    tests = re.fullmatch(r"(\d+) tests, 0 failed checks", gpu_tests)
    if not (tests and replay and "viewport 5120x2880, samples 4" in summary and int(replay[3]) > 10_000):
        raise ValueError(f"The GPU regression or native framebuffer replay failed for budget {budget}")
    return {"gpu_tests_passed": int(tests[1]), "replay_outputs": int(replay[1]),
            "readback_hash": replay[2], "draw_colored_pixels": int(replay[3])}


def summarize(root):
    baseline = json.loads((HERE / "results/raid-native-5k-max-baseline.json").read_text())
    runs = [
        read_run(root / f"native-5k-natural-{order}-{budget}", budget, baseline)
        for order, budget in enumerate((REFERENCE, NATURAL, NATURAL, REFERENCE), 1)
    ]
    validation = {budget: check_replay(root, budget) for budget in (REFERENCE, NATURAL)}
    keys = ("readback_hash", "draw_colored_pixels", "gpu_tests_passed")
    if any(validation[REFERENCE][key] != validation[NATURAL][key] for key in keys):
        raise ValueError("The two submission policies produce different regression or replay results")
    comparisons = {}
    for budget in (REFERENCE, NATURAL):
        windows = [run["gpu_window"] for run in runs if run["budget"] == budget]
        comparisons[budget] = {metric: median(window[metric] for window in windows) for metric in (FPS, P99)}
    return {
        "artifact_sha256": baseline["artifact_sha256"],
        "viewport": baseline["viewport"],
        "multisample_count": baseline["multisample_count"],
        "verified_settings": baseline["graphics"]["actual"],
        "runs_in_capture_order": runs,
        "median_by_draw_budget": comparisons,
        "paired_natural_minus_reference": [
            {metric: runs[natural]["gpu_window"][metric] - runs[ref]["gpu_window"][metric]
             for metric in (FPS, P99)}
            for ref, natural in ((0, 1), (3, 2))
        ],
        "renderer_validation": validation,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_root", type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize(args.capture_root), indent=2))
