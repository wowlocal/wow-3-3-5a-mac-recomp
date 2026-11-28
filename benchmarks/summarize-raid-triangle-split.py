"""Compare the first submission's triangle budget on the full raid."""

import argparse
from bisect import bisect_right
from collections import Counter
import hashlib
import json
from pathlib import Path
from runpy import run_path
from statistics import median

from metal_submission_log import interval_coverage


HERE = Path(__file__).resolve().parent
LIFE = run_path(str(HERE / "summarize-raid-frame-tail.py"))
READ_WINDOW = run_path(str(HERE / "summarize-raid-shadow-reuse.py"))["read_window"]
SPLIT = run_path(str(HERE / "summarize-raid-render-reuse.py"))
FPS = "average_completed_fps"
P99 = "p99_gpu_completion_gap_ns"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_run(folder, mode, logging, seconds, artifacts):
    settings = {
        "triangle-split-mode.txt": [str(mode), str(logging)],
        "pgo-mode.txt": ["candidate", "0"],
        "map-shadow-mode.txt": ["1", "0"],
        "pass-mode.txt": ["64", str(logging)],
        "render-end-mode.txt": ["deferred", "0"],
    }
    for filename, expected in settings.items():
        if (folder / filename).read_text().split() != expected:
            raise ValueError(f"Wrong settings in {folder / filename}")
    if ("/artifacts/candidate/libwinemetalgl.dylib" not in (folder / "renderer-map.txt").read_text() or
            json.loads((folder / "artifact-sha256.json").read_text()) != artifacts or
            json.loads((folder / "legacy.json").read_text()) != {
                "legacy_paused": True, "legacy_completed_frames_during_launch": 0,
            } or any((folder / name).is_file() != bool(logging)
                      for name in ("submissions.ns", "triangle-split.tsv"))):
        raise ValueError(f"Different binaries, logging or isolation in {folder}")
    return READ_WINDOW(folder, seconds)


def triangle_records(log):
    result = {}
    for stream, serial, draws, triangles, target in (
        tuple(map(int, fields)) for fields in LIFE["rows"](log, 5)
    ):
        if (stream, serial) in result or not stream or not serial or min(draws, triangles, target) < 0:
            raise ValueError(f"Invalid triangle record in {log}")
        result[stream, serial] = (draws, triangles, target)
    return result


def measure_policy(folder):
    triangles = triangle_records(folder / "triangle-split.tsv")
    window = json.loads((folder / "window.json").read_text())
    by_end, by_stream = LIFE["render_submissions"](folder / "submissions.ns", window["pid"])
    frames, _ = LIFE["read_frames"](folder, window, by_end, False)
    if len(frames) != window["verified_completed_renders"] or len(frames) < 3:
        raise ValueError("Full ordered render outputs were not recorded")
    stream = by_stream[frames[0].final.stream]
    serials = [row.serial for row in stream]
    intervals = LIFE["explain_intervals"](frames, False, stream)
    previous_triangles = None
    first_draw_counts = Counter()
    for row, before, after in zip(intervals, frames, frames[1:]):
        begin = bisect_right(serials, before.final.serial)
        end = bisect_right(serials, after.final.serial)
        submissions = stream[begin:end]
        values = [triangles[submission.stream, submission.serial] for submission in submissions]
        if any(record[0] != submission.draws for record, submission in zip(values, submissions)):
            raise ValueError("Triangle and submission logs disagree on draw counts")
        target = values[0][2]
        if any(each[2] != target for each in values) or (
            previous_triangles is not None and previous_triangles // 2 != target
        ):
            raise ValueError("Triangle budget does not match the preceding frame")
        total_triangles = sum(each[1] for each in values)
        if not total_triangles:
            raise ValueError("No measured triangle work in a complete raid output")
        previous_triangles = total_triangles
        draw_buffers = [(submission, record) for submission, record in zip(submissions, values) if submission.draws]
        first, first_record = draw_buffers[0]
        first_draw_counts[first.draws] += 1
        render_spans = [(submission.start, submission.end) for submission in submissions]
        covered = interval_coverage(render_spans)
        row.update(
            first_buffer_gpu_span_ns=first.end - first.start,
            summed_draw_bearing_submissions=len(draw_buffers),
            tracked_render_span_union_ns=covered(min(start for start, _ in render_spans), after.final.end),
            first_draws=first.draws, first_triangle_lists=first_record[1],
            first_triangle_fraction=first_record[1] / total_triangles,
            all_triangle_lists=total_triangles,
            previous_frame_half_triangles=target,
        )
    return {
        "first_draw_count_histogram": dict(sorted(first_draw_counts.items())),
        **LIFE["summarize"](intervals),
    }


def summarize(root):
    runtime = json.loads((HERE / "results/raid-aarchx-runtime-pgo-ab.json").read_text())
    artifacts = json.loads((root / "artifact-sha256.json").read_text())
    if (
        sha(root / "artifacts/candidate/ocerz") != artifacts["runtime_unsigned_sha256"] or
        sha(root / "artifacts/candidate/libwinemetalgl.dylib") != artifacts["renderer_sha256"] or
        artifacts["runtime_unsigned_sha256"] != runtime["runtime_artifact_sha256"]["candidate"]
    ):
        raise ValueError("Wrong renderer or accepted PGO runtime")
    replay = SPLIT["replay_pixels"](root, ("0", "1"))
    paths = [root / f"replay-{mode}-triangles.tsv" for mode in (0, 1)]
    replay_counts = [
        dict(Counter(draws for draws, _, _ in triangle_records(log).values() if draws))
        for log in paths
    ]
    if replay_counts[0].keys() == replay_counts[1].keys():
        raise ValueError("Ordered replay did not exercise a different split")
    diagnostics = {}
    for mode in (0, 1):
        folder = root / f"diagnostic-{mode}"
        diagnostics[str(mode)] = {
            "verified_gpu_window": verify_run(folder, mode, 1, 25, artifacts),
            **measure_policy(folder),
        }
    runs = [
        {"order": order, "balanced_triangles": bool(mode),
         **verify_run(root / f"timed-{order}-{mode}", mode, 0, 60, artifacts)}
        for order, mode in enumerate((0, 1, 1, 0), 1)
    ]
    comparisons = {}
    for mode, name in enumerate(("ordinary", "balanced_triangles")):
        rows = [row for row in runs if row["balanced_triangles"] == mode]
        comparisons[name] = {
            "median_gpu_completed_fps": median(row[FPS] for row in rows),
            "median_p99_completion_gap_ns": median(row[P99] for row in rows),
        }
    return {
        "artifact_sha256": artifacts,
        "gpu_tests_per_mode": 343,
        "indexed_replay": replay, "replay_draw_bearing_buffer_histograms": replay_counts,
        "diagnostics": diagnostics, "timed_runs": runs, "comparison": comparisons,
        "paired_balanced_minus_ordinary": [
            {"gpu_completed_fps": runs[test][FPS] - runs[control][FPS],
             "p99_completion_gap_ns": runs[test][P99] - runs[control][P99]}
            for control, test in ((0, 1), (3, 2))
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_root", type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize(args.capture_root), indent=2))
