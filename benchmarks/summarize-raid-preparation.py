"""Measure two guest preparation loops at their executed i386 boundaries."""

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
from runpy import run_path
from statistics import median
import struct


HERE = Path(__file__).resolve().parent
VERIFY = run_path(str(HERE / "summarize-raid-renderer-pgo.py"))["verify"]
BOUNDS = run_path(str(HERE / "summarize-raid-vertex-bounds.py"))
GPU_BOUNDS = BOUNDS["WINDOW"]
SWAPS = BOUNDS["read_swaps"]
STACK = BOUNDS["STACK"]["GUEST"]
CAPACITY = 1 << 20
STRIDE = 64
RECORD = struct.Struct("<QQQI8I")
DISPATCH_NEXT, DISPATCH_BEGIN, DISPATCH_PUBLISH, DISPATCH_END = (
    0x97D2F0, 0x97D31D, 0x97D3B0, 0x97D404,
)
LIST_ITEM, UPDATE_BEGIN, UPDATE_END, CONSUME_BEGIN, CONSUME_END = (
    0x980636, 0x9806AE, 0x9806B3, 0x9801A7, 0x9801AC,
)
STARTS = {DISPATCH_BEGIN: "dispatch_body", UPDATE_BEGIN: "list_update", CONSUME_BEGIN: "list_consumer"}
ENDS = {DISPATCH_END: "dispatch_body", UPDATE_END: "list_update", CONSUME_END: "list_consumer"}
SITES = set(STARTS) | set(ENDS) | {DISPATCH_NEXT, DISPATCH_PUBLISH, LIST_ITEM}
CALL_RETURNS = {0x97D3A6, 0x97D4E3, 0x97D506, 0x97D526, 0x97D557, 0x97D571,
                UPDATE_END, CONSUME_END}
HEAVIEST_DISPATCH_RETURN = 0x97D557


def records(capture, first, end):
    raw = (capture / "raid-prep.bin").read_bytes()
    count, enabled = struct.unpack_from("<QQ", raw)
    gates = json.loads((capture / "prep-gates.json").read_text())
    if len(raw) != (CAPACITY + 1) * STRIDE or enabled or not 0 < count < CAPACITY:
        raise ValueError(f"Invalid probe ring in {capture}")
    if len(gates) != 3 or gates[0][0] != 0 or gates[-1][1] != count:
        raise ValueError(f"Invalid probe gate endpoints in {capture}")
    previous_index = 0
    previous_tick = first
    for start, stop in gates:
        if start != previous_index or not start < stop <= count:
            raise ValueError(f"Missing probe records between gates in {capture}")
        rows = []
        for index in range(start, stop):
            sequence, tick, cpu, pc, *regs = RECORD.unpack_from(raw, (index + 1) * STRIDE)
            if sequence != index + 1 or not previous_tick <= tick < end or not cpu or pc not in SITES:
                raise ValueError(f"Invalid probe row {index} in {capture}")
            rows.append((tick, cpu, pc, regs))
            previous_tick = tick
        previous_index = stop
        yield rows


def summarize_gate(rows, swaps):
    first, last = rows[0][0], rows[-1][0]
    complete = range(bisect_right(swaps, first) + 1, bisect_right(swaps, last))
    if len(complete) < 2 or len({cpu for _, cpu, _, _ in rows}) != 1:
        raise ValueError("Need complete frames on one guest CPU per gate")
    counts = Counter()
    frames = defaultdict(lambda: defaultdict(list))
    dispatch_tags = defaultdict(lambda: defaultdict(list))
    pending = {}
    published = {}
    previous_publish = {}
    previous_iteration = {}
    previous_list_item = {}
    list_updates_in_frame = set()
    for tick, cpu, pc, regs in rows:
        eax, ebx, ecx, edx, esi, _, ebp, esp = regs
        frame = bisect_right(swaps, tick)
        counts[hex(pc)] += 1
        if pc == DISPATCH_NEXT:
            previous_iteration[cpu, ebp, esp] = (frame, ebx)
        if pc == LIST_ITEM:
            previous_list_item[cpu, ebp, esp] = (frame, ebx)
        if pc in STARTS:
            name = STARTS[pc]
            key = (cpu, ebp, esp, name)
            if key in pending:
                raise ValueError(f"Overlapping {name} boundaries")
            pending[key] = tick, frame, ebx, eax, edx, esi, ecx
            if pc == DISPATCH_BEGIN and previous_iteration.get((cpu, ebp, esp)) == (frame, ebx):
                counts["dispatch_bodies_with_matching_iteration"] += 1
            if pc == UPDATE_BEGIN and previous_list_item.get((cpu, ebp, esp)) == (frame, ebx):
                counts["updates_with_matching_list_item"] += 1
            if pc == CONSUME_BEGIN and (cpu, frame, ebp, ebx) in list_updates_in_frame:
                counts["consumers_with_earlier_update_of_same_item"] += 1
        if pc == DISPATCH_PUBLISH:
            key = (cpu, ebp, esp, "dispatch_body")
            started = pending.get(key)
            if started and (started[3] != eax or not edx):
                raise ValueError("Dispatch publication changed context")
            if started:
                published[key] = (eax, started[4])
        if pc in ENDS:
            name = ENDS[pc]
            key = (cpu, ebp, esp, name)
            start = pending.pop(key, None)
            output = published.pop(key, None)
            if not start or start[1] != frame:
                counts[f"{name}_unpaired_or_cross_swap"] += 1
                continue
            if (name != "dispatch_body" and start[2] != ebx) or (name == "list_update" and start[5] != esi):
                raise ValueError(f"Mismatched {name} item at return")
            counts[f"{name}_paired"] += 1
            frames[frame][name].append((start[0], tick))
            if name == "list_update":
                list_updates_in_frame.add((cpu, frame, ebp, ebx))
            if name == "dispatch_body":
                dispatch_tags[start[6]][frame].append((start[0], tick))
            if output:
                counts["dispatch_paired_publications"] += 1
                counts["dispatch_bodies_adjusting_iteration_index"] += start[2] != ebx
                old = previous_publish.get(key)
                if old and old[0] == frame and old[1] + 1 == start[2] and old[2] == output[0]:
                    counts["dispatch_consecutive_same_context"] += 1
                    counts["dispatch_consecutive_different_item"] += old[3] != output[1]
                previous_publish[key] = (frame, ebx, *output)

    if any(not counts[hex(pc)] for pc in SITES):
        raise ValueError("A selected probe site was not executed")

    def summarize_intervals(by_frame):
        durations = []
        invocations = []
        for frame in complete:
            intervals = sorted(by_frame(frame))
            if any(left[1] > right[0] for left, right in zip(intervals, intervals[1:])):
                raise ValueError(f"Overlapping {name} intervals within a frame")
            invocations.append(len(intervals))
            durations.append(sum(end - start for start, end in intervals))
        return {
            "total_pairs_in_complete_frames": sum(invocations),
            "median_pairs_per_frame": median(invocations),
            "median_elapsed_ns_per_frame": median(durations),
            "min_elapsed_ns_per_frame": min(durations),
            "max_elapsed_ns_per_frame": max(durations),
        }

    return {
        "complete_swap_intervals": len(complete),
        "probe_rows": len(rows),
        "observed_span_ns": last - first,
        "counts": dict(sorted(counts.items())),
        "work_per_frame": {
            name: summarize_intervals(lambda frame: frames[frame][name]) for name in STARTS.values()
        },
        "dispatch_by_input_tag": {
            str(tag): summarize_intervals(lambda frame: by_frame[frame])
            for tag, by_frame in sorted(dispatch_tags.items())
        },
    }


def corroborate_call_weights(guest_binary, captures):
    samples = [STACK["read_stack_window"](capture)[1] for capture in captures]
    observed_returns = {pc for rows in samples for _, _, unwind in rows for pc in unwind}
    calls = STACK["find_i386_call_sites"](guest_binary, observed_returns | CALL_RETURNS)
    if not CALL_RETURNS <= calls.keys():
        raise ValueError("A selected return address does not follow a decoded call")
    ranges = STACK["read_unwind_ranges"](guest_binary)
    starts = [entry for entry, _ in ranges]
    reports = []
    for rows in samples:
        total = sum(weight for weight, _, _ in rows)
        weights = Counter()
        leaves = Counter()
        for weight, leaf, unwind in rows:
            validated = set()
            for pc in unwind:
                if pc not in calls:
                    break
                validated.add(pc)
            for pc in validated & CALL_RETURNS:
                weights[pc] += weight
            if HEAVIEST_DISPATCH_RETURN in validated:
                index = bisect_right(starts, leaf) - 1
                if index >= 0 and leaf < ranges[index][1]:
                    leaves[starts[index]] += weight
        reports.append({
            "total_cpu_usec": total,
            "inclusive_cpu_by_return": {
                hex(pc): {"cpu_usec": weights[pc], "share": weights[pc] / total}
                for pc in sorted(CALL_RETURNS)
            },
            "top_leaf_weights_under_0x97d557": [
                {"entry": hex(pc), "cpu_usec": weight, "share": weight / total}
                for pc, weight in leaves.most_common(8)
            ],
        })
    return reports


def gpu_times(log, pid):
    prefix = f"{pid} ".encode()
    with log.open("rb") as stream:
        for line in stream:
            if line.startswith(prefix) and line.endswith(b"\n"):
                _, _, _, timestamp, _, _ = line.split()
                yield int(timestamp)


def verify_legacy_log(log, pid, capture_spans):
    bounds = [{"before": 0, "after": None, "overlap": 0} for _ in capture_spans]
    for timestamp in gpu_times(log, pid):
        for (first, last), markers in zip(capture_spans, bounds):
            if timestamp < first:
                markers["before"] = max(timestamp, markers["before"])
            elif timestamp > last:
                if markers["after"] is None or timestamp < markers["after"]:
                    markers["after"] = timestamp
            else:
                markers["overlap"] += 1
    results = []
    for (first, last), markers in zip(capture_spans, bounds):
        if markers["overlap"] or not markers["before"] or markers["after"] is None:
            raise ValueError("Earlier client did not bracket the trial output with an idle interval")
        results.append({
            "completed_outputs_during_trial_output": markers["overlap"],
            "before_first_trial_output_ns": first - markers["before"],
            "after_last_trial_output_ns": markers["after"] - last,
        })
    return results


def summarize(root, guest_binary, stack_captures, legacy_gpu, legacy_pid):
    artifacts = json.loads((root / "artifact-sha256.json").read_text())
    runtime = root / "artifacts" / "observe" / "ocerz"
    baseline = json.loads((HERE / "results" / "raid-aarchx-gl-map-copy-policy.json").read_text())
    if hashlib.sha256(runtime.read_bytes()).hexdigest() != artifacts["instrumented_runtime"]:
        raise ValueError("Diagnostic runtime artifact changed")
    if artifacts["renderer"] != baseline["artifacts_sha256"]["renderer"]:
        raise ValueError("Different renderer from the accepted baseline")
    guest_hash = hashlib.sha256(guest_binary.read_bytes()).hexdigest()
    stack_report = HERE / "results" / "raid-aarchx-headless-i386-inclusive-stack-profile.json"
    expected = json.loads(stack_report.read_text())
    if guest_hash != expected["guest_image_sha256"]:
        raise ValueError("Probe addresses require the profiled guest image")
    captures = []
    gpu_spans = []
    for index in (1, 2):
        capture = root / f"measured-{index}"
        window = VERIFY(capture, "candidate", seconds=35, check_legacy_witness=False)
        if (capture / "shadow-bucket-mode.txt").read_text().split() != ["0", "0"]:
            raise ValueError(f"Wrong shadow lookup settings in {capture}")
        original_window, first, end = GPU_BOUNDS(capture)
        times = list(gpu_times(capture / "gpu.ns", original_window["pid"]))
        gpu_spans.append((min(times), max(times)))
        swaps = SWAPS(capture, first, end)
        captures.append({"diagnostic_gpu_window": window,
                         "gates": [summarize_gate(rows, swaps) for rows in records(capture, first, end)]})
    for capture, isolation in zip(captures, verify_legacy_log(legacy_gpu, legacy_pid, gpu_spans)):
        capture["legacy_gpu_isolation"] = isolation
    config = ("hardware", "macos", "viewport", "vsync", "caster_count", "boss_count", "camera")
    return {**{name: baseline[name] for name in config}, "base_commit": "5b0e910",
            "artifacts_sha256": artifacts, "guest_image_sha256": guest_hash,
            "verified_timed_script_rows": 26, "captures": captures,
            "previous_guest_stack_call_site_weights": corroborate_call_weights(guest_binary, stack_captures)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_root", type=Path)
    parser.add_argument("guest_binary", type=Path)
    parser.add_argument("--stack-captures", type=Path, nargs=2, required=True)
    parser.add_argument("--legacy-gpu", type=Path, required=True)
    parser.add_argument("--legacy-pid", type=int, required=True)
    args = parser.parse_args()
    print(json.dumps(summarize(args.capture_root, args.guest_binary, args.stack_captures,
                               args.legacy_gpu, args.legacy_pid), indent=2))


if __name__ == "__main__":
    main()
