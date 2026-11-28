"""Verify concurrent original scene updates and compare full raid timings."""

import argparse
import json
from pathlib import Path
from runpy import run_path
from statistics import median


HERE = Path(__file__).resolve().parent
ARGS = run_path(str(HERE / "summarize-raid-scene-args.py"))
COMMON = ARGS["COMMON"]
FPS, P99 = COMMON["FPS"], COMMON["P99"]
COUNTER_FILE = "scene-workers.bin"
FIELDS = (
    "update_calls", "queued_updates", "fallback_calls", "batches", "multi_item_batches",
    "updates_on_worker_threads", "updates_on_producer_thread", "largest_batch",
    "batches_with_overlapping_participants", "producer_wait_and_audit_ns", "summed_participant_elapsed_ns",
    "serial_replay_batches", "serial_replay_bytes_compared", "serial_replay_mismatches",
    "changes_to_observed_traversal_fields",
)
EXPECTED = {
    **ARGS["EXPECTED"],
    0x980620: ("movl", "0x44(%ebx), %eax"),
    0x980625: ("je", "0x9806b8"),
    0x98062B: ("movl", "0x44(%eax), %ebx"),
    0x980630: ("je", "0x9806b8"),
    0x980636: ("movl", "0x48(%ebx), %edi"),
    0x98063B: ("jne", "0x980620"),
    0x98063D: ("testb", "$0x10, 0x11(%ebx)"),
    0x980641: ("jne", "0x9808bd"),
    0x9806B8: ("movl", "0x8(%ebp), %edx"),
    0x9808BD: ("leal", "-0xb4(%ebp), %eax"),
}


def checked_counters(values, sampled, replay):
    counts = dict(zip(FIELDS, values))
    retired = counts["updates_on_worker_threads"] + counts["updates_on_producer_thread"]
    if (abs(counts["queued_updates"] - retired) > 24
            or abs(counts["update_calls"] - counts["queued_updates"] - counts["fallback_calls"]) > 1
            or not 1000 <= counts["updates_on_worker_threads"] <= retired
            or not 100 <= counts["batches_with_overlapping_participants"] <= counts["multi_item_batches"] <= counts["batches"]
            or counts["serial_replay_mismatches"] or counts["changes_to_observed_traversal_fields"]):
        raise ValueError(f"Invalid worker retirement, concurrency or replay counters {counts}")
    if replay:
        if (counts["serial_replay_bytes_compared"] <= 0 or
                abs(counts["serial_replay_batches"] - counts["batches"]) > 1):
            raise ValueError("Some diagnostic batches lack their serial result comparison")
    elif counts["serial_replay_batches"] or counts["serial_replay_bytes_compared"]:
        raise ValueError("The wait diagnostic included serial replay")
    if sampled:
        del counts["largest_batch"]
    elif not 2 <= counts["largest_batch"] <= 12:
        raise ValueError("Invalid batch capacity witness")
    counts["worker_fraction_of_retired_updates"] = counts["updates_on_worker_threads"] / retired
    return counts


def diagnostic_report(folder, artifacts, replay):
    window = COMMON["verify_run"](folder, 1, 1, 25, artifacts, "scene-workers-mode.txt",
                                  counter_file=COUNTER_FILE, render_logging=1)
    bracket, sampled, total = COMMON["descriptor_counter_samples"](
        folder, slots=len(FIELDS), counter_file=COUNTER_FILE, gate_file="scene-workers-gate.json")
    if not bracket["samples_inside_nominal_gpu_window"]:
        raise ValueError("The worker samples must fall inside the confirmed raid")
    return {
        "verified_gpu_window": window,
        **bracket,
        "counters_between_samples": checked_counters(sampled, True, replay),
        "whole_process_counters": checked_counters(total, False, replay),
    }


def summarize(root, guest):
    sha = COMMON["sha"]
    prior = json.loads((HERE / "results/raid-aarchx-scene-argument-packet-ab.json").read_text())
    artifacts = json.loads((root / "artifact-sha256.json").read_text())
    guest_hash = sha(guest)
    if (guest_hash != prior["guest_image_sha256"] or
            sha(root / "artifacts/candidate/ocerz") != artifacts["runtime_unsigned_sha256"] or
            sha(root / "artifacts/candidate/libwinemetalgl.dylib") != artifacts["renderer_sha256"] or
            artifacts["renderer_sha256"] != prior["artifact_sha256"]["renderer_sha256"]):
        raise ValueError("Unexpected guest image or trial binaries")
    disassemble = ARGS["FOOTPRINT"]["SCENE"]["PHASE"]["GUEST"]
    decoded = {pc: (op, operands.strip()) for pc, (op, operands) in disassemble(guest, EXPECTED).items()}
    if decoded != EXPECTED:
        raise ValueError("Original scene enumeration or update boundaries differ")
    for mode in ("control", "candidate"):
        if not (root / f"general-{mode}.log").read_text().endswith("test_m32: 812 checks, 0 failed\n"):
            raise ValueError(f"The general i386 checks failed in {mode}")

    verify = COMMON["verify_run"]
    replay = diagnostic_report(root / "diagnostic-1", artifacts, True)
    drain = diagnostic_report(root / "diagnostic-drain", artifacts, False)
    waits = drain["counters_between_samples"]
    drain["mean_producer_drain_ns_per_bracketed_output"] = (
        waits["producer_wait_and_audit_ns"] / drain["gpu_completions_between_counter_samples"]
    )
    drain["mean_producer_drain_ns_per_batch"] = waits["producer_wait_and_audit_ns"] / waits["batches"]
    runs = [
        {"order": order, "scene_workers": bool(mode),
         **verify(root / f"timed-{order}-{mode}", mode, 0, 60, artifacts, "scene-workers-mode.txt",
                  counter_file=COUNTER_FILE)}
        for order, mode in enumerate((0, 1, 1, 0), 1)
    ]
    comparison = {}
    for mode, label in enumerate(("serial", "scene_workers")):
        selected = [run for run in runs if run["scene_workers"] == bool(mode)]
        comparison[label] = {key: median(run[key] for run in selected) for key in (FPS, P99)}
    return {
        "artifact_sha256": artifacts,
        "guest_image_sha256": guest_hash,
        "general_i386_checks_per_mode": 812,
        "diagnostic": {"serial_replay": replay, "drain_timing": drain},
        "timed_runs": runs,
        "comparison": comparison,
        "paired_worker_minus_serial": [
            {key: runs[w][key] - runs[s][key] for key in (FPS, P99)}
            for s, w in ((0, 1), (3, 2))
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_root", type=Path)
    parser.add_argument("guest_binary", type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize(args.capture_root, args.guest_binary), indent=2))
