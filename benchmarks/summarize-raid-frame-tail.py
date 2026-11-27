"""Join CPU and Metal timestamps for the verified headless frame window."""

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
import json
from math import ceil
from pathlib import Path
from statistics import mean, median
from typing import NamedTuple


from metal_submission_log import Submission, interval_coverage, read_submissions


class Frame(NamedTuple):
    sequence: int
    draws: int
    final: Submission
    entry: int
    cpu: int


def rows(path, width):
    with path.open() as lines:
        for line in lines:
            if not line.endswith("\n"):
                break  # The process can stop in the middle of its final write.
            fields = line.split()
            if len(fields) != width:
                raise ValueError(f"Unexpected log row in {path.name}")
            yield fields


def render_submissions(path, pid):
    by_end = defaultdict(list)
    by_stream = defaultdict(list)
    for kind, submission in read_submissions(path, pid):
        if kind == "render":
            by_end[submission.end].append(submission)
            by_stream[submission.stream].append(submission)
    for trace in by_stream.values():
        trace.sort(key=lambda row: row.serial)
    return by_end, by_stream


def read_frames(root, window, by_end):
    pid, stream = window["pid"], window["stream"]
    first, last = window["first_sequence"], window["last_sequence"]
    width, height = window["window_size"]
    expected_dimensions = width << 32 | height
    cpu_log = root / "cpu-swaps.ns"
    has_cpu = cpu_log.exists()
    cpu_samples = {}
    threads = set()
    if has_cpu:
        for process, source, seq, thread, entry, cpu in (
            tuple(map(int, row)) for row in rows(cpu_log, 6)
        ):
            if (process, source) == (pid, stream) and first <= seq <= last:
                if seq in cpu_samples:
                    raise ValueError(f"Duplicate CPU sequence {seq}")
                cpu_samples[seq] = (entry, cpu)
                threads.add(thread)
        if len(threads) != 1 or 0 in threads or len(cpu_samples) != last - first + 1:
            raise ValueError("Expected one calling thread and a CPU sample for every frame")

    frames = []
    for process, source, seq, end, draws, dimensions in (
        tuple(map(int, row)) for row in rows(root / "gpu.ns", 6)
    ):
        if (process, source) != (pid, stream) or not first <= seq <= last:
            continue
        if dimensions != expected_dimensions or draws < 600:
            raise ValueError(f"Unexpected output dimensions or draw count at {seq}")
        matches = by_end[end]
        if len(matches) != 1:
            raise ValueError(f"Expected one Metal submission ending at output {seq}")
        final = matches[0]
        if not final.submit <= final.start <= final.end:
            raise ValueError(f"Metal timestamps for output {seq} are out of order")
        entry, cpu = cpu_samples.get(seq, (0, 0))
        if has_cpu and not 0 < entry <= final.submit:
            raise ValueError(f"CPU entry and Metal submission timestamps disagree at output {seq}")
        frames.append(Frame(seq, draws, final, entry, cpu))
    frames.sort()
    if [frame.sequence for frame in frames] != list(range(first, last + 1)):
        raise ValueError("Frame sequences are missing or duplicated")
    return frames, has_cpu


def distribution(values):
    ordered = sorted(values)
    return {
        "mean": mean(ordered),
        "median": median(ordered),
        "p99": ordered[ceil(0.99 * len(ordered)) - 1],
    }


def explain_intervals(frames, has_cpu, trace):
    serials = [submission.serial for submission in trace]
    if len(serials) != len(set(serials)):
        raise ValueError("Render submission serials are duplicated")
    render_covered = interval_coverage((row.start, row.end) for row in trace)
    intervals = []
    for previous, current in zip(frames, frames[1:]):
        before, final = previous.final, current.final
        if before.stream != final.stream or before.serial >= final.serial:
            raise ValueError("Output completions do not follow one ordered Metal stream")
        start = bisect_right(serials, before.serial)
        end = bisect_right(serials, final.serial)
        submissions = trace[start:end]
        if not submissions or submissions[-1] != final:
            raise ValueError(f"Missing final command buffer for output {current.sequence}")
        if sum(submission.draws for submission in submissions) != current.draws:
            raise ValueError(f"Submission draw counts disagree at output {current.sequence}")

        submit_gap = final.submit - before.submit
        queue_delay = final.start - final.submit
        queue_change = queue_delay - (before.start - before.submit)
        gpu_duration = final.end - final.start
        gpu_change = gpu_duration - (before.end - before.start)
        completion_gap = final.end - before.end
        components = {"final_submit_interval_ns": submit_gap}
        if has_cpu:
            swap_wall = current.entry - previous.entry
            cpu = current.cpu - previous.cpu
            if swap_wall <= 0 or cpu <= 0:
                raise ValueError(f"Nonmonotonic calling-thread timestamps at output {current.sequence}")
            components = {
                "calling_thread_cpu_ns": cpu,
                "calling_thread_wall_minus_cpu_ns": swap_wall - cpu,
                "swap_to_final_submit_change_ns": submit_gap - swap_wall,
            }
        components["final_submit_to_gpu_start_change_ns"] = queue_change
        components["final_gpu_span_change_ns"] = gpu_change
        if completion_gap <= 0 or sum(components.values()) != completion_gap:
            raise ValueError(f"Frame gap does not balance at output {current.sequence}")
        covered_queue = render_covered(final.submit, final.start)
        covered_gap = render_covered(before.end, final.end)
        earlier_spans = [(submission.start, submission.end) for submission in submissions[:-1]]
        earlier_covered = interval_coverage(earlier_spans)
        earlier_start = min((start for start, _ in earlier_spans), default=0)
        earlier_end = max((end for _, end in earlier_spans), default=0)
        intervals.append({
            "completion_gap_ns": completion_gap,
            "final_submit_interval_ns": submit_gap,
            "final_submit_to_gpu_start_ns": queue_delay,
            "final_gpu_span_ns": gpu_duration,
            "final_dispatch_wait_outside_recorded_render_spans_ns": queue_delay - covered_queue,
            "final_dispatch_wait_covered_by_same_outputs_earlier_submissions_ns": earlier_covered(
                final.submit, final.start
            ),
            "completion_gap_outside_recorded_render_spans_ns": completion_gap - covered_gap,
            "earlier_submissions_gpu_spans_ns": earlier_covered(earlier_start, earlier_end),
            "render_submissions": len(submissions),
            "first_to_final_submit_ns": final.submit - submissions[0].submit,
            "draws_per_submission": tuple(submission.draws for submission in submissions),
            "components": components,
        })
    return intervals


def summarize(intervals):
    metric_names = [key for key in intervals[0] if key not in ("components", "draws_per_submission")]
    metrics = {key: distribution([row[key] for row in intervals]) for key in metric_names}
    threshold = metrics["completion_gap_ns"]["p99"]
    tail = [row for row in intervals if row["completion_gap_ns"] >= threshold]
    keys = intervals[0]["components"]
    typical = {key: median(row["components"][key] for row in intervals) for key in keys}
    primary = Counter(
        max(keys, key=lambda key: row["components"][key] - typical[key])
        for row in tail
    )
    return {
        "completion_intervals": len(intervals),
        "most_common_draw_splits": [
            {"draws_per_submission": split, "count": count}
            for split, count in Counter(row["draws_per_submission"] for row in intervals).most_common(8)
        ],
        "metrics": metrics,
        "tail_intervals_at_or_above_p99": len(tail),
        "all_interval_component_medians_ns": typical,
        "tail_component_medians_ns": {
            key: median(row["components"][key] for row in tail) for key in keys
        },
        "largest_component_increase_above_its_baseline_median": dict(primary),
        "tail_medians": {key: median(row[key] for row in tail) for key in metric_names},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    args = parser.parse_args()
    window = json.loads((args.capture / "window.json").read_text())
    if window["missing_sequences"] or window["verified_completed_renders"] < 2:
        raise ValueError("Expected a continuous measured output window")
    by_end, by_stream = render_submissions(args.capture / "submissions.ns", window["pid"])
    frames, has_cpu = read_frames(args.capture, window, by_end)
    if len(frames) != window["verified_completed_renders"]:
        raise ValueError("Joined frames disagree with the verified output count")
    intervals = explain_intervals(frames, has_cpu, by_stream[frames[0].final.stream])
    summary = summarize(intervals)
    if summary["metrics"]["completion_gap_ns"]["p99"] != window["p99_gpu_completion_gap_ns"]:
        raise ValueError("Joined intervals disagree with the verified p99 gap")
    result = {
        "window_size": window["window_size"],
        "measurement_duration_ns": window["measurement_duration_ns"],
        "average_completed_fps": window["average_completed_fps"],
        "verified_output_count": len(frames),
        "render_thread_cpu_samples_joined": has_cpu,
        **summary,
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
