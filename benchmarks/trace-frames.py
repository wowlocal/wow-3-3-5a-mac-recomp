#!/usr/bin/env python3
"""Find completed writes to a WoW window's surfaces in an Instruments trace."""

import argparse
import json
import math
import subprocess
import xml.etree.ElementTree as ElementTree
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median


def trace_rows(path, run, schema, category=None):
    predicate = f'@schema="{schema}"'
    if category:
        predicate += f' and @category="{category}"'
    xpath = f'/trace-toc/run[@number="{run}"]/data/table[{predicate}]'
    xml = subprocess.check_output(
        ["xcrun", "xctrace", "export", "--input", str(path), "--xpath", xpath]
    )
    root = ElementTree.fromstring(xml)
    nodes = root.findall("node")
    if len(nodes) != 1:
        raise ValueError(f"Expected one {schema} table in run {run}, found {len(nodes)}")
    node = nodes[0]
    columns = [column.findtext("mnemonic") for column in node.findall("schema/col")]
    references = {entry.attrib["id"]: entry for entry in node.iter() if "id" in entry.attrib}
    for row in node.findall("row"):
        if len(columns) != len(row):
            raise ValueError(f"Unexpected row format in {schema}")
        yield dict(zip(columns, (references.get(entry.get("ref"), entry) for entry in row)))


def window_writes(accesses, completions, pid, width, height):
    compositor_surfaces = set()
    writes = defaultdict(dict)
    for row in accesses:
        if (int(row["width"].text), int(row["height"].text)) != (width, height):
            continue
        surface = int(row["surface-id"].text)
        access = row["access-type"].text
        if access == "0" and row["process"].get("fmt", "").startswith("WindowServer ("):
            compositor_surfaces.add(surface)
        if access == "1" and int(row["pid"].text) == pid:
            buffer_id = int(row["cmdbuffer-id"].text)
            # An encoder can access the same surface more than once.
            timestamp = int(row["timestamp"].text)
            previous = writes[buffer_id].get(surface, timestamp)
            writes[buffer_id][surface] = max(previous, timestamp)

    completion_times = {
        int(row["cmdbuffer-id"].text): int(row["timestamp"].text) for row in completions
    }
    output_buffers = set()
    unmatched = 0
    ambiguous = 0
    complete = []
    for buffer_id, surfaces in writes.items():
        window_surfaces = compositor_surfaces.intersection(surfaces)
        if not window_surfaces:
            continue
        output_buffers.add(buffer_id)
        if len(window_surfaces) != 1:
            ambiguous += 1
            continue
        surface = next(iter(window_surfaces))
        finished = completion_times.get(buffer_id)
        if finished is None or finished < surfaces[surface]:
            unmatched += 1
            continue
        complete.append(
            {
                "command_buffer": buffer_id,
                "surface": surface,
                "write_ns": surfaces[surface],
                "completed_ns": finished,
            }
        )

    return {
        "pid": pid,
        "window_size": [width, height],
        "output_buffers": len(output_buffers),
        "ambiguous_output_buffers": ambiguous,
        "unmatched_output_buffers": unmatched,
        "completed_window_writes": sorted(complete, key=lambda entry: entry["completed_ns"]),
    }, output_buffers


def swap_clock(rows, pid):
    markers = {}
    for row in rows:
        process = row["process"].get("fmt", "")
        if (
            process.endswith(f" ({pid})")
            and row["subsystem"].text == "org.local.wrath-trial"
            and row["signpost-name"].get("fmt") == "AGL Swap"
            and row["name"].text == "arg0"
        ):
            markers[int(row["value"].text)] = int(row["time"].text)
    if len(markers) < 2:
        raise ValueError("Too few AGL Swap signposts to align the frame log")
    return markers


def active_duration(intervals):
    elapsed = 0
    previous_end = 0
    for start, end in sorted(intervals):
        elapsed += max(0, end - max(start, previous_end))
        previous_end = max(previous_end, end)
    return elapsed


def identify_draws(writes, output_buffers, assignments, intervals, pid):
    assignments_by_buffer = {
        int(row["cmdbuffer-id"].text): (
            int(row["frame-number"].text),
            int(row["present-surface-id"].text),
        )
        for row in assignments
        if int(row["pid"].text) == pid
    }
    channels_by_frame = defaultdict(set)
    work_by_frame = defaultdict(list)
    for row in intervals:
        buffer_id = row["cmdbuffer-id"].text
        if buffer_id is not None and row["state"].get("fmt") == "Active":
            frame, _ = assignments_by_buffer.get(int(buffer_id), (None, None))
            if frame is not None:
                start = int(row["start"].text)
                work_by_frame[frame].append((start, start + int(row["duration"].text)))
                channels_by_frame[frame].add(row["channel-name"].get("fmt"))
    frame_outputs = Counter(
        assignments_by_buffer.get(buffer_id, (None, None))[0] for buffer_id in output_buffers
    )
    for write in writes:
        frame, surface = assignments_by_buffer.get(write["command_buffer"], (None, None))
        write["trace_frame"] = frame
        write["render_verified"] = (
            frame is not None
            and surface == write["surface"]
            and frame_outputs[frame] == 1
            and {"Vertex", "Fragment"} <= channels_by_frame[frame]
        )
        if write["render_verified"]:
            write["gpu_active_ns"] = active_duration(work_by_frame[frame])


def compare_swaps(writes, markers, frame_log):
    with frame_log.open() as stream:
        returns = [int(line.split()[-1]) for line in stream if line.endswith("\n")]
    if returns != sorted(returns):
        raise ValueError("AGL swap timestamps are out of order")
    if not markers.keys() <= set(returns):
        raise ValueError("The signpost timestamps are missing from this frame log")
    if not writes:
        raise ValueError("The trace contains no completed writes to the game window")

    offsets = [trace_time - monotonic_time for monotonic_time, trace_time in markers.items()]
    offset = int(median(offsets))
    start = max(min(markers.values()), writes[0]["write_ns"])
    stop = min(max(markers.values()), writes[-1]["completed_ns"])
    if stop <= start:
        raise ValueError("The AGL swaps and GPU completions have no common recorded interval")

    completions = [write for write in writes if start <= write["completed_ns"] <= stop]
    render_times = [write["completed_ns"] for write in completions if write["render_verified"]]
    # Completion gaps include the time spent on frames with incomplete trace evidence.
    gaps = sorted(right - left for left, right in zip(render_times, render_times[1:]))
    return {
        "matched_clock_markers": len(markers),
        "max_clock_marker_deviation_ns": max(abs(value - offset) for value in offsets),
        "interval_ns": [start, stop],
        "agl_swaps_in_interval": sum(start <= returned + offset <= stop for returned in returns),
        "window_gpu_completions_in_interval": len(completions),
        "render_verified_completions_in_interval": len(render_times),
        "p99_render_completion_gap_ns": gaps[math.ceil(0.99 * len(gaps)) - 1] if gaps else None,
    }


def mailbox_frames(rows, pid, width, height):
    events = defaultdict(dict)
    names = ("arg0", "arg1", "arg2", "arg3")
    for row in rows:
        if (
            not row["process"].get("fmt", "").endswith(f" ({pid})")
            or row["subsystem"].text != "org.local.wrath-trial"
            or row["signpost-name"].get("fmt") != "WMGL Rendered Output"
        ):
            continue
        name = row["name"].text
        if name not in names:
            continue
        event = events[int(row["time"].text)]
        if name in event:
            raise ValueError("Repeated WineMetalGL completion marker argument")
        event[name] = int(row["value"].text)

    frames = []
    for event in events.values():
        if len(event) != len(names):
            raise ValueError("Incomplete WineMetalGL completion marker")
        sequence, gpu_end, draws, dimensions = (event[name] for name in names)
        if dimensions == ((width << 32) | height) and gpu_end > 0 and draws > 0:
            frames.append((sequence, gpu_end, draws))
    if len(frames) < 2:
        raise ValueError("Fewer than two completed WineMetalGL renders at the requested size")
    frames.sort()
    sequences = [frame[0] for frame in frames]
    times = [frame[1] for frame in frames]
    if len(set(sequences)) != len(frames) or any(a >= b for a, b in zip(times, times[1:])):
        raise ValueError("WineMetalGL markers do not identify one ordered stream of GPU completions")
    gaps = sorted(b - a for a, b in zip(times, times[1:]))
    duration = times[-1] - times[0]
    return {
        "pid": pid,
        "window_size": [width, height],
        "verified_completed_renders": len(frames),
        "first_sequence": sequences[0],
        "last_sequence": sequences[-1],
        "missing_sequences": sequences[-1] - sequences[0] + 1 - len(frames),
        "gpu_completion_interval_ns": duration,
        "average_completed_fps": len(gaps) * 1e9 / duration,
        "p99_gpu_completion_gap_ns": gaps[math.ceil(0.99 * len(gaps)) - 1],
        "min_draws": min(frame[2] for frame in frames),
        "max_draws": max(frame[2] for frame in frames),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path)
    parser.add_argument("pid", type=int)
    parser.add_argument("width", type=int)
    parser.add_argument("height", type=int)
    parser.add_argument("--run", type=int, default=1)
    parser.add_argument("--frame-log", type=Path, help="align the frame log using AGL Swap signposts")
    parser.add_argument("--wmgl", action="store_true", help="read WineMetalGL's completed mailbox renders")
    options = parser.parse_args()
    if min(options.pid, options.width, options.height, options.run) <= 0:
        parser.error("PID, viewport dimensions, and run number must be positive")
    if options.wmgl and options.frame_log:
        parser.error("--frame-log applies to the AGL window-write analysis")
    try:
        if options.wmgl:
            events = trace_rows(options.trace, options.run, "os-signpost-arg", "PointsOfInterest")
            report = mailbox_frames(events, options.pid, options.width, options.height)
            print(json.dumps(report, indent=2))
            return
        report, output_buffers = window_writes(
            trace_rows(options.trace, options.run, "metal-io-surface-access"),
            trace_rows(options.trace, options.run, "metal-command-buffer-completed"),
            options.pid,
            options.width,
            options.height,
        )
        writes = report["completed_window_writes"]
        identify_draws(
            writes,
            output_buffers,
            trace_rows(options.trace, options.run, "metal-command-buffer-frame-assignment"),
            trace_rows(options.trace, options.run, "metal-gpu-intervals"),
            options.pid,
        )
        report["render_verified_window_writes"] = sum(write["render_verified"] for write in writes)
        if options.frame_log:
            markers = swap_clock(
                trace_rows(options.trace, options.run, "os-signpost-arg", "PointsOfInterest"),
                options.pid,
            )
            report["overlap"] = compare_swaps(writes, markers, options.frame_log)
    except (ElementTree.ParseError, KeyError, OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"{parser.prog}: {error}\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
