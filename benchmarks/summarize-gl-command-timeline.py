"""Measure command dependencies and model GL worker overlap from a live timeline."""

import argparse
from collections import Counter
import json
import math
from pathlib import Path
from statistics import median
from typing import NamedTuple


class Call(NamedTuple):
    start: int
    end: int
    kind: str
    context: str
    command: str
    reason: str = "api"


class Frame(NamedTuple):
    sequence: int
    start: int
    swap_entry: int
    swap_end: int
    draws: int
    calls: tuple


def read_timeline(path):
    with path.open() as trace:
        marker, stream, anchor, initial = trace.readline().split()
        if marker != "H":
            raise ValueError("Missing GL timeline header")
        expected = int(anchor) + 1
        start = int(initial)
        calls = []
        frames = []
        for line in trace:
            fields = line.split()
            if fields == ["F"]:
                if calls or trace.read().strip():
                    raise ValueError("Unexpected records after the last complete frame")
                return int(stream), frames
            if len(fields) in (6, 7) and fields[0] == "C":
                entered, returned, kind, context, command = fields[1:6]
                reason = fields[6] if len(fields) == 7 else "api"
                call = Call(int(entered), int(returned), kind, context, command, reason)
                previous = calls[-1].end if calls else start
                if not previous <= call.start <= call.end or kind not in {"B", "D", "P", "S"}:
                    raise ValueError(f"Invalid GL call order or category in output {expected}")
                calls.append(call)
            elif len(fields) == 5 and fields[0] == "S":
                sequence, entry, end, draws = map(int, fields[1:])
                previous = calls[-1].end if calls else start
                if sequence != expected or not previous <= entry <= end or draws <= 0:
                    raise ValueError(f"Invalid GL frame boundary at output {expected}")
                contexts = {call.context for call in calls if call.context != "0"}
                if len(contexts) != 1:
                    raise ValueError(f"Expected one GL context in output {expected}")
                frames.append(Frame(sequence, start, entry, end, draws, tuple(calls)))
                expected += 1
                start = end
                calls.clear()
            else:
                raise ValueError("Invalid GL timeline record")
    raise ValueError("Incomplete GL timeline")


def verify_gpu_outputs(root, stream, frames):
    window = json.loads((root / "window.json").read_text())
    pid = window["pid"]
    width, height = window["window_size"]
    if window["stream"] != stream or window["missing_sequences"]:
        raise ValueError("GPU verification window must use the captured stream")
    dimensions = width << 32 | height
    expected = {frame.sequence: frame.draws for frame in frames}
    matched = []
    with (root / "gpu.ns").open() as log:
        for line in log:
            if not line.endswith("\n"):
                break
            process, source, sequence, end, draws, size = map(int, line.split())
            if (process, source, size) == (pid, stream, dimensions) and sequence in expected:
                if draws != expected[sequence]:
                    raise ValueError(f"GPU draw count differs for output {sequence}")
                matched.append((sequence, end))
    if [sequence for sequence, _ in matched] != list(expected):
        raise ValueError("Missing, duplicate, or out-of-order GPU timeline completions")
    if any(a >= b for (_, a), (_, b) in zip(matched, matched[1:])):
        raise ValueError("GPU timeline completion times are not strictly increasing")
    return [width, height]


DRAW_SNAPSHOT_REASONS = {
    "client_indices", "mapped_indices", "pinned_indices",
    "enabled_client_array", "mapped_array", "pinned_array",
}


def queueable(call, allowed, snapshot_draws):
    return call.kind in allowed or (snapshot_draws and call.reason in DRAW_SNAPSHOT_REASONS)


def worker_frame(frame, allowed, enqueue, snapshot_draws=False):
    game = 0
    worker = 0
    last = frame.start
    pending = 0
    packets = []
    for call in frame.calls:
        game += call.start - last
        duration = call.end - call.start
        if queueable(call, allowed, snapshot_draws):
            game += enqueue
            worker = max(game, worker) + duration
            pending += 1
        else:
            if pending:
                packets.append(pending)
                pending = 0
            game = max(game, worker) + duration
            worker = game
        last = call.end
    if pending:
        packets.append(pending)
    game += frame.swap_entry - last
    game = max(game, worker) + frame.swap_end - frame.swap_entry
    return game, packets


def stats(values):
    values = sorted(values)
    if not values:
        return None
    return {
        "median": median(values),
        "p99": values[math.ceil(len(values) * 0.99) - 1],
        "maximum": values[-1],
    }


def summarize(root):
    stream, frames = read_timeline(root / "gl-timeline.tsv")
    if not frames:
        raise ValueError("Expected at least one complete timed GL frame")
    viewport = verify_gpu_outputs(root, stream, frames)
    frame_times = [frame.swap_end - frame.start for frame in frames]
    call_times = []
    class_counts = Counter()
    class_times = Counter()
    barriers = Counter()
    barrier_times = Counter()
    draw_barriers = Counter()
    draw_barrier_times = Counter()
    for frame in frames:
        call_times.append(sum(call.end - call.start for call in frame.calls))
        for call in frame.calls:
            class_counts[call.kind] += 1
            class_times[call.kind] += call.end - call.start
            if call.kind == "B":
                barriers[call.command] += 1
                barrier_times[call.command] += call.end - call.start
                if call.command.startswith("glDraw"):
                    draw_barriers[call.reason] += 1
                    draw_barrier_times[call.reason] += call.end - call.start
    models = {}
    total_gl_time = sum(call_times)
    scenarios = [
        ("scalar_and_buffered_draws", "SD", False),
        ("plus_staged_input_copies", "SDP", False),
    ]
    if DRAW_SNAPSHOT_REASONS.intersection(draw_barriers):
        scenarios.append(("plus_snapshotted_draw_inputs", "SDP", True))
    for key, allowed, snapshot_draws in scenarios:
        ideal = [worker_frame(frame, allowed, 0, snapshot_draws) for frame in frames]
        overhead = [worker_frame(frame, allowed, 200, snapshot_draws)[0] for frame in frames]
        durations = [duration for duration, _ in ideal]
        queueable_time = sum(class_times[kind] for kind in allowed)
        if snapshot_draws:
            queueable_time += sum(draw_barrier_times[reason] for reason in DRAW_SNAPSHOT_REASONS)
        models[key] = {
            "ideal_frame_ns": stats(durations),
            "ideal_saved_frame_ns": stats([base - projected for base, projected in zip(frame_times, durations)]),
            "frame_ns_with_200ns_per_enqueue": stats(overhead),
            "nonempty_packets_per_frame": stats([len(packets) for _, packets in ideal]),
            "calls_per_packet": stats([count for _, packets in ideal for count in packets]),
            "queueable_gl_share_of_observed_call_time": queueable_time / total_gl_time if total_gl_time else 0,
        }
    return {
        "viewport": viewport,
        "frames": len(frames),
        "first_sequence": frames[0].sequence,
        "last_sequence": frames[-1].sequence,
        "draws_per_frame": stats([frame.draws for frame in frames]),
        "frame_wall_ns": stats(frame_times),
        "recorded_gl_wall_ns": stats(call_times),
        "wall_ns_outside_recorded_calls": stats([base - gl for base, gl in zip(frame_times, call_times)]),
        "calls_by_category": dict(class_counts),
        "elapsed_ns_by_category": dict(class_times),
        "top_barrier_calls_by_count": dict(barriers.most_common(12)),
        "top_barrier_calls_by_elapsed_ns": dict(barrier_times.most_common(12)),
        "draw_barriers_by_reason": dict(draw_barriers),
        "draw_barrier_elapsed_ns_by_reason": dict(draw_barrier_times),
        "models": models,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_dir", type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize(args.capture_dir), indent=2))
