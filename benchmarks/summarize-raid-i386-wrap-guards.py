"""Verify the i386 guard-page load comparison and its sampled coverage."""

import argparse
from collections import defaultdict
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
PREFIX = re.compile(
    r"^ocerz: i386 guard-page load rip=(0x[0-9a-f]+) base=([0-7]) loads=(\d+) displaced=(\d+) prefix=(\d+)$", re.M
)
LOAD = re.compile(
    r"^(-?(?:0x[0-9a-f]+|\d+))?\(%(e(?:ax|cx|dx|bx|bp|si|di))\),\s*%e(?:ax|cx|dx|bx|bp|si|di)$"
)
REGISTERS = ("eax", "ecx", "edx", "ebx", "esp", "ebp", "esi", "edi")
RECOVERY = "ocerz: i386 guard-page recovery"


def verify(capture, mode, log, seconds):
    if (capture / "m32-wrap-fault-mode.txt").read_text().split() != [str(mode), str(log)]:
        raise ValueError(f"Wrong guard-page setting in {capture}")
    if (capture / "map-shadow-mode.txt").read_text().split() != ["1", "0"]:
        raise ValueError(f"Different mapping settings in {capture}")
    if json.loads((capture / "legacy_witness.json").read_text())["legacy_paused"] is not True:
        raise ValueError(f"Earlier test ran during {capture}")
    text = (capture / "client.log").read_text()
    if bool(PREFIX.search(text)) != bool(log) or (not log and RECOVERY in text):
        raise ValueError(f"Unexpected guard-page logging in {capture}")
    return PROFILE["READ_VERIFIED_WINDOW"](capture, seconds)


def parse_prefixes(text):
    return {(int(rip, 16), *map(int, row)) for rip, *row in PREFIX.findall(text)}


def match_prefixes(binary, prefixes):
    starts = defaultdict(list)
    for row in prefixes:
        starts[row[0]].append(row)
    active = []
    confirmed = set()
    targets = set()
    short_targets = set()
    instruction = PROFILE["INSTRUCTION"]
    in_text = False
    command = ["xcrun", "llvm-objdump", "--arch=i386", "-d", "--no-show-raw-insn", str(binary)]
    with subprocess.Popen(command, stdout=subprocess.PIPE, text=True) as decoder:
        for line in decoder.stdout:
            if line.startswith("(") and " section" in line:
                in_text = line.strip() == "(__TEXT,__text) section"
                active.clear()
            if not in_text or not (row := instruction.match(line)):
                continue
            pc = int(row[1], 16)
            active.extend((prefix, prefix[-1], []) for prefix in starts.get(pc, ()))
            if not active:
                continue
            mem = LOAD.fullmatch(row[3].split("##", 1)[0].strip()) if row[2] == "movl" else None
            following = []
            for prefix, remaining, loads in active:
                rip, base, count, displaced, _ = prefix
                if mem and mem[2] == REGISTERS[base] and -256 <= (offset := int(mem[1] or "0", 0)) <= 255:
                    loads.append((pc, base, offset))
                if remaining > 1:
                    following.append((prefix, remaining - 1, loads))
                elif len(loads) == count and sum(disp != 0 for _, _, disp in loads) == displaced:
                    confirmed.add(prefix)
                    targets.update((address, reg) for address, reg, _ in loads)
                    if count < 6:
                        short_targets.update((address, reg) for address, reg, _ in loads)
            active = following
        if status := decoder.wait():
            raise subprocess.CalledProcessError(status, command)
    if not confirmed:
        raise ValueError("No recorded prefix matched the original client")
    return confirmed, targets, short_targets


def coverage(captures, targets, short_targets):
    samples = [PROFILE["read_samples"](capture) for capture in captures]
    addresses = set(address for address, _ in targets)
    words = {row.host_instruction for run in samples for row in run if row.leaf in addresses}
    code = PROFILE["host_instructions"](words) if words else {}
    address_reg = {}
    for word, (op, operands) in code.items():
        form = PROFILE["W_IMMEDIATE"].fullmatch(operands)
        if op in ("add", "sub") and form and form[1] == "w15":
            address_reg[word] = int(form[2][1:]) - 21
    result = []
    for run in samples:
        total = sum(row.weight for row in run)
        weights = {"full_loads": 0, "address_instructions": 0, "short_prefix_address_instructions": 0}
        for row in run:
            if row.leaf not in addresses:
                continue
            weights["full_loads"] += row.weight
            pair = row.leaf, address_reg.get(row.host_instruction)
            if pair in targets:
                weights["address_instructions"] += row.weight
            if pair in short_targets:
                weights["short_prefix_address_instructions"] += row.weight
        result.append({
            "total_cpu_weight_usec": total,
            "shares_of_total_cpu_weight": {kind: weight / total for kind, weight in weights.items()},
        })
    return result


def summarize(root, binary, profiles):
    diagnostic = root / "diagnostic"
    window = verify(diagnostic, 1, 1, 25)
    raw = (diagnostic / "client.log").read_text()
    _, marker, later = raw.rpartition("saved Metal trial backbuffer readback")
    if not marker or hashlib.sha256(binary.read_bytes()).hexdigest() != IMAGE_SHA256:
        raise ValueError("Missing world marker or wrong i386 image")
    emitted = parse_prefixes(raw)
    matched, targets, short_targets = match_prefixes(binary, emitted)

    runs = [
        {"order": order, "guard_pages": bool(mode), **verify(root / "timed" / f"{order}-{mode}", mode, 0, 60)}
        for order, mode in enumerate((0, 1, 1, 0), 1)
    ]
    fps, p99 = "average_completed_fps", "p99_gpu_completion_gap_ns"
    return {
        "diagnostic_gpu_window": window,
        "guest_image_sha256": IMAGE_SHA256,
        "distinct_emitted_prefixes": len(emitted),
        "prefixes_confirmed_in_original_client": len(matched),
        "confirmed_prefixes_with_under_six_loads": sum(row[2] < 6 for row in matched),
        "confirmed_prefixes_emitted_after_world_readback": len(parse_prefixes(later) & matched),
        "recoveries_in_diagnostic_launch": raw.count(RECOVERY),
        "prior_cpu_profile_coverage": coverage(profiles, targets, short_targets),
        "timed_runs": runs,
        "comparison": {
            title: {
                "median_gpu_completed_fps": median(row[fps] for row in runs if row["guard_pages"] == mode),
                "median_p99_completion_gap_ns": median(row[p99] for row in runs if row["guard_pages"] == mode),
            }
            for title, mode in (("explicit_bounds", False), ("guard_pages", True))
        },
        "paired_guard_pages_minus_explicit": [
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
