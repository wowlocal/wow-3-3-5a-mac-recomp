"""Compare Metal draw budgets with identical native 5K raid settings."""

import argparse
from collections import Counter
import json
from pathlib import Path
from runpy import run_path
from statistics import median


HERE = Path(__file__).resolve().parent
VERIFY = run_path(str(HERE / "verify-raid-native-5k.py"))
FRAMES = run_path(str(HERE / "summarize-raid-frame-tail.py"))
REFERENCE, LARGER = 64, 192
BUDGETS = (REFERENCE, LARGER, LARGER, REFERENCE)
FPS, P99 = "average_completed_fps", "p99_gpu_completion_gap_ns"


def run_summary(case, budget, baseline):
    run = VERIFY["verify"](case, case / "graphics-settings.lua")
    if (run["graphics"]["issues"] or run["graphics"]["actual"] != baseline["graphics"]["actual"]
            or run["artifact_sha256"] != baseline["artifact_sha256"]):
        raise ValueError(f"The {case.name} run differs from the verified native 5K baseline")
    if ((case / "pass-mode.txt").read_text().split() != [str(budget), "1"]
            or (case / "render-end-mode.txt").read_text().split() != ["deferred", "0"]):
        raise ValueError(f"The {case.name} run lacks the requested submission and logging modes")
    window = run["verified_gpu_window"]
    ends, streams = FRAMES["render_submissions"](case / "submissions.ns", window["pid"])
    frames, has_cpu = FRAMES["read_frames"](case, window, ends, False)
    intervals = FRAMES["explain_intervals"](frames, has_cpu, streams[frames[0].final.stream])
    timing = FRAMES["summarize"](intervals)
    metrics = timing["metrics"]
    return {
        "draw_budget": budget,
        "verified_cast_rows": run["verified_cast_rows"],
        "verified_gpu_window": window,
        "readback_sha256": run["world_readback_sha256"],
        "mean_draws_per_completed_output": sum(frame.draws for frame in frames) / len(frames),
        "draw_splits": timing["most_common_draw_splits"],
        "intervals_by_submission_count": dict(sorted(Counter(row["render_submissions"] for row in intervals).items())),
        "submission_metrics_ns": {
            name: metrics[name] for name in ("final_submit_interval_ns", "final_submit_to_gpu_start_ns",
                                             "final_gpu_span_ns", "earlier_submissions_gpu_spans_ns",
                                             "completion_gap_outside_recorded_render_spans_ns")
        },
        "render_submissions": metrics["render_submissions"],
    }


def summarize(root):
    baseline = json.loads((HERE / "results/raid-native-5k-max-baseline.json").read_text())
    runs = [
        run_summary(root / f"native-5k-draw-{order}-{budget}", budget, baseline)
        for order, budget in enumerate(BUDGETS, 1)
    ]
    comparison = {}
    for budget in (REFERENCE, LARGER):
        group = [run["verified_gpu_window"] for run in runs if run["draw_budget"] == budget]
        comparison[str(budget)] = {key: median(run[key] for run in group) for key in (FPS, P99)}
    return {
        "artifact_sha256": baseline["artifact_sha256"],
        "viewport": baseline["viewport"],
        "multisample_count": baseline["multisample_count"],
        "verified_settings": baseline["graphics"]["actual"],
        "runs_in_capture_order": runs,
        "comparison": comparison,
        "paired_larger_minus_reference": [
            {key: runs[t]["verified_gpu_window"][key] - runs[c]["verified_gpu_window"][key] for key in (FPS, P99)}
            for c, t in ((0, 1), (3, 2))
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_root", type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize(args.capture_root), indent=2))
