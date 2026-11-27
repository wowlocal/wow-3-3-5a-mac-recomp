#!/usr/bin/env python3
"""Find completed writes to a WoW window's surfaces in an Instruments trace."""

import argparse
import json
import math
import subprocess
import xml.etree.ElementTree as ElementTree
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict, namedtuple
from pathlib import Path
from statistics import median

from metal_submission_log import interval_coverage, read_submissions


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
    return assignments_by_buffer


def encoder_breakdown(writes, assignments, encoders, pid, overlap=None):
    frames = {
        write["trace_frame"]
        for write in writes
        if write["render_verified"] and (
            overlap is None or overlap[0] <= write["completed_ns"] <= overlap[1]
        )
    }
    if not frames:
        raise ValueError("No verified game outputs in the requested interval")

    buffers = {buffer: frame for buffer, (frame, _) in assignments.items() if frame in frames}
    labels_by_buffer = defaultdict(set)
    for row in encoders:
        if (not row["process"].get("fmt", "").endswith(f" ({pid})")
                or row["event-type"].get("fmt") != "Encoding"):
            continue
        buffer_id = row["cmdbuffer-id"].text
        if buffer_id is None:
            continue
        buffer = int(buffer_id)
        if buffer in buffers:
            labels_by_buffer[buffer].add(row["encoder-label"].get("fmt", ""))

    counts = defaultdict(Counter)
    for buffer, frame in buffers.items():
        labels = labels_by_buffer.get(buffer)
        counts[frame]["all"] += 1
        # These labels come from Apple's OpenGL trace. Preserve any other or
        # absent labels, so the classified counts do not imply full coverage.
        if not labels:
            kind = "missing_encoder"
        elif labels == {"metal.gl"}:
            kind = "metal_gl_only"
        elif all(label.startswith("Blit Command ") for label in labels):
            kind = "blit_only"
        else:
            kind = "other_or_mixed"
        counts[frame][kind] += 1

    summary = {}
    for kind in ("all", "metal_gl_only", "blit_only", "other_or_mixed", "missing_encoder"):
        values = sorted(counts[frame][kind] for frame in frames)
        summary[kind] = {
            "total": sum(values),
            "p50": median(values),
            "p95": values[math.ceil(0.95 * len(values)) - 1],
            "min": values[0],
            "max": values[-1],
        }
    return {"verified_outputs": len(frames), "command_buffers_per_output": summary}


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
    return mailbox_report(frames, pid, width, height)


def mailbox_gpu_work(rows, completions, pid, output_buffer, anchor):
    """Join active intervals to verified mailbox renders using a shared GPU clock."""
    groups = defaultdict(list)
    render_ends = {}
    for row in rows:
        if (not row["process"].get("fmt", "").endswith(f" ({pid})")
                or row["state"].get("fmt") != "Active"):
            continue
        frame = int(row["frame-number"].text)
        start = int(row["start"].text)
        end = start + int(row["duration"].text)
        stage = row["channel-name"].get("fmt", "")
        label = row["event-label"].get("fmt", "").split(":", 1)[0]
        groups[frame].append((start, end, stage, label))
        if stage == "Fragment" and label == output_buffer:
            render_ends[frame] = max(end, render_ends.get(frame, end))

    anchor_frame, anchor_sequence = anchor
    by_sequence = {sequence: time for sequence, time, _ in completions}
    if anchor_frame not in render_ends or anchor_sequence not in by_sequence:
        raise ValueError("The requested trace frame or GPU completion is missing")
    offset = by_sequence[anchor_sequence] - render_ends[anchor_frame]
    completions = sorted((time, sequence, draws) for sequence, time, draws in completions)
    times = [time for time, _, _ in completions]
    matches = []
    unmatched = Counter()
    per_output = defaultdict(list)
    work_by_buffer = Counter()
    for frame, events in sorted(groups.items()):
        if frame not in render_ends:
            unmatched["missing_output_fragment_interval"] += 1
            continue
        render_end = render_ends[frame]
        target = render_end + offset
        index = bisect_left(times, target)
        actual, sequence, draws = min(completions[max(0, index - 1):index + 1],
                                      key=lambda completion: abs(target - completion[0]))
        error = abs(target - actual)
        # Metal timestamps and the trace's converted GPU ticks can round
        # independently. Reject matches beyond this sub-microsecond window.
        if error > 250:
            unmatched["clock_mismatch"] += 1
            continue
        stages = {stage for _, _, stage, _ in events}
        if not {"Vertex", "Fragment"} <= stages:
            unmatched["missing_vertex_or_fragment_stage"] += 1
            continue
        matches.append((sequence, draws, error))
        for stage in stages:
            per_output[stage].append(active_duration((a, b) for a, b, kind, _ in events if kind == stage))
        per_output["all_gpu"].append(active_duration((a, b) for a, b, _, _ in events))
        per_output["through_render_completion"].append(
            active_duration((a, min(b, render_end)) for a, b, _, _ in events if a < render_end)
        )
        for stage, label in {(stage, label) for _, _, stage, label in events}:
            work_by_buffer[(label, stage)] += active_duration(
                (a, b) for a, b, kind, buffer in events if kind == stage and buffer == label
            )
    sequences = [sequence for sequence, _, _ in matches]
    if len(sequences) < 2 or any(a >= b for a, b in zip(sequences, sequences[1:])):
        raise ValueError("The GPU intervals did not match an ordered series of mailbox outputs")
    if len(sequences) < len(render_ends) * 0.9:
        raise ValueError("The clock anchor matched fewer than 90 percent of traced outputs")
    return {
        "trace_frame_groups": len(groups),
        "matched_outputs": len(matches),
        "unmatched_trace_groups": dict(unmatched),
        "first_sequence": sequences[0],
        "last_sequence": sequences[-1],
        "unmatched_sequences_within_range": sequences[-1] - sequences[0] + 1 - len(matches),
        "max_absolute_clock_error_ns": max(error for _, _, error in matches),
        "min_draws": min(draws for _, draws, _ in matches),
        "max_draws": max(draws for _, draws, _ in matches),
        "active_time_per_output": {
            stage: {"outputs": len(values), "mean_ns": sum(values) / len(values), **timing_stats(values)}
            for stage, values in sorted(per_output.items())
        },
        "total_active_time_by_buffer_and_stage": {
            label: {stage: time for (name, stage), time in sorted(work_by_buffer.items()) if name == label}
            for label in sorted({label for label, _ in work_by_buffer})
        },
    }


def mailbox_sample(times, seconds):
    sample_duration = times[-1] - times[0] if seconds is None else seconds * 10**9
    cutoff = times[0] + sample_duration
    if times[-1] < cutoff:
        raise ValueError("The GPU completion log does not yet cover the requested duration")
    # Include the gap crossing the cutoff in p99, including any stall
    # that starts before the end and completes just after it.
    sample_end = next((i for i, time in enumerate(times) if time > cutoff), len(times))
    if sample_end < 2:
        raise ValueError("Fewer than two GPU completions inside the measurement window")
    gap_end = sample_end + (sample_end < len(times) and times[sample_end - 1] < cutoff)
    return sample_end, gap_end, sample_duration


def completion_segments(times, sequences, duration_ns, segment_ns):
    start = times[0]
    segments = []
    for offset in range(0, duration_ns, segment_ns):
        stop_offset = min(offset + segment_ns, duration_ns)
        lower, upper = start + offset, start + stop_offset
        # Count completions in (lower, upper], so the segment counts add
        # up to the window's FPS numerator without double-counting markers.
        completions = bisect_right(times, upper) - bisect_right(times, lower)
        # Assign each gap to the segment where it begins, including a gap
        # crossing a segment or the full measurement's right boundary.
        first = bisect_left(times, lower)
        last = min(bisect_left(times, upper), len(times) - 1)
        gaps = sorted(times[i + 1] - times[i] for i in range(first, last))
        segments.append({
            "start_offset_ns": offset,
            "duration_ns": stop_offset - offset,
            "completed_frames": completions,
            "average_completed_fps": completions * 1e9 / (stop_offset - offset),
            "gap_count": len(gaps),
            "p99_gpu_completion_gap_ns": gaps[math.ceil(0.99 * len(gaps)) - 1] if gaps else None,
            "missing_sequences": sequences[last] - sequences[first] - len(gaps) if gaps else 0,
        })
    return segments


def mailbox_report(frames, pid, width, height, seconds=None, segment_seconds=None):
    if len(frames) < 2:
        raise ValueError("Fewer than two completed WineMetalGL renders at the requested size")
    frames.sort()
    sequences = [frame[0] for frame in frames]
    times = [frame[1] for frame in frames]
    if len(set(sequences)) != len(frames) or any(a >= b for a, b in zip(times, times[1:])):
        raise ValueError("WineMetalGL markers do not identify one ordered stream of GPU completions")
    verified, gap_end, sample_duration = mailbox_sample(times, seconds)
    frames = frames[:verified]
    hardware_span = times[verified - 1] - times[0]
    missing = sequences[gap_end - 1] - sequences[0] + 1 - gap_end
    times = times[:gap_end]
    gaps = sorted(b - a for a, b in zip(times, times[1:]))
    report = {
        "pid": pid,
        "window_size": [width, height],
        "verified_completed_renders": verified,
        "first_sequence": sequences[0],
        "last_sequence": sequences[verified - 1],
        "missing_sequences": missing,
        "gpu_completion_interval_ns": hardware_span,
        **({"measurement_duration_ns": sample_duration} if seconds is not None else {}),
        "average_completed_fps": (verified - 1) * 1e9 / sample_duration,
        "p99_gpu_completion_gap_ns": gaps[math.ceil(0.99 * len(gaps)) - 1],
        "min_draws": min(frame[2] for frame in frames),
        "max_draws": max(frame[2] for frame in frames),
    }
    if segment_seconds is not None:
        report["segments"] = completion_segments(times, sequences, sample_duration, segment_seconds * 10**9)
    return report


def presentation_report(path, pid, stream, completed_sequences):
    callbacks = {}
    with path.open() as log:
        for line in log:
            if not line.endswith("\n"):
                break
            process, stream_id, sequence, host_time = map(int, line.split())
            if process != pid or stream_id != stream or sequence not in completed_sequences:
                continue
            if host_time < 0 or sequence in callbacks:
                raise ValueError("Invalid presentation callback for a completed output")
            callbacks[sequence] = host_time
    times = [time for time in callbacks.values() if time > 0]
    span = max(times) - min(times) if times else 0
    return {
        "matched_callbacks": len(callbacks),
        "presented_frames": len(times),
        "presentation_interval_ns": span,
        "average_presented_fps": (len(times) - 1) * 1e9 / span if span else None,
    }


FrameTiming = namedtuple("FrameTiming", "slot_begin slot_end submit gpu_start gpu_end callback ready")


def timing_stats(values):
    ordered = sorted(values)
    return {"p50_ns": median(ordered), "p99_ns": ordered[math.ceil(.99 * len(ordered)) - 1],
            "max_ns": ordered[-1]}


def pass_timing_report(path, pid, timings, gpu_dominant_indices):
    expected = {timing.gpu_end for timing in timings}
    intervals = defaultdict(lambda: defaultdict(list))
    kinds = ("render", "blit", "other")
    with path.open() as log:
        for line in log:
            if not line.endswith("\n"):
                break
            process, gpu_end, kind, start, end = line.split()
            gpu_end = int(gpu_end)
            if int(process) != pid or gpu_end not in expected:
                continue
            start, end = int(start), int(end)
            if kind not in kinds or start < 0 or end < start:
                raise ValueError(f"Invalid pass interval for GPU completion {gpu_end}")
            intervals[gpu_end][kind].append((start, end))
    if len(intervals) != len(expected):
        raise ValueError("The pass timing log does not cover every GPU completion in the sample")

    spans = []
    render_to_copy = []
    overlapping = 0
    for timing in timings:
        encoders = intervals[timing.gpu_end]
        durations = {kind: active_duration(encoders.get(kind, [])) for kind in kinds}
        sampled = sum(durations.values())
        overlapping += sampled != active_duration([pair for passes in encoders.values() for pair in passes])
        durations["remainder"] = timing.gpu_end - timing.gpu_start - sampled
        spans.append(durations)
        if len(encoders.get("render", [])) == 1 and len(encoders.get("blit", [])) == 1 and "other" not in encoders:
            render_to_copy.append(encoders["blit"][0][0] - encoders["render"][0][1])

    dominant = Counter()
    for index in gpu_dominant_indices:
        previous, current = spans[index - 1:index + 1]
        phase = max(current, key=lambda name: current[name] - previous[name])
        dominant[phase] += 1
    return {
        "matched_command_buffers": len(spans),
        "pass_spans": {name: timing_stats(s[name] for s in spans) for name in (*kinds, "remainder")},
        "overlapping_stage_buffers": overlapping,
        "sampled_time_exceeds_buffer": sum(s["remainder"] < 0 for s in spans),
        "render_to_copy_samples": len(render_to_copy),
        **({"render_to_copy": timing_stats(render_to_copy)} if render_to_copy else {}),
        "gpu_dominated_long_gap_largest_pass_changes": dict(dominant),
    }


def submission_report(path, pid, stream, frames, timings, dispatch_dominant_indices):
    render, present = [], []
    for kind, entry in read_submissions(path, pid):
        if kind == "render":
            render.append(entry)
        elif entry.stream == stream:
            present.append(entry)

    expected = {(t.gpu_start, t.gpu_end) for t in timings}
    finals = [entry for entry in render if (entry.start, entry.end) in expected]
    streams = {entry.stream for entry in finals}
    if len(finals) != len(expected) or {(entry.start, entry.end) for entry in finals} != expected or len(streams) != 1:
        raise ValueError("The submission log does not identify one render stream for the sampled frames")
    render = sorted((entry for entry in render if entry.stream in streams), key=lambda entry: entry.serial)
    if len({entry.serial for entry in render}) != len(render):
        raise ValueError("The render submission log includes duplicate serials")
    if any(a.submit >= b.submit for a, b in zip(render, render[1:])):
        raise ValueError("Render submission timestamps disagree with command serial order")
    positions = {(entry.start, entry.end): index for index, entry in enumerate(render)}
    final_indices = [positions[t.gpu_start, t.gpu_end] for t in timings]
    if any(a >= b for a, b in zip(final_indices, final_indices[1:])):
        raise ValueError("Render submissions and completed frames disagree on command order")

    render_intervals = [(entry.start, entry.end) for entry in render]
    render_covered = interval_coverage(render_intervals)
    recorded_covered = interval_coverage(render_intervals + [(entry.start, entry.end) for entry in present])
    phases, same_frame_wait, driver_to_gpu = [], [], []
    prior_execution, frame_execution = [], []
    counts, draw_counts = Counter(), Counter()
    for i, (timing, final_index) in enumerate(zip(timings, final_indices)):
        final = render[final_index]
        if timing.submit > final.submit:
            raise ValueError(f"Flush and commit timestamps disagree for frame {frames[i][0]}")
        begin, end = timing.submit, timing.gpu_start
        renders = render_covered(begin, end)
        recorded = recorded_covered(begin, end)
        phases.append({"render": renders, "present_only": recorded - renders, "uncovered": end - begin - recorded})
        if final.submit <= final.kernel_start <= final.kernel_end <= final.start:
            driver_to_gpu.append(final.start - final.kernel_end)
        if i:
            group = render[final_indices[i - 1] + 1:final_index + 1]
            if sum(entry.draws for entry in group) != frames[i][2]:
                raise ValueError(f"Submission draw counts do not match frame {frames[i][0]}")
            counts[len(group)] += 1
            draw_counts[",".join(str(entry.draws) for entry in group)] += 1
            frame_execution.append(active_duration((entry.start, entry.end) for entry in group))
            prior_execution.append(active_duration((entry.start, entry.end) for entry in group[:-1]))
            same_frame_wait.append(active_duration(
                (max(begin, entry.start), min(end, entry.end))
                for entry in group[:-1] if entry.start < end and begin < entry.end
            ))

    changes = Counter()
    for index in dispatch_dominant_indices:
        previous, current = phases[index - 1:index + 1]
        changes[max(current, key=lambda name: current[name] - previous[name])] += 1
    report = {
        "matched_final_command_buffers": len(final_indices),
        "recorded_present_command_buffers": len(present),
        "submissions_per_frame": dict(counts),
        "draw_counts_per_frame": dict(draw_counts),
        "final_dispatch_coverage": {name: timing_stats(phase[name] for phase in phases)
                                    for name in ("render", "present_only", "uncovered")},
        "same_frame_prior_render_coverage": timing_stats(same_frame_wait),
        "prior_submission_gpu_execution": timing_stats(prior_execution),
        "frame_gpu_execution": timing_stats(frame_execution),
        "dispatch_dominated_gap_largest_coverage_changes": dict(changes),
        "driver_schedule_samples": len(driver_to_gpu),
    }
    if driver_to_gpu:
        report["driver_schedule_end_to_gpu_start"] = timing_stats(driver_to_gpu)
    return report


def frame_timing_report(path, pid, stream, frames, pass_log=None, submission_log=None):
    expected = {seq: end for seq, end, _ in frames}
    by_sequence = {}
    with path.open() as log:
        for line in log:
            if not line.endswith("\n"):
                break
            process, stream_id, sequence, *fields = map(int, line.split())
            if process != pid or stream_id != stream or sequence not in expected:
                continue
            timing = FrameTiming(*fields) if len(fields) == len(FrameTiming._fields) else None
            if not timing or sequence in by_sequence or timing.gpu_end != expected[sequence]:
                raise ValueError(f"Invalid timing record for GPU completion {sequence}")
            if timing.slot_begin <= 0 or any(a > b for a, b in zip(timing, timing[1:])):
                raise ValueError(f"CPU and GPU timestamps do not align for sequence {sequence}")
            by_sequence[sequence] = timing
    if len(by_sequence) != len(frames):
        raise ValueError("The frame timing log does not cover every GPU completion in the sample")
    timings = [by_sequence[seq] for seq, _, _ in frames]

    long_components = defaultdict(list)
    dominant = Counter()
    gpu_dominant = []
    dispatch_dominant = []
    unattributed = 0
    over_budget = 0
    budget_ns = 2_000_000
    for index, (previous, current) in enumerate(zip(timings, timings[1:])):
        long_gap = current.gpu_end - previous.gpu_end > budget_ns
        over_budget += long_gap
        if frames[index + 1][0] != frames[index][0] + 1 or current.slot_begin < previous.submit:
            unattributed += 1
            continue
        if not long_gap:
            continue
        components = {
            "before_slot": current.slot_begin - previous.submit,
            "slot_acquire": current.slot_end - current.slot_begin,
            "after_slot": current.submit - current.slot_end,
            "dispatch_change": ((current.gpu_start - current.submit) -
                                (previous.gpu_start - previous.submit)),
            "gpu_duration_change": ((current.gpu_end - current.gpu_start) -
                                    (previous.gpu_end - previous.gpu_start)),
        }
        for name, length in components.items():
            long_components[name].append(length)
        largest = max(components, key=components.get)
        dominant[largest] += 1
        if pass_log is not None and largest == "gpu_duration_change":
            gpu_dominant.append(index + 1)
        if submission_log is not None and largest == "dispatch_change":
            dispatch_dominant.append(index + 1)
    report = {
        "matched_frames": len(timings),
        "frame_phases": {
            "slot_acquire": timing_stats(t.slot_end - t.slot_begin for t in timings),
            "flush_to_gpu_start": timing_stats(t.gpu_start - t.submit for t in timings),
            "gpu_execution": timing_stats(t.gpu_end - t.gpu_start for t in timings),
            "gpu_end_to_callback": timing_stats(t.callback - t.gpu_end for t in timings),
            "callback_to_slot_ready": timing_stats(t.ready - t.callback for t in timings),
        },
        "gap_count": len(timings) - 1,
        "unattributed_gaps": unattributed,
        "long_gap_budget_ns": budget_ns,
        "over_budget_gaps": over_budget,
        "long_gap_median_components_ns": {name: median(values) for name, values in long_components.items()},
        "long_gap_largest_component_counts": dict(dominant),
    }
    if pass_log is not None:
        report["pass_timing"] = pass_timing_report(pass_log, pid, timings, gpu_dominant)
    if submission_log is not None:
        report["submissions"] = submission_report(submission_log, pid, stream, frames, timings, dispatch_dominant)
    return report


def mailbox_stream(path, pid, width, height, stream):
    streams = defaultdict(list)
    dimensions = (width << 32) | height
    with path.open() as log:
        for line in log:
            if not line.endswith("\n"):
                break  # The writer may still be appending the final line.
            process, stream_id, seq, gpu_end, draws, size = map(int, line.split())
            if process == pid and size == dimensions and gpu_end > 0 and draws > 0:
                streams[stream_id].append((seq, gpu_end, draws))
    if stream is None:
        if len(streams) > 1:
            raise ValueError(f"Several mailbox streams at the requested size. Select one with --stream {sorted(streams)}")
        if not streams:
            raise ValueError("No GPU completions at the requested PID and size")
        stream = next(iter(streams))
    return stream, streams.get(stream, [])


def mailbox_log(path, pid, width, height, stream, start_sequence, seconds, present_log=None, timing_log=None,
                pass_log=None, submission_log=None, segment_seconds=None):
    stream, frames = mailbox_stream(path, pid, width, height, stream)
    if start_sequence is not None:
        frames = [frame for frame in frames if frame[0] >= start_sequence]
        if not any(frame[0] == start_sequence for frame in frames):
            raise ValueError("The requested starting GPU completion is missing")
    report = mailbox_report(frames, pid, width, height, seconds, segment_seconds)
    if present_log is not None:
        # GPU and display events join by the renderer's sequence identity.
        completed = {seq for seq, _, _ in frames[:report["verified_completed_renders"]]}
        report["presentation"] = presentation_report(present_log, pid, stream, completed)
    if timing_log is not None:
        _, gap_end, _ = mailbox_sample([end for _, end, _ in frames], seconds)
        report["frame_timing"] = frame_timing_report(timing_log, pid, stream, frames[:gap_end], pass_log, submission_log)
    return {"stream": stream, **report}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path)
    parser.add_argument("pid", type=int)
    parser.add_argument("width", type=int)
    parser.add_argument("height", type=int)
    parser.add_argument("--run", type=int, default=1)
    parser.add_argument("--frame-log", type=Path, help="align the frame log using AGL Swap signposts")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--wmgl", action="store_true", help="read WineMetalGL's completed mailbox renders")
    mode.add_argument("--wmgl-log", action="store_true", help="read a WineMetalGL GPU completion log")
    mode.add_argument("--wmgl-gpu-work", type=Path, metavar="GPU_LOG",
                      help="join Metal GPU intervals to WineMetalGL completions")
    mode.add_argument("--encoder-breakdown", action="store_true", help="group traced AGL command buffers by encoder label")
    parser.add_argument("--output-buffer", help="trace label of the buffer that completes rendering")
    parser.add_argument("--gpu-clock-anchor", type=int, nargs=2, metavar=("TRACE_FRAME", "SEQUENCE"),
                        help="trace frame and completion sequence for the same rendered output")
    parser.add_argument("--stream", type=int, help="mailbox generation to select from a GPU completion log")
    parser.add_argument("--present-log", type=Path, help="join Metal drawable presentations by render sequence")
    parser.add_argument("--timing-log", type=Path, help="attribute windowed GPU completion gaps to host and GPU phases")
    parser.add_argument("--pass-log", type=Path, help="join sampled GPU encoder intervals to frame timings")
    parser.add_argument("--submission-log", type=Path, help="attribute final-buffer dispatch wait to recorded GPU work")
    parser.add_argument("--start-sequence", type=int, help="first completed frame in the measurement")
    parser.add_argument("--seconds", type=int, help="require this many seconds of completed GPU work")
    parser.add_argument("--segment-seconds", type=int, help="summarize consecutive parts of a GPU completion window")
    options = parser.parse_args()
    if min(options.pid, options.width, options.height, options.run) <= 0:
        parser.error("PID, viewport dimensions, and run number must be positive")
    if (options.wmgl or options.wmgl_log) and options.frame_log:
        parser.error("--frame-log applies to the AGL window-write analysis")
    if options.wmgl_gpu_work and (not options.output_buffer or not options.gpu_clock_anchor or options.frame_log):
        parser.error("--wmgl-gpu-work requires --output-buffer and --gpu-clock-anchor")
    if not options.wmgl_gpu_work and (options.output_buffer or options.gpu_clock_anchor):
        parser.error("--output-buffer and --gpu-clock-anchor require --wmgl-gpu-work")
    if options.stream is not None and not (options.wmgl_gpu_work or options.wmgl_log):
        parser.error("--stream requires --wmgl-log or --wmgl-gpu-work")
    log_options = (options.start_sequence, options.seconds, options.present_log, options.timing_log,
                   options.segment_seconds)
    if not options.wmgl_log and any(value is not None for value in log_options):
        parser.error("GPU completion log options require --wmgl-log")
    if options.pass_log is not None and options.timing_log is None:
        parser.error("--pass-log requires --timing-log")
    if options.submission_log is not None and options.timing_log is None:
        parser.error("--submission-log requires --timing-log")
    if any(value is not None and value <= 0 for value in (
        options.stream, options.start_sequence, options.seconds, options.segment_seconds,
    )):
        parser.error("Stream, starting sequence, and durations must be positive")
    try:
        if options.wmgl_gpu_work:
            _, completions = mailbox_stream(options.wmgl_gpu_work, options.pid, options.width, options.height,
                                            options.stream)
            report = mailbox_gpu_work(trace_rows(options.trace, options.run, "metal-gpu-intervals"), completions,
                                      options.pid, options.output_buffer, options.gpu_clock_anchor)
            print(json.dumps(report, indent=2))
            return
        if options.wmgl_log:
            report = mailbox_log(
                options.trace, options.pid, options.width, options.height,
                options.stream, options.start_sequence, options.seconds, options.present_log, options.timing_log,
                options.pass_log, options.submission_log, options.segment_seconds,
            )
            print(json.dumps(report, indent=2))
            return
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
        assignments = identify_draws(
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
        if options.encoder_breakdown:
            report["encoder_breakdown"] = encoder_breakdown(
                writes, assignments,
                trace_rows(options.trace, options.run, "metal-application-encoders-list"),
                options.pid, report.get("overlap", {}).get("interval_ns"),
            )
    except (ElementTree.ParseError, KeyError, OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"{parser.prog}: {error}\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
