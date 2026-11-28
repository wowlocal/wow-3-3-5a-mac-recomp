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


def read_run(case, mode, log, baseline, artifacts, duration, opaque):
    renderer_policy = ("color-only-msaa-mode.txt", 1, 0) if opaque else ("early-depth-mode.txt", mode, log)
    report = VERIFY(case, baseline, artifacts, *renderer_policy, measurement_seconds=duration)
    if opaque and (case / "early-opaque-mode.txt").read_text().split() != [str(mode), str(log)]:
        raise ValueError(f"The wrong opaque early-depth mode was active in {case.name}")
    if opaque and not log:
        observed = read_shader_log(case / "client.log", opaque)
        if any(observed.values()):
            raise ValueError(f"Unexpected shader diagnostics in timing window {case.name}")
    return {"early_depth": mode, "gpu_window": report["verified_gpu_window"],
            "readback_sha256": report["world_readback_sha256"], "cast_rows": report["verified_cast_rows"]}


def read_shader_log(path, opaque=False):
    text = path.read_text()
    name = "opaque early depth" if opaque else "read-only early depth"
    counts = [int(n) for n in re.findall(rf"{name} requests (\d+)", text)]
    compiled = sorted({int(n) for n in re.findall(rf"{name} shader (\d+) compiled", text)})
    fallback = sorted({int(n) for n in re.findall(rf"{name} shader (\d+) fallback", text)})
    return {"requests_log_high_water_mark": max(counts, default=0), "shaders_compiled": compiled,
            "shader_fallbacks": fallback, "pipeline_fallbacks": text.count(f"{name} pipeline fallback")}


def test_witness(root, opaque):
    result = {}
    test = "arb_early_opaque_depth_keeps_alpha_and_program_discard" if opaque else "arb_read_only_early_depth_msaa"
    for mode in (0, 1):
        text = (root / f"gpu-tests-{mode}.log").read_text()
        count = re.search(r"^(\d+) tests, 0 failed checks$", text, re.M)
        if not count or f"PASS {test}" not in text:
            raise ValueError(f"Renderer tests failed in mode {mode}")
        focused = read_shader_log(root / f"focused-{mode}.log", opaque)
        if (bool(mode) != bool(focused["requests_log_high_water_mark"])
                or bool(mode) != bool(focused["shaders_compiled"]) or focused["shader_fallbacks"]
                or focused["pipeline_fallbacks"]):
            raise ValueError(f"The focused test did not exercise the expected pipeline in mode {mode}")
        result[mode] = {"tests_passed": int(count[1]), "focused_4x_msaa_shader_log": focused}
    if result[0]["tests_passed"] != result[1]["tests_passed"]:
        raise ValueError("The renderer modes did not exercise the same tests")
    return result


def read_post_window_gpu(case, window, legacy_pid):
    witness = json.loads((case / "after-window-metal-witness.json").read_text())
    pid = window["pid"]
    if (witness["attach_pid"] != pid or witness["requested_seconds"] != 6 or witness["returncode"]
            or not witness["trace_exists"] or
            not witness["last_verified_gpu_completion_ns"] < witness["begin_uptime_ns"] < witness["end_uptime_ns"]):
        raise ValueError(f"The post-window GPU trace is unverified in {case.name}")
    cast = run_path(str(HERE / "summarize-raid-native-5k-caster-sweep.py"))
    frames = [(seq, tick, draws, size) for process, stream, seq, tick, draws, size in (
        tuple(map(int, row)) for row in cast["LIFE"]["rows"](case / "gpu.ns", 6)
    ) if (process, stream) == (pid, window["stream"]) and seq >= window["last_sequence"]]
    w, h = window["window_size"]
    if (not frames or frames[0][:2] != (window["last_sequence"], witness["last_verified_gpu_completion_ns"])
            or any(draws <= 0 or size != ((w << 32) | h) for _, _, draws, size in frames)):
        raise ValueError("The post-window trace used an unexpected GPU stream")
    return {"trace_witness": witness,
            "stages": cast["compare_gpu"](case, window, witness, frames[1:], legacy_pid)}


def summarize(root, opaque=False, legacy_pid=None):
    baseline = json.loads((HERE / "results/raid-native-5k-max-baseline.json").read_text())
    name = "early-opaque-5k" if opaque else "early-depth"
    artifacts = json.loads((root / name / "artifact-sha256.json").read_text())
    for key in ("runtime_unsigned_sha256", "runtime_signed_sha256"):
        if artifacts[key] != baseline["artifact_sha256"][key]:
            raise ValueError("The i386 runtime differs from the original 5K benchmark")
    diagnostic_dir = root / f"native-5k-{name}-diagnostic"
    diagnostic = read_run(diagnostic_dir, 1, 1, baseline, artifacts, 25, opaque)
    shader_log = read_shader_log(diagnostic_dir / "client.log", opaque)
    if not shader_log["requests_log_high_water_mark"] or not shader_log["shaders_compiled"]:
        raise ValueError("No live native 5K draws requested a compiled early-depth shader")
    if opaque and (shader_log["shader_fallbacks"] or shader_log["pipeline_fallbacks"]):
        raise ValueError("Some eligible native 5K draws fell back from opaque early depth")
    cases = [root / f"native-5k-{name}-{order}-{mode}" for order, mode in enumerate((0, 1, 1, 0), 1)]
    runs = [read_run(case, mode, 0, baseline, artifacts, 60, opaque) for case, mode in zip(cases, (0, 1, 1, 0))]
    result = {
        "viewport": baseline["viewport"],
        "multisample_count": baseline["multisample_count"],
        "verified_settings": baseline["graphics"]["actual"],
        "artifact_sha256": artifacts,
        "renderer_validation": test_witness(root / name, opaque),
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
    if legacy_pid is not None:
        result["post_window_gpu_profiles"] = {
            run["early_depth"]: read_post_window_gpu(case, run["gpu_window"], legacy_pid)
            for case, run in zip(cases[2:], runs[2:])
        }
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_root", type=Path)
    parser.add_argument("--opaque", action="store_true", help="verify the opaque ARB depth-write trial")
    parser.add_argument("--legacy-pid", type=int, help="verify the paired post-window GPU traces")
    args = parser.parse_args()
    print(json.dumps(summarize(args.capture_root, args.opaque, args.legacy_pid), indent=2))
