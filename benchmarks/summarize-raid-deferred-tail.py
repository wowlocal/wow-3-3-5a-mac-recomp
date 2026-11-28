"""Attribute the selected PGO renderer's long completion gaps to host and GPU phases."""

import argparse
from bisect import bisect_left
from collections import defaultdict
import json
from pathlib import Path
from runpy import run_path
from statistics import median

from metal_submission_log import interval_coverage


HERE = Path(__file__).resolve().parent
SYSTEM = run_path(str(HERE / "summarize-raid-system-tail.py"))
LIFE = SYSTEM["LIFECYCLE"]
RENDER_END = run_path(str(HERE / "summarize-raid-render-end.py"))
QUEUED = RENDER_END["QUEUED"]


def verify_capture(capture, artifacts, legacy_pid=None):
    if (json.loads((capture / "artifact-sha256.json").read_text()) != artifacts or
            (capture / "pgo-mode.txt").read_text().split() != ["candidate", "0"] or
            (capture / "pass-mode.txt").read_text().split() != ["64", "1"] or
            (capture / "render-end-mode.txt").read_text().split() != ["deferred", "1"] or
            (capture / "map-shadow-mode.txt").read_text().split() != ["1", "0"] or
            "/artifacts/candidate/libwinemetalgl.dylib" not in (capture / "renderer-map.txt").read_text()):
        raise ValueError(f"Wrong renderer, runtime or instrumentation in {capture}")
    witness = capture / "legacy-window.json"
    if witness.is_file():
        earlier = json.loads(witness.read_text())
        first, last = RENDER_END["completion_bounds"](capture)
        if (earlier["legacy_pid"] != legacy_pid or earlier["legacy_completed_frames_in_verified_window"] or
                not earlier["legacy_last_completion_before_window_ns"] < first <= last <
                    earlier["legacy_first_completion_after_window_ns"]):
            raise ValueError("Earlier GPU completions do not bracket a quiet verified interval")
    else:
        earlier = json.loads((capture / "legacy.json").read_text())
        if earlier != {"legacy_paused": True, "legacy_completed_frames_during_launch": 0}:
            raise ValueError(f"Earlier test ran during {capture}")
    return QUEUED["TIMING"]["VERIFY_WINDOW"](capture, 45), earlier


def worker_timings(capture, eligible_submissions):
    producer = {}
    for mode, queue, serial, begin, returned in (
        tuple(map(int, row)) for row in LIFE["rows"](capture / "render-finalize.tsv", 5)
    ):
        if mode != 1 or not 0 < begin <= returned:
            raise ValueError("Invalid deferred finalization interval")
        producer[queue, serial] = begin, returned
    by_serial = defaultdict(list)
    for queue, serial, reserve, start, closed, returned in (
        tuple(map(int, row)) for row in LIFE["rows"](capture / "render-worker.tsv", 6)
    ):
        if not 0 < reserve <= start <= closed <= returned:
            raise ValueError("Invalid worker timestamp order")
        begin, producer_return = producer.get((queue, serial), (0, 0))
        if begin <= reserve <= producer_return:
            by_serial[serial].append((begin, producer_return, reserve, start, closed, returned))
    result = {}
    for submit in eligible_submissions:
        matches = [row for row in by_serial[submit.serial] if row[0] <= submit.submit <= row[2]]
        if len(matches) != 1 or matches[0][4] > submit.start:
            raise ValueError(f"Expected one worker close before GPU start for buffer {submit.serial}")
        result[submit.serial] = matches[0]
    return result


def buffer_matches(frames, endings, offset, tolerance):
    times = [end for end, _ in endings]
    matches = {}
    for frame in frames:
        expect = frame.final.end - offset
        index = bisect_left(times, expect - tolerance)
        options = []
        while index < len(times) and times[index] <= expect + tolerance:
            options.append(endings[index])
            index += 1
        if len(options) == 1:
            matches[frame.sequence] = options[0][1]
    return matches


def continuous_stage_frames(frames, matches):
    longest = []
    current = []
    for frame in frames:
        if frame.sequence in matches:
            current.append(frame)
            if len(current) > len(longest):
                longest = current
        else:
            current = []
    return longest


def compare_equal_draw_splits(intervals, threshold):
    normal = defaultdict(list)
    for row in intervals:
        if row["completion_gap_ns"] < threshold:
            normal[row["draws_per_submission"]].append(row)
    pairs = [(row, normal[row["draws_per_submission"]]) for row in intervals
             if row["completion_gap_ns"] >= threshold and normal[row["draws_per_submission"]]]
    fields = ("final_gpu_span_ns", "client_vertex_active_ns", "client_overlap_with_windowserver_ns")
    return {
        "matched_tail_intervals": len(pairs),
        "median_tail_minus_same_split_regular_ns": {
            field: median(row[field] - median(other[field] for other in peers) for row, peers in pairs)
            for field in fields
        } if pairs else {},
    }


def add_worker_phase(interval, previous, current, workers):
    before, final = previous.final, current.final
    first_worker = workers[before.serial]
    final_worker = workers[final.serial]
    begin, producer_return, reserve, start, closed, committed = final_worker
    parts = interval["components"]
    del parts["final_submit_to_gpu_start_change_ns"]
    parts["final_submit_to_encoder_close_change_ns"] = (
        (closed - final.submit) - (first_worker[4] - before.submit))
    parts["final_encoder_close_to_gpu_start_change_ns"] = (
        (final.start - closed) - (before.start - first_worker[4]))
    interval["draws_in_completed_output"] = current.draws
    interval["final_worker_reservation_to_start_ns"] = start - reserve
    interval["final_worker_drain_and_close_ns"] = closed - start
    interval["final_close_to_commit_return_ns"] = committed - closed
    interval["final_producer_finalize_ns"] = producer_return - begin
    interval["final_close_to_gpu_start_ns"] = final.start - closed


def summarize_capture(capture, scheduler, legacy_pid, *, include_worker_phases=True):
    window = json.loads((capture / "window.json").read_text())
    pid = window["pid"]
    by_end, by_stream = LIFE["render_submissions"](capture / "submissions.ns", pid)
    frames, _ = LIFE["read_frames"](capture, window, by_end, False)
    submissions = by_stream[frames[0].final.stream]
    buffers, spans, counts = SYSTEM["gpu_activity"](capture / "gpu-intervals.xml", pid, legacy_pid,
                                                    separate_windowserver=True)
    if counts.get("legacy"):
        raise ValueError("Earlier test had GPU work in the supposedly isolated trace")
    offset, tolerance, endings, alignment = SYSTEM["ALIGN_FRAGMENT_ENDS"](buffers, submissions)
    matches = buffer_matches(frames, endings, offset, tolerance)
    if scheduler:
        states = SYSTEM["main_thread_activity"](capture / "thread-states.xml", pid)
        covered_cpu = interval_coverage(span for group in states.values() for span in group)
        running_cpu = interval_coverage(states["Running"])
        first = min(start for group in states.values() for start, _ in group)
        last = max(end for group in states.values() for _, end in group)
        selected = [frame for frame in frames if first <= frame.final.submit - offset
                    <= frame.final.end - offset <= last]
    else:
        selected = continuous_stage_frames(frames, matches)
        covered_gpu = {name: interval_coverage(spans[name]) for name in (
            "client", "Vertex", "Fragment", "windowserver", "other", "unspecified")}
        client_and_ws = interval_coverage(spans["client"] + spans["windowserver"])
    if len(selected) < 2 or selected[-1].sequence - selected[0].sequence + 1 != len(selected):
        raise ValueError("The trace has no continuous verified output interval")
    workers = worker_timings(capture, [row.final for row in selected]) if include_worker_phases else None
    intervals = LIFE["explain_intervals"](selected, False, submissions)
    for interval, previous, current in zip(intervals, selected, selected[1:]):
        if workers is not None:
            add_worker_phase(interval, previous, current, workers)
        else:
            interval["draws_in_completed_output"] = current.draws
        before, final = previous.final, current.final
        parts = interval["components"]
        if scheduler:
            a, b = before.submit - offset, final.submit - offset
            if covered_cpu(a, b) != b - a:
                raise ValueError("Main thread states do not cover the producer interval")
            cpu = running_cpu(a, b)
            interval["main_thread_running_between_final_submits_ns"] = cpu
            interval["main_thread_not_running_between_final_submits_ns"] = b - a - cpu
            del parts["final_submit_interval_ns"]
            parts["main_thread_running_between_final_submits_ns"] = cpu
            parts["main_thread_not_running_between_final_submits_ns"] = b - a - cpu
        else:
            a, b = before.end - offset, final.end - offset
            for key, field in (("client", "client_gpu_active_ns"), ("Vertex", "client_vertex_active_ns"),
                               ("Fragment", "client_fragment_active_ns"), ("windowserver", "windowserver_active_ns"),
                               ("other", "other_named_processes_active_ns"),
                               ("unspecified", "unattributed_gpu_active_ns")):
                interval[field] = covered_gpu[key](a, b)
            interval["client_overlap_with_windowserver_ns"] = (
                covered_gpu["client"](a, b) + covered_gpu["windowserver"](a, b) - client_and_ws(a, b))
        if sum(parts.values()) != interval["completion_gap_ns"]:
            raise ValueError("Deferred handoff intervals do not balance")

    result = {"selected_outputs": len(selected), "verified_outputs_with_traced_fragment_endpoint": len(matches),
              "selected_output_completion_span_ns": selected[-1].final.end - selected[0].final.end,
              "clock_alignment": alignment, "active_gpu_rows_by_process_group": counts,
              **LIFE["summarize"](intervals)}
    if not scheduler:
        result["same_draw_split_tail_comparison"] = compare_equal_draw_splits(
            intervals, result["metrics"]["completion_gap_ns"]["p99"])
    return result


def summary(root, legacy_pid):
    baseline = json.loads((HERE / "results/raid-winemetalgl-defer-render-end-pgo-ab.json").read_text())
    artifacts = json.loads((root / "artifact-sha256.json").read_text())
    sha = QUEUED["sha"]
    if (sha(root / "artifacts/candidate/ocerz") != artifacts["runtime_unsigned_sha256"] or
            sha(root / "artifacts/candidate/libwinemetalgl.dylib") != artifacts["renderer_sha256"] or
            artifacts["runtime_unsigned_sha256"] != baseline["artifact_sha256"]["runtime_unsigned_sha256"] or
            artifacts["renderer_sha256"] != baseline["artifact_sha256"]["renderer_sha256_by_role"]["candidate"]):
        raise ValueError("The current selected AArchX and PGO renderer did not run")
    results = {}
    for name in ("scheduler", "stages"):
        capture = root / name
        window, legacy = verify_capture(capture, artifacts, legacy_pid)
        results[name] = {"diagnostic_gpu_window": window, "legacy_isolation": legacy,
                         **summarize_capture(capture, name == "scheduler", legacy_pid)}
    config = ("hardware", "macos", "viewport", "vsync", "caster_count", "boss_count", "camera")
    return {**{key: baseline[key] for key in config}, "artifact_sha256": artifacts, "captures": results}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_root", type=Path)
    parser.add_argument("--legacy-pid", type=int, required=True)
    args = parser.parse_args()
    print(json.dumps(summary(args.capture_root, args.legacy_pid), indent=2))
