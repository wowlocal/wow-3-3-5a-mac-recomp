"""Join verified raid outputs to the vertex stage of each Metal encoder."""

import argparse
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
import json
from pathlib import Path
import re
from runpy import run_path
from statistics import median
from typing import NamedTuple

from metal_submission_log import interval_coverage


HERE = Path(__file__).resolve().parent
SYSTEM = run_path(str(HERE / "summarize-raid-system-tail.py"))
LIFECYCLE = SYSTEM["LIFECYCLE"]
VERIFY = run_path(str(HERE / "summarize-raid-shadow-reuse.py"))["read_window"]
ENCODER_LABEL = re.compile(r"WMGL encoder (\d+)")
EXTERNAL_WINDOW_NS = 500_000


class Encoder(NamedTuple):
    stream: int
    serial: int
    width: int
    height: int
    colors: int
    depth: int
    clear: int
    draws: int


def read_encoder_work(path, pid):
    encoders = {}
    programs = defaultdict(dict)
    with path.open() as stream:
        for line in stream:
            if not line.endswith("\n"):
                break
            kind, process, *fields = line.split()
            if int(process) != pid:
                continue
            if kind == "P":
                source, serial, label, width, height, colors, depth, clear, draws = map(int, fields)
                if label in encoders:
                    raise ValueError(f"Duplicate Metal encoder label {label}")
                encoders[label] = Encoder(source, serial, width, height, colors, depth, clear, draws)
            elif kind == "V":
                label, program, opcodes, parameters, draws, references = map(int, fields)
                if label not in encoders or program in programs[label] or draws <= 0 or references < draws:
                    raise ValueError(f"Invalid vertex work for Metal encoder {label}")
                programs[label][program] = (opcodes, parameters, draws, references)
            else:
                raise ValueError("Unknown encoder work record")
    if not encoders or not programs:
        raise ValueError("Missing encoder work records")
    return encoders, programs


def read_trace(root, pid):
    export = SYSTEM["exported_rows"]
    target = f" ({pid})"
    encoders = {}
    cols = {"cmdbuffer-id", "encoder-id", "event-type", "encoder-label", "process"}
    for row in export(root / "encoders.xml", "metal-application-encoders-list", cols):
        if row["process"].get("fmt", "").endswith(target) and row["event-type"].text == "Encoding":
            key = int(row["cmdbuffer-id"].text), int(row["encoder-id"].text)
            if key in encoders:
                raise ValueError("Duplicate Metal encoder identities")
            encoders[key] = row["encoder-label"].text

    buffers = defaultdict(list)
    external = defaultdict(list)
    cols = {"start", "duration", "channel-name", "state", "cmdbuffer-id", "encoder-id", "process"}
    for row in export(root / "gpu-intervals.xml", "metal-gpu-intervals", cols):
        if row["state"].text != "Active":
            continue
        start = int(row["start"].text)
        end = start + int(row["duration"].text)
        if end <= start:
            continue
        process = row["process"].get("fmt", "")
        if not process.endswith(target):
            group = "unattributed"
            if process:
                group = "windowserver" if process.startswith("WindowServer (") else "other_processes"
            external[group].append((start, end))
        elif row["cmdbuffer-id"].text and row["encoder-id"].text:
            buffer = int(row["cmdbuffer-id"].text)
            encoder = int(row["encoder-id"].text)
            stage = row["channel-name"].text
            buffers[buffer].append((start, end, stage, encoder))
    external["all"] = [span for spans in external.values() for span in spans]
    return buffers, encoders, {group: interval_coverage(spans) for group, spans in external.items()}


def active_ns(spans):
    if not spans:
        return 0
    return interval_coverage(spans)(min(start for start, _ in spans), max(end for _, end in spans))


def attribute_buffer(submission, buffer, spans, names, work, programs):
    vertex_by_encoder = defaultdict(list)
    for start, end, stage, encoder in spans:
        if stage == "Vertex":
            vertex_by_encoder[encoder].append((start, end))
    if not vertex_by_encoder:
        raise ValueError("A draw submission has no active vertex intervals")
    begins = min(start for intervals in vertex_by_encoder.values() for start, _ in intervals)
    active = {key: active_ns(spans) for key, spans in vertex_by_encoder.items()}
    weights = Counter()
    shapes = Counter()
    targets = Counter()
    draw_coverage = 0
    total_encoder_draws = 0
    tagged = 0
    for encoder in active:
        name = names.get((buffer, encoder), "")
        tag = ENCODER_LABEL.fullmatch(name or "")
        if not tag:
            continue
        label = int(tag[1])
        meta = work[label]
        if (meta.stream, meta.serial) != (submission.stream, submission.serial):
            raise ValueError("Trace encoder resolves to a different render submission")
        tagged += 1
        total_encoder_draws += meta.draws
        targets[meta.width, meta.height, meta.colors, meta.depth, meta.clear] += 1
        for program, (opcodes, parameters, draws, references) in programs[label].items():
            draw_coverage += draws
            weights[program] += references
            shapes[opcodes, parameters] += references
    if draw_coverage > total_encoder_draws or total_encoder_draws > submission.draws:
        raise ValueError("Attributed draw totals exceed the render submission")
    longest = max(active, key=active.get)
    all_vertex = [span for spans in vertex_by_encoder.values() for span in spans]
    return {
        "begin": begins,
        "vertex_active_ns": active_ns(all_vertex),
        "vertex_span_ns": max(stop for _, stop in all_vertex) - begins,
        "longest_encoder_vertex_active_ns": active[longest],
        "vertex_encoder_count": len(active),
        "named_vertex_encoder_count": sum((buffer, key) in names for key in active),
        "tagged_vertex_encoder_count": tagged,
        "vertex_encoder_draws": total_encoder_draws,
        "counted_direct_draws": draw_coverage,
        "submitted_vertex_references": sum(weights.values()),
        "reference_counts": weights,
        "references_by_program_shape": shapes,
        "targets_by_encoder": targets,
    }


def summarize_groups(rows):
    averaged = (
        "vertex_active_ns", "vertex_span_ns", "longest_encoder_vertex_active_ns",
        "vertex_encoder_count", "submitted_vertex_references", "external_activity_in_500us_before_vertex_ns",
        "external_activity_in_500us_after_vertex_ns", "windowserver_activity_in_500us_before_vertex_ns",
        "other_processes_activity_in_500us_before_vertex_ns", "unattributed_activity_in_500us_before_vertex_ns",
    )
    result = {}
    for ordinal, label in enumerate(("first", "final")):
        groups = [row["submissions"][ordinal] for row in rows]
        result[label] = {key: median(sub[key] for sub in groups) for key in averaged}
        shapes = Counter()
        targets = Counter()
        for sub in groups:
            shapes.update(sub["references_by_program_shape"])
            targets.update(sub["targets_by_encoder"])
        result[label]["largest_program_shapes_by_submitted_references"] = [
            {"instructions": i, "parameters": p, "references": n}
            for (i, p), n in shapes.most_common(6)
        ]
        result[label]["render_targets"] = [
            {"width": w, "height": h, "colors": c, "depth": bool(d), "clear": bool(clear), "encoders": count}
            for (w, h, c, d, clear), count in targets.most_common()
        ]
        for key in ("named_vertex_encoder_count", "tagged_vertex_encoder_count", "vertex_encoder_draws", "counted_direct_draws"):
            result[label][f"total_{key}"] = sum(sub[key] for sub in groups)
    return {
        "frames": len(rows),
        "completion_gap_median_ns": median(row["completion_gap"] for row in rows),
        "vertex_active_in_completion_gap_median_ns": median(row["vertex_in_gap"] for row in rows),
        "same_output_vertex_activity_in_completion_gap_median_ns": median(row["own_vertex_in_gap"] for row in rows),
        "other_client_output_vertex_activity_outside_same_output_median_ns": median(
            row["vertex_in_gap"] - row["own_vertex_in_gap"] for row in rows
        ),
        "submissions": result,
        "submission_with_longer_vertex_stage": dict(Counter(
            "first" if row["submissions"][0]["vertex_active_ns"] >= row["submissions"][1]["vertex_active_ns"] else "final"
            for row in rows
        )),
    }


def compare_similar_submissions(tail, regular, ordinal):
    distances = []
    time_ratios = []
    for slow in tail:
        slow_buffer = slow["submissions"][ordinal]
        references = slow_buffer["reference_counts"]
        if not references:
            continue
        def difference(candidate):
            observed = candidate["submissions"][ordinal]["reference_counts"]
            return sum(abs(references[k] - observed[k]) for k in references.keys() | observed.keys())
        valid = [candidate for candidate in regular if candidate["split"][ordinal] == slow["split"][ordinal]]
        if not valid:
            continue
        closest = min(valid, key=lambda row: (difference(row), abs(row["sequence"] - slow["sequence"])))
        distances.append(difference(closest) / slow_buffer["submitted_vertex_references"])
        fast = closest["submissions"][ordinal]["vertex_active_ns"]
        time_ratios.append(slow_buffer["vertex_active_ns"] / fast)
    if not distances:
        return None
    close = [ratio for distance, ratio in zip(distances, time_ratios) if distance <= 0.01]
    return {
        "matches": len(distances),
        "median_normalized_vertex_reference_distance": median(distances),
        "median_tail_to_matched_vertex_active_ratio": median(time_ratios),
        "matches_with_reference_distance_at_most_one_percent": len(close),
        "median_tail_to_matched_vertex_active_ratio_within_one_percent": median(close) if close else None,
    }


def summarize(capture):
    window = json.loads((capture / "window.json").read_text())
    verify = VERIFY(capture, window["measurement_duration_ns"] // 1_000_000_000)
    pid = window["pid"]
    by_end, by_stream = LIFECYCLE["render_submissions"](capture / "submissions.ns", pid)
    frames, _ = LIFECYCLE["read_frames"](capture, window, by_end, False)
    submissions = by_stream[frames[0].final.stream]
    serials = [entry.serial for entry in submissions]
    buffers, names, external = read_trace(capture, pid)
    for group in ("windowserver", "other_processes", "unattributed"):
        external.setdefault(group, interval_coverage(()))
    work_path = capture / "encoder-work.tsv"
    work, programs = read_encoder_work(work_path, pid) if work_path.exists() else ({}, {})
    all_vertex = interval_coverage(
        (start, end) for spans in buffers.values() for start, end, stage, _ in spans if stage == "Vertex"
    )
    offset, tolerance, endpoints, alignment = SYSTEM["ALIGN_FRAGMENT_ENDS"](buffers, submissions)
    times = [timestamp for timestamp, _ in endpoints]

    def matching_buffer(submission):
        tick = submission.end - offset
        i = bisect_left(times, tick)
        options = [buffer for end, buffer in endpoints[max(0, i - 1):i + 1] if abs(end - tick) <= tolerance]
        if len(options) > 1:
            raise ValueError("Ambiguous render submission in the Metal System Trace")
        return options[0] if options else None

    joined = []
    buffers_with_vertex = {
        buffer for buffer, spans in buffers.items() if any(stage == "Vertex" for _, _, stage, _ in spans)
    }
    for before, frame in zip(frames, frames[1:]):
        split = submissions[bisect_right(serials, before.final.serial):bisect_right(serials, frame.final.serial)]
        if len(split) != 2 or split[-1] != frame.final or sum(s.draws for s in split) != frame.draws:
            raise ValueError("Expected two render submissions with the recorded draw counts")
        native = [matching_buffer(s) for s in split]
        if any(buffer is None for buffer in native) or matching_buffer(before.final) is None:
            continue
        a, b = before.final.end - offset, frame.final.end - offset
        frame_row = {"sequence": frame.sequence, "split": [s.draws for s in split], "completion_gap": b - a}
        if any(buffer not in buffers_with_vertex for buffer in native):
            joined.append(frame_row)
            continue
        attributed = [attribute_buffer(s, buf, buffers[buf], names, work, programs) for s, buf in zip(split, native)]
        for buf in attributed:
            begin = buf["begin"]
            buf["external_activity_in_500us_before_vertex_ns"] = external["all"](begin - EXTERNAL_WINDOW_NS, begin)
            buf["external_activity_in_500us_after_vertex_ns"] = external["all"](begin, begin + EXTERNAL_WINDOW_NS)
            for group in ("windowserver", "other_processes", "unattributed"):
                buf[f"{group}_activity_in_500us_before_vertex_ns"] = external[group](begin - EXTERNAL_WINDOW_NS, begin)
        own_vertex = interval_coverage(
            (start, end) for buf in native for start, end, stage, _ in buffers[buf] if stage == "Vertex"
        )
        joined.append({
            **frame_row,
            "vertex_in_gap": all_vertex(a, b),
            "own_vertex_in_gap": own_vertex(a, b),
            "submissions": attributed,
        })
    if len(joined) < 100 or joined[-1]["sequence"] - joined[0]["sequence"] + 1 != len(joined):
        raise ValueError("Expected consecutive joined completions across the trace")
    threshold = LIFECYCLE["distribution"]([row["completion_gap"] for row in joined])["p99"]
    with_vertex = [row for row in joined if "submissions" in row]
    tail = [row for row in with_vertex if row["completion_gap"] >= threshold]
    regular = [row for row in with_vertex if row["completion_gap"] < threshold]
    all_tail = sum(row["completion_gap"] >= threshold for row in joined)
    if not tail or len(with_vertex) < 0.95 * len(joined):
        raise ValueError("Insufficient coverage from Metal vertex intervals")
    return {
        "diagnostic_gpu_window": verify,
        "joined_clock_alignment": alignment,
        "endpoint_matched_frames": len(joined),
        "endpoint_matched_tail_frames": all_tail,
        "endpoint_matched_frames_missing_vertex_intervals": len(joined) - len(with_vertex),
        "joined_p99_completion_gap_ns": threshold,
        "all_joined_intervals_with_vertex_activity": summarize_groups(with_vertex),
        "slowest_one_percent": summarize_groups(tail),
        "nearest_non_tail_submissions_by_vertex_references": {
            label: compare_similar_submissions(tail, regular, index)
            for index, label in enumerate(("first", "final"))
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("captures", nargs="+", type=Path)
    args = parser.parse_args()
    print(json.dumps({"captures": [summarize(path) for path in args.captures]}, indent=2))


if __name__ == "__main__":
    main()
