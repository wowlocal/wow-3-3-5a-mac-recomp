"""Verify i386 address dependencies and compare GPU-completed raid output."""

import argparse
from bisect import bisect_right
import hashlib
import json
from pathlib import Path
import re
from runpy import run_path
from statistics import median
import subprocess


HERE = Path(__file__).resolve().parent
PROFILE = run_path(str(HERE / "summarize-raid-guest-native-stacks.py"))
IMAGE_SHA256 = "1063f877ec7f758ab25003e49d49a1257a8e12f9e1d38832ee6a46199c8155c8"
SITE = re.compile(r"^ocerz: i386 dependent load (0x[0-9a-f]+) (0x[0-9a-f]+)$", re.MULTILINE)
INSTRUCTION = re.compile(r"^\s*([0-9a-f]+):\s+(\S+)\s*(.*)")
MOVE = re.compile(r"^(.+),\s*(%e(?:ax|cx|dx|bx|bp|si|di))$")
GPR = re.compile(r"%e(?:ax|cx|dx|bx|bp|si|di)")


def verify(capture, enabled, logging, seconds):
    if (capture / "m32-dep-plain-mode.txt").read_text().split() != [str(enabled), str(logging)]:
        raise ValueError(f"Unexpected dependency settings in {capture}")
    if (capture / "map-shadow-mode.txt").read_text().split() != ["1", "0"]:
        raise ValueError(f"Unexpected buffer reuse settings in {capture}")
    if json.loads((capture / "legacy_witness.json").read_text())["legacy_paused"] is not True:
        raise ValueError(f"Earlier test ran during {capture}")
    text = (capture / "client.log").read_text()
    if bool(SITE.search(text)) != bool(logging) or "DEP_PLAIN_CHECK" in text:
        raise ValueError(f"Unexpected code-generation check in {capture}")
    return PROFILE["READ_VERIFIED_WINDOW"](capture, seconds)


def log_pairs(text):
    return {(int(source, 16), int(consumer, 16)) for source, consumer in SITE.findall(text)}


def read_instructions(binary, pairs):
    # Merge the small guest intervals before reading the disassembly.
    ranges = []
    for start, stop in sorted(pairs):
        if not 0 < stop - start <= 12 * 15:
            raise ValueError("An emitted dependency crosses the scan limit")
        if ranges and start <= ranges[-1][1]:
            ranges[-1] = (ranges[-1][0], max(stop, ranges[-1][1]))
        else:
            ranges.append((start, stop))
    starts = [start for start, _ in ranges]
    result = {}
    in_text = False
    command = ["xcrun", "llvm-objdump", "--arch=i386", "-d", "--no-show-raw-insn", str(binary)]
    with subprocess.Popen(command, stdout=subprocess.PIPE, text=True) as decoder:
        for line in decoder.stdout:
            if line.startswith("(") and " section" in line:
                in_text = line.strip() == "(__TEXT,__text) section"
            if not in_text or not (row := INSTRUCTION.match(line)):
                continue
            pc = int(row[1], 16)
            index = bisect_right(starts, pc) - 1
            if index >= 0 and pc <= ranges[index][1]:
                result[pc] = (row[2], row[3].split("##", 1)[0].strip())
        if status := decoder.wait():
            raise subprocess.CalledProcessError(status, command)
    return result


def is_dependent(sequence):
    carried = set()
    for index, (opcode, arguments) in enumerate(sequence):
        if index and index < len(sequence) - 1 and opcode.startswith("nop"):
            continue
        if opcode != "movl" or not (move := MOVE.fullmatch(arguments)):
            return False
        source, dest = move.groups()
        if index == 0:
            if source.startswith(("$", "%")):
                return False
            carried.add(dest)
        elif index == len(sequence) - 1:
            return "(" in source and any(reg in carried for reg in GPR.findall(source))
        else:
            if not GPR.fullmatch(source):
                return False
            if source in carried:
                carried.add(dest)
            else:
                carried.discard(dest)
    return False


def summarize(root, binary, profiles):
    diagnostic = root / "diagnostic"
    window = verify(diagnostic, 1, 1, 25)
    log = (diagnostic / "client.log").read_text()
    _, marker, later = log.rpartition("saved Metal trial backbuffer readback")
    if not marker or hashlib.sha256(binary.read_bytes()).hexdigest() != IMAGE_SHA256:
        raise ValueError("Missing world marker or wrong i386 image")
    emitted = log_pairs(log)
    instructions = read_instructions(binary, emitted)
    pcs = sorted(instructions)
    client_pairs = set()
    for start, stop in emitted:
        if start not in instructions or stop not in instructions:
            continue
        sequence = [instructions[pc] for pc in pcs[bisect_right(pcs, start) - 1:bisect_right(pcs, stop)]]
        if not is_dependent(sequence):
            raise ValueError(f"Compiler diagnostic did not match an i386 dependency at {start:#x}")
        client_pairs.add((start, stop))
    if not client_pairs:
        raise ValueError("No emitted dependency matched the original client")
    sources = {source for source, _ in client_pairs}

    coverage = []
    for profile in profiles:
        samples = PROFILE["read_samples"](profile)
        total = sum(row.weight for row in samples)
        matched = [row for row in samples if row.leaf in sources]
        coverage.append({
            "total_cpu_weight_usec": total,
            "emitted_source_cpu_share": sum(row.weight for row in matched) / total,
            "sampled_source_addresses": len({row.leaf for row in matched}),
        })

    runs = [
        {"order": order, "dependent": bool(mode), **verify(root / "timed" / f"{order}-{mode}", mode, 0, 60)}
        for order, mode in enumerate((0, 1, 1, 0), 1)
    ]
    fps, p99 = "average_completed_fps", "p99_gpu_completion_gap_ns"
    return {
        "diagnostic_gpu_window": window,
        "guest_image_sha256": IMAGE_SHA256,
        "distinct_emitted_dependencies": len(emitted),
        "dependencies_matched_in_original_client": len(client_pairs),
        "client_dependencies_emitted_after_world_readback": len(log_pairs(later) & client_pairs),
        "prior_cpu_profile_coverage": coverage,
        "timed_runs": runs,
        "comparison": {
            title: {
                "median_gpu_completed_fps": median(row[fps] for row in runs if row["dependent"] == mode),
                "median_p99_completion_gap_ns": median(row[p99] for row in runs if row["dependent"] == mode),
            }
            for title, mode in (("stock_ordering", False), ("dependent_load_ordering", True))
        },
        "paired_dependent_minus_stock": [
            {"completed_fps": runs[b][fps] - runs[a][fps], "p99_gap_ns": runs[b][p99] - runs[a][p99]}
            for a, b in ((0, 1), (3, 2))
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_root", type=Path)
    parser.add_argument("i386_client", type=Path)
    parser.add_argument("--cpu-profile", type=Path, action="append", default=[])
    args = parser.parse_args()
    print(json.dumps(summarize(args.capture_root, args.i386_client, args.cpu_profile), indent=2))


if __name__ == "__main__":
    main()
