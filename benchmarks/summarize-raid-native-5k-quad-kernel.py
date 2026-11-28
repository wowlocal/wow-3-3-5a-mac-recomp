"""Verify the live rotated-quad comparison and native 5K GPU timing runs."""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
from runpy import run_path
from statistics import median


HERE = Path(__file__).resolve().parent
VERIFY = run_path(str(HERE / "verify-raid-native-5k.py"))["verify_renderer_mode"]
PROFILE = run_path(str(HERE / "summarize-raid-native-5k-profile.py"))
METRICS = ("average_completed_fps", "p99_gpu_completion_gap_ns")
COMPARISON = re.compile(r"^ocerz: native quad compare matched=(\d+) mismatched=(\d+) declined=(\d+)$", re.M)
ACTIVE = "ocerz: native rotated quad replacement active"


def read_case(case, mode, compare, baseline, artifacts, duration):
    report = VERIFY(case, baseline, artifacts, "color-only-msaa-mode.txt", 1, 0, measurement_seconds=duration)
    if (case / "quad-native-mode.txt").read_text().split() != [str(mode), str(compare)]:
        raise ValueError(f"Wrong quad kernel mode in {case.name}")
    log = (case / "client.log").read_text()
    counts = [tuple(map(int, row)) for row in COMPARISON.findall(log)]
    if bool(counts) != bool(compare) or ("ocerz: native quad mismatch " in log):
        raise ValueError(f"Wrong or failed original-code comparison in {case.name}")
    if (ACTIVE in log) != bool(mode and not compare):
        raise ValueError(f"Wrong quad kernel activation in {case.name}")
    result = {
        "mode": mode,
        "verified_cast_rows": report["verified_cast_rows"],
        "world_readback_sha256": report["world_readback_sha256"],
        "gpu_window": report["verified_gpu_window"],
    }
    if compare:
        matched, mismatched, declined = max(counts)
        if matched < 4096 or any(row[1] for row in counts):
            raise ValueError("The original rotated-quad path lacks a clean live comparison")
        result["last_logged_comparison"] = {"matched": matched, "mismatched": mismatched, "declined": declined}
    return result


def read_cpu_profile(case, window):
    witness = json.loads((case / "after-window-cpu-witness.json").read_text())
    pid = window["pid"]
    if (witness["attach_pid"] != pid or witness["requested_seconds"] != 8 or witness["returncode"]
            or not witness["trace_exists"] or
            not witness["last_verified_gpu_completion_ns"] < witness["begin_uptime_ns"] < witness["end_uptime_ns"]):
        raise ValueError(f"The post-window CPU trace is unverified in {case.name}")
    path = case / "time-profile.xml"
    profile = PROFILE["cpu_profile"](path, pid)
    if profile["in_process_sample_span_ns"] < 6_000_000_000:
        raise ValueError(f"The trial CPU profile is too short in {case.name}")
    leaves = Counter()
    columns = {"thread", "process", "thread-state", "weight", "stack"}
    cpu = PROFILE["CPU"]
    for row in cpu["read_table"](path, "time-profile", columns):
        if ((row["process"][0] or "").endswith(f" ({pid})") and row["thread-state"][1] == "Running"
                and cpu["is_main_thread"](row["thread"][0], pid)):
            stack = row["stack"]
            if any(image == "ocerz" and name == "m32_quad_native_boundary" for _, name, image in stack):
                _, function, image = stack[0]
                leaves[image or "Unattributed", function or "Unresolved"] += int(row["weight"][1])
    inclusive = sum(leaves.values())
    return {
        "trace_witness": witness,
        "process_sample_span_ns": profile["in_process_sample_span_ns"],
        "running_main_thread_ns": profile["main_sample_weight_ns_by_state"]["Running"],
        "native_boundary_inclusive_running_ns": inclusive,
        "native_boundary_running_share": inclusive / profile["main_sample_weight_ns_by_state"]["Running"],
        "native_boundary_leaves": [
            {"image": image, "symbol": symbol, "running_ns": weight}
            for (image, symbol), weight in leaves.most_common(10)
        ],
    }


def summarize(root, binary, compare_only):
    baseline = json.loads((HERE / "results/raid-native-5k-max-baseline.json").read_text())
    selected = json.loads((HERE / "results/raid-native-5k-guest-profile.json").read_text())
    output = root / "quad-native-live"
    artifacts = json.loads((output / "artifact-sha256.json").read_text())
    restored = json.loads((output / "restored-runtime.json").read_text())
    guest_hash = hashlib.sha256(binary.read_bytes()).hexdigest()
    if (guest_hash != selected["guest_image_sha256"]
            or artifacts["renderer_sha256"] != selected["artifact_sha256"]["renderer_sha256"]
            or restored != selected["restored_runtime_sha256"]):
        raise ValueError("The native quad comparison used a different guest, renderer or restored runtime")
    general = {}
    for mode in (0, 1):
        last_line = (output / f"general-{mode}.log").read_text().splitlines()[-1]
        count = re.fullmatch(r"test_m32: (\d+) checks, 0 failed", last_line)
        if not count:
            raise ValueError(f"The general i386 suite failed with quad mode {mode}")
        general[mode] = int(count[1])
    result = {
        "viewport": baseline["viewport"],
        "multisample_count": baseline["multisample_count"],
        "verified_settings": baseline["graphics"]["actual"],
        "artifact_sha256": artifacts,
        "restored_runtime_sha256": restored,
        "guest_image_sha256": guest_hash,
        "general_i386_checks": general,
        "diagnostic": read_case(root / "native-5k-quad-native-live-diagnostic", 1, 1, baseline, artifacts, 25),
    }
    if compare_only:
        return result
    cases = [root / f"native-5k-quad-native-live-{index}-{mode}" for index, mode in enumerate((0, 1, 1, 0), 1)]
    runs = [read_case(case, mode, 0, baseline, artifacts, 60) for case, mode in zip(cases, (0, 1, 1, 0))]
    result["runs_in_capture_order"] = runs
    result["medians_by_mode"] = {
        mode: {metric: median(run["gpu_window"][metric] for run in runs if run["mode"] == mode) for metric in METRICS}
        for mode in (0, 1)
    }
    result["paired_enabled_minus_disabled"] = [
        {metric: runs[enabled]["gpu_window"][metric] - runs[disabled]["gpu_window"][metric] for metric in METRICS}
        for disabled, enabled in ((0, 1), (3, 2))
    ]
    if all((case / "time-profile.xml").is_file() for case in cases[2:]):
        profiles = {run["mode"]: read_cpu_profile(case, run["gpu_window"])
                    for case, run in zip(cases[2:], runs[2:])}
        if profiles[0]["native_boundary_running_share"] or not profiles[1]["native_boundary_running_share"]:
            raise ValueError("The post-window CPU profiles disagree with replacement activation")
        result["post_window_cpu_profiles"] = profiles
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_root", type=Path)
    parser.add_argument("i386_executable", type=Path)
    parser.add_argument("--compare-only", action="store_true")
    args = parser.parse_args()
    print(json.dumps(summarize(args.capture_root, args.i386_executable, args.compare_only), indent=2))
