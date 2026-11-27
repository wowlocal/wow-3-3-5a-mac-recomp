"""Verify the short EBP handoff comparison and estimate its earlier CPU coverage."""

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
SHORT = re.compile(r"^ocerz: i386 short EBP handoff rip=(0x[0-9a-f]+) accesses=([2-5]) prefix=(\d+)$", re.M)
LINK = re.compile(r"^ocerz: i386 mapped base handoff target=(0x[0-9a-f]+) base=(\d+)$", re.M)
SETTINGS = {"reference": (0, 0), "long": (1, 0), "short": (1, 1)}


def verify_capture(folder, label, diagnostic, paused):
    handoff, short = SETTINGS[label]
    expected = {
        "OCERZ_M32_GENERAL_BASE_HOIST": "1",
        "OCERZ_M32_GENERAL_BASE_HOIST_LOG": "0",
        "OCERZ_M32_BASE_HANDOFF": str(handoff),
        "OCERZ_M32_BASE_HANDOFF_LOG": str(int(diagnostic)),
        "OCERZ_M32_BASE_HANDOFF_VERIFY": str(int(diagnostic)),
    }
    flags = dict(row.split() for row in (folder / "shared-base.txt").read_text().splitlines())
    if flags != expected or (folder / "short-ebp-mode.txt").read_text().strip() != str(short):
        raise ValueError(f"Wrong handoff flags in {folder}")
    if (folder / "map-shadow-mode.txt").read_text().split() != ["1", "0"]:
        raise ValueError(f"Wrong shadow allocation flags in {folder}")
    if json.loads((folder / "legacy_witness.json").read_text())["legacy_paused"] != paused:
        raise ValueError(f"Legacy GPU load differs in {folder}")
    return READ_WINDOW(folder, 25 if diagnostic else 60)


def read_witness(folder, label, paused):
    window = verify_capture(folder, label, True, paused)
    text = (folder / "client.log").read_text()
    _, marker, after = text.rpartition("saved Metal trial backbuffer readback")
    if not marker:
        raise ValueError(f"Missing world readback in {folder}")
    prefixes = {}
    for match in SHORT.finditer(text):
        pc, length = int(match[1], 16), int(match[3])
        prefixes[pc] = max(length, prefixes.get(pc, 0))
    links = {(int(match[1], 16), int(match[2])) for match in LINK.finditer(text)}
    linked = {pc: length for pc, length in prefixes.items() if (pc, 5) in links}
    if not links or bool(linked) != (label == "short"):
        raise ValueError(f"Missing expected link witness in {folder}")
    return {
        "mode": label,
        "verified_gpu_window": window,
        "distinct_linked_target_base_pairs": len(links),
        "distinct_linked_target_base_pairs_after_world_readback": len(set(LINK.findall(after))),
        "distinct_short_prefixes": len(prefixes),
        "distinct_short_prefixes_with_a_linked_entry": len(linked),
        "distinct_short_prefixes_emitted_after_world_readback": len({m[1] for m in SHORT.finditer(after)}),
    }, linked


def address_coverage(binary, prior_captures, prefixes):
    samples = [PROFILE["read_samples"](capture) for capture in prior_captures]
    addresses = {sample.leaf for run in samples for sample in run if sample.leaf}
    candidates = set()
    seen = set()
    remaining = 0
    in_text = False
    command = ["xcrun", "llvm-objdump", "--arch=i386", "-d", "--no-show-raw-insn", str(binary)]
    pattern = PROFILE["GUEST_MEMORY"]
    with subprocess.Popen(command, stdout=subprocess.PIPE, text=True) as process:
        for line in process.stdout:
            if line.startswith("(") and " section" in line:
                in_text = line.strip() == "(__TEXT,__text) section"
                remaining = 0
                continue
            match = PROFILE["INSTRUCTION"].match(line) if in_text else None
            if not match:
                continue
            pc, opcode, operands = int(match[1], 16), match[2], match[3].split("##", 1)[0]
            if pc in prefixes:
                seen.add(pc)
                remaining = max(remaining, prefixes[pc])
            if remaining and pc in addresses and opcode.startswith("mov"):
                mem = pattern.search(operands)
                if mem and mem["base"] == "ebp" and not mem["index"] and -256 <= int(mem["disp"] or "0", 0) <= 255:
                    candidates.add(pc)
            remaining = max(0, remaining - 1)
        if process.wait() != 0:
            raise subprocess.CalledProcessError(process.returncode, command)

    words = {sample.host_instruction for run in samples for sample in run if sample.leaf in candidates}
    decoded = PROFILE["host_instructions"](words) if words else {}
    shaped = set()
    for word, (opcode, operands) in decoded.items():
        form = PROFILE["W_IMMEDIATE"].match(operands)
        if opcode in {"add", "sub"} and form and form[1] == "w15" and form[2] == "w26":
            shaped.add(word)
    result = []
    for index, run in enumerate(samples, 1):
        total = sum(sample.weight for sample in run)
        selected = sum(sample.weight for sample in run
                       if sample.leaf in candidates and sample.host_instruction in shaped)
        result.append({
            "earlier_capture": index,
            "sampled_cpu_usec": total,
            "matching_short_prefix_address_instruction_cpu_usec": selected,
            "matching_share_of_all_sampled_cpu": selected / total,
        })
    return {"linked_short_prefixes_in_original_text": len(seen), "earlier_captures": result}


def summarize(root, binary, prior_captures):
    paused = json.loads((root / "diagnostic/long/legacy_witness.json").read_text())["legacy_paused"]
    long, _ = read_witness(root / "diagnostic/long", "long", paused)
    short, linked_prefixes = read_witness(root / "diagnostic/short", "short", paused)
    order = ("reference", "long", "short", "short", "long", "reference")
    runs = []
    for index, label in enumerate(order, 1):
        folder = root / "timed" / f"{index}-{label}"
        log = (folder / "client.log").read_text()
        if SHORT.search(log) or LINK.search(log):
            raise ValueError(f"Unexpected diagnostic logging in {folder}")
        runs.append({"order": index, "mode": label, **verify_capture(folder, label, False, paused)})
    comparison = {}
    for label in SETTINGS:
        selected = [row for row in runs if row["mode"] == label]
        comparison[label] = {
            "median_completed_fps": median(row["average_completed_fps"] for row in selected),
            "median_p99_gap_ns": median(row["p99_gpu_completion_gap_ns"] for row in selected),
        }
    pairs = {}
    for label, indices in (("long", ((1, 2), (4, 3))), ("reference", ((0, 2), (5, 3)))):
        pairs[f"short_minus_{label}"] = [
            {
                "completed_fps": runs[t]["average_completed_fps"] - runs[c]["average_completed_fps"],
                "p99_gap_ns": runs[t]["p99_gpu_completion_gap_ns"] - runs[c]["p99_gpu_completion_gap_ns"],
            }
            for c, t in indices
        ]
    return {
        "legacy_paused_in_all_launches": paused,
        "diagnostics": [long, short],
        "earlier_cpu_coverage": address_coverage(binary, prior_captures, linked_prefixes),
        "timed_runs": runs,
        "comparison": comparison,
        "pairs": pairs,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_root", type=Path)
    parser.add_argument("client", type=Path)
    parser.add_argument("prior_native_stack_captures", type=Path, nargs="+")
    args = parser.parse_args()
    print(json.dumps(summarize(args.capture_root, args.client, args.prior_native_stack_captures), indent=2))


if __name__ == "__main__":
    main()
