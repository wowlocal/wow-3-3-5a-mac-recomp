"""Measure the original quad-emitter batches and their observed memory ranges."""

import argparse
from collections import Counter
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
from runpy import run_path
from statistics import median


HERE = Path(__file__).resolve().parent
LAYOUT = run_path(str(HERE / "summarize-raid-quad-layout.py"))
ASM = run_path(str(HERE / "summarize-raid-guest-native-stacks.py"))
WRAPPER_BEGIN = 0x882C89
WRAPPER_ENDS = {0x882CB9, 0x882E1A}
CALL_INPUT_BYTES = {0x882CE8: 32, 0x882D1C: 64, 0x882EFE: 64}
CALL_RETURNS = {pc + 5: pc for pc in CALL_INPUT_BYTES}
SITES = LAYOUT["WINDOWS"] | {WRAPPER_BEGIN} | WRAPPER_ENDS | set(CALL_INPUT_BYTES) | set(CALL_RETURNS)


@dataclass
class Call:
    pc: int
    start: int
    input_address: int
    end: int = 0
    edges: list = field(default_factory=list)


@dataclass
class Batch:
    start: int
    frame: int
    context: int
    cursor: int
    count: int
    calls: list = field(default_factory=list)


def captured_batches(rows):
    active = {}
    for tick, cpu, data in rows:
        pc, frame, context, cursor, count = data[:5]
        batch = active.get(cpu)
        if pc == WRAPPER_BEGIN:
            if batch is not None:
                raise ValueError("Overlapping batch on one guest thread")
            active[cpu] = Batch(tick, frame, context, cursor, count)
        elif batch is None:
            continue
        elif pc in LAYOUT["WINDOWS"]:
            if pc in (LAYOUT["BEGIN"], LAYOUT["END"]) and batch.calls and not batch.calls[-1].end:
                batch.calls[-1].edges.append((tick, data))
        else:
            if data[1:5] != [batch.frame, batch.context, batch.cursor, batch.count]:
                raise ValueError("Wrapper arguments changed during batch")
            call = batch.calls[-1] if batch.calls else None
            if pc in CALL_INPUT_BYTES:
                input_address, index = data[5:7]
                if index != len(batch.calls) + 1 or (call and not call.end):
                    raise ValueError("Unpaired or out-of-order emitter call")
                batch.calls.append(Call(pc, tick, input_address))
            elif pc in CALL_RETURNS:
                if call is None or call.end or call.pc != CALL_RETURNS[pc]:
                    raise ValueError("Emitter returned to a different call site")
                call.end = tick
            elif pc in WRAPPER_ENDS:
                if (call and not call.end) or len(batch.calls) != batch.count:
                    raise ValueError("Original batch count and completed calls differ")
                del active[cpu]
                yield batch, tick


def has_cross_overlap(outputs, sources):
    output_index = source_index = 0
    while output_index < len(outputs) and source_index < len(sources):
        output, source = outputs[output_index], sources[source_index]
        if output[1] <= source[0]:
            output_index += 1
        elif source[1] <= output[0]:
            source_index += 1
        else:
            return True
    return False


def classify_ranges(batch):
    recorded_layout = (LAYOUT["BEGIN"], LAYOUT["END"])
    if any(edge_sites(call) != recorded_layout for call in batch.calls):
        return "incomplete_layout_coverage"
    interval = LAYOUT["interval"]
    outputs = []
    sources = [interval(batch.context, 0x230), interval(batch.cursor, 36), interval(0xF4AD00, 12)]
    stable_normals = {}
    for call in batch.calls:
        (_, first), (_, last) = call.edges
        if first[2:4] != [batch.cursor, batch.context] or first[1:5] != last[1:5]:
            return "changed_layout_owner"
        addresses, strides = first[5:9], first[9:13]
        if (strides != last[9:13] or last[13] != ((first[13] + 4) & 0xFFFFFFFF) or
                last[5:9] != [(address + 4 * stride) & 0xFFFFFFFF
                              for address, stride in zip(addresses, strides)]):
            raise ValueError("Emitter cursor did not advance by four vertices")
        sources.extend([interval(call.input_address, CALL_INPUT_BYTES[call.pc]), interval(first[4], 4)])
        for stream, (address, stride, size) in enumerate(zip(addresses, strides, LAYOUT["STORE_WIDTHS"])):
            if stream == 1 and not stride:
                value = first[14:17]
                if value != last[14:17]:
                    return "changing_repeated_normal"
                previous = stable_normals.get(address)
                if previous is None:
                    stable_normals[address] = value
                    outputs.append(interval(address, size))
                elif previous != value:
                    return "changing_repeated_normal"
            else:
                outputs.extend(interval(address + vertex * stride, size) for vertex in range(4))
    if None in outputs or None in sources:
        return "wrapped_or_null_address"
    outputs.sort()
    if (any(LAYOUT["overlaps"](a, b) for a, b in zip(outputs, outputs[1:])) or
            has_cross_overlap(outputs, sorted(sources))):
        return "tracked_memory_overlap"
    return "disjoint_tracked_ranges"


def edge_sites(call):
    return tuple(data[0] for _, data in call.edges)


def four_worker_savings(tasks, overhead):
    total = sum(tasks)
    return max(0.0, total - max(max(tasks), total / 4) - overhead)


def summarize_gate(rows):
    totals = Counter()
    batch_sizes = []
    by_class = Counter()
    work_by_class = Counter()
    calls_by_site = Counter()
    modeled_prefix_savings = dict.fromkeys((0, 1000, 2000, 4000), 0.0)
    for batch, finish in captured_batches(rows):
        if not batch.calls:
            totals["empty_batches"] += 1
            continue
        status = classify_ranges(batch)
        call_times = [call.end - call.start for call in batch.calls]
        emitter_ns = sum(call_times)
        if emitter_ns > finish - batch.start:
            raise ValueError("Emitter calls overlap")
        by_class[status] += 1
        work_by_class[status] += emitter_ns
        calls_by_site.update(hex(call.pc) for call in batch.calls)
        totals["emitter_calls"] += batch.count
        totals["paired_emission_layouts"] += sum(
            edge_sites(call) == (LAYOUT["BEGIN"], LAYOUT["END"]) for call in batch.calls
        )
        totals["observed_emitter_thread_elapsed_ns"] += emitter_ns
        totals["observed_wrapper_thread_elapsed_ns"] += finish - batch.start
        if status == "disjoint_tracked_ranges":
            if all(a.edges[-1][1][5:14] == b.edges[0][1][5:14]
                   for a, b in zip(batch.calls, batch.calls[1:])):
                totals["disjoint_batches_with_contiguous_cursor"] += 1
            prefixes = [call.edges[0][0] - call.start for call in batch.calls]
            totals["before_tracked_writes_in_disjoint_batches_ns"] += sum(prefixes)
            for overhead in modeled_prefix_savings:
                modeled_prefix_savings[overhead] += four_worker_savings(prefixes, overhead)
            for threshold in (8, 16, 32, 64):
                if batch.count >= threshold:
                    totals[f"disjoint_batches_at_least_{threshold}"] += 1
                    totals[f"disjoint_emitter_ns_in_batches_at_least_{threshold}"] += emitter_ns
        batch_sizes.append(batch.count)
    if not batch_sizes:
        raise ValueError("No complete nonempty wrapper batches")
    return {"recorded_rows": len(rows), "nonempty_batches": len(batch_sizes),
            "median_emitter_calls_per_batch": median(batch_sizes), "max_emitter_calls_per_batch": max(batch_sizes),
            "batches_by_layout_class": dict(sorted(by_class.items())),
            "emitter_thread_ns_by_layout_class": dict(sorted(work_by_class.items())),
            "emitter_calls_by_site": dict(sorted(calls_by_site.items())),
            "four_worker_ideal_prefix_ns_saved_by_batch_overhead_ns": modeled_prefix_savings,
            **dict(sorted(totals.items()))}


def profile_share(capture):
    samples = ASM["read_samples"](capture)
    total = sum(sample.weight for sample in samples)
    wrapper = sum(sample.weight for sample in samples if set(CALL_RETURNS).intersection(sample.returns))
    leaf = sum(sample.weight for sample in samples if 0x880C70 <= sample.leaf < 0x882C80)
    return {"all_weighted_cpu_usec": total, "sample_weight_with_emitter_return_fraction": wrapper / total,
            "exclusive_emitter_fraction": leaf / total}


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_root", type=Path)
    parser.add_argument("guest_binary", type=Path)
    parser.add_argument("--cpu-profiles", type=Path, nargs=2, required=True)
    args = parser.parse_args()
    baseline = json.loads((HERE / "results/raid-aarchx-i386-item-preparation.json").read_text())
    guest_sha = sha256(args.guest_binary)
    if guest_sha != baseline["guest_image_sha256"]:
        raise ValueError("Probe requires the profiled i386 guest")
    instructions = ASM["guest_instructions"](args.guest_binary, set(CALL_INPUT_BYTES))
    if any(op != "calll" or operand.strip() != "0x880c70" for op, operand in instructions.values()):
        raise ValueError("Emitter call target differs from the probed instructions")
    if set(instructions) != set(CALL_INPUT_BYTES):
        raise ValueError("A probed call site is missing from the original guest")
    artifacts = json.loads((args.capture_root / "artifact-sha256.json").read_text())
    if (artifacts["instrumented_runtime"] != sha256(args.capture_root / "artifacts/observe/ocerz") or
            artifacts["renderer"] != baseline["artifacts_sha256"]["renderer"]):
        raise ValueError("Wrong diagnostic runtime or renderer")
    captures = []
    for index in (1, 2):
        capture = args.capture_root / f"layout-{index}"
        window = LAYOUT["VERIFY"](capture, "candidate", seconds=35)
        _, start, end = LAYOUT["BOUNDS"]["WINDOW"](capture)
        captures.append({"verified_gpu_window": window,
                         "gates": [summarize_gate(rows) for rows in LAYOUT["read_gates"](capture, start, end, SITES)]})
    settings = ("hardware", "macos", "viewport", "vsync", "camera", "caster_count", "boss_count")
    result = {key: baseline[key] for key in settings}
    result.update(guest_image_sha256=guest_sha, diagnostic_artifacts=artifacts,
                  verified_timed_script_rows=26, captures=captures,
                  earlier_guest_cpu_profiles=[profile_share(capture) for capture in args.cpu_profiles])
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
