"""Find observed read-after-write dependencies between raid preparation items."""

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from functools import cache
import hashlib
import json
import mmap
from pathlib import Path
from runpy import run_path
import struct


HERE = Path(__file__).resolve().parent
PREP = run_path(str(HERE / "summarize-raid-preparation.py"))
NATIVE_STACKS = run_path(str(HERE / "summarize-raid-guest-native-stacks.py"))
RECORD = struct.Struct("<QQQI9I")
MEMORY_REGIONS = ((0x880C70, 0x882F20), (0x964CA0, 0x9677BE), (0x967F20, 0x9689D5),
                  (0x979130, 0x979B7A), (0x97B830, 0x97DBB8), (0x97E390, 0x980B2A))
ADDRESS_MASK = (1 << 32) - 1
CAPACITY = 1 << 23
MATRIX_RETURNS = {0x8C17C0: 0x8C1856, 0x8C1E90: 0x8C23DF, 0x8C2F50: 0x8C2FDE, 0x8C3880: 0x8C39A0}


@cache
def memory_site(pc):
    return any(start <= pc < end for start, end in MEMORY_REGIONS)


def records(capture, first, end):
    stride = PREP["STRIDE"]
    gates = json.loads((capture / "prep-gates.json").read_text())

    last_tick_by_cpu = {}

    def checked_rows(raw, start, stop):
        for index in range(start, stop):
            seq, tick, cpu, pc, *words, tag = RECORD.unpack_from(raw, (index + 1) * stride)
            valid = pc in PREP["SITES"] if not tag else tag in (1, 2, 3, 5) and memory_site(pc)
            if seq != index + 1 or not last_tick_by_cpu.get(cpu, first) <= tick < end or not cpu or not valid:
                raise ValueError(f"Invalid item trace row {index} in {capture}")
            yield tick, cpu, pc, tag, words
            last_tick_by_cpu[cpu] = tick

    with (capture / "raid-prep.bin").open("rb") as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as raw:
        count, enabled = struct.unpack_from("<QQ", raw)
        if (len(raw) != (CAPACITY + 1) * stride or enabled or not 0 < count < CAPACITY
                or len(gates) != 3 or gates[-1][1] != count):
            raise ValueError(f"Incomplete item trace in {capture}")
        previous_index = 0
        for start, stop in gates:
            if start != previous_index or not start < stop <= count:
                raise ValueError(f"Missing item trace rows in {capture}")
            first_tick, = struct.unpack_from("<Q", raw, (start + 1) * stride + 8)
            last_tick, = struct.unpack_from("<Q", raw, stop * stride + 8)
            yield stop - start, first_tick, last_tick, checked_rows(raw, start, stop)
            previous_index = stop


@dataclass
class Item:
    name: str
    frame: int
    stack: int
    number: int
    context: int = 0
    tag: int = 0
    index: int = 0
    reads: dict = field(default_factory=dict)
    writes: dict = field(default_factory=dict)
    possible_writes: set = field(default_factory=set)
    mem_count: int = 0
    outside_calls: Counter = field(default_factory=Counter)
    unknown_accesses: int = 0
    unsupported_operands: int = 0
    started_ns: int = 0
    elapsed_ns: int = 0
    access_times: tuple = ()
    flag_word_touches: tuple = ()

    def mark(self, address, width, access, pc, tick, known_clears):
        if not 1 <= width <= 16 or access not in (1, 2, 3, 4):
            raise ValueError(f"Invalid memory operand at {pc:#x}")
        self.mem_count += 1
        self.unknown_accesses += access == 4
        if self.flag_word_touches:
            exact_clear = pc in known_clears and access == 3 and width == 4 and address % 4 == 0
            touched = self.flag_word_touches[0 if exact_clear else 1]
            for offset in range(0, (address % 4) + width, 4):
                touched.add(((address + offset) & ADDRESS_MASK) & ~3)
        for offset in range(width):
            byte = (address + offset) & ADDRESS_MASK
            # Count only dependencies outside the current and nested call stack.
            if self.stack - 4096 <= byte < self.stack + 8:
                continue
            if access == 4:
                self.possible_writes.add(byte)
                self.writes.pop(byte, None)
                continue
            if access != 2 and byte not in self.writes and byte not in self.possible_writes:
                self.reads.setdefault(byte, pc)
                if self.access_times:
                    self.access_times[0].setdefault(byte, tick)
            if access != 1:
                self.writes[byte] = pc
                if self.access_times:
                    self.access_times[1][byte] = tick


def completed_items(rows, swaps, first_tick, last_tick, dispatch_access_times=False, known_clears=()):
    pending = defaultdict(dict)
    complete = range(bisect_right(swaps, first_tick) + 1, bisect_right(swaps, last_tick))
    for tick, cpu, pc, kind, words in rows:
        items = pending[cpu]
        if kind:
            for item in items.values():
                if kind in (2, 5):
                    item.outside_calls[pc] += 1
                item.unsupported_operands += kind == 3
                if kind in (1, 5):
                    address, width, access = words[:3]
                    if pc == PREP["DISPATCH_BEGIN"] and item.name == "dispatch_body":
                        if (address, width, access) != (item.context + 0x50, 4, 2):
                            raise ValueError("Probe disagrees with the known dispatch context store")
                    item.mark(address, width, access, pc, tick, known_clears)
            continue
        eax, ebx, ecx, edx, _, _, ebp, esp = words
        if pc in PREP["STARTS"]:
            name = PREP["STARTS"][pc]
            key = (name, ebp, esp)
            if key in items:
                raise ValueError("Recursively overlapping preparation scope")
            if name == "dispatch_body":
                item = Item(name, bisect_right(swaps, tick), ebp, edx, eax, ecx, ebx)
                if dispatch_access_times:
                    item.access_times = ({}, {})
                if known_clears:
                    item.flag_word_touches = (set(), set())
            else:
                item = Item(name, bisect_right(swaps, tick), ebp, ebx)
            item.started_ns = tick
            items[key] = item
        elif pc in PREP["ENDS"]:
            name = PREP["ENDS"][pc]
            item = items.pop((name, ebp, esp), None)
            if item is not None and item.frame in complete and item.frame == bisect_right(swaps, tick):
                if (name != "dispatch_body" and item.number != ebx) or not item.mem_count:
                    raise ValueError("Item identity or memory coverage changed")
                if name == "dispatch_body" and item.writes.get(item.context + 0x50) != PREP["DISPATCH_BEGIN"]:
                    # A permitted later write can replace the initial publication.
                    if item.context + 0x50 not in item.writes:
                        raise ValueError("Missing initial dispatch publication")
                item.elapsed_ns = tick - item.started_ns
                if item.elapsed_ns <= 0:
                    raise ValueError("Preparation scope timestamps did not advance")
                yield cpu, item, ebx


def compare(previous, current, counts, sites):
    counts["pairs"] += 1
    counts["pairs_with_untraced_calls"] += bool(previous.outside_calls or current.outside_calls)
    raw = previous.writes.keys() & current.reads.keys()
    counts["pairs_with_read_after_write"] += bool(raw)
    counts["pairs_with_write_after_write"] += bool(previous.writes.keys() & current.writes.keys())
    shared_context = previous.context and previous.context == current.context
    witnesses = set()
    excludes_context = False
    for byte in raw:
        if shared_context and current.context + 0x50 <= byte < current.context + 0xA0:
            area = f"context+{(byte - current.context) & ~3:#x}"
        elif previous.number == current.number and current.number <= byte < current.number + 0x300:
            area = f"same_item+{(byte - current.number) & ~3:#x}"
            excludes_context = True
        else:
            area = "other"
            excludes_context = True
        witnesses.add((previous.writes[byte], current.reads[byte], area))
    counts["pairs_with_read_after_write_outside_dispatch_fields"] += excludes_context
    sites.update(witnesses)


def compare_consumer_with_later_updates(later, consumer, counts):
    if not later:
        return
    counts["consumers_with_later_updates"] += 1
    counts["later_update_consumer_pairs"] += len(later)
    conflicts = 0
    for update in later:
        access_conflict = (not update.writes.keys().isdisjoint(consumer.reads) or
                           not update.reads.keys().isdisjoint(consumer.writes) or
                           not update.writes.keys().isdisjoint(consumer.writes))
        conflicts += access_conflict
    counts["later_update_consumer_pairs_with_conflict"] += conflicts
    counts["consumers_with_conflicting_later_updates"] += bool(conflicts)


def summarize_gate(gate, swaps):
    row_count, first_tick, last_tick, rows = gate
    recent = {}
    updates = {}
    update_order = {}
    scopes = defaultdict(Counter)
    comparisons = defaultdict(Counter)
    witnesses = defaultdict(Counter)
    escape_sites = defaultdict(Counter)
    tag_pairs = defaultdict(Counter)
    consumer_overlaps = Counter()
    active_frame = {}
    for cpu, item, returned_index in completed_items(rows, swaps, first_tick, last_tick):
        if item.frame != active_frame.get(cpu):
            active_frame[cpu] = item.frame
            updates[cpu] = {}
            update_order[cpu] = defaultdict(list)
        counts = scopes[item.name]
        counts["observed_scopes"] += 1
        counts["memory_operands"] += item.mem_count
        counts["scopes_with_untraced_calls"] += bool(item.outside_calls)
        counts["untraced_calls"] += sum(item.outside_calls.values())
        escape_sites[item.name].update(item.outside_calls)
        counts["unknown_memory_destination_operands"] += item.unknown_accesses
        counts["unsupported_operands"] += item.unsupported_operands
        key = (cpu, item.name, item.stack)
        prior = recent.get(key)
        if prior:
            previous, previous_index = prior
            adjacent = previous.frame == item.frame and previous.number != item.number
            if item.name == "dispatch_body":
                adjacent = adjacent and previous.context == item.context and previous_index + 1 == item.index
            if adjacent:
                compare(previous, item, comparisons[item.name], witnesses[item.name])
                if item.name == "dispatch_body":
                    tag_count = tag_pairs[f"{previous.tag}->{item.tag}"]
                    tag_count["pairs"] += 1
                    tag_count["pairs_with_read_after_write"] += not previous.writes.keys().isdisjoint(item.reads)
        recent[key] = item, returned_index
        object_key = (item.stack, item.number)
        if item.name == "list_update":
            order = update_order[cpu][item.stack]
            updates[cpu][object_key] = len(order)
            order.append(item)
        elif item.name == "list_consumer" and object_key in updates[cpu]:
            order = update_order[cpu][item.stack]
            index = updates[cpu][object_key]
            compare(order[index], item, comparisons["same_item_update_to_consumer"],
                    witnesses["same_item_update_to_consumer"])
            later = [update for update in order[index + 1:] if update.number != item.number]
            compare_consumer_with_later_updates(later, item, consumer_overlaps)
    if not scopes or not comparisons:
        raise ValueError("No complete preparation scopes with memory observations")
    return {"probe_rows": row_count, "scopes": dict(scopes), "comparisons": dict(comparisons),
            "consumer_vs_later_updates_of_other_items": dict(consumer_overlaps),
            "dispatch_adjacent_pairs_by_input_tag": dict(sorted(tag_pairs.items())),
            "most_frequent_untraced_call_sites": {
                name: [{"guest_call": hex(pc), "calls": count} for pc, count in counter.most_common(8)]
                for name, counter in escape_sites.items()},
            "raw_witnesses": {
                name: [{"previous_writer": hex(writer), "first_reader": hex(reader),
                        "region": region, "pairs": count}
                       for (writer, reader, region), count in sorted(sites.items(),
                                                                    key=lambda row: (-row[1], row[0]))[:12]]
                for name, sites in witnesses.items()
            }}


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def matrix_cpu_profiles(guest, captures):
    boundaries = set(MATRIX_RETURNS) | set(MATRIX_RETURNS.values())
    instructions = NATIVE_STACKS["guest_instructions"](guest, boundaries)
    for entry, ret in MATRIX_RETURNS.items():
        if instructions.get(entry, (None,))[0] != "pushl" or instructions.get(ret, (None,))[0] != "retl":
            raise ValueError("Matrix helper boundary differs from the original client")
    result = []
    for capture in captures:
        samples = NATIVE_STACKS["read_samples"](capture)
        total = sum(sample.weight for sample in samples)
        functions = []
        for entry, ret in MATRIX_RETURNS.items():
            weight = sum(sample.weight for sample in samples if entry <= sample.leaf <= ret)
            functions.append({"entry": hex(entry), "return_pc": hex(ret), "exclusive_cpu_usec": weight,
                              "exclusive_share_of_all_weighted_cpu": weight / total})
        result.append({"total_cpu_usec": total, "functions": functions})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_root", type=Path)
    parser.add_argument("guest_binary", type=Path)
    parser.add_argument("--cpu-profiles", type=Path, nargs=2, required=True)
    args = parser.parse_args()
    baseline = json.loads((HERE / "results/raid-aarchx-i386-item-preparation.json").read_text())
    witness = json.loads((args.capture_root / "artifact-sha256.json").read_text())
    guest = file_hash(args.guest_binary)
    if (guest != baseline["guest_image_sha256"] or
            witness["renderer"] != baseline["artifacts_sha256"]["renderer"] or
            witness["instrumented_runtime"] != file_hash(args.capture_root / "artifacts/observe/ocerz")):
        raise ValueError("Different guest, runtime or renderer from the diagnostic witness")
    captures = []
    for index in (1, 2):
        capture = args.capture_root / f"measured-{index}"
        window = PREP["VERIFY"](capture, "candidate", seconds=35)
        _, first, end = PREP["GPU_BOUNDS"](capture)
        swaps = PREP["SWAPS"](capture, first, end)
        gates = [summarize_gate(gate, swaps) for gate in records(capture, first, end)]
        captures.append({"verified_gpu_window": window, "gates": gates})
    config = ("hardware", "macos", "viewport", "vsync", "caster_count", "boss_count", "camera")
    print(json.dumps({**{key: baseline[key] for key in config}, "guest_image_sha256": guest,
                      "diagnostic_artifacts": witness, "verified_timed_script_rows": 26,
                      "captures": captures,
                      "earlier_matrix_function_cpu_profiles": matrix_cpu_profiles(args.guest_binary, args.cpu_profiles)},
                     indent=2))


if __name__ == "__main__":
    main()
