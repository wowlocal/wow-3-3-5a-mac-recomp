"""Bound worker speedup using observed per-item dependencies in the real raid."""

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
from heapq import heappop, heappush
import json
from pathlib import Path
from runpy import run_path
from statistics import mean, median


HERE = Path(__file__).resolve().parent
MEMORY = run_path(str(HERE / "summarize-raid-item-memory.py"))
PREP = MEMORY["PREP"]
SCOPES = ("dispatch", "list_update", "list_consumer", "list_with_item_dependencies")
FLAG_CLEAR_PCS = {0x97B8B7, 0x97BD8F, 0x97BEC9, 0x97C19B, 0x97C292, 0x97C7E0}
POLICIES = (
    ("observed_reads_and_writes", True, False, False),
    ("read_dependencies_with_ordered_publication_assumed", False, False, False),
    ("isolated_dispatch_context", False, True, False),
    ("isolated_context_and_commuting_flag_clears", False, True, True),
)


def ordered_groups(items):
    dispatch, lists = defaultdict(list), defaultdict(list)
    for cpu, item, final_index in items:
        if item.name == "dispatch_body":
            dispatch[cpu, item.frame, item.stack, item.context].append((item, final_index))
        else:
            lists[cpu, item.frame, item.stack].append(item)
    result = defaultdict(lambda: defaultdict(list))
    for (_, frame, _, _), entries in dispatch.items():
        batch = []
        for item, final_index in entries:
            if batch and item.index != batch[-1].index + 1:
                result[frame]["dispatch"].append(batch)
                batch = []
            if final_index != item.index:
                if batch:
                    result[frame]["dispatch"].append(batch)
                    batch = []
                result[frame]["dispatch"].append([item])
            else:
                batch.append(item)
        if batch:
            result[frame]["dispatch"].append(batch)
    for (_, frame, _), batch in lists.items():
        if len({item.name for item in batch}) == 2:
            result[frame]["list_with_item_dependencies"].append(batch)
        for name in ("list_update", "list_consumer"):
            selected = [item for item in batch if item.name == name]
            if selected:
                result[frame][name].append(selected)
    return result


def verified_flag_clears(guest):
    patterns = {
        0: ("andl", "$-0x2, 0x108(%edx,%eax,4)"),
        8: ("movl", "0x10d9a40, %ecx"),
        14: ("shll", "$0x6, %eax"),
        17: ("addl", "%eax, %edx"),
    }
    expected = {pc + offset: instruction for pc in FLAG_CLEAR_PCS for offset, instruction in patterns.items()}
    actual = MEMORY["NATIVE_STACKS"]["guest_instructions"](guest, expected.keys())
    if expected != {pc: (op, operands.strip()) for pc, (op, operands) in actual.items()}:
        raise ValueError("Original graphics flag clears differ from their expected instructions")
    return FLAG_CLEAR_PCS


def prerequisites(items, include_ordered_writes, isolate_dispatch_context, commute_flag_clears=False):
    predecessors, last_writer, readers, last_update = [], {}, defaultdict(set), {}
    last_regular_writer = {}
    last_consumer = None
    for number, item in enumerate(items):
        start, stop = item.context + 0x50, item.context + 0xA0
        private_candidates = isolate_dispatch_context and item.name == "dispatch_body" and item.context
        # These fields still require a correct ordered producer in a real implementation.
        if private_candidates:
            reads = {byte for byte in item.reads if not start <= byte < stop}
            writes = {byte for byte in item.writes if not start <= byte < stop}
        else:
            reads, writes = item.reads.keys(), item.writes.keys()
        if item.possible_writes:
            reads = set(reads) | item.possible_writes
            writes = set(writes) | item.possible_writes
        clears = item.flag_word_touches[0] - item.flag_word_touches[1] if commute_flag_clears else set()
        needs = set()
        for byte in reads:
            if byte not in last_writer:
                continue
            previous, was_clear = last_writer[byte]
            if was_clear and (byte & ~3) in clears:
                if byte in last_regular_writer:
                    needs.add(last_regular_writer[byte])
            else:
                needs.add(previous)
        if include_ordered_writes:
            for byte in writes:
                if byte in last_writer:
                    needs.add(last_writer[byte][0])
                needs.update(readers.pop(byte, ()))
            for byte in reads - writes:
                readers[byte].add(number)
        if item.name == "list_consumer":
            if last_consumer is not None:
                needs.add(last_consumer)
            if item.number in last_update:
                needs.add(last_update[item.number])
            last_consumer = number
        elif item.name == "list_update":
            last_update[item.number] = number
        for byte in writes:
            is_clear = (byte & ~3) in clears
            last_writer[byte] = number, is_clear
            if commute_flag_clears and not is_clear:
                last_regular_writer[byte] = number
        predecessors.append(needs)
    return predecessors


def schedule(predecessors, weights, workers=4):
    remaining = list(map(len, predecessors))
    successors = [[] for _ in weights]
    for index, needs in enumerate(predecessors):
        if any(prior >= index for prior in needs):
            raise ValueError("Task edges do not follow the recorded order")
        for prior in needs:
            successors[prior].append(index)
    tail = list(weights)
    for index in range(len(weights) - 1, -1, -1):
        tail[index] += max((tail[after] for after in successors[index]), default=0)
    ready, running = [], []
    for index, count in enumerate(remaining):
        if not count:
            heappush(ready, (-tail[index], index))
    now = finished = 0
    while ready or running:
        while ready and len(running) < workers:
            _, index = heappop(ready)
            heappush(running, (now + weights[index], index))
        now = running[0][0]
        while running and running[0][0] == now:
            _, index = heappop(running)
            finished += 1
            for after in successors[index]:
                remaining[after] -= 1
                if not remaining[after]:
                    heappush(ready, (-tail[after], after))
    critical_path = max(tail)
    if finished != len(weights) or not critical_path <= now <= sum(weights):
        raise ValueError("Invalid simulated schedule")
    return critical_path, now


def access_time_bound(items, weights, isolate_dispatch_context, binding_delays=None, commute_flag_clears=False):
    """Use offsets of observed live-in reads and final writes within each task."""
    last_writer, last_regular_writer = {}, {}
    last_start = finished = 0
    for item, work in zip(items, weights):
        reads, writes = item.access_times
        scale = work / item.elapsed_ns
        start_offset = item.started_ns
        isolate = isolate_dispatch_context and item.context
        clears = item.flag_word_touches[0] - item.flag_word_touches[1] if commute_flag_clears else set()
        lo, hi = item.context + 0x50, item.context + 0xA0
        earliest = last_start
        binding_edge = None
        for byte in item.reads.keys() | item.possible_writes:
            if isolate and lo <= byte < hi and byte not in item.possible_writes:
                continue
            read = reads.get(byte, item.started_ns)
            if not start_offset <= read <= start_offset + item.elapsed_ns:
                raise ValueError("Read offset falls outside the task")
            if byte in last_writer:
                available, writer, was_clear = last_writer[byte]
                if was_clear and (byte & ~3) in clears:
                    if byte not in last_regular_writer:
                        continue
                    available, writer = last_regular_writer[byte]
                dependency_start = available - (read - start_offset) * scale
                if dependency_start > earliest:
                    earliest = dependency_start
                    area = f"context+{(byte - item.context) & ~3:#x}" if item.context and lo <= byte < hi else "other"
                    binding_edge = writer, item.reads.get(byte, 0), area
        for byte in item.writes.keys() | item.possible_writes:
            if isolate and lo <= byte < hi and byte not in item.possible_writes:
                continue
            written = writes.get(byte, start_offset + item.elapsed_ns)
            if not start_offset <= written <= start_offset + item.elapsed_ns:
                raise ValueError("Write offset falls outside the task")
            available = earliest + (written - start_offset) * scale
            writer = item.writes.get(byte, 0)
            is_clear = (byte & ~3) in clears
            last_writer[byte] = available, writer, is_clear
            if commute_flag_clears and not is_clear:
                last_regular_writer[byte] = available, writer
        if binding_delays is not None and binding_edge is not None:
            binding_delays[binding_edge] += earliest - last_start
        finished = max(finished, earliest + work)
        last_start = earliest
    return finished


def timing_calibration(root):
    prior = json.loads((HERE / "results/raid-aarchx-i386-item-preparation.json").read_text())
    artifact = json.loads((root / "artifact-sha256.json").read_text())
    if (artifact != prior["artifacts_sha256"] or
            MEMORY["file_hash"](root / "artifacts/observe/ocerz") != artifact["instrumented_runtime"]):
        raise ValueError("Timing-only probe artifacts differ from the recorded comparison")
    weights = defaultdict(list)
    frames = 0
    for index in (1, 2):
        capture = root / f"measured-{index}"
        witness = prior["captures"][index - 1]
        window = PREP["VERIFY"](capture, "candidate", seconds=35, check_legacy_witness=False)
        if window != witness["diagnostic_gpu_window"] or witness["legacy_gpu_isolation"]["completed_outputs_during_trial_output"]:
            raise ValueError("Calibration does not match the previously verified timing window")
        _, first, stop = PREP["GPU_BOUNDS"](capture)
        swaps = PREP["SWAPS"](capture, first, stop)
        for gate, rows in enumerate(PREP["records"](capture, first, stop)):
            expected = PREP["summarize_gate"](rows, swaps)
            if expected != witness["gates"][gate]:
                raise ValueError("Calibration does not match the previously verified probe gate")
            complete = range(bisect_right(swaps, rows[0][0]) + 1, bisect_right(swaps, rows[-1][0]))
            frames += len(complete)
            pending = {}
            counts = defaultdict(int)
            for tick, cpu, pc, registers in rows:
                frame = bisect_right(swaps, tick)
                _, _, tag, _, _, _, ebp, esp = registers
                if pc in PREP["STARTS"]:
                    name = PREP["STARTS"][pc]
                    pending[cpu, ebp, esp, name] = tick, frame, tag if name == "dispatch_body" else 0
                if pc in PREP["ENDS"]:
                    name = PREP["ENDS"][pc]
                    entry = pending.pop((cpu, ebp, esp, name), None)
                    if entry is not None and entry[1] == frame and frame in complete:
                        weights[name, entry[2]].append(tick - entry[0])
                        counts[name] += 1
            if any(counts[name] != summary["total_pairs_in_complete_frames"]
                   for name, summary in expected["work_per_frame"].items()):
                raise ValueError("Calibration lost a timed probe scope")
    return weights, frames


def evaluate(frames, calibration):
    result = {}
    for scope in SCOPES:
        captured = [groups[scope] for groups in frames if groups.get(scope)]
        result[scope] = {
            "frames": len(captured),
            "tasks": sum(len(batch) for frame in captured for batch in frame),
            "workgroups": sum(map(len, captured)),
            "tasks_with_untraced_callees": sum(bool(item.outside_calls)
                                               for frame in captured for batch in frame for item in batch),
            "policies": {},
        }
        for name, ordered_writes, isolated_context, commuting_clears in POLICIES:
            if isolated_context and scope != "dispatch":
                continue
            fractions = defaultdict(list)
            overlap_fractions = defaultdict(list)
            binding_delays = Counter()
            edge_count = 0
            for frame in captured:
                path = defaultdict(int)
                worker = defaultdict(int)
                serial = defaultdict(int)
                overlap = defaultdict(float)
                worker_lower_bound = defaultdict(float)
                for batch in frame:
                    if any(a.started_ns + a.elapsed_ns > b.started_ns for a, b in zip(batch, batch[1:])):
                        raise ValueError("Task boundaries are not serial in the original capture")
                    predecessors = prerequisites(batch, ordered_writes, isolated_context, commuting_clears)
                    edge_count += sum(map(len, predecessors))
                    models = {
                        "equal_costs": [1] * len(batch),
                        "instrumented_elapsed_costs": [item.elapsed_ns for item in batch],
                    }
                    for label, by_tag in calibration.items():
                        models[label] = [by_tag[item.name, item.tag if item.name == "dispatch_body" else 0]
                                         for item in batch]
                    for model, weights in models.items():
                        critical, simulated = schedule(predecessors, weights)
                        path[model] += critical
                        worker[model] += simulated
                        serial[model] += sum(weights)
                        if scope == "dispatch" and not ordered_writes:
                            delays = binding_delays if model == "timing_only_median_by_tag" else None
                            lower = access_time_bound(batch, weights, isolated_context, delays, commuting_clears)
                            overlap[model] += lower
                            worker_lower_bound[model] += max(lower, sum(weights) / 4)
                for model, conventional in serial.items():
                    fractions[model].append((path[model] / conventional, worker[model] / conventional))
                    if model in overlap:
                        overlap_fractions[model].append((overlap[model] / conventional,
                                                         worker_lower_bound[model] / conventional))
            result[scope]["policies"][name] = {
                "observed_edges": edge_count,
                "weighted_results": {
                    model: {
                        "median_critical_path_share_of_serial": median(ratio[0] for ratio in ratios),
                        "median_four_worker_share_of_serial": median(ratio[1] for ratio in ratios),
                        "modeled_frames": len(ratios),
                    }
                    for model, ratios in fractions.items()
                },
            }
            if overlap_fractions:
                result[scope]["policies"][name]["access_offset_bounds"] = {
                    model: {
                        "median_unlimited_worker_lower_bound_share": median(ratio[0] for ratio in ratios),
                        "median_four_worker_lower_bound_share": median(ratio[1] for ratio in ratios),
                    }
                    for model, ratios in overlap_fractions.items()
                }
                delay_total = sum(binding_delays.values())
                result[scope]["policies"][name]["largest_binding_delays_with_calibrated_median_costs"] = [
                    {"previous_writer": hex(writer), "next_reader": hex(reader), "region": area,
                     "modeled_start_delay_share": elapsed / delay_total}
                    for (writer, reader, area), elapsed in binding_delays.most_common(8)
                ]
    return result


def summarize(root, guest, timing_root):
    earlier = json.loads((HERE / "results/raid-aarchx-i386-item-memory.json").read_text())
    artifact = json.loads((root / "artifact-sha256.json").read_text())
    if (artifact != earlier["diagnostic_artifacts"] or
            MEMORY["file_hash"](guest) != earlier["guest_image_sha256"] or
            MEMORY["file_hash"](root / "artifacts/observe/ocerz") != artifact["instrumented_runtime"]):
        raise ValueError("Wrong game image or diagnostic runtime")
    calibration, calibrated_frames = timing_calibration(timing_root)
    known_clears = verified_flag_clears(guest)
    frames, captures = [], []
    for number in (1, 2):
        folder = root / f"measured-{number}"
        window = PREP["VERIFY"](folder, "candidate", seconds=35)
        _, first, last = PREP["GPU_BOUNDS"](folder)
        swaps = PREP["SWAPS"](folder, first, last)
        gates = []
        for count, begin, end, rows in MEMORY["records"](folder, first, last):
            items = list(MEMORY["completed_items"](rows, swaps, begin, end, dispatch_access_times=True,
                                                   known_clears=known_clears))
            grouped = ordered_groups(items)
            gates.append({"probe_rows": count, "completed_scopes": len(items), "matched_frames": len(grouped)})
            frames.extend(grouped.values())
        captures.append({"verified_gpu_window": window, "gates": gates})
    costs = {
        label: {key: max(1, round(aggregate(samples))) for key, samples in calibration.items()}
        for label, aggregate in (("timing_only_median_by_tag", median), ("timing_only_mean_by_tag", mean))
    }
    return {
        "guest_image_sha256": earlier["guest_image_sha256"],
        "diagnostic_artifacts": artifact,
        "captures": captures,
        "timing_only_calibration_frames": calibrated_frames,
        "verified_flag_clear_sites": [hex(pc) for pc in sorted(known_clears)],
        "timing_only_calibration": [
            {"scope": key[0], "tag": key[1], "samples": len(samples),
             "median_elapsed_ns": costs["timing_only_median_by_tag"][key],
             "mean_elapsed_ns": costs["timing_only_mean_by_tag"][key]}
            for key, samples in sorted(calibration.items())
        ],
        "scopes": evaluate(frames, costs),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_root", type=Path)
    parser.add_argument("guest_binary", type=Path)
    parser.add_argument("timing_root", type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize(args.capture_root, args.guest_binary, args.timing_root), indent=2))
