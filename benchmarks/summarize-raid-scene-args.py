"""Validate the original i386 update call with a private argument packet."""

import argparse
import json
from pathlib import Path
from runpy import run_path
from statistics import median


HERE = Path(__file__).resolve().parent
COMMON = run_path(str(HERE / "summarize-raid-private-descriptor.py"))
FOOTPRINT = run_path(str(HERE / "summarize-raid-scene-footprints.py"))
COUNTER_FILE = "scene-args.bin"
COUNTER_FIELDS = ("entries", "packed", "fallback", "returned", "unexpected_stack", "packet_mismatch")
FPS, P99 = COMMON["FPS"], COMMON["P99"]
EXPECTED = {
    **FOOTPRINT["ARGUMENT_SETUP"],
    0x9806AE: ("calll", "0x964ca0"),
    0x9806B3: ("jmp", "0x980620"),
    0x965B26: ("movl", "0x10(%ebp), %ebx"),
    0x965B4C: ("mulss", "0x4(%ebx), %xmm2"),
    0x965B51: ("mulss", "0x8(%ebx), %xmm0"),
    0x965B56: ("mulss", "(%ebx), %xmm1"),
    0x965BCD: ("movl", "0x14(%ebp), %ecx"),
    0x965BE0: ("addss", "(%ecx), %xmm0"),
    0x965BF1: ("addss", "0x4(%ecx), %xmm0"),
    0x965C00: ("addss", "0x8(%ecx), %xmm0"),
}


def validate_counters(values):
    result = dict(zip(COUNTER_FIELDS, values))
    if (result["packed"] < 1000 or abs(result["entries"] - result["packed"] - result["fallback"]) > 1
            or abs(result["packed"] - result["returned"]) > 1 or result["unexpected_stack"]
            or result["packet_mismatch"]):
        raise ValueError(f"Invalid runtime argument-packet counters {result}")
    return result


def summarize(root, guest):
    sha = COMMON["sha"]
    original = json.loads((HERE / "results/raid-aarchx-scene-footprints.json").read_text())
    artifacts = json.loads((root / "artifact-sha256.json").read_text())
    guest_hash = sha(guest)
    if (guest_hash != original["guest_image_sha256"] or
            sha(root / "artifacts/candidate/ocerz") != artifacts["runtime_unsigned_sha256"] or
            sha(root / "artifacts/candidate/libwinemetalgl.dylib") != artifacts["renderer_sha256"] or
            artifacts["renderer_sha256"] != original["artifacts"]["renderer_sha256"]):
        raise ValueError("Wrong i386 executable or trial binaries")
    disassemble = FOOTPRINT["SCENE"]["PHASE"]["GUEST"]
    decoded = {pc: (name, operands.strip()) for pc, (name, operands) in disassemble(guest, EXPECTED).items()}
    if decoded != EXPECTED:
        raise ValueError("The original i386 call boundary or vector reads differ")
    checks = ("original-layout", "unaligned-stack", "mismatched-frame",
              "alternate-pointers", "low-stack-page", "cross-page-vector")
    for label in ("control", "candidate"):
        if not (root / f"general-{label}.log").read_text().endswith("test_m32: 812 checks, 0 failed\n"):
            raise ValueError(f"The general i386 tests failed in {label}")
        focused = (root / f"focused-{label}.log").read_text().splitlines()
        expected = [f"ok {name} packet={int(index == 0 and label == 'candidate')}"
                    for index, name in enumerate(checks)]
        if focused != [*expected, "test_scene_args: 6 checks, 0 failed"]:
            raise ValueError(f"The packet ABI checks failed in {label}")

    verify = COMMON["verify_run"]
    diagnostic = root / "diagnostic-1"
    window = verify(diagnostic, 1, 1, 25, artifacts, "scene-args-mode.txt",
                    counter_file=COUNTER_FILE, render_logging=1)
    bracket, scoped, total = COMMON["descriptor_counter_samples"](
        diagnostic, slots=len(COUNTER_FIELDS), counter_file=COUNTER_FILE, gate_file="scene-args-gate.json")
    if not bracket["samples_inside_nominal_gpu_window"]:
        raise ValueError("The counter samples do not fall inside the verified raid window")

    runs = [
        {"order": order, "private_arguments": bool(mode),
         **verify(root / f"timed-{order}-{mode}", mode, 0, 60, artifacts, "scene-args-mode.txt",
                  counter_file=COUNTER_FILE)}
        for order, mode in enumerate((0, 1, 1, 0), 1)
    ]
    comparison = {}
    for mode, label in enumerate(("original_stack", "private_arguments")):
        selected = [row for row in runs if row["private_arguments"] == bool(mode)]
        comparison[label] = {FPS: median(row[FPS] for row in selected), P99: median(row[P99] for row in selected)}
    return {
        "guest_image_sha256": guest_hash,
        "artifact_sha256": artifacts,
        "general_i386_checks_per_mode": 812,
        "focused_checks_per_mode": list(checks),
        "diagnostic": {
            "verified_gpu_window": window,
            **bracket,
            "counters_between_samples": validate_counters(scoped),
            "whole_process_counters": validate_counters(total),
        },
        "timed_runs": runs,
        "comparison": comparison,
        "paired_private_minus_original": [
            {FPS: runs[t][FPS] - runs[c][FPS], P99: runs[t][P99] - runs[c][P99]} for c, t in ((0, 1), (3, 2))
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_root", type=Path)
    parser.add_argument("guest_binary", type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize(args.capture_root, args.guest_binary), indent=2))
