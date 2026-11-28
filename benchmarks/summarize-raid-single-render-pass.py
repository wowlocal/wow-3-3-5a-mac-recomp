"""Compare automatic Metal pass splitting with submit-at-swap in the live raid."""

import argparse
from bisect import bisect_left, bisect_right
from collections import Counter
import json
from pathlib import Path
from runpy import run_path
from statistics import median

from metal_submission_log import interval_coverage


HERE = Path(__file__).resolve().parent
ENCODERS = run_path(str(HERE / "summarize-raid-encoder-tail.py"))
SYSTEM = ENCODERS["SYSTEM"]
LIFECYCLE = ENCODERS["LIFECYCLE"]
VERIFY_WINDOW = ENCODERS["VERIFY"]
TRACE_PERIOD_NS = 500_000


def verify(capture, setting, diagnostic):
    observed = (capture / "pass-mode.txt").read_text().split()
    if observed != [str(setting), str(int(diagnostic))]:
        raise ValueError(f"Draw setting or trace witness differs in {capture}")
    if (capture / "submissions.ns").exists() != diagnostic or (capture / "encoder-work.tsv").exists():
        raise ValueError(f"Unexpected renderer instrumentation in {capture}")
    if (capture / "map-shadow-mode.txt").read_text().split() != ["1", "0"]:
        raise ValueError(f"Different guest buffer reuse settings in {capture}")
    if json.loads((capture / "legacy.json").read_text()) != {
        "legacy_paused": True, "legacy_completed_frames_during_launch": 0,
    }:
        raise ValueError(f"Earlier test ran during {capture}")
    return VERIFY_WINDOW(capture, 35 if diagnostic else 60)


def summarize_trace(capture, setting, window, frames, submissions):
    buffers, names, external = ENCODERS["read_trace"](capture, window["pid"])
    application_encoders = Counter(buffer for buffer, _ in names)
    windowserver = external.get("windowserver", interval_coverage(()))
    offset, tolerance, fragment_ends, alignment = SYSTEM["ALIGN_FRAGMENT_ENDS"](buffers, submissions)
    times = [tick for tick, _ in fragment_ends]
    serials = [submission.serial for submission in submissions]

    def match(submission):
        tick = submission.end - offset
        i = bisect_left(times, tick)
        options = [
            buffer for end, buffer in fragment_ends[max(0, i - 1):i + 1]
            if abs(end - tick) <= tolerance
        ]
        if len(options) > 1:
            raise ValueError("Ambiguous native buffer completion")
        return options[0] if options else None

    all_vertex = interval_coverage(
        (a, b) for spans in buffers.values() for a, b, stage, _ in spans if stage == "Vertex"
    )
    anchor_indices = [i for i, frame in enumerate(frames) if match(frame.final) is not None]
    if len(anchor_indices) < 100:
        raise ValueError(f"Too few matched output endpoints for draw setting {setting}")
    # Keep every verified application completion between the matched trace
    # boundaries, including those with incomplete per-encoder telemetry.
    selected = frames[anchor_indices[0]:anchor_indices[-1] + 1]
    joined = []
    for previous, frame in zip(selected, selected[1:]):
        outputs = submissions[bisect_right(serials, previous.final.serial):bisect_right(serials, frame.final.serial)]
        draw_buffers = [match(submission) for submission in outputs if submission.draws]
        start, stop = previous.final.end - offset, frame.final.end - offset
        row = {
            "sequence": frame.sequence,
            "completion_gap_ns": stop - start,
            "vertex_activity_in_gap_ns": all_vertex(start, stop),
            "windowserver_activity_in_gap_ns": windowserver(start, stop),
            "final_submission_interval_ns": frame.final.submit - previous.final.submit,
            "final_gpu_span_ns": frame.final.end - frame.final.start,
            "draw_buffers": len(draw_buffers),
        }
        vertex_by_buffer = [
            [(a, b, encoder) for a, b, stage, encoder in buffers.get(buffer, ()) if stage == "Vertex"]
            for buffer in draw_buffers
        ]
        if vertex_by_buffer and all(vertex_by_buffer):
            final_vertex = vertex_by_buffer[-1]
            begin_final = min(a for a, _, _ in final_vertex)
            final_buffer = draw_buffers[-1]
            encoder_count = len({encoder for _, _, encoder in final_vertex})
            own_vertex = interval_coverage((a, b) for spans in vertex_by_buffer for a, b, _ in spans)
            row.update({
                "own_vertex_activity_in_gap_ns": own_vertex(start, stop),
                "final_buffer_vertex_active_ns": ENCODERS["active_ns"]([(a, b) for a, b, _ in final_vertex]),
                "final_buffer_vertex_encoder_count": encoder_count,
                "final_buffer_application_encoder_count": application_encoders[final_buffer],
                "windowserver_activity_before_final_vertex_ns": windowserver(
                    begin_final - TRACE_PERIOD_NS, begin_final
                ),
            })
        joined.append(row)

    if len(joined) < 100 or joined[-1]["sequence"] - joined[0]["sequence"] + 1 != len(joined):
        raise ValueError(f"Nonconsecutive Metal trace outputs for draw setting {setting}")
    gap_metrics = LIFECYCLE["distribution"]([row["completion_gap_ns"] for row in joined])
    tail = [row for row in joined if row["completion_gap_ns"] >= gap_metrics["p99"]]
    common = (
        "completion_gap_ns", "vertex_activity_in_gap_ns", "windowserver_activity_in_gap_ns",
        "final_submission_interval_ns", "final_gpu_span_ns",
    )
    specific = (
        "own_vertex_activity_in_gap_ns", "final_buffer_vertex_active_ns", "final_buffer_vertex_encoder_count",
        "final_buffer_application_encoder_count", "windowserver_activity_before_final_vertex_ns",
    )

    def summarize(rows):
        have_vertex = [row for row in rows if "final_buffer_vertex_active_ns" in row]
        if not have_vertex:
            raise ValueError(f"No attribution for the final encoder at draw setting {setting}")
        return {
            "intervals": len(rows),
            "complete_vertex_attribution_intervals": len(have_vertex),
            "draw_buffers_per_output": dict(sorted(Counter(row["draw_buffers"] for row in rows).items())),
            "medians": {
                **{name: median(row[name] for row in rows) for name in common},
                **{name: median(row[name] for row in have_vertex) for name in specific},
            },
        }

    return {
        "clock_alignment": alignment,
        "matched_final_output_endpoints_inside_trace_boundaries": len(anchor_indices),
        "p99_completion_gap_ns": gap_metrics["p99"],
        "all_intervals": summarize(joined),
        "slowest_one_percent": summarize(tail),
    }


def diagnose(capture, setting):
    verified_window = verify(capture, setting, True)
    window = json.loads((capture / "window.json").read_text())
    by_end, by_stream = LIFECYCLE["render_submissions"](capture / "submissions.ns", window["pid"])
    frames, _ = LIFECYCLE["read_frames"](capture, window, by_end, False)
    submissions = by_stream[frames[0].final.stream]
    intervals = LIFECYCLE["explain_intervals"](frames, False, submissions)
    counts = Counter(row["render_submissions"] for row in intervals)
    draw_counts = Counter(sum(draws > 0 for draws in row["draws_per_submission"]) for row in intervals)
    return {
        "draw_setting": setting,
        "verified_gpu_window": verified_window,
        "render_submissions_per_output": dict(sorted(counts.items())),
        "draw_bearing_submissions_per_output": dict(sorted(draw_counts.items())),
        "most_common_draw_splits": [
            {"draws": split, "intervals": number}
            for split, number in Counter(row["draws_per_submission"] for row in intervals).most_common(6)
        ],
        "trace": summarize_trace(capture, setting, window, frames, submissions),
    }


def summarize(root):
    diagnostics = [diagnose(root / f"diagnostic-{value}", value) for value in (64, 0)]
    timed = [
        {"order": order, "draw_setting": setting, **verify(root / f"timed-{order}-{setting}", setting, False)}
        for order, setting in enumerate((64, 0, 0, 64), 1)
    ]
    fps = "average_completed_fps"
    gap = "p99_gpu_completion_gap_ns"
    comparison = {
        title: {
            "median_gpu_completed_fps": median(row[fps] for row in timed if row["draw_setting"] == setting),
            "median_p99_completion_gap_ns": median(row[gap] for row in timed if row["draw_setting"] == setting),
        }
        for title, setting in (("automatic_split", 64), ("automatic_split_disabled", 0))
    }
    return {
        "diagnostics": diagnostics,
        "timed_runs": timed,
        "comparison": comparison,
        "paired_disabled_minus_automatic": [
            {"completed_fps": timed[b][fps] - timed[a][fps], "p99_gap_ns": timed[b][gap] - timed[a][gap]}
            for a, b in ((0, 1), (3, 2))
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_root", type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize(args.capture_root), indent=2))


if __name__ == "__main__":
    main()
