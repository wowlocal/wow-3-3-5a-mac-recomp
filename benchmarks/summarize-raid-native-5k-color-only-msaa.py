"""Verify the color-only MSAA passes and compare the native 5K raid windows."""

import argparse
import json
from pathlib import Path
import re
from runpy import run_path
from statistics import median


HERE = Path(__file__).resolve().parent
VERIFY = run_path(str(HERE / "verify-raid-native-5k.py"))["verify_renderer_mode"]
FPS, P99 = "average_completed_fps", "p99_gpu_completion_gap_ns"
FOCUSED_TEST = "arb_msaa_color_pass_preserves_depth_and_stencil"
PASS_LOG = re.compile(r"color-only MSAA passes (\d+), (\d+)x(\d+) samples (\d+)")


def read_run(case, mode, log, baseline, artifacts, duration):
    report = VERIFY(case, baseline, artifacts, "color-only-msaa-mode.txt", mode, log, measurement_seconds=duration)
    return {"color_only_msaa": mode, "gpu_window": report["verified_gpu_window"],
            "readback_sha256": report["world_readback_sha256"], "cast_rows": report["verified_cast_rows"]}


def pass_samples(text, width, height, samples):
    return [int(count) for count, w, h, n in PASS_LOG.findall(text)
            if (int(w), int(h), int(n)) == (width, height, samples)]


def renderer_tests(root):
    result = {}
    for mode in (0, 1):
        text = (root / f"gpu-tests-{mode}.log").read_text()
        count = re.search(r"^(\d+) tests, 0 failed checks$", text, re.M)
        focused = (root / f"focused-{mode}.log").read_text()
        observed = pass_samples(focused, 64, 64, 4)
        if (not count or f"PASS {FOCUSED_TEST}" not in text or f"PASS {FOCUSED_TEST}" not in focused
                or bool(observed) != bool(mode)):
            raise ValueError(f"Renderer tests did not validate the expected four-sample encoder in mode {mode}")
        result[mode] = {"tests_passed": int(count[1]), "focused_color_only_encoders_logged": observed}
    if result[0]["tests_passed"] != result[1]["tests_passed"]:
        raise ValueError("The renderer modes did not exercise the same tests")
    return result


def summarize(root):
    baseline = json.loads((HERE / "results/raid-native-5k-max-baseline.json").read_text())
    artifacts = json.loads((root / "color-only-msaa/artifact-sha256.json").read_text())
    for key in ("runtime_unsigned_sha256", "runtime_signed_sha256"):
        if artifacts[key] != baseline["artifact_sha256"][key]:
            raise ValueError("The i386 runtime differs from the original 5K benchmark")
    diagnostic_dir = root / "native-5k-color-only-msaa-diagnostic"
    diagnostic = read_run(diagnostic_dir, 1, 1, baseline, artifacts, 25)
    text = (diagnostic_dir / "client.log").read_text()
    anchor = text.index("native trial GL viewport 0 0 5120 2880 draw-fbo 0 samples 4 buffers 1")
    samples = pass_samples(text[anchor:], 5120, 2880, 4)
    if not samples:
        raise ValueError("The raid did not use a color-only encoder at native 5K")
    runs = [read_run(root / f"native-5k-color-only-msaa-{order}-{mode}", mode, 0, baseline, artifacts, 60)
            for order, mode in enumerate((0, 1, 1, 0), 1)]
    return {
        "viewport": baseline["viewport"],
        "multisample_count": baseline["multisample_count"],
        "verified_settings": baseline["graphics"]["actual"],
        "artifact_sha256": artifacts,
        "renderer_validation": renderer_tests(root / "color-only-msaa"),
        "diagnostic_run": {**diagnostic, "post_viewport_native_pass_counter_samples": samples},
        "runs_in_capture_order": runs,
        "medians_by_mode": {
            mode: {metric: median(run["gpu_window"][metric] for run in runs if run["color_only_msaa"] == mode)
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
