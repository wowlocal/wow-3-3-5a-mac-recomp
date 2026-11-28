"""Compare 1440p against its bracketing native 5K raid runs."""

import argparse
import json
from pathlib import Path
from runpy import run_path
from statistics import median


HERE = Path(__file__).resolve().parent
VERIFY = run_path(str(HERE / "verify-raid-native-5k.py"))["verify"]
CPU_PROFILE = run_path(str(HERE / "summarize-raid-native-5k-profile.py"))["cpu_profile"]
NATIVE_5K, NATIVE_1440P = (5120, 2880), (2560, 1440)
FPS, P99 = "average_completed_fps", "p99_gpu_completion_gap_ns"


def read_run(case, baseline, artifacts, dimensions):
    run = VERIFY(case, case / "graphics-settings.lua", dimensions=dimensions)
    expected = dict(baseline["graphics"]["actual"], gxResolution=f"{dimensions[0]}x{dimensions[1]}")
    if (run["graphics"]["issues"] or run["graphics"]["actual"] != expected
            or run["artifact_sha256"] != artifacts
            or (case / "color-only-msaa-mode.txt").read_text().split() != ["0", "0"]
            or (case / "pass-mode.txt").read_text().split() != ["2048", "0"]
            or (case / "render-end-mode.txt").read_text().split() != ["deferred", "0"]
            or (case / "submissions.ns").exists()):
        raise ValueError(f"{case.name} changed more than the native renderer dimensions")
    return run


def check_cpu_profile(case, window):
    witness = json.loads((case / "after-window-cpu-witness.json").read_text())
    pid = window["pid"]
    sequence = window["last_sequence"]
    endings = []
    with (case / "gpu.ns").open() as source:
        for line in source:
            row = line.split()
            if len(row) == 6 and tuple(map(int, row[:3])) == (pid, window["stream"], sequence):
                endings.append(int(row[3]))
    if (witness["pid"] != pid or witness["requested_seconds"] != 8
            or endings != [witness["last_verified_gpu_completion_ns"]]
            or not endings[0] < witness["begin_uptime_ns"] < witness["end_uptime_ns"]):
        raise ValueError("The 5K CPU profile does not follow the verified timing window")
    return {"witness": witness, "profile": CPU_PROFILE(case / "after-window-cpu.xml", pid)}


def summarize(root):
    baseline = json.loads((HERE / "results/raid-native-5k-max-baseline.json").read_text())
    artifacts = json.loads((root / "color-only-msaa/artifact-sha256.json").read_text())
    if any(artifacts[key] != baseline["artifact_sha256"][key]
           for key in ("runtime_unsigned_sha256", "runtime_signed_sha256")):
        raise ValueError("The i386 runtime differs from the original 5K benchmark")
    restoration = json.loads((root / "resolution-diagnostic-preferences/restoration.json").read_text())
    if ({item["filename"] for item in restoration} != {"Config.wtf", "com.blizzard.World of Warcraft.prefs"}
            or any(item["original_sha256"] != item["restored_sha256"] for item in restoration)):
        raise ValueError("The original cloned preferences were not restored")
    runs = [
        read_run(root / case, baseline, artifacts, dimensions)
        for case, dimensions in (
            ("native-5k-color-only-msaa-4-0", NATIVE_5K),
            ("resolution-diagnostic-1440p", NATIVE_1440P),
            ("resolution-diagnostic-restored-5k", NATIVE_5K),
        )
    ]
    before, diagnostic, restored = [run["verified_gpu_window"] for run in runs]
    native = {metric: median((before[metric], restored[metric])) for metric in (FPS, P99)}
    return {
        "scope": "diagnostic resolution comparison",
        "native_5k_before": runs[0],
        "native_1440p": runs[1],
        "native_5k_restored": runs[2],
        "native_5k_post_window_cpu_profile": check_cpu_profile(root / "resolution-diagnostic-restored-5k", restored),
        "cloned_preferences_restored": restoration,
        "bracketing_5k_median": native,
        "native_1440p_change_from_5k": {metric: diagnostic[metric] - native[metric] for metric in (FPS, P99)},
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_root", type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize(args.capture_root), indent=2))
