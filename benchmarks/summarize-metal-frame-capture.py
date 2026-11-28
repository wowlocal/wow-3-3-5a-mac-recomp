"""Join a WineMetalGL frame capture to its GPU completion and Metal buffers."""

import argparse
from bisect import bisect_left
from collections import Counter, defaultdict
import json
from pathlib import Path
import plistlib
import re
from statistics import median
import xml.etree.ElementTree as ElementTree

from metal_submission_log import interval_coverage, read_submissions


def read_capture_log(path):
    text = path.read_text()
    begin = list(re.finditer(r"^WMGL_CAPTURE begin stream=(\d+) after_sequence=(\d+) draws=(\d+)$", text, re.M))
    end = list(re.finditer(r"^WMGL_CAPTURE end sequence=(\d+) draws=(\d+)$", text, re.M))
    if len(begin) != 1 or len(end) != 1 or begin[0].end() >= end[0].start():
        raise ValueError("Expected one complete capture in the client log")
    stream, previous, previous_draws = map(int, begin[0].groups())
    sequence, draws = map(int, end[0].groups())
    labels = re.findall(r"^WMGL_CAPTURE_DRAW (.+)$", text[begin[0].end():end[0].start()], re.M)
    if sequence != previous + 1 or len(labels) != draws:
        raise ValueError("Capture does not contain one consecutive, fully labeled output")
    return stream, sequence, previous_draws, labels


def read_completions(path, pid, stream, dimensions):
    completions = {}
    with path.open() as log:
        for line in log:
            if not line.endswith("\n"):
                break
            process, source, seq, end, draws, size = map(int, line.split())
            if (process, source, size) == (pid, stream, dimensions):
                if seq in completions:
                    raise ValueError(f"Duplicate GPU completion for output {seq}")
                completions[seq] = end, draws
    return completions


def read_active_intervals(path, pid):
    node = ElementTree.parse(path).getroot().find("node")
    columns = [column.findtext("mnemonic") for column in node.findall("schema/col")]
    refs = {entry.get("id"): entry for entry in node.iter() if entry.get("id")}
    buffers = defaultdict(list)
    for row in node.findall("row"):
        entries = dict(zip(columns, (refs.get(entry.get("ref"), entry) for entry in row)))
        if (
            not entries["process"].get("fmt", "").endswith(f" ({pid})")
            or entries["state"].get("fmt") != "Active"
            or not entries["cmdbuffer-id"].text
        ):
            continue
        start = int(entries["start"].text)
        end = start + int(entries["duration"].text)
        buffer = int(entries["cmdbuffer-id"].text)
        stage = entries["channel-name"].get("fmt", "")
        encoder = entries["encoder-id"].text
        buffers[buffer].append((start, end, stage, encoder))
    return buffers


def align_fragment_ends(buffers, submissions):
    trace_ends = sorted(
        (max(fragment_ends), buffer)
        for buffer, intervals in buffers.items()
        if (fragment_ends := [end for _, end, name, _ in intervals if name == "Fragment"])
    )
    gpu_ends = sorted({row.end for row in submissions if row.draws})
    if len(trace_ends) < 24 or len(gpu_ends) < 24:
        raise ValueError("Too few fragment endpoints to align the GPU clocks")
    sample = [end for end, _ in trace_ends[len(trace_ends) // 32::max(1, len(trace_ends) // 32)]]
    anchor = trace_ends[len(trace_ends) // 2][0]
    tolerance = 250

    def closest_error(time):
        index = bisect_left(gpu_ends, time)
        return min(abs(time - end) for end in gpu_ends[max(0, index - 1):index + 1])

    def rank(offset):
        errors = [closest_error(end + offset) for end in sample]
        matches = [error for error in errors if error <= tolerance]
        return len(matches), -sum(matches)

    offset = max((end - anchor for end in gpu_ends), key=rank)
    if rank(offset)[0] < max(16, len(sample) // 2):
        raise ValueError("Fragment endpoints did not align to the submission log")
    errors = [closest_error(end + offset) for end, _ in trace_ends]
    matched = [error for error in errors if error <= tolerance]
    evidence = {"matched_endpoints": len(matched), "maximum_error_ns": max(matched)}
    return offset, tolerance, trace_ends, evidence


def align_capture_stages(buffers, output_ends):
    fragment_ends = sorted({max(ends) for intervals in buffers.values()
                            if (ends := [end for _, end, stage, _ in intervals if stage == "Fragment"])})
    tolerance = 250
    votes = Counter((end - trace_end) // tolerance
                    for end in output_ends[::max(1, len(output_ends) // 48)] for trace_end in fragment_ends)
    if not votes:
        raise ValueError("The captured stream has no trace clock anchors")

    def residuals(offset):
        for end in output_ends:
            relative = end - offset
            index = bisect_left(fragment_ends, relative)
            nearby = fragment_ends[max(0, index - 1):index + 1]
            difference = end - min(nearby, key=lambda time: abs(relative - time))
            if abs(difference - offset) <= tolerance:
                yield difference

    candidates = [(bucket * tolerance + tolerance // 2, list(residuals(bucket * tolerance + tolerance // 2)))
                  for bucket, _ in votes.most_common(8)]
    _, matched = max(candidates, key=lambda item: (len(item[1]), -sum(abs(item[0] - x) for x in item[1])))
    if len(matched) < 30:
        raise ValueError("Too few output completions match the trace clock")
    offset = round(median(matched))
    errors = [abs(value - offset) for value in residuals(offset)]
    evidence = {"anchor": "final fragment endpoint", "matched_endpoints": len(errors), "maximum_error_ns": max(errors)}
    return offset, tolerance, evidence


def summarize_system_trace(root, xml, pid, previous_end, capture_end, labels, *, output_ends=None):
    submissions = [row for kind, row in read_submissions(root / "submissions.ns", pid) if kind == "render"]
    by_end = defaultdict(list)
    for submit in submissions:
        by_end[submit.end].append(submit)
    if len(by_end[previous_end]) != 1 or len(by_end[capture_end]) != 1:
        raise ValueError("Capture boundaries must match unique Metal submission timestamps")
    previous, final = by_end[previous_end][0], by_end[capture_end][0]
    if previous.stream != final.stream:
        raise ValueError("Capture boundaries use different renderer command streams")
    captured = sorted(
        (submit for submit in submissions
         if submit.stream == final.stream and previous.serial < submit.serial <= final.serial),
        key=lambda row: row.serial,
    )
    if sum(row.draws for row in captured) != len(labels):
        raise ValueError("Draw counts do not match the captured command buffers")

    buffers = read_active_intervals(xml, pid)
    if output_ends is None:
        offset, tolerance, endings, alignment = align_fragment_ends(buffers, submissions)
    else:
        offset, tolerance, alignment = align_capture_stages(buffers, output_ends)
        limits = {buffer: (min(start for start, _, _, _ in spans), max(end for _, end, _, _ in spans))
                  for buffer, spans in buffers.items()}
    groups = []
    for submit in captured:
        if not submit.draws:
            continue
        if output_ends is None:
            matches = [(end, buffer) for end, buffer in endings if abs(end + offset - submit.end) <= tolerance]
        else:
            matches = [(end, buffer) for buffer, (start, end) in limits.items()
                       if abs(start + offset - submit.start) <= tolerance and abs(end + offset - submit.end) <= tolerance]
        if len(matches) != 1:
            raise ValueError(f"Expected one traced command buffer for submission {submit.serial}")
        trace_end, buffer = matches[0]
        intervals = buffers[buffer]
        stage_names = {name for _, _, name, _ in intervals}
        stage_activity = {}
        for name in sorted(stage_names):
            bounds = [(a + offset, b + offset) for a, b, stage, _ in intervals if stage == name]
            stage_activity[name] = interval_coverage(bounds)(submit.start, submit.end)
        begins_at = sum(group["draws"] for group in groups)
        group = {
            "submission_serial": submit.serial,
            "trace_command_buffer_id": buffer,
            "draws": submit.draws,
            "submit_to_gpu_start_ns": submit.start - submit.submit,
            "gpu_start_to_end_ns": submit.end - submit.start,
            "gpu_active_ns_by_stage": stage_activity,
            "traced_encoder_count": len({encoder for _, _, _, encoder in intervals if encoder is not None}),
            "shader_combination_counts": dict(Counter(labels[begins_at:begins_at + submit.draws]).most_common()),
        }
        if output_ends is None:
            group["fragment_endpoint_error_ns"] = abs(trace_end + offset - submit.end)
        else:
            group["stage_start_error_ns"] = abs(limits[buffer][0] + offset - submit.start)
            group["stage_end_error_ns"] = abs(trace_end + offset - submit.end)
        groups.append(group)
    return {"clock_alignment": alignment, "submissions": groups}


def capture_report(root, intervals, *, all_stages=False):
    stream, sequence, previous_draws, labels = read_capture_log(root / "client.log")
    window = json.loads((root / "window.json").read_text())
    width, height = window["window_size"]
    if window["stream"] != stream or window["missing_sequences"]:
        raise ValueError("Expected the verified GPU window on the captured stream")
    completions = read_completions(root / "gpu.ns", window["pid"], stream, width << 32 | height)
    previous_end, previous_count = completions[sequence - 1]
    capture_end, draw_count = completions[sequence]
    if (previous_count, draw_count) != (previous_draws, len(labels)):
        raise ValueError("Capture log and GPU completion counts disagree")
    archive = root / "raid-frame.gputrace"
    metadata = plistlib.loads((archive / "metadata").read_bytes())
    frame_count = metadata["DYCaptureEngine.captured_frames_count"]
    draw_counts = Counter(labels)
    capture_data = (archive / "capture").read_bytes()
    if frame_count != 1 or any(capture_data.count(name.encode()) < count for name, count in draw_counts.items()):
        raise ValueError("GPU trace is missing the captured frame or its draw signposts")
    report = {
        "viewport": [width, height],
        "stream": stream,
        "captured_sequence": sequence,
        "previous_output_draws": previous_draws,
        "captured_output_draws": draw_count,
        "completion_gap_during_capture_ns": capture_end - previous_end,
        "archive_frame_count": frame_count,
        "archive_bytes": sum(path.stat().st_size for path in archive.rglob("*") if path.is_file()),
        "shader_combination_counts": dict(draw_counts.most_common()),
    }
    if intervals:
        output_ends = None
        if all_stages:
            witness = json.loads((root / "trace-witness.json").read_text())
            if (witness["attach_pid"] != window["pid"] or witness["returncode"] or not witness["trace_exists"]
                    or not (window["last_sequence"] < sequence and
                            completions[window["last_sequence"]][0] < witness["begin_uptime_ns"]
                            <= witness["trigger_uptime_ns"] <= capture_end <= witness["end_uptime_ns"])):
                raise ValueError("The all-stage trace was unverified or overlapped the measured window")
            output_ends = sorted(end for end, _ in completions.values()
                                 if witness["begin_uptime_ns"] <= end <= witness["end_uptime_ns"])
        report["metal_system_trace"] = summarize_system_trace(
            root, intervals, window["pid"], previous_end, capture_end, labels, output_ends=output_ends,
        )
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_dir", type=Path)
    parser.add_argument("--intervals-xml", type=Path, help="exported metal-gpu-intervals table from the same run")
    parser.add_argument("--all-stages", action="store_true", help="match each captured command buffer across all active stages")
    options = parser.parse_args()
    if options.all_stages and not options.intervals_xml:
        parser.error("--all-stages requires --intervals-xml")
    print(json.dumps(capture_report(options.capture_dir, options.intervals_xml, all_stages=options.all_stages), indent=2))
