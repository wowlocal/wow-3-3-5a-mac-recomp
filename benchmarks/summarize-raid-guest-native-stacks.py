"""Attribute sampled ARM64 instructions to verified i386 call trees."""

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
import json
from pathlib import Path
import re
from runpy import run_path
import subprocess
import tempfile
from typing import NamedTuple


HERE = Path(__file__).resolve().parent
GUEST = run_path(str(HERE / "summarize-i386-unwind-hotspots.py"))
READ_VERIFIED_WINDOW = run_path(str(HERE / "summarize-raid-shadow-reuse.py"))["read_window"]
INSTRUCTION = re.compile(r"^\s*([0-9a-f]+):\s+(\S+)\s*(.*)")
GUEST_MEMORY = re.compile(r"(?P<disp>-?(?:0x[0-9a-f]+|\d+))?\(%(?P<base>[a-z0-9]+)(?P<index>,.*?)?\)")
W_IMMEDIATE = re.compile(r"^(w\d+),\s*(w\d+),\s*#[-\w]+(?:,\s*lsl\s*#\d+)?$")
CPU_STATE_MEMORY = re.compile(r"\[x20(?:,\s*#(-?(?:0x[0-9a-f]+|\d+)))?\]")


class Sample(NamedTuple):
    weight: int
    leaf: int
    host_pc: int
    host_instruction: int
    returns: tuple


def read_samples(capture):
    _, start, stop = GUEST["read_stack_window_bounds"](capture)
    first = None
    last = None
    samples = []
    with (capture / "i386-arm64.ns").open() as source:
        for line in source:
            if not line.endswith("\n"):
                break
            time, weight, leaf, host_pc, instruction, *returns = line.split()
            timestamp = int(time)
            sample = Sample(int(weight), int(leaf, 16), int(host_pc, 16),
                            int(instruction, 16), tuple(int(pc, 16) for pc in returns))
            if (
                sample.weight <= 0 or (last is not None and timestamp < last) or
                bool(sample.leaf) != bool(sample.host_pc) or sample.host_pc % 4 or
                (not sample.leaf and (sample.host_instruction or sample.returns))
            ):
                raise ValueError(f"Invalid CPU sample in {capture}")
            if first is None:
                first = timestamp
            last = timestamp
            if start <= timestamp < stop:
                samples.append(sample)
    if not samples or first > start or last < stop:
        raise ValueError(f"CPU samples do not cover the GPU measurement in {capture}")
    return samples


def guest_instructions(binary, addresses):
    result = {}
    in_text = False
    command = ["xcrun", "llvm-objdump", "--arch=i386", "-d", "--no-show-raw-insn", str(binary)]
    with subprocess.Popen(command, stdout=subprocess.PIPE, text=True) as decoder:
        for line in decoder.stdout:
            if line.startswith("(") and " section" in line:
                in_text = line.strip() == "(__TEXT,__text) section"
            match = INSTRUCTION.match(line) if in_text else None
            if match and (pc := int(match[1], 16)) in addresses:
                result[pc] = match[2], match[3].split("##", 1)[0]
        if decoder.wait():
            raise subprocess.CalledProcessError(decoder.returncode, command)
    return result


def host_instructions(words):
    ordered = sorted(words)
    with tempfile.TemporaryDirectory(prefix="raid-aarch64-profile.") as root:
        source = Path(root) / "words.s"
        obj = source.with_suffix(".o")
        source.write_text(".text\n.p2align 2\n" + "".join(f".long 0x{word:08x}\n" for word in ordered))
        subprocess.run(["xcrun", "clang", "-arch", "arm64", "-c", str(source), "-o", str(obj)],
                       check=True, capture_output=True)
        dump = subprocess.check_output(["xcrun", "llvm-objdump", "-d", "--no-show-raw-insn", str(obj)],
                                       text=True)
    result = {}
    for line in dump.splitlines():
        match = INSTRUCTION.match(line)
        if match:
            offset = int(match[1], 16)
            if offset % 4 == 0 and offset // 4 < len(ordered):
                result[ordered[offset // 4]] = match[2], match[3].split(";", 1)[0].strip()
    if len(result) != len(ordered):
        raise ValueError("Disassembly missed sampled ARM64 words")
    return result


def ranked(counts, labels, denominator, limit=12):
    return [
        {**dict(zip(labels, key)), "cpu_usec": weight, "share_of_all_sampled_cpu": weight / denominator}
        for key, weight in counts.most_common(limit)
    ]


def report_group(group, guest_ops, host_ops, denominator, page_bytes):
    pairs = Counter()
    arm_ops = Counter()
    address_ops = Counter()
    address_bands = Counter()
    branch_state = Counter()
    pages = Counter()
    mapped = 0
    for sample in group:
        host_op, host_args = host_ops[sample.host_instruction]
        guest_op, guest_args = guest_ops.get(sample.leaf, ("unknown", ""))
        mapped += sample.weight if guest_op != "unknown" else 0
        pairs[guest_op, host_op] += sample.weight
        arm_ops[host_op,] += sample.weight
        pages[sample.host_pc // page_bytes] += sample.weight
        address = GUEST_MEMORY.search(guest_args)
        shape = W_IMMEDIATE.match(host_args) if host_op in {"add", "sub"} else None
        if guest_op.startswith("mov") and address and shape:
            base = address["base"]
            disp = int(address["disp"] or "0", 0)
            band = "near" if -256 <= disp <= 255 else "deep_negative" if -768 <= disp < -256 else "outside"
            address_ops[guest_op, base, host_op, shape[1], shape[2]] += sample.weight
            address_bands[base, band, "indexed" if address["index"] else "base_only"] += sample.weight
        if guest_op.startswith("j") and guest_op not in {"jmp", "jmpl"} and (state := CPU_STATE_MEMORY.search(host_args)):
            branch_state[host_op, int(state[1] or "0", 0)] += sample.weight
    total = sum(sample.weight for sample in group)
    cumulative = 0
    page_counts = {}
    for index, (_, weight) in enumerate(pages.most_common(), 1):
        cumulative += weight
        for percent in (50, 80, 90):
            if 100 * cumulative >= percent * total:
                page_counts.setdefault(str(percent), index)
    return {
        "cpu_usec": total,
        "share_of_all_sampled_cpu": total / denominator,
        "guest_opcode_matched_cpu_usec": mapped,
        "distinct_host_instruction_addresses": len({sample.host_pc for sample in group}),
        "sampled_host_code_pages": len(pages),
        "host_code_pages_to_cover_cpu_weight_percent": page_counts,
        "top_arm64_opcodes": ranked(arm_ops, ("arm64",), denominator),
        "top_i386_arm64_opcode_pairs": ranked(pairs, ("i386", "arm64"), denominator),
        "mov_memory_w_immediate_cpu_usec": sum(address_ops.values()),
        "top_mov_memory_w_immediate_forms": ranked(
            address_ops, ("i386", "i386_base", "arm64", "arm64_destination", "arm64_source"), denominator,
        ),
        "top_mov_memory_w_immediate_bands": ranked(
            address_bands, ("i386_base", "displacement_band", "indexing"), denominator,
        ),
        "conditional_branch_cpu_state_memory_cpu_usec": sum(branch_state.values()),
        "top_conditional_branch_cpu_state_memory_forms": ranked(
            branch_state, ("arm64", "byte_offset_from_x20"), denominator,
        ),
    }, pairs


def summarize(binary, roots, captures, page_bytes):
    ranges = GUEST["read_unwind_ranges"](binary)
    starts = [start for start, _ in ranges]
    if any(root not in starts for root in roots):
        raise ValueError("Expected the requested call tree roots at i386 unwind boundaries")
    samples = [read_samples(capture) for capture in captures]
    returns = {pc for group in samples for sample in group for pc in sample.returns}
    calls = GUEST["find_i386_call_sites"](binary, returns)
    guest_ops = guest_instructions(binary, {sample.leaf for group in samples for sample in group if sample.leaf})
    host_ops = host_instructions({sample.host_instruction for group in samples for sample in group if sample.leaf})

    def containing_entry(pc):
        index = bisect_right(starts, pc) - 1
        if index >= 0 and pc < ranges[index][1]:
            return ranges[index][0]
        return None

    report = []
    for number, (capture, rows) in enumerate(zip(captures, samples), 1):
        denominator = sum(row.weight for row in rows)
        translated = []
        by_root = defaultdict(list)
        overlap = 0
        union = []
        for row in rows:
            if not row.leaf:
                continue
            translated.append(row)
            entries = {containing_entry(row.leaf)}
            for address in row.returns:
                if address not in calls:
                    break
                entries.add(containing_entry(calls[address]))
            selected = entries.intersection(roots)
            if selected:
                union.append(row)
                overlap += row.weight if len(selected) > 1 else 0
                for root in selected:
                    by_root[root].append(row)
        if not translated or any(not by_root[root] for root in roots):
            raise ValueError("The captures need samples in both requested trees")
        all_translated, _ = report_group(translated, guest_ops, host_ops, denominator, page_bytes)
        union_report, _ = report_group(union, guest_ops, host_ops, denominator, page_bytes)
        groups = {}
        pairs = {}
        for root in roots:
            label = hex(root)
            groups[label], pairs[label] = report_group(by_root[root], guest_ops, host_ops,
                                                       denominator, page_bytes)
        shared = set.intersection(*(set(counts) for counts in pairs.values()))
        shared = sorted(shared, key=lambda key: (-sum(counter[key] for counter in pairs.values()), key))
        report.append({
            "capture": number,
            "diagnostic_gpu_window": READ_VERIFIED_WINDOW(capture, 60),
            "samples": len(rows),
            "all_sampled_cpu_usec": denominator,
            "all_translated": all_translated,
            "guest_call_trees": groups,
            "tree_union_share_of_all_sampled_cpu": union_report["share_of_all_sampled_cpu"],
            "tree_union_mov_memory_w_immediate_cpu_usec": union_report["mov_memory_w_immediate_cpu_usec"],
            "tree_union_top_mov_memory_w_immediate_bands": union_report["top_mov_memory_w_immediate_bands"],
            "tree_overlap_share_of_all_sampled_cpu": overlap / denominator,
            "top_opcode_pairs_present_in_every_tree": [
                {"i386": guest, "arm64": host,
                 "cpu_usec_by_tree": {label: counts[guest, host] for label, counts in pairs.items()}}
                for guest, host in shared[:12]
            ],
        })
    return {"host_code_page_bytes": page_bytes, "runs": report}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("client", type=Path)
    parser.add_argument("captures", type=Path, nargs="+")
    parser.add_argument("--roots", required=True, nargs=2, type=lambda value: int(value, 0))
    parser.add_argument("--host-page-bytes", default=16384, type=int)
    args = parser.parse_args()
    if args.roots[0] == args.roots[1] or args.host_page_bytes <= 0:
        parser.error("Choose two distinct roots and a positive host page size")
    print(json.dumps(summarize(args.client, args.roots, args.captures, args.host_page_bytes), indent=2))


if __name__ == "__main__":
    main()
