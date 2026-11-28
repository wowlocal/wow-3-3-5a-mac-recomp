"""Verify the adjacent memory COMIS comparison and estimate sampled coverage."""

import argparse
import json
from pathlib import Path
import re
from runpy import run_path
from statistics import median
import subprocess


HERE = Path(__file__).resolve().parent
READ_WINDOW = run_path(str(HERE / "summarize-raid-shadow-reuse.py"))["read_window"]
PROFILE = run_path(str(HERE / "summarize-raid-guest-native-stacks.py"))
LOG = re.compile(r"^ocerz: i386 adjacent memory COMIS rip=(0x[0-9a-f]+)$", re.M)
COMIS = {"comiss", "comisd", "ucomiss", "ucomisd"}


def verify(capture, mode, logging, duration, legacy_paused):
    if json.loads((capture / "legacy_witness.json").read_text())["legacy_paused"] != legacy_paused:
        raise ValueError(f"Different legacy load in {capture}")
    if (capture / "comis-mode.txt").read_text().split() != [str(mode), str(logging)]:
        raise ValueError(f"Unexpected COMIS flags in {capture}")
    if (capture / "map-shadow-mode.txt").read_text().split() != ["1", "0"]:
        raise ValueError(f"Unexpected shadow reuse flags in {capture}")
    return READ_WINDOW(capture, duration)


def client_pairs(binary, producers):
    pairs = {}
    in_text = False
    pending = None
    command = ["xcrun", "llvm-objdump", "--arch=i386", "-d", "--no-show-raw-insn", str(binary)]
    with subprocess.Popen(command, stdout=subprocess.PIPE, text=True) as decoder:
        for line in decoder.stdout:
            if line.startswith("(") and " section" in line:
                in_text = line.strip() == "(__TEXT,__text) section"
                pending = None
                continue
            match = PROFILE["INSTRUCTION"].match(line) if in_text else None
            if not match:
                continue
            pc, opcode = int(match[1], 16), match[2]
            if pending is not None and opcode.startswith("j") and opcode not in {"jmp", "jmpl"}:
                pairs[pending] = pc
            pending = pc if pc in producers and opcode in COMIS else None
        if decoder.wait() != 0:
            raise subprocess.CalledProcessError(decoder.returncode, command)
    if not pairs:
        raise ValueError("No optimized producer and branch pairs matched the original client")
    return pairs


def sampled_coverage(binary, producers, captures, offset):
    pairs = client_pairs(binary, producers)
    addresses = pairs.keys() | set(pairs.values())
    branches = set(pairs.values())
    runs = [PROFILE["read_samples"](path) for path in captures]
    words = {sample.host_instruction for run in runs for sample in run if sample.leaf in addresses}
    decoded = PROFILE["host_instructions"](words) if words else {}
    kinds = {}
    for word, (opcode, operands) in decoded.items():
        state = PROFILE["CPU_STATE_MEMORY"].search(operands)
        if state and int(state[1] or "0", 0) == offset:
            kinds[word] = opcode
    summaries = []
    for index, samples in enumerate(runs, 1):
        total = sum(sample.weight for sample in samples)
        loads = sum(sample.weight for sample in samples
                    if sample.leaf in branches and kinds.get(sample.host_instruction) == "ldr")
        stores = sum(sample.weight for sample in samples
                     if sample.leaf in pairs and kinds.get(sample.host_instruction) == "str")
        summaries.append({
            "earlier_capture": index,
            "sampled_cpu_usec": total,
            "matching_load_cpu_usec": loads,
            "matching_store_cpu_usec": stores,
            "matching_load_and_store_share_of_sampled_cpu": (loads + stores) / total,
        })
    return {"matched_adjacent_pairs": len(pairs), "cpu_state_operand_offset": offset, "captures": summaries}


def summarize(root, binary, earlier, offset):
    diagnostic = root / "diagnostic"
    paused = json.loads((diagnostic / "legacy_witness.json").read_text())["legacy_paused"]
    window = verify(diagnostic, 1, 1, 25, paused)
    text = (diagnostic / "client.log").read_text()
    producers = {int(value, 16) for value in LOG.findall(text)}
    _, marker, after = text.rpartition("saved Metal trial backbuffer readback")
    if not marker or not producers:
        raise ValueError("Missing world readback or emission evidence in the diagnostic run")
    timed = []
    for order, mode in enumerate((0, 1, 1, 0), 1):
        folder = root / "timed" / f"{order}-{mode}"
        if LOG.search((folder / "client.log").read_text()):
            raise ValueError(f"Unexpected diagnostic logging in {folder}")
        timed.append({"order": order, "adjacent_comis": bool(mode), **verify(folder, mode, 0, 60, paused)})
    groups = {}
    for mode, label in ((False, "saved_operand"), (True, "adjacent_compare")):
        rows = [row for row in timed if row["adjacent_comis"] == mode]
        groups[label] = {
            "median_completed_fps": median(row["average_completed_fps"] for row in rows),
            "median_p99_gap_ns": median(row["p99_gpu_completion_gap_ns"] for row in rows),
        }
    return {
        "legacy_paused_in_all_launches": paused,
        "diagnostic_gpu_window": window,
        "distinct_fused_producers": len(producers),
        "distinct_fused_producers_emitted_after_world_readback": len(set(LOG.findall(after))),
        "earlier_cpu_coverage": sampled_coverage(binary, producers, earlier, offset),
        "timed_runs": timed,
        "comparison": groups,
        "paired_trial_minus_control": [
            {
                "completed_fps": timed[t]["average_completed_fps"] - timed[c]["average_completed_fps"],
                "p99_gap_ns": timed[t]["p99_gpu_completion_gap_ns"] - timed[c]["p99_gpu_completion_gap_ns"],
            }
            for c, t in ((0, 1), (3, 2))
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_root", type=Path)
    parser.add_argument("client", type=Path)
    parser.add_argument("earlier_native_stack_captures", nargs="+", type=Path)
    parser.add_argument("--operand-offset", type=int, default=5176)
    args = parser.parse_args()
    print(json.dumps(summarize(args.capture_root, args.client, args.earlier_native_stack_captures,
                               args.operand_offset), indent=2))


if __name__ == "__main__":
    main()
