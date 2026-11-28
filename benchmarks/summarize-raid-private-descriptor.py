"""Verify real private descriptor reads and time the dual-read safety path."""

import argparse
import json
from pathlib import Path
from runpy import run_path
from statistics import median
import struct


HERE = Path(__file__).resolve().parent
SPLIT = run_path(str(HERE / "summarize-raid-triangle-split.py"))
READ_WINDOW = SPLIT["READ_WINDOW"]
LIFE = SPLIT["LIFE"]
sha = SPLIT["sha"]
READERS = {
    0x9791BF: ("cmpl", "0x5c(%edi), %ecx"),
    0x979523: ("cmpl", "0x64(%edi), %eax"),
    0x979532: ("movl", "0x94(%edi), %edx"),
    0x979553: ("cmpl", "0x6c(%edi), %edx"),
}
MARKERS = {
    0x97D31D: ("movl", "%edx, 0x50(%eax)"),
    0x97D331: ("movl", "%edx, 0x68(%ecx)"),
    0x97D3FE: ("movl", "%eax, 0x9c(%edx)"),
    0x97D404: ("incl", "%ebx"),
}
FPS = "average_completed_fps"
P99 = "p99_gpu_completion_gap_ns"


def verify_run(folder, mode, logging, seconds, artifacts, mode_file="raid-descriptor-mode.txt"):
    settings = {
        mode_file: [str(mode), str(logging)],
        "pgo-mode.txt": ["candidate", "0"],
        "map-shadow-mode.txt": ["1", "0"],
        "pass-mode.txt": ["64", str(logging)],
        "render-end-mode.txt": ["deferred", "0"],
    }
    for name, expected in settings.items():
        if (folder / name).read_text().split() != expected:
            raise ValueError(f"Unexpected diagnostic mode in {folder / name}")
    if ("/artifacts/candidate/libwinemetalgl.dylib" not in (folder / "renderer-map.txt").read_text() or
            json.loads((folder / "artifact-sha256.json").read_text()) != artifacts or
            json.loads((folder / "legacy.json").read_text()) != {
                "legacy_paused": True, "legacy_completed_frames_during_launch": 0,
            } or any((folder / name).is_file() != bool(logging) for name in ("submissions.ns", "raid-descriptor.bin"))):
        raise ValueError(f"Different runtime, renderer or isolation in {folder}")
    return READ_WINDOW(folder, seconds)


def describe_counters(counters):
    readers = []
    for i, pc in enumerate(READERS):
        used, different, absent = counters[4 + 3 * i:7 + 3 * i]
        if not used:
            raise ValueError(f"The diagnostic never supplied a private value at {pc:#x}")
        readers.append({"pc": hex(pc), "matches_used": used, "value_mismatches": different,
                        "original_loads_without_a_usable_descriptor": absent})
    return {
        "dispatch_entries": counters[0],
        "input_descriptors_created": counters[1],
        "descriptors_with_the_original_prefix_word": counters[2],
        "descriptors_published_in_original_item_order": counters[3],
        "readers": readers,
    }


def descriptor_counter_samples(folder, slots=16):
    all_counts = struct.unpack(f"<{slots}Q", (folder / "raid-descriptor.bin").read_bytes())
    gate = json.loads((folder / "descriptor-gate.json").read_text())
    start, end = gate["start"], gate["end"]
    window = json.loads((folder / "window.json").read_text())
    by_end, _ = LIFE["render_submissions"](folder / "submissions.ns", window["pid"])
    source_frames, _ = LIFE["read_frames"](folder, window, by_end, False)
    begin, finish = start["before_uptime_ns"], end["after_uptime_ns"]
    if not source_frames[0].final.end < begin <= start["after_uptime_ns"] < end["before_uptime_ns"] <= finish:
        raise ValueError("Invalid uptime clock order for the warmed counter samples")
    # A diagnostic process continues to render the fixed fixture after its
    # nominal timing cutoff. Independently verify the entire bracket if the
    # counter samples extend past that cutoff.
    outputs = sorted(
        (tick, sequence) for pid, stream, sequence, tick, _, _ in
        (tuple(map(int, row)) for row in LIFE["rows"](folder / "gpu.ns", 6))
        if (pid, stream) == (window["pid"], window["stream"])
    )
    before = [row for row in outputs if row[0] < begin]
    after = [row for row in outputs if row[0] > finish]
    if not before or not after:
        raise ValueError("The counter samples lack completed render brackets")
    bracket = {**window, "first_sequence": before[-1][1], "last_sequence": after[0][1]}
    frames, _ = LIFE["read_frames"](folder, bracket, by_end, False)
    if (len(frames) < 3 or frames[0].final.end != before[-1][0] or frames[-1].final.end != after[0][0] or
            any(a.final.end >= b.final.end for a, b in zip(frames, frames[1:]))):
        raise ValueError("The counter bracket is not one consecutive series of full raid outputs")
    first_counts, last_counts = start["counters"], end["counters"]
    if (len(first_counts) != slots or len(last_counts) != slots or
            any(not 0 <= left <= right <= total for left, right, total in zip(first_counts, last_counts, all_counts))):
        raise ValueError("Descriptor counters are not monotonic")
    counted_frames = sum(start["after_uptime_ns"] < frame.final.end < end["before_uptime_ns"] for frame in frames)
    if counted_frames < 2:
        raise ValueError("The descriptor samples do not span a warmed sequence of outputs")
    return ({
        "gpu_completions_between_counter_samples": counted_frames,
        "samples_inside_nominal_gpu_window": finish < source_frames[-1].final.end,
        "counter_sample_interval_ns": end["before_uptime_ns"] - start["after_uptime_ns"],
    }, [b - a for a, b in zip(first_counts, last_counts)], all_counts)


def measure_descriptor_gate(folder):
    witness, scoped_counts, all_counts = descriptor_counter_samples(folder)
    return {
        **witness,
        "counters_between_samples": describe_counters(scoped_counts),
        "whole_process_counters": describe_counters(all_counts),
    }


def summarize(root, guest):
    prior = json.loads((HERE / "results/raid-aarchx-context-values.json").read_text())
    accepted = json.loads((HERE / "results/raid-winemetalgl-defer-render-end-pgo-ab.json").read_text())
    artifacts = json.loads((root / "artifact-sha256.json").read_text())
    guest_hash = sha(guest)
    if (guest_hash != prior["guest_image_sha256"] or
            sha(root / "artifacts/candidate/ocerz") != artifacts["runtime_unsigned_sha256"] or
            sha(root / "artifacts/candidate/libwinemetalgl.dylib") != artifacts["renderer_sha256"] or
            artifacts["renderer_sha256"] != accepted["artifact_sha256"]["renderer_sha256_by_role"]["candidate"]):
        raise ValueError("Wrong original i386 executable or test artifacts")
    for name in ("control", "candidate"):
        if not (root / f"general-{name}.log").read_text().endswith("test_m32: 805 checks, 0 failed\n"):
            raise ValueError(f"General i386 checks failed in {name}")
    guest_disassembly = run_path(str(HERE / "summarize-raid-guest-native-stacks.py"))["guest_instructions"]
    expected = {**READERS, **MARKERS}
    instructions = guest_disassembly(guest, expected.keys())
    if {pc: (op, operands.strip()) for pc, (op, operands) in instructions.items()} != expected:
        raise ValueError("The descriptor reads or dispatcher boundaries differ")
    folder = root / "diagnostic-1"
    diagnosis = {
        "verified_gpu_window": verify_run(folder, 1, 1, 25, artifacts),
        **measure_descriptor_gate(folder),
    }
    runs = [
        {"order": i, "verified_private_read": bool(mode), **verify_run(root / f"timed-{i}-{mode}", mode, 0, 60, artifacts)}
        for i, mode in enumerate((0, 1, 1, 0), 1)
    ]
    comparison = {}
    for mode, name in enumerate(("original_load", "private_descriptor_with_equality_gate")):
        samples = [run for run in runs if run["verified_private_read"] == mode]
        comparison[name] = {"median_gpu_completed_fps": median(run[FPS] for run in samples),
                            "median_p99_completion_gap_ns": median(run[P99] for run in samples)}
    return {
        "artifact_sha256": artifacts, "guest_image_sha256": guest_hash, "general_i386_checks_per_mode": 805,
        "diagnostic": diagnosis, "timed_runs": runs, "comparison": comparison,
        "paired_checked_descriptor_minus_original": [
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
