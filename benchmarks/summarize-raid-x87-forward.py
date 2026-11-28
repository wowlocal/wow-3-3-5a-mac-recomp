"""Verify i386 x87 forwarding sites and compare GPU-completed raid output."""

import argparse
import hashlib
import json
from pathlib import Path
import re
from runpy import run_path
from statistics import median
import subprocess


HERE = Path(__file__).resolve().parent
PROFILE = run_path(str(HERE / "summarize-raid-guest-native-stacks.py"))
VERIFY = PROFILE["READ_VERIFIED_WINDOW"]
SITE = re.compile(r"^ocerz: x87 float forward (0x[0-9a-f]+) (0x[0-9a-f]+)$", re.MULTILINE)
INSTRUCTION = re.compile(r"^\s*([0-9a-f]+):\s+(\S+)\s*(.*)")
IMAGE_SHA256 = "1063f877ec7f758ab25003e49d49a1257a8e12f9e1d38832ee6a46199c8155c8"


def verify(capture, enabled, logging, seconds):
    fields = (capture / "x87-forward-mode.txt").read_text().split()
    if fields != [str(enabled), str(logging)]:
        raise ValueError(f"Unexpected x87 settings in {capture}")
    if (capture / "map-shadow-mode.txt").read_text().split() != ["1", "0"]:
        raise ValueError(f"Unexpected buffer reuse settings in {capture}")
    if json.loads((capture / "legacy_witness.json").read_text())["legacy_paused"] is not True:
        raise ValueError(f"The earlier test process ran during {capture}")
    text = (capture / "client.log").read_text()
    if bool(SITE.search(text)) != bool(logging):
        raise ValueError(f"Unexpected compiler logging in {capture}")
    return VERIFY(capture, seconds)


def instruction_pairs(binary):
    if hashlib.sha256(binary.read_bytes()).hexdigest() != IMAGE_SHA256:
        raise ValueError("The image differs from the instrumented i386 client")
    pairs = set()
    in_text = False
    previous = None
    command = ["xcrun", "llvm-objdump", "--arch=i386", "-d", "--no-show-raw-insn", str(binary)]
    with subprocess.Popen(command, stdout=subprocess.PIPE, text=True) as decoder:
        for line in decoder.stdout:
            if line.startswith("(") and " section" in line:
                in_text = line.strip() == "(__TEXT,__text) section"
                previous = None
            if not in_text or not (row := INSTRUCTION.match(line)):
                continue
            pc, opcode = int(row[1], 16), row[2]
            if opcode == "fistpl" and previous and previous[1] == "flds":
                pairs.add((previous[0], pc))
            previous = pc, opcode
        if status := decoder.wait():
            raise subprocess.CalledProcessError(status, command)
    if not pairs:
        raise ValueError("No adjacent x87 float/integer pairs decoded")
    return pairs


def log_pairs(text):
    return {(int(source, 16), int(destination, 16)) for source, destination in SITE.findall(text)}


def coverage(profile, pairs, emitted):
    samples = PROFILE["read_samples"](profile)
    total = sum(row.weight for row in samples)
    sources = {source for source, _ in pairs}
    all_destinations = {destination for _, destination in pairs}
    emitted_destinations = {destination for _, destination in emitted}
    weights = {
        "all_adjacent_flds": sum(row.weight for row in samples if row.leaf in sources),
        "all_adjacent_fistpl": sum(row.weight for row in samples if row.leaf in all_destinations),
        "emitted_fistpl": sum(row.weight for row in samples if row.leaf in emitted_destinations),
    }
    return {
        "total_cpu_weight_usec": total,
        "weighted_cpu_shares": {key: value / total for key, value in weights.items()},
        "sampled_adjacent_fistpl_sites": len({row.leaf for row in samples if row.leaf in all_destinations}),
        "sampled_forwarded_fistpl_sites": len({row.leaf for row in samples if row.leaf in emitted_destinations}),
    }


def summarize(root, binary, profiles):
    diagnostic = root / "diagnostic"
    verified = verify(diagnostic, 1, 1, 25)
    raw = (diagnostic / "client.log").read_text()
    _, marker, in_world = raw.rpartition("saved Metal trial backbuffer readback")
    if not marker:
        raise ValueError("Missing world readback in diagnostic client log")
    decoded = instruction_pairs(binary)
    emitted = log_pairs(raw)
    # The diagnostic can also translate loaded frameworks. Restrict the CPU
    # coverage to the verified client image and its decoded instruction pairs.
    client = emitted & decoded
    if not client:
        raise ValueError("No compiled pair matched the original client image")

    runs = [
        {"order": order, "forward": bool(mode), **verify(root / "timed" / f"{order}-{mode}", mode, 0, 60)}
        for order, mode in enumerate((0, 1, 1, 0), 1)
    ]
    fps, p99 = "average_completed_fps", "p99_gpu_completion_gap_ns"
    return {
        "diagnostic_gpu_window": verified,
        "original_guest_image_sha256": IMAGE_SHA256,
        "decoded_client_flds_fistpl_pairs": len(decoded),
        "distinct_emitted_pairs": len(emitted),
        "distinct_emitted_client_pairs": len(client),
        "emitted_client_pairs_after_world_readback": len(log_pairs(in_world) & client),
        "prior_cpu_profile_coverage": [coverage(profile, decoded, client) for profile in profiles],
        "timed_runs": runs,
        "comparison": {
            title: {
                "median_gpu_completed_fps": median(row[fps] for row in runs if row["forward"] == mode),
                "median_p99_completion_gap_ns": median(row[p99] for row in runs if row["forward"] == mode),
            }
            for title, mode in (("stack_reload", False), ("forward", True))
        },
        "paired_forward_minus_reload": [
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
