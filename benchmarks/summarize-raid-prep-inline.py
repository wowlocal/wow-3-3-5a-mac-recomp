"""Verify the expanded i386 preparation block and its full-raid A/B."""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
from runpy import run_path


HERE = Path(__file__).resolve().parent
TIMING = run_path(str(HERE / "summarize-raid-renderer-pgo.py"))
NATIVE = run_path(str(HERE / "summarize-raid-guest-native-stacks.py"))
CALLS = {0x965473: (0x8C17C0, 0x8C1856), 0x96548F: (0x8C1E90, 0x8C23DF)}
STARTS = range(0x9653BF, 0x96548F + 1)
CALLEE = re.compile(r"^ocerz: i386 prep inline callee=(0x[0-9a-f]+) ret=(0x[0-9a-f]+) insns=(\d+)$", re.MULTILINE)
BLOCK = re.compile(r"^ocerz: i386 prep block start=(0x[0-9a-f]+) end=(0x[0-9a-f]+) "
                   r"insns=(\d+) calls=(\d+) host_words=(\d+)$", re.MULTILINE)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_run(root, name, flag, logged, artifacts):
    capture = root / name
    if (json.loads((capture / "artifact-sha256.json").read_text()) != artifacts or
            (capture / "prep-inline-mode.txt").read_text().split() != [str(flag), str(int(logged))]):
        raise ValueError(f"Wrong signed runtime or launch mode in {capture}")
    text = (capture / "client.log").read_text()
    if ("ocerz: i386 prep block" in text) != logged:
        raise ValueError(f"Unexpected logging in {capture}")
    return TIMING["verify"](capture, "candidate", seconds=25 if logged else 60)


def verify_expansion(guest, blocks, callee_rows):
    wanted = set(CALLS) | {address for start, ret in CALLS.values() for address in range(start, ret + 1)}
    wanted.update(range(min(start for start, *_ in blocks), 0x9655F8 + 1))
    original = NATIVE["guest_instructions"](guest, wanted)
    sizes = {}
    for site, (target, ret) in CALLS.items():
        op, operand = original[site]
        if op != "calll" or operand.strip() != hex(target) or original[ret][0] != "retl":
            raise ValueError("Known i386 call or return changed")
        sizes[target, ret] = sum(target <= address <= ret for address in original)
    if any(sizes.get((start, ret)) != size for start, ret, size in callee_rows):
        raise ValueError("Compiled helper length differs from the original guest")
    if set(sizes) != {(start, ret) for start, ret, _ in callee_rows}:
        raise ValueError("Expected both original preparation helpers in the compiler log")

    coverage = []
    for start, end, size, calls, host_words in sorted(set(blocks)):
        guest_size = sum(start <= address <= end for address in original) + sum(sizes.values())
        if start not in STARTS or end != 0x9655F8 or calls != 2 or size != guest_size or host_words <= 0:
            raise ValueError("Expanded block differs from the two helpers and their caller")
        coverage.append({"start": hex(start), "last_original_pc": hex(end), "guest_instructions": size,
                         "expanded_calls": calls, "host_code_words": host_words})
    return coverage


def cpu_coverage(cpu_profiles, start, end):
    profiles = []
    for capture in cpu_profiles:
        samples = NATIVE["read_samples"](capture)
        total = sum(row.weight for row in samples)
        weights = Counter()
        for row in samples:
            if start <= row.leaf <= end:
                weights["caller"] += row.weight
            if row.returns:
                site = row.returns[0] - 5
                target = CALLS.get(site)
                if target and target[0] <= row.leaf <= target[1]:
                    weights[hex(site)] += row.weight
        profiles.append({"total_cpu_usec": total, "selected_cpu_usec": sum(weights.values()),
                         "selected_share_of_all_weighted_cpu": sum(weights.values()) / total,
                         "cpu_usec_by_region": dict(sorted(weights.items()))})
    return profiles


def summary(root, guest, profiles):
    previous = json.loads((HERE / "results/raid-aarchx-i386-item-memory.json").read_text())
    artifacts = json.loads((root / "artifact-sha256.json").read_text())
    if (sha(guest) != previous["guest_image_sha256"] or
            sha(root / "artifacts/candidate/ocerz") != artifacts["runtime_unsigned_sha256"] or
            artifacts["renderer_sha256"] != previous["diagnostic_artifacts"]["renderer"]):
        raise ValueError("Wrong original client, instrumented binary or profiled renderer")
    diagnostic = verify_run(root, "diagnostic", 1, True, artifacts)
    log = (root / "diagnostic/client.log").read_text()
    blocks = [(int(a, 16), int(b, 16), int(n), int(c), int(w)) for a, b, n, c, w in BLOCK.findall(log)]
    callees = [(int(a, 16), int(b, 16), int(n)) for a, b, n in CALLEE.findall(log)]
    if not blocks or not callees:
        raise ValueError("No compiled block containing both preparation calls")
    witness = verify_expansion(guest, blocks, callees)
    runs = [
        {"order": i, "mode": "candidate" if flag else "control",
         **verify_run(root, f"timed-{i}-{flag}", flag, False, artifacts)}
        for i, flag in enumerate((0, 1, 1, 0), 1)
    ]
    for name in ("focused-control.log", "focused-candidate.log"):
        lines = (root / name).read_text()
        if ("test_jit32: all cases identical (interpreter vs JIT)" not in lines or
                "  ok  two preparation calls preserve flags, guest return slots and ret imm16" not in lines or
                "  ok  inlined RET follows a rewritten return address" not in lines):
            raise ValueError(f"Missing i386 differential checks in {name}")
        count = len(CALLEE.findall(lines))
        if count != (3 if name == "focused-candidate.log" else 0):
            raise ValueError(f"Wrong i386 call expansion mode in {name}")
    if not (root / "general.log").read_text().endswith("test_m32: 805 checks, 0 failed\n"):
        raise ValueError("The i386 runtime checks did not pass")
    settings = ("hardware", "macos", "viewport", "vsync", "caster_count", "boss_count", "camera")
    return {**{key: previous[key] for key in settings},
            "guest_image_sha256": previous["guest_image_sha256"], "artifact_sha256": artifacts,
            "verified_timed_script_rows": 26,
            "diagnostic_gpu_window": diagnostic, "observed_inlined_blocks": witness,
            "earlier_cpu_profile_coverage": cpu_coverage(profiles, min(a for a, *_ in blocks), 0x9655F8),
            "general_i386_checks": 805, "focused_differential_checks": 2, **TIMING["summarize_timing"](runs)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_root", type=Path)
    parser.add_argument("guest_binary", type=Path)
    parser.add_argument("--cpu-profiles", type=Path, nargs=2, required=True)
    args = parser.parse_args()
    print(json.dumps(summary(args.capture_root, args.guest_binary, args.cpu_profiles), indent=2))
