"""Verify the deep EBP raid comparison and measure earlier sampled address coverage."""

import argparse
from collections import Counter
import json
from pathlib import Path
import re
from runpy import run_path
from statistics import median
import subprocess


HERE = Path(__file__).resolve().parent
READ_WINDOW = run_path(str(HERE / "summarize-raid-shadow-reuse.py"))["read_window"]
READ_STACK_WINDOW = run_path(str(HERE / "summarize-i386-unwind-hotspots.py"))["read_stack_window"]
WITNESS = re.compile(r"^ocerz: i386 deep EBP hoist rip=(\w+) deep=(\d+) accesses=(\d+) prefix=(\d+)$", re.M)
INSTRUCTION = re.compile(r"^\s*([0-9a-f]+):\s+(\S+)\s*(.*)")
EBP_DISPLACEMENT = re.compile(r"(-?(?:0x[0-9a-f]+|\d+))?\(%ebp\)")


def verify_run(folder, mode, logging, seconds, legacy_paused):
    witness = json.loads((folder / "legacy_witness.json").read_text())
    if witness["legacy_paused"] != legacy_paused:
        raise ValueError(f"Different legacy load in {folder}")
    if (folder / "deep-ebp-mode.txt").read_text().split() != [str(mode), str(logging)]:
        raise ValueError(f"Wrong deep EBP settings in {folder}")
    if (folder / "map-shadow-mode.txt").read_text().split() != ["1", "0"]:
        raise ValueError(f"Wrong shadow settings in {folder}")
    return READ_WINDOW(folder, seconds)


def address_coverage(binary, stack_captures, prefixes):
    weights = Counter()
    total = 0
    for capture in stack_captures:
        _, samples = READ_STACK_WINDOW(capture)
        for weight, pc, _ in samples:
            total += weight
            if pc:
                weights[pc] += weight
    if not total:
        raise ValueError("No earlier CPU samples")

    seen = 0
    eligible = 0
    selected = 0
    remaining = 0
    in_text = False
    command = ["xcrun", "llvm-objdump", "--arch=i386", "-d", "--no-show-raw-insn", str(binary)]
    with subprocess.Popen(command, stdout=subprocess.PIPE, text=True) as decoder:
        for line in decoder.stdout:
            if line.startswith("(") and " section" in line:
                in_text = line.strip() == "(__TEXT,__text) section"
                remaining = 0
                continue
            match = INSTRUCTION.match(line) if in_text else None
            if not match:
                continue
            pc, opcode, operands = int(match[1], 16), match[2], match[3].split("##", 1)[0]
            if pc in prefixes:
                seen += 1
                remaining = max(remaining, prefixes[pc])
            if pc in weights and (opcode in {"movb", "movw", "movl"} or "%xmm" in operands):
                address = EBP_DISPLACEMENT.search(operands)
                if address and address[1] and -768 <= int(address[1], 0) < -256:
                    eligible += weights[pc]
                    if remaining:
                        selected += weights[pc]
            remaining = max(0, remaining - 1)
        if decoder.wait() != 0:
            raise subprocess.CalledProcessError(decoder.returncode, command)
    if not seen:
        raise ValueError("No emitted block entries matched the original i386 text")
    return {
        "earlier_stack_captures": len(stack_captures),
        "earlier_sampled_cpu_usec": total,
        "emitted_prefixes_matching_i386_text": seen,
        "deep_ebp_address_shape_cpu_usec": eligible,
        "deep_ebp_address_shape_in_selected_prefix_cpu_usec": selected,
        "deep_ebp_address_shape_share_of_total": eligible / total,
        "selected_prefix_deep_shape_share_of_total": selected / total,
    }


def summarize(root, binary, stack_captures):
    diagnostic = root / "diagnostic"
    legacy_paused = json.loads((diagnostic / "legacy_witness.json").read_text())["legacy_paused"]
    diagnostic_window = verify_run(diagnostic, 1, 1, 25, legacy_paused)
    text = (diagnostic / "client.log").read_text()
    prefixes = {}
    for row in WITNESS.finditer(text):
        pc, count = int(row[1], 0), int(row[4])
        prefixes[pc] = max(count, prefixes.get(pc, 0))
    if not prefixes or "saved Metal trial backbuffer readback" not in text:
        raise ValueError("Missing live compilation or world readback witness")
    after_readback = text.rsplit("saved Metal trial backbuffer readback", 1)[1]

    runs = []
    for order, mode in enumerate((0, 1, 1, 0), 1):
        folder = root / "timed" / f"{order}-{mode}"
        if WITNESS.search((folder / "client.log").read_text()):
            raise ValueError(f"Compilation logging was enabled during a timed run in {folder}")
        runs.append({"order": order, "deep_ebp": bool(mode), **verify_run(folder, mode, 0, 60, legacy_paused)})
    comparison = {}
    for mode, label in ((False, "near_only"), (True, "deep_and_near")):
        rows = [row for row in runs if row["deep_ebp"] == mode]
        comparison[label] = {
            "median_completed_fps": median(row["average_completed_fps"] for row in rows),
            "median_p99_gap_ns": median(row["p99_gpu_completion_gap_ns"] for row in rows),
        }
    return {
        "legacy_test_paused_in_all_launches": legacy_paused,
        "diagnostic_gpu_window": diagnostic_window,
        "distinct_emitted_prefixes": len(prefixes),
        "distinct_emitted_prefixes_after_world_readback": len({int(row[1], 0) for row in WITNESS.finditer(after_readback)}),
        "earlier_sample_coverage": address_coverage(binary, stack_captures, prefixes),
        "timed_runs": runs,
        "comparison": comparison,
        "paired_trial_minus_control": [
            {
                "completed_fps": runs[t]["average_completed_fps"] - runs[c]["average_completed_fps"],
                "p99_gap_ns": runs[t]["p99_gpu_completion_gap_ns"] - runs[c]["p99_gpu_completion_gap_ns"],
            }
            for c, t in ((0, 1), (3, 2))
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_root", type=Path)
    parser.add_argument("client", type=Path)
    parser.add_argument("earlier_stack_captures", type=Path, nargs="+")
    args = parser.parse_args()
    print(json.dumps(summarize(args.capture_root, args.client, args.earlier_stack_captures), indent=2))


if __name__ == "__main__":
    main()
