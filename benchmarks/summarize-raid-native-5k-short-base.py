"""Verify and measure compact i386 mapped-base reuse in the native 5K raid."""

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
PROFILE = run_path(str(HERE / "summarize-raid-native-5k-guest-profile.py"))
MIXED = PROFILE["MIXED"]
VERIFY = PROFILE["VERIFY"]
BASES = tuple(PROFILE["PINNED_BASE"])
FPS, P99 = "average_completed_fps", "p99_gpu_completion_gap_ns"
WITNESS = re.compile(
    r"^ocerz: i386 short base hoist rip=(0x[0-9a-f]+) base=(\d+) accesses=(\d+) displaced=(\d+) prefix=(\d+) added=([01])$",
    re.M,
)


def read_run(case, mode, log, baseline, artifacts, duration=60):
    checked = VERIFY(case, baseline, artifacts, "color-only-msaa-mode.txt", 1, 0, measurement_seconds=duration)
    if ((case / "short-base-mode.txt").read_text().split() != [str(mode), str(log)]
            or "OCERZ_M32_GENERAL_BASE_HOIST 1" not in (case / "shared-base.txt").read_text().splitlines()):
        raise ValueError(f"Wrong i386 base policy in {case.name}")
    text = (case / "client.log").read_text()
    if bool(WITNESS.search(text)) != bool(log):
        raise ValueError(f"Wrong code generation logging in {case.name}")
    return checked["verified_gpu_window"], text


def verify_tests(output):
    results = {}
    for mode in (0, 1):
        count = re.fullmatch(r"test_m32: (\d+) checks, 0 failed",
                             (output / f"general-{mode}.log").read_text().splitlines()[-1])
        if not count:
            raise ValueError(f"The i386 suite failed with short-base mode {mode}")
        layout_results = {}
        for layout in ("plain", "veneer"):
            exits = []
            for case in range(6):
                text = (output / f"focused-{mode}-{layout}-{case}.log").read_text()
                expected = int(mode == 1 and case in (1, 2))
                entries = [row for row in WITNESS.findall(text) if row[0] == "0x400000" and row[5] == "1"]
                if ("  ok  " not in text or "FAIL" in text
                        or f"short-base case={case} guard_exits={expected}" not in text.splitlines()
                        or bool(entries) != (mode == 1 and case < 4)):
                    raise ValueError(f"The focused interpreter comparison failed in {mode}/{layout}/{case}")
                exits.append(expected)
            layout_results[layout] = exits
        results[mode] = {"general_checks_passed": int(count[1]), "guard_exits_by_layout": layout_results}
    return results


def read_selected_operands(binary, prefixes):
    starts = defaultdict(list)
    for pc, reg, length in sorted(prefixes):
        starts[pc].append((reg, length))
    active = []
    candidates = {}
    matched = 0
    in_text = False
    command = ["xcrun", "llvm-objdump", "--arch=i386", "-d", "--no-show-raw-insn", str(binary)]
    with subprocess.Popen(command, stdout=subprocess.PIPE, text=True) as decoder:
        for line in decoder.stdout:
            if line.startswith("(") and " section" in line:
                in_text = line.strip() == "(__TEXT,__text) section"
                active.clear()
            if not in_text or not (instruction := MIXED["INSTRUCTION"].match(line)):
                continue
            pc, opcode = int(instruction[1], 16), instruction[2]
            operands = instruction[3].split("##", 1)[0]
            active.extend((reg, length, {}) for reg, length in starts.get(pc, ()))
            if not active:
                continue
            memory = MIXED["GUEST_MEMORY"].search(operands) if opcode == "movl" else None
            pending = []
            for reg, remaining, local in active:
                if memory and memory["base"] == BASES[reg] and not memory["index"] and memory["disp"]:
                    disp = int(memory["disp"], 0)
                    if -256 <= disp <= 255 and disp:
                        local[pc] = (opcode, operands)
                if remaining > 1:
                    pending.append((reg, remaining - 1, local))
                elif len(local) == 5:
                    matched += 1
                    candidates.update(local)
            active = pending
        if decoder.wait():
            raise subprocess.CalledProcessError(decoder.returncode, command)
    if not matched:
        raise ValueError("No short prefixes match five displaced MOVLs in the original client")
    return matched, candidates


def earlier_5k_coverage(root, binary, prefixes):
    count, instructions = read_selected_operands(binary, prefixes)
    samples = [MIXED["read_samples"](root / f"native-5k-guest-profile-{index}") for index in (1, 2)]
    words = {row.host_instruction for run in samples for row in run if row.leaf in instructions}
    host_ops = MIXED["host_instructions"](words) if words else {}
    reports = []
    for index, run in enumerate(samples, 1):
        total = sum(row.weight for row in run)
        matching = sum(row.weight for row in run if row.leaf in instructions and
                       PROFILE["movl_address_calculation"](*instructions[row.leaf], *host_ops[row.host_instruction]))
        reports.append({"profile": index, "sampled_cpu_usec": total, "additional_address_cpu_usec": matching,
                        "share_of_all_sampled_cpu": matching / total})
    return {"matched_guest_prefixes": count, "distinct_i386_movl_addresses": len(instructions), "profiles": reports}


def summarize(root, binary):
    baseline = json.loads((HERE / "results/raid-native-5k-max-baseline.json").read_text())
    selected = json.loads((HERE / "results/raid-native-5k-guest-profile.json").read_text())
    output = root / "compact-base"
    artifacts = json.loads((output / "artifact-sha256.json").read_text())
    restored = json.loads((output / "restored-runtime.json").read_text())
    guest_hash = hashlib.sha256(binary.read_bytes()).hexdigest()
    if (guest_hash != selected["guest_image_sha256"]
            or artifacts["renderer_sha256"] != selected["artifact_sha256"]["renderer_sha256"]
            or restored != selected["restored_runtime_sha256"]):
        raise ValueError("The comparison used a different guest image, renderer or restored runtime")
    diagnostic, text = read_run(root / "native-5k-compact-base-diagnostic", 1, 1, baseline, artifacts, 25)
    records = {(int(pc, 16), int(reg), int(length)) for pc, reg, count, displaced, length, added in WITNESS.findall(text)
               if added == "1" and count == displaced == "5" and 0 <= int(reg) < len(BASES)}
    if not records:
        raise ValueError("The native 5K raid emitted no additional short prefixes")
    runs = [
        {"mode": mode, "gpu_window": read_run(root / f"native-5k-compact-base-{index}-{mode}",
                                             mode, 0, baseline, artifacts)[0]}
        for index, mode in enumerate((0, 1, 1, 0), 1)
    ]
    return {
        "viewport": baseline["viewport"], "multisample_count": baseline["multisample_count"],
        "verified_settings": baseline["graphics"]["actual"], "artifact_sha256": artifacts,
        "restored_runtime_sha256": restored, "guest_image_sha256": guest_hash,
        "diagnostic_window": diagnostic, "new_prefix_shapes_logged": len(records),
        "prior_native_5k_sample_coverage": earlier_5k_coverage(root, binary, records),
        "focused_and_general_checks": verify_tests(output),
        "runs_in_capture_order": runs,
        "medians_by_mode": {
            mode: {metric: median(run["gpu_window"][metric] for run in runs if run["mode"] == mode)
                   for metric in (FPS, P99)}
            for mode in (0, 1)
        },
        "paired_enabled_minus_disabled": [
            {metric: runs[enabled]["gpu_window"][metric] - runs[disabled]["gpu_window"][metric]
             for metric in (FPS, P99)}
            for disabled, enabled in ((0, 1), (3, 2))
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_root", type=Path)
    parser.add_argument("i386_executable", type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize(args.capture_root, args.i386_executable), indent=2))
