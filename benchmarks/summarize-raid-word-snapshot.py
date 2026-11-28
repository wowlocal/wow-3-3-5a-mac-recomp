"""Compare page copies and guarded word capture for the live descriptor worker."""

import argparse
import json
from pathlib import Path
from runpy import run_path
from statistics import median


HERE = Path(__file__).resolve().parent
WORKER = run_path(str(HERE / "summarize-raid-descriptor-worker.py"))
DESCRIPTOR = WORKER["DESCRIPTOR"]
sha = WORKER["sha"]
FPS, P99 = WORKER["FPS"], WORKER["P99"]
TAGS = ("mach_page_copy", "guarded_words")


def capture_report(values, mode):
    report = WORKER["report_counters"](values)
    busy, readable_pages, denied = values[29:32]
    copied = report["worker"]["snapshot_pages_read"]
    if bool(readable_pages) != bool(mode) or bool(copied) == bool(mode):
        raise ValueError("The observed guest-memory reads differ from the requested snapshot mode")
    report["guarded_capture"] = {
        "mapping_guard_busy": busy, "readable_guest_pages_consulted": readable_pages,
        "guest_page_checks_rejected": denied,
    }
    verified = sum(row["matches_used"] for row in report["readers"])
    mismatches = sum(row["value_mismatches"] for row in report["readers"])
    unavailable = sum(row["original_loads_without_a_usable_descriptor"] for row in report["readers"])
    report["worker_fraction_of_original_read_sites"] = (
        report["worker"]["original_readers_using_worker_values"] / (verified + mismatches + unavailable)
    )
    return report


def summarize(root, guest):
    previous = json.loads((HERE / "results/raid-aarchx-descriptor-worker-ab.json").read_text())
    artifacts = json.loads((root / "artifact-sha256.json").read_text())
    digest = sha(guest)
    if (digest != previous["guest_image_sha256"] or
            sha(root / "artifacts/candidate/ocerz") != artifacts["runtime_unsigned_sha256"] or
            sha(root / "artifacts/candidate/libwinemetalgl.dylib") != artifacts["renderer_sha256"] or
            artifacts["renderer_sha256"] != previous["artifact_sha256"]["renderer_sha256"]):
        raise ValueError("Wrong WoW executable or renderer for the snapshot comparison")
    for mode in ("control", "candidate"):
        if not (root / f"general-{mode}.log").read_text().endswith("test_m32: 812 checks, 0 failed\n"):
            raise ValueError(f"i386 or guarded snapshot tests failed in {mode}")
    disassemble = run_path(str(HERE / "summarize-raid-guest-native-stacks.py"))["guest_instructions"]
    instructions = disassemble(guest, WORKER["EXPECTED"].keys())
    if {pc: (op, text.strip()) for pc, (op, text) in instructions.items()} != WORKER["EXPECTED"]:
        raise ValueError("Original i386 worker or dispatcher checkpoints changed")

    verify = DESCRIPTOR["verify_run"]
    diagnostics = {}
    for mode, label in enumerate(TAGS):
        folder = root / f"diagnostic-{mode}"
        window = verify(folder, mode, 1, 25, artifacts, "raid-snapshot-mode.txt")
        gate, scoped, total = DESCRIPTOR["descriptor_counter_samples"](folder, slots=32)
        if not gate["samples_inside_nominal_gpu_window"]:
            raise ValueError("The scope samples must fall inside the matched live raid window")
        counts = capture_report(scoped, mode)
        counts["mean_producer_snapshot_ns_per_bracketed_gpu_output"] = (
            counts["worker"]["producer_snapshot_elapsed_ns"] / gate["gpu_completions_between_counter_samples"]
        )
        diagnostics[label] = {
            "verified_gpu_window": window, **gate, "counters_between_samples": counts,
            "whole_process_counters": capture_report(total, mode),
        }

    runs = [
        {"order": order, "guarded_words": bool(mode),
         **verify(root / f"timed-{order}-{mode}", mode, 0, 60, artifacts, "raid-snapshot-mode.txt")}
        for order, mode in enumerate((0, 1, 1, 0), 1)
    ]
    comparison = {}
    for mode, name in enumerate(TAGS):
        samples = [run for run in runs if run["guarded_words"] == mode]
        comparison[name] = {"median_gpu_completed_fps": median(run[FPS] for run in samples),
                            "median_p99_completion_gap_ns": median(run[P99] for run in samples)}
    return {
        "artifact_sha256": artifacts, "guest_image_sha256": digest, "i386_checks_per_mode": 812,
        "diagnostics": diagnostics, "timed_runs": runs, "comparison": comparison,
        "paired_guarded_minus_mach": [
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
