"""Verify owned worker inputs reaching the original live renderer readers."""

import argparse
import json
from pathlib import Path
from runpy import run_path
from statistics import median


HERE = Path(__file__).resolve().parent
DESCRIPTOR = run_path(str(HERE / "summarize-raid-private-descriptor.py"))
sha = DESCRIPTOR["sha"]
FPS, P99 = DESCRIPTOR["FPS"], DESCRIPTOR["P99"]
WORKER_COUNTS = (
    "dispatch_batches_seen", "batches_queued", "batches_completed", "owned_input_records",
    "worker_descriptors_selected", "serial_descriptor_fallbacks", "prefix_value_mismatches",
    "busy_worker_fallbacks", "producer_snapshot_elapsed_ns", "worker_execution_elapsed_ns",
    "original_readers_using_worker_values", "snapshot_pages_read", "snapshot_page_failures",
)
EXPECTED = {
    **DESCRIPTOR["READERS"],
    **DESCRIPTOR["MARKERS"],
    0x97D2E1: ("xorl", "%ebx, %ebx"),
    0x97D2F0: ("movl", "0x14(%ebp), %edx"),
    0x97D2F3: ("movl", "0x10(%ebp), %ecx"),
    0x97D2F9: ("movl", "(%esi), %eax"),
    0x97D300: ("leal", "(%edx,%eax,4), %eax"),
    0x97D303: ("leal", "(%eax,%ecx), %edx"),
}


def report_counters(values):
    worker = dict(zip(WORKER_COUNTS, values[16:]))
    checked = DESCRIPTOR["describe_counters"](values[:16])
    checked["serial_input_descriptors_created"] = checked.pop("input_descriptors_created")
    if not all(worker[name] > 0 for name in (
        "batches_queued", "batches_completed", "worker_descriptors_selected", "original_readers_using_worker_values",
    )):
        raise ValueError("No actual worker values reached the original dispatcher readers")
    used = sum(row["matches_used"] for row in checked["readers"])
    if not 0 < worker["original_readers_using_worker_values"] <= used:
        raise ValueError("Worker-originated reads exceed the validated descriptor loads")
    if worker["prefix_value_mismatches"] > worker["worker_descriptors_selected"]:
        raise ValueError("More prefix mismatches than selected worker descriptors")
    return {
        **checked,
        "worker": worker,
        "worker_fraction_of_successful_private_reads": worker["original_readers_using_worker_values"] / used,
        "mean_snapshot_ns_per_queued_batch": worker["producer_snapshot_elapsed_ns"] / worker["batches_queued"],
        "mean_worker_ns_per_completed_batch": worker["worker_execution_elapsed_ns"] / worker["batches_completed"],
    }


def summarize(root, guest):
    previous = json.loads((HERE / "results/raid-aarchx-private-descriptor-ab.json").read_text())
    artifacts = json.loads((root / "artifact-sha256.json").read_text())
    digest = sha(guest)
    if (digest != previous["guest_image_sha256"] or
            sha(root / "artifacts/candidate/ocerz") != artifacts["runtime_unsigned_sha256"] or
            sha(root / "artifacts/candidate/libwinemetalgl.dylib") != artifacts["renderer_sha256"] or
            artifacts["renderer_sha256"] != previous["artifact_sha256"]["renderer_sha256"]):
        raise ValueError("Wrong WoW executable or renderer for the worker comparison")
    for mode in ("control", "candidate"):
        if not (root / f"general-{mode}.log").read_text().endswith("test_m32: 805 checks, 0 failed\n"):
            raise ValueError(f"General i386 checks failed for {mode}")
    disassemble = run_path(str(HERE / "summarize-raid-guest-native-stacks.py"))["guest_instructions"]
    decoded = disassemble(guest, EXPECTED.keys())
    if {pc: (op, args.strip()) for pc, (op, args) in decoded.items()} != EXPECTED:
        raise ValueError("The original item indexing or descriptor reads differ")
    verify = DESCRIPTOR["verify_run"]
    probe = root / "diagnostic-1"
    gate, scoped, total = DESCRIPTOR["descriptor_counter_samples"](probe, slots=32)
    scoped_report = report_counters(scoped)
    scoped_report["mean_producer_snapshot_ns_per_bracketed_gpu_output"] = (
        scoped_report["worker"]["producer_snapshot_elapsed_ns"] / gate["gpu_completions_between_counter_samples"]
    )
    diagnosis = {
        "verified_gpu_window": verify(probe, 1, 1, 25, artifacts, "raid-worker-mode.txt"),
        **gate, "counters_between_samples": scoped_report,
        "whole_process_counters": report_counters(total),
    }
    runs = [
        {"order": index, "uses_worker": bool(mode),
         **verify(root / f"timed-{index}-{mode}", mode, 0, 60, artifacts, "raid-worker-mode.txt")}
        for index, mode in enumerate((0, 1, 1, 0), 1)
    ]
    comparison = {}
    for mode, name in enumerate(("serial_descriptor", "worker_descriptor")):
        samples = [run for run in runs if run["uses_worker"] == mode]
        comparison[name] = {"median_gpu_completed_fps": median(run[FPS] for run in samples),
                            "median_p99_completion_gap_ns": median(run[P99] for run in samples)}
    return {
        "artifact_sha256": artifacts, "guest_image_sha256": digest, "general_i386_checks_per_mode": 805,
        "diagnostic": diagnosis, "timed_runs": runs, "comparison": comparison,
        "paired_worker_minus_serial": [
            {"gpu_completed_fps": runs[t][FPS] - runs[c][FPS], "p99_completion_gap_ns": runs[t][P99] - runs[c][P99]}
            for c, t in ((0, 1), (3, 2))
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_root", type=Path)
    parser.add_argument("guest_binary", type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize(args.capture_root, args.guest_binary), indent=2))
