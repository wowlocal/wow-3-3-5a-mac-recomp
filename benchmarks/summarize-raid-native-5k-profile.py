"""Join the verified native 5K fixture with Metal and CPU profiles."""

import argparse
from collections import Counter
import json
from pathlib import Path
from runpy import run_path


HERE = Path(__file__).resolve().parent
VERIFY = run_path(str(HERE / "verify-raid-native-5k.py"))
METAL = run_path(str(HERE / "summarize-raid-deferred-tail.py"))
CPU = run_path(str(HERE / "summarize-raid-cpu-trace.py"))


def cpu_profile(path, pid):
    columns = {"time", "thread", "process", "thread-state", "weight", "stack"}
    states, images, symbols, activity = Counter(), Counter(), Counter(), Counter()
    first = last = None
    main = set()
    unresolved_under_guest_vm = 0
    for row in CPU["read_table"](path, "time-profile", columns):
        if not (row["process"][0] or "").endswith(f" ({pid})"):
            continue
        tick = int(row["time"][1])
        first = tick if first is None else min(first, tick)
        last = tick if last is None else max(last, tick)
        thread = row["thread"][0]
        state = row["thread-state"][1] or "Unknown"
        weight = int(row["weight"][1])
        if state == "Running":
            activity[thread] += weight
        if not CPU["is_main_thread"](thread, pid):
            continue
        main.add(thread)
        states[state] += weight
        if state != "Running" or not row["stack"]:
            continue
        _, name, image = row["stack"][0]
        images[image or "Unattributed"] += weight
        symbols[image or "Unattributed", name or "Unresolved"] += weight
        if not image and any(frame_image == "ocerz" and function in ("vm_call_core", "ocerz_jit_step")
                             for _, function, frame_image in row["stack"][1:]):
            unresolved_under_guest_vm += weight
    if len(main) != 1 or not states["Running"] or first is None or not first < last:
        raise ValueError("The trace lacks running samples for the native trial main thread")
    running = states["Running"]
    return {
        "in_process_sample_span_ns": last - first,
        "main_sample_weight_ns_by_state": dict(states.most_common()),
        "main_running_sample_weight_to_profile_span": running / (last - first),
        "unresolved_main_running_leaves_with_guest_vm_caller_ns": unresolved_under_guest_vm,
        "main_running_sample_weight_ns_by_leaf_image": dict(images.most_common(12)),
        "highest_weight_main_running_leaves": [
            {"image": image, "symbol": symbol, "weight_ns": weight, "running_share": weight / running}
            for (image, symbol), weight in symbols.most_common(15)
        ],
        "other_threads_running_sample_weight_ns": sum(value for thread, value in activity.items() if thread not in main),
    }


def verify_trace_witness(case, pid):
    witness = json.loads((case / "trace-witness.json").read_text())
    if witness.keys() != {"metal", "cpu"}:
        raise ValueError("Both diagnostic traces must be recorded")
    previous = 0
    for name, record in sorted(witness.items(), key=lambda item: item[1]["begin_uptime_ns"]):
        if (record["attach_pid"] != pid or record["returncode"] or not record["trace_exists"] or
                not previous <= record["begin_uptime_ns"] < record["end_uptime_ns"]):
            raise ValueError(f"Invalid {name} trace witness")
        previous = record["end_uptime_ns"]
    return witness


def summarize(case, legacy_pid):
    run = VERIFY["verify"](case, case / "graphics-settings.lua")
    baseline = json.loads((HERE / "results/raid-native-5k-max-baseline.json").read_text())
    if (run["graphics"]["issues"] or run["graphics"]["actual"] != baseline["graphics"]["actual"] or
            run["artifact_sha256"] != baseline["artifact_sha256"]):
        raise ValueError("The profile differs from the verified native 5K baseline")
    pid = run["verified_gpu_window"]["pid"]
    witness = verify_trace_witness(case, pid)
    stages = METAL["summarize_capture"](case, False, legacy_pid, include_worker_phases=False)
    mean_gpu_active = stages["metrics"]["client_gpu_active_ns"]["mean"]
    mean_output_gap = stages["metrics"]["completion_gap_ns"]["mean"]
    return {
        "diagnostic_run": run,
        "trace_witness": witness,
        "matched_metal_activity": {
            **stages, "client_gpu_active_fraction_of_joined_completion_gaps": mean_gpu_active / mean_output_gap,
        },
        "time_profiler": cpu_profile(case / "time-profile.xml", pid),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    parser.add_argument("--legacy-pid", type=int, required=True)
    args = parser.parse_args()
    print(json.dumps(summarize(args.capture, args.legacy_pid), indent=2))
