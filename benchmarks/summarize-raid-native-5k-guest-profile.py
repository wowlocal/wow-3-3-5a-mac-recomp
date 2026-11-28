"""Attribute native 5K raid CPU samples to the executing guest and host instructions."""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
from runpy import run_path


HERE = Path(__file__).resolve().parent
VERIFY = run_path(str(HERE / "verify-raid-native-5k.py"))["verify_renderer_mode"]
MIXED = run_path(str(HERE / "summarize-raid-guest-native-stacks.py"))
GUEST = MIXED["GUEST"]
ROOTS = (0x73F8E0, 0x97DBC0)
HOT_THREAD = re.compile(r"GUESTPROF\[(\d+)\] next period samples only thread (0x[0-9a-f]+)")
PINNED_BASE = {name: f"w{21 + offset}" for offset, name in enumerate(
    ("eax", "ecx", "edx", "ebx", "esp", "ebp", "esi", "edi"))}
ADDRESS_IMMEDIATE = re.compile(r"w15,\s*(w\d+),\s*#(0x[0-9a-f]+|\d+)")


def read_run(case, baseline, artifacts):
    verified = VERIFY(case, baseline, artifacts, "color-only-msaa-mode.txt", 1, 0)
    if (case / "guest-profile-mode.txt").read_text().split() != ["2500", "4", "1", "i386-arm64.ns"]:
        raise ValueError(f"{case.name} used a different i386 profiler mode")
    window = verified["verified_gpu_window"]
    client_log = (case / "client.log").read_text()
    marker = "native trial GL viewport 0 0 5120 2880 draw-fbo 0 samples 4 buffers 1"
    hot_threads = HOT_THREAD.findall(client_log[client_log.index(marker):])
    if (len(hot_threads) < 4 or len(set(hot_threads)) != 1
            or int(hot_threads[0][0]) != window["pid"]):
        raise ValueError("The sampler did not retain one hot guest thread after world entry")
    return verified, MIXED["read_samples"](case), len(hot_threads)


def instruction_weights(group, guest_ops, host_ops, denominator):
    opcodes, addresses = Counter(), Counter()
    address_calculations = Counter()
    for row in group:
        guest, operands = guest_ops.get(row.leaf, ("unmapped", ""))
        opcodes[guest] += row.weight
        addresses[row.leaf] += row.weight
        if guest != "movl":
            continue
        host, args = host_ops[row.host_instruction]
        if host not in ("add", "sub") or not (offset := ADDRESS_IMMEDIATE.fullmatch(args)):
            continue
        memory = MIXED["GUEST_MEMORY"].search(operands)
        if (not memory or memory["index"] or not memory["disp"]
                or PINNED_BASE.get(memory["base"]) != offset[1]):
            continue
        displacement = int(memory["disp"], 0)
        immediate = int(offset[2], 0) * (-1 if host == "sub" else 1)
        prefix = operands[:memory.start()].strip()
        if not displacement or displacement != immediate or (prefix and not prefix.endswith(",")):
            continue
        direction = "store" if prefix else "load"
        address_calculations[direction, memory["base"]] += row.weight
    return {
        "guest_mnemonics": [
            {"i386": mnemonic, "cpu_usec": weight, "share_of_all_sampled_cpu": weight / denominator}
            for mnemonic, weight in opcodes.most_common(16)
        ],
        "hot_guest_instructions": [
            {"guest_pc": hex(pc), "i386": guest_ops.get(pc, ("unmapped", ""))[0],
             "cpu_usec": weight, "share_of_all_sampled_cpu": weight / denominator}
            for pc, weight in addresses.most_common(20)
        ],
        "movl_address_add_sub_cpu_usec": sum(address_calculations.values()),
        "movl_address_add_sub_by_operand": MIXED["ranked"](
            address_calculations, ("direction", "i386_base"), denominator, limit=16),
    }


def summarize(root, binary):
    baseline = json.loads((HERE / "results/raid-native-5k-max-baseline.json").read_text())
    selected = json.loads((HERE / "results/raid-native-5k-color-only-msaa-ab.json").read_text())
    previous = json.loads((HERE / "results/raid-aarchx-headless-i386-inclusive-stack-profile.json").read_text())
    guest_hash = hashlib.sha256(binary.read_bytes()).hexdigest()
    if guest_hash != previous["guest_image_sha256"]:
        raise ValueError("The local i386 image differs from the image used by the fixture")
    output = root / "guest-5k-profile"
    artifacts = json.loads((output / "artifact-sha256.json").read_text())
    restored = json.loads((output / "restored-runtime.json").read_text())
    if (artifacts["renderer_sha256"] != selected["artifact_sha256"]["renderer_sha256"]
            or restored != {key: baseline["artifact_sha256"][key]
                            for key in ("runtime_unsigned_sha256", "runtime_signed_sha256")}):
        raise ValueError("The selected renderer or restored clone runtime differs from the verified binaries")
    tests = re.fullmatch(r"test_m32: (\d+) checks, 0 failed", (output / "general.log").read_text().splitlines()[-1])
    if not tests:
        raise ValueError("The general i386 suite failed with the profiler disabled")

    captures = [read_run(root / f"native-5k-guest-profile-{i}", baseline, artifacts) for i in (1, 2)]
    ranges = GUEST["read_unwind_ranges"](binary)
    if not set(ROOTS) <= {start for start, _ in ranges}:
        raise ValueError("The known guest call trees are missing from the image")
    leaf_rows = [row for _, samples, _ in captures for row in samples if row.leaf]
    calls = GUEST["find_i386_call_sites"](binary, {pc for row in leaf_rows for pc in row.returns})
    guest_ops = MIXED["guest_instructions"](binary, {row.leaf for row in leaf_rows})
    host_ops = MIXED["host_instructions"]({row.host_instruction for row in leaf_rows})
    runs = []
    for verified, samples, periods in captures:
        stack = GUEST["summarize_stack_samples"](
            [(row.weight, row.leaf, row.returns) for row in samples], ranges, calls, ROOTS)
        translated = [row for row in samples if row.leaf]
        total = stack["total_cpu_usec"]
        completions = verified["verified_gpu_window"]["verified_completed_renders"]
        guest_leaf_weights = Counter()
        for row in translated:
            guest_leaf_weights[row.leaf] += row.weight
        exclusive = GUEST["attribute_weights"](guest_leaf_weights, ranges)
        host, _ = MIXED["report_group"](translated, guest_ops, host_ops, total, 16384)
        runs.append({
            "gpu_window": verified["verified_gpu_window"],
            "world_readback_sha256": verified["world_readback_sha256"],
            "verified_cast_rows": verified["verified_cast_rows"],
            "periods_with_same_hot_guest_thread_after_world_entry": periods,
            "weighted_cpu_ms_per_gpu_completion": {
                "all": total / (1000 * completions),
                "translated": stack["translated_cpu_usec"] / (1000 * completions),
                "in_either_known_tree": (total * stack["compared_entries"]["share_of_all_cpu_in_either_entry"]
                                         / (1000 * completions)),
            },
            "validated_stacks": stack,
            "hottest_exclusive_unwind_ranges": [
                {"entry": hex(start), "cpu_usec": weight, "share_of_all_sampled_cpu": weight / total}
                for (start, _), weight in exclusive.most_common(12)
            ],
            "translated_instruction_attribution": {
                **host,
                **instruction_weights(translated, guest_ops, host_ops, total),
            },
        })
    return {
        "viewport": baseline["viewport"], "multisample_count": baseline["multisample_count"],
        "verified_settings": baseline["graphics"]["actual"], "guest_image_sha256": guest_hash,
        "artifact_sha256": artifacts, "restored_runtime_sha256": restored,
        "general_i386_checks_passed": int(tests[1]), "runs": runs,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_root", type=Path)
    parser.add_argument("i386_executable", type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize(args.capture_root, args.i386_executable), indent=2))
