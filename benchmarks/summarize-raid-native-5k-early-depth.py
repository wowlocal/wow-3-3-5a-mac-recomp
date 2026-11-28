"""Verify the early-depth tests and compare the native 5K raid windows."""

import argparse
import json
from pathlib import Path
import re
from runpy import run_path
from statistics import median


HERE = Path(__file__).resolve().parent
VERIFY = run_path(str(HERE / "verify-raid-native-5k.py"))["verify_renderer_mode"]
FPS, P99 = "average_completed_fps", "p99_gpu_completion_gap_ns"


def read_run(case, mode, log, baseline, artifacts, duration):
    report = VERIFY(case, baseline, artifacts, "early-depth-mode.txt", mode, log, measurement_seconds=duration)
    return {"early_depth": mode, "gpu_window": report["verified_gpu_window"],
            "readback_sha256": report["world_readback_sha256"], "cast_rows": report["verified_cast_rows"]}


def read_shader_log(path):
    text = path.read_text()
    counts = [int(n) for n in re.findall(r"read-only early depth requests (\d+)", text)]
    compiled = sorted({int(n) for n in re.findall(r"read-only early depth shader (\d+) compiled", text)})
    fallback = sorted({int(n) for n in re.findall(r"read-only early depth shader (\d+) fallback", text)})
    return {"requests_log_high_water_mark": max(counts, default=0), "shaders_compiled": compiled,
            "shader_fallbacks": fallback, "pipeline_fallbacks": text.count("read-only early depth pipeline fallback")}


def test_witness(root):
    result = {}
    for mode in (0, 1):
        text = (root / f"gpu-tests-{mode}.log").read_text()
        count = re.search(r"^(\d+) tests, 0 failed checks$", text, re.M)
        if not count or "PASS arb_read_only_early_depth_msaa" not in text:
            raise ValueError(f"Renderer tests failed in mode {mode}")
        focused = read_shader_log(root / f"focused-{mode}.log")
        if (bool(mode) != bool(focused["requests_log_high_water_mark"])
                or bool(mode) != bool(focused["shaders_compiled"]) or focused["shader_fallbacks"]
                or focused["pipeline_fallbacks"]):
            raise ValueError(f"The focused test did not exercise the expected pipeline in mode {mode}")
        result[mode] = {"tests_passed": int(count[1]), "focused_4x_msaa_shader_log": focused}
    if result[0]["tests_passed"] != result[1]["tests_passed"]:
        raise ValueError("The renderer modes did not exercise the same tests")
    return result


def summarize(root):
    baseline = json.loads((HERE / "results/raid-native-5k-max-baseline.json").read_text())
    artifacts = json.loads((root / "early-depth/artifact-sha256.json").read_text())
    for key in ("runtime_unsigned_sha256", "runtime_signed_sha256"):
        if artifacts[key] != baseline["artifact_sha256"][key]:
            raise ValueError("The i386 runtime differs from the original 5K benchmark")
    diagnostic_dir = root / "native-5k-early-depth-diagnostic"
    diagnostic = read_run(diagnostic_dir, 1, 1, baseline, artifacts, 25)
    shader_log = read_shader_log(diagnostic_dir / "client.log")
    if not shader_log["requests_log_high_water_mark"] or not shader_log["shaders_compiled"]:
        raise ValueError("No live native 5K draws requested a compiled early-depth shader")
    runs = [read_run(root / f"native-5k-early-depth-{order}-{mode}", mode, 0, baseline, artifacts, 60)
            for order, mode in enumerate((0, 1, 1, 0), 1)]
    return {
        "viewport": baseline["viewport"],
        "multisample_count": baseline["multisample_count"],
        "verified_settings": baseline["graphics"]["actual"],
        "artifact_sha256": artifacts,
        "renderer_validation": test_witness(root / "early-depth"),
        "diagnostic_run": {**diagnostic, "shader_log": shader_log},
        "runs_in_capture_order": runs,
        "medians_by_mode": {
            mode: {metric: median(run["gpu_window"][metric] for run in runs if run["early_depth"] == mode)
                   for metric in (FPS, P99)}
            for mode in (0, 1)
        },
        "paired_enabled_minus_disabled": [
            {metric: runs[enabled]["gpu_window"][metric] - runs[disabled]["gpu_window"][metric]
             for metric in (FPS, P99)}
            for disabled, enabled in ((0, 1), (3, 2))
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_root", type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize(args.capture_root), indent=2))
