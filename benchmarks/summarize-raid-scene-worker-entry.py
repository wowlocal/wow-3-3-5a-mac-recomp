"""Measure one i386 entry per worker participant instead of one per update."""

import argparse
import json
from pathlib import Path
from runpy import run_path
from statistics import median


HERE = Path(__file__).resolve().parent
WORKERS = run_path(str(HERE / "summarize-raid-scene-workers.py"))
COMMON = WORKERS["COMMON"]
FPS, P99 = COMMON["FPS"], COMMON["P99"]
COUNTER_FILE = WORKERS["COUNTER_FILE"]
MODE_FILE = "scene-worker-entry-mode.txt"


def check_counts(values, sampled, replay, reuse):
    counts = WORKERS["checked_counters"](values[:15], sampled, replay)
    entries, updates, waves = values[15:]
    retired = counts["updates_on_worker_threads"] + counts["updates_on_producer_thread"]
    slack = 24 if sampled else 12
    if not 0 < entries <= retired + slack:
        raise ValueError("Guest entries do not match retired updates")
    if reuse:
        loop_entries = entries - (retired - updates)
        if (not 100 < waves <= counts["multi_item_batches"] + 1 or
                not 4 * waves - slack <= updates <= 12 * waves + slack or
                not waves - slack <= loop_entries <= 3 * waves + slack or entries >= retired):
            raise ValueError("The native worker entry was not reused for the recorded updates")
    elif updates or waves or abs(entries - retired) > slack:
        raise ValueError("The per-update control used a reused guest entry")
    return {
        **counts, "guest_entries": entries, "updates_using_guest_loop": updates,
        "waves_using_guest_loop": waves, "guest_entries_per_retired_update": entries / retired,
    }


def diagnostic_report(folder, artifacts, replay, reuse):
    window = COMMON["verify_run"](folder, int(reuse), 1, 25, artifacts, MODE_FILE,
                                  counter_file=COUNTER_FILE, render_logging=1)
    bracket, sampled, total = COMMON["descriptor_counter_samples"](
        folder, slots=18, counter_file=COUNTER_FILE, gate_file="scene-workers-gate.json")
    if not bracket["samples_inside_nominal_gpu_window"]:
        raise ValueError("The worker entry samples must fall inside the confirmed raid")
    counters = check_counts(sampled, True, replay, reuse)
    frames = bracket["gpu_completions_between_counter_samples"]
    return {
        "verified_gpu_window": window,
        **bracket,
        "counters_between_samples": counters,
        "whole_process_counters": check_counts(total, False, replay, reuse),
        "mean_guest_entries_per_bracketed_output": counters["guest_entries"] / frames,
        "mean_producer_drain_ns_per_bracketed_output": counters["producer_wait_and_audit_ns"] / frames,
        "mean_producer_drain_ns_per_batch": counters["producer_wait_and_audit_ns"] / counters["batches"],
    }


def completion_cadence(folder):
    window = json.loads((folder / "window.json").read_text())
    counts = [0] * 6
    first_tick = None
    draw_sum = 0
    for row in COMMON["LIFE"]["rows"](folder / "gpu.ns", 6):
        pid, stream, sequence, tick, draws, _ = map(int, row)
        if ((pid, stream) != (window["pid"], window["stream"]) or
                not window["first_sequence"] <= sequence <= window["last_sequence"]):
            continue
        if first_tick is None:
            first_tick = tick
        interval = (tick - first_tick) // 10_000_000_000
        if not 0 <= interval < len(counts):
            raise ValueError("The counted GPU frame is outside the 60-second window")
        counts[interval] += 1
        draw_sum += draws
    if sum(counts) != window["verified_completed_renders"]:
        raise ValueError("GPU completion cadence disagrees with the verified window")
    return {
        "completions_per_10s_from_first_record": counts,
        "mean_draws_per_completed_output": draw_sum / sum(counts),
    }


def summarize(root, guest):
    sha = COMMON["sha"]
    previous = json.loads((HERE / "results/raid-aarchx-scene-worker-ab.json").read_text())
    artifacts = json.loads((root / "artifact-sha256.json").read_text())
    guest_hash = sha(guest)
    if (guest_hash != previous["guest_image_sha256"] or
            sha(root / "artifacts/candidate/ocerz") != artifacts["runtime_unsigned_sha256"] or
            sha(root / "artifacts/candidate/libwinemetalgl.dylib") != artifacts["renderer_sha256"] or
            artifacts["renderer_sha256"] != previous["artifact_sha256"]["renderer_sha256"]):
        raise ValueError("Unexpected guest image or trial binaries")
    disassemble = WORKERS["ARGS"]["FOOTPRINT"]["SCENE"]["PHASE"]["GUEST"]
    expected = WORKERS["EXPECTED"]
    decoded = {pc: (op, operands.strip()) for pc, (op, operands) in disassemble(guest, expected).items()}
    if decoded != expected:
        raise ValueError("Original scene enumeration or worker update boundaries differ")
    for mode in ("control", "candidate"):
        if not (root / f"general-{mode}.log").read_text().endswith("test_m32: 812 checks, 0 failed\n"):
            raise ValueError(f"The general i386 checks failed in {mode}")
    diagnostics = {
        "serial_replay": diagnostic_report(root / "diagnostic-1", artifacts, True, True),
        "drain_per_update_entry": diagnostic_report(root / "diagnostic-drain-0", artifacts, False, False),
        "drain_reuse_entry": diagnostic_report(root / "diagnostic-drain-1", artifacts, False, True),
    }
    runs = []
    for order, mode in enumerate((0, 1, 1, 0, 1, 0), 1):
        folder = root / f"timed-{order}-{mode}"
        verified = COMMON["verify_run"](folder, mode, 0, 60, artifacts, MODE_FILE, counter_file=COUNTER_FILE)
        runs.append({"order": order, "reuse_entry": bool(mode), **verified, **completion_cadence(folder)})
    comparisons = {}
    for mode, label in enumerate(("per_update_entry", "reuse_entry")):
        selected = [run for run in runs if run["reuse_entry"] == bool(mode)]
        comparisons[label] = {key: median(run[key] for run in selected) for key in (FPS, P99)}
    return {
        "artifact_sha256": artifacts,
        "guest_image_sha256": guest_hash,
        "general_i386_checks_per_mode": 812,
        "diagnostic": diagnostics,
        "timed_runs": runs,
        "comparison": comparisons,
        "paired_reuse_minus_per_update": [
            {key: runs[t][key] - runs[c][key] for key in (FPS, P99)}
            for c, t in ((0, 1), (3, 2), (5, 4))
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_root", type=Path)
    parser.add_argument("guest_binary", type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize(args.capture_root, args.guest_binary), indent=2))
