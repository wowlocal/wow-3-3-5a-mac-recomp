"""Align a raid's render timings with GPU activity and main-thread scheduling."""

import argparse
from bisect import bisect_left
from collections import Counter, defaultdict
import json
from pathlib import Path
from runpy import run_path
from statistics import median
from xml.etree import ElementTree

from metal_submission_log import interval_coverage


HERE = Path(__file__).resolve().parent
LIFECYCLE = run_path(str(HERE / "summarize-raid-frame-tail.py"))
ALIGN_FRAGMENT_ENDS = run_path(str(HERE / "summarize-metal-frame-capture.py"))["align_fragment_ends"]


def exported_rows(path, schema, columns):
    names = None
    node = None
    references = {}
    for event, item in ElementTree.iterparse(path, events=("start", "end")):
        if event == "start":
            if item.tag == "node":
                node = item
            continue
        if item.tag == "schema":
            if names is not None or item.get("name") != schema:
                raise ValueError(f"Expected exactly one {schema} table")
            names = [col.findtext("mnemonic") for col in item]
            if not columns <= set(names):
                raise ValueError(f"Missing columns in {schema}")
        if item.tag != "row":
            continue
        if node is None or names is None or len(item) != len(names):
            raise ValueError(f"Invalid row in {schema}")
        for value in item.iter():
            if (identifier := value.get("id")) is not None:
                references[identifier] = value
        yield {
            name: references[value.get("ref")] if value.get("ref") else value
            for name, value in zip(names, item) if name in columns
        }
        node.remove(item)


def gpu_activity(path, pid, legacy_pid, separate_windowserver=False):
    columns = {"state", "start", "duration", "process", "channel-name", "cmdbuffer-id"}
    buffers = defaultdict(list)
    spans = defaultdict(list)
    counts = Counter()
    for row in exported_rows(path, "metal-gpu-intervals", columns):
        if row["state"].get("fmt") != "Active":
            continue
        start = int(row["start"].text)
        end = start + int(row["duration"].text)
        if end <= start:
            continue
        process = row["process"].get("fmt", "")
        stage = row["channel-name"].get("fmt", "")
        if process.endswith(f" ({pid})"):
            group = "client"
            if row["cmdbuffer-id"].text:
                buffers[int(row["cmdbuffer-id"].text)].append((start, end, stage, None))
            spans[stage].append((start, end))
        elif legacy_pid is not None and process.endswith(f" ({legacy_pid})"):
            group = "legacy"
        elif separate_windowserver and process.startswith("WindowServer ("):
            group = "windowserver"
        else:
            group = "other" if process else "unspecified"
        spans[group].append((start, end))
        counts[group] += 1
    return buffers, spans, dict(counts)


def main_thread_activity(path, pid):
    columns = {"start", "thread", "state", "duration", "process"}
    spans = defaultdict(list)
    main_threads = set()
    for row in exported_rows(path, "thread-state", columns):
        process = row["process"].get("fmt", "")
        thread = row["thread"].get("fmt", "")
        if not process.endswith(f" ({pid})") or not thread.startswith("Main Thread "):
            continue
        main_threads.add(thread)
        start = int(row["start"].text)
        stop = start + int(row["duration"].text)
        if stop > start:
            spans[row["state"].text].append((start, stop))
    if len(main_threads) != 1 or not spans["Running"]:
        raise ValueError("Expected running intervals for one target main thread")
    return spans


def summarize(capture, intervals_xml, states_xml, legacy_pid, scheduler_window=False):
    window = json.loads((capture / "window.json").read_text())
    if window["missing_sequences"]:
        raise ValueError("Expected a continuous verified output window")
    by_end, by_stream = LIFECYCLE["render_submissions"](capture / "submissions.ns", window["pid"])
    frames, _ = LIFECYCLE["read_frames"](capture, window, by_end, False)
    submissions = by_stream[frames[0].final.stream]
    buffers, gpu, counts = gpu_activity(intervals_xml, window["pid"], legacy_pid)
    offset, tolerance, endings, alignment = ALIGN_FRAGMENT_ENDS(buffers, submissions)
    endpoints = [stamp for stamp, _ in endings]
    matched = []
    errors = []
    for frame in frames:
        end = frame.final.end - offset
        i = bisect_left(endpoints, end)
        error = min(abs(end - stamp) for stamp in endpoints[max(0, i - 1):i + 1])
        if error <= tolerance:
            matched.append(frame)
            errors.append(error)
    if states_xml:
        states = main_thread_activity(states_xml, window["pid"])
        all_cpu = interval_coverage(t for spans in states.values() for t in spans)
        running = interval_coverage(states["Running"])
    if scheduler_window:
        # The GPU trace supplies the common clock. Use the independently complete
        # submission log inside the scheduler timeline even if GPU events are sparse.
        start = min(a for spans in states.values() for a, _ in spans)
        stop = max(b for spans in states.values() for _, b in spans)
        selected = [f for f in frames if start <= f.final.submit - offset <= f.final.end - offset <= stop]
    else:
        selected = matched
        cover = {key: interval_coverage(gpu[key]) for key in (
            "client", "Vertex", "Fragment", "Compute", "legacy", "other", "unspecified",
        )}
        external_spans = gpu["legacy"] + gpu["other"] + gpu["unspecified"]
        external_gpu = interval_coverage(external_spans)
        all_gpu = interval_coverage(gpu["client"] + external_spans)
    if len(selected) < 2 or selected[-1].sequence - selected[0].sequence + 1 != len(selected):
        raise ValueError("Selected trace window does not cover a continuous series of verified outputs")
    intervals = LIFECYCLE["explain_intervals"](selected, False, submissions)
    for interval, before, after in zip(intervals, selected, selected[1:]):
        interval["draws_in_completed_output"] = after.draws
        if not scheduler_window:
            a, b = before.final.end - offset, after.final.end - offset
            client_period = cover["client"](a, b)
            active_period = all_gpu(a, b)
            interval["client_gpu_active_ns"] = client_period
            for key, name in (
                ("Vertex", "client_vertex"), ("Fragment", "client_fragment"), ("Compute", "client_compute"),
                ("legacy", "legacy_gpu"), ("other", "other_gpu"), ("unspecified", "unattributed_gpu"),
            ):
                interval[f"{name}_active_ns"] = cover[key](a, b)
            interval["outside_client_with_other_gpu_activity_ns"] = active_period - client_period
            interval["client_overlapping_other_gpu_activity_ns"] = client_period + external_gpu(a, b) - active_period
            interval["no_recorded_gpu_activity_ns"] = b - a - active_period
        if states_xml:
            a, b = before.final.submit - offset, after.final.submit - offset
            cpu = running(a, b)
            recorded = all_cpu(a, b)
            if recorded != b - a:
                raise ValueError("Main-thread states do not cover the final-submit interval")
            interval["main_thread_running_between_final_submits_ns"] = cpu
            interval["main_thread_not_running_between_final_submits_ns"] = b - a - cpu
            parts = interval["components"]
            span = parts.pop("final_submit_interval_ns")
            parts["main_thread_running_between_final_submits_ns"] = cpu
            parts["main_thread_not_running_between_final_submits_ns"] = span - cpu
    result = LIFECYCLE["summarize"](intervals)
    result["selected_outputs"] = len(selected)
    result["selected_output_completion_span_ns"] = selected[-1].final.end - selected[0].final.end
    result["matched_gpu_endpoints_inside_verified_window"] = len(matched)
    result["maximum_final_endpoint_error_ns"] = max(errors) if errors else None
    result["clock_alignment"] = alignment
    result["active_gpu_rows_by_process_group"] = counts
    result["main_thread_states_joined"] = states_xml is not None
    result["selected_window_by"] = "scheduler_boundaries" if scheduler_window else "matched_gpu_endpoints"
    if not scheduler_window and legacy_pid is not None:
        threshold = result["metrics"]["completion_gap_ns"]["p99"]
        result["legacy_gpu_active_in_intervals"] = sum(row["legacy_gpu_active_ns"] > 0 for row in intervals)
        result["legacy_gpu_active_in_tail_intervals"] = sum(
            row["legacy_gpu_active_ns"] > 0 and row["completion_gap_ns"] >= threshold for row in intervals
        )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    parser.add_argument("gpu_intervals", type=Path)
    parser.add_argument("--thread-states", type=Path)
    parser.add_argument("--legacy-pid", type=int)
    parser.add_argument("--scheduler-window", action="store_true")
    args = parser.parse_args()
    if args.scheduler_window and not args.thread_states:
        parser.error("scheduler-window requires a thread-state export")
    if args.legacy_pid is not None and args.legacy_pid <= 0:
        parser.error("legacy-pid must be positive")
    result = summarize(args.capture, args.gpu_intervals, args.thread_states, args.legacy_pid,
                       args.scheduler_window)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
