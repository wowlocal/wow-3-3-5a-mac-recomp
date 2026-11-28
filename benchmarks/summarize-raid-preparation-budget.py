"""Compare GL call costs and a worker model using captured raid frames."""

import argparse
from bisect import bisect_right
from collections import defaultdict
import json
from pathlib import Path
from runpy import run_path
from statistics import median


HERE = Path(__file__).resolve().parent
PREP = run_path(str(HERE / "summarize-raid-preparation.py"))
TIMELINE = run_path(str(HERE / "summarize-gl-command-timeline.py"))
STATE_CALLS = {
    "glActiveTexture", "glClientActiveTexture", "glDisable",
    "glLightModelfv", "glVertexAttribPointer",
}


def dispatch_frames(rows, swaps):
    complete = range(bisect_right(swaps, rows[0][0]) + 1,
                     bisect_right(swaps, rows[-1][0]))
    frames = {frame: [] for frame in complete}
    pending = {}
    crossed = set()
    for tick, cpu, pc, registers in rows:
        if pc not in (PREP["DISPATCH_BEGIN"], PREP["DISPATCH_END"]):
            continue
        _, index, _, _, _, _, frame_pointer, stack_pointer = registers
        key = (cpu, frame_pointer, stack_pointer)
        frame = bisect_right(swaps, tick)
        if pc == PREP["DISPATCH_BEGIN"]:
            if key in pending:
                raise ValueError("Overlapping dispatch entry for the same stack")
            pending[key] = tick, frame, index
            continue
        begin = pending.pop(key, None)
        if begin is None:
            if frame in frames:
                raise ValueError("Dispatch return has no entry inside the capture")
            continue
        if begin[1] != frame:
            crossed.update(set(range(begin[1], frame + 1)).intersection(complete))
        elif frame in frames:
            frames[frame].append((begin[0], tick, begin[2] != index))
    for intervals in frames.values():
        if any(left[1] > right[0] for left, right in zip(intervals, intervals[1:])):
            raise ValueError("Overlapping dispatch iterations in a complete frame")
    return list(frames.values()), len(crossed)


def parallel_savings(intervals, workers, eligible_fraction):
    # Preserve the observed order across every iteration that changes the index.
    # Within a run, lower-bound worker time by total work/N and the largest task.
    # The remaining work and the time between iterations stay unchanged.
    total = largest = saved = 0
    for start, end, changes_index in intervals:
        if changes_index:
            saved += total - max(total / workers, largest)
            total = largest = 0
        else:
            movable = (end - start) * eligible_fraction
            total += movable
            largest = max(largest, movable)
    return saved + total - max(total / workers, largest)


def measure_dispatch(root):
    gates = []
    completions = []
    median_dispatch_by_gate = []
    frames_with_cross_swap_dispatch = 0
    for index in (1, 2):
        capture = root / f"measured-{index}"
        window = PREP["VERIFY"](capture, "candidate", seconds=35, check_legacy_witness=False)
        completions.append(window["verified_completed_renders"])
        _, first, end = PREP["GPU_BOUNDS"](capture)
        swaps = PREP["SWAPS"](capture, first, end)
        for rows in PREP["records"](capture, first, end):
            validated = PREP["summarize_gate"](rows, swaps)
            gate_frames, crossed = dispatch_frames(rows, swaps)
            frames_with_cross_swap_dispatch += crossed
            if len(gate_frames) != validated["complete_swap_intervals"]:
                raise ValueError("Dispatch frame count differs from the validated capture")
            durations = [sum(end - start for start, end, _ in frame) for frame in gate_frames]
            expected = validated["work_per_frame"]["dispatch_body"]
            if (sum(map(len, gate_frames)) != expected["total_pairs_in_complete_frames"]
                    or median(durations) != expected["median_elapsed_ns_per_frame"]):
                raise ValueError("Dispatch timing differs from the validated capture")
            median_dispatch_by_gate.append(median(durations))
            gates.append(gate_frames)
    frames = [frame for gate in gates for frame in gate]
    return {
        "verified_capture_gpu_outputs": completions,
        "frames_with_complete_probe_records": len(frames),
        "frames_with_dispatch_crossing_a_swap": frames_with_cross_swap_dispatch,
        "median_dispatch_ns_per_frame_by_gate": median_dispatch_by_gate,
        "median_index_changing_dispatch_ns_per_frame": median(
            sum(end - start for start, end, changes_index in frame if changes_index)
            for frame in frames
        ),
        "ideal_worker_model": [
            {
                "total_workers_including_calling_thread": workers,
                "eligible_fraction_of_each_non_index_changing_iteration": fraction,
                "median_modeled_ns_saved_per_frame": median(
                    parallel_savings(frame, workers, fraction) for frame in frames
                ),
                "minimum_median_ns_saved_across_gates": min(
                    median(parallel_savings(frame, workers, fraction) for frame in gate_frames)
                    for gate_frames in gates
                ),
            }
            for workers in (2, 4) for fraction in (0.5, 1.0)
        ],
    }


def measure_gl(capture):
    stream, frames = TIMELINE["read_timeline"](capture / "gl-timeline.tsv")
    if len(frames) != 64 or TIMELINE["verify_gpu_outputs"](capture, stream, frames) != [1365, 768]:
        raise ValueError(f"Expected 64 verified full-size outputs in {capture}")
    state_times = []
    by_frame = []
    for frame in frames:
        elapsed = defaultdict(int)
        for call in frame.calls:
            elapsed[call.command.removesuffix("ARB")] += call.end - call.start
        by_frame.append(elapsed)
        state_times.append(sum(elapsed[command] for command in STATE_CALLS))
    return {
        "verified_outputs": len(frames),
        "median_traced_gl_ns_per_frame": median(sum(elapsed.values()) for elapsed in by_frame),
        "median_selected_gl_state_ns_per_frame": median(state_times),
        "median_gl_state_ns_per_frame_by_command": {
            command: median(elapsed[command] for elapsed in by_frame)
            for command in sorted(STATE_CALLS)
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("preparation_root", type=Path)
    parser.add_argument("gl_captures", type=Path, nargs=4)
    args = parser.parse_args()
    result = {
        "scope": "Offline bounds from the existing 24-caster raid diagnostics at 1365 × 768.",
        "dispatch": measure_dispatch(args.preparation_root),
        "gl_calls_by_capture": [measure_gl(capture) for capture in args.gl_captures],
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
