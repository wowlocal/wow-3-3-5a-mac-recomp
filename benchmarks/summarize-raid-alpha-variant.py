"""Verify compatibility alpha specialization and compare full-raid timing."""

import argparse
import hashlib
import json
from pathlib import Path
import re
from runpy import run_path
from statistics import median


HERE = Path(__file__).resolve().parent
READ_WINDOW = run_path(str(HERE / "summarize-raid-shadow-reuse.py"))["read_window"]
LIFE = run_path(str(HERE / "summarize-raid-frame-tail.py"))
FPS = "average_completed_fps"
P99 = "p99_gpu_completion_gap_ns"
COMPILED = "wmgl compatibility alpha variant compiled"
SPECIALIZATION_FAILURES = (
    "Could not disable the inactive compatibility alpha check",
    "Could not create the compatibility alpha function",
)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_run(folder, mode, logging, seconds, artifacts):
    settings = {
        "alpha-variant-mode.txt": [str(mode), str(logging)],
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
            } or any((folder / file).is_file() != bool(logging)
                      for file in ("alpha-variant.tsv", "submissions.ns"))):
        raise ValueError(f"Wrong runtime, renderer, tracing or isolation in {folder}")
    client_log = (folder / "client.log").read_text()
    if any(message in client_log for message in SPECIALIZATION_FAILURES):
        raise ValueError(f"A compatibility shader failed to specialize in {folder}")
    return READ_WINDOW(folder, seconds)


def verify_gpu_tests(root):
    results = {}
    for mode in (0, 1):
        focused = (root / f"focus-{mode}.log").read_text()
        if not focused.endswith("1 tests, 0 failed checks\n") or (COMPILED in focused) != bool(mode):
            raise ValueError(f"The color/depth comparison missed mode {mode}")
        if not (root / f"gpu-tests-{mode}.log").read_text().endswith("344 tests, 0 failed checks\n"):
            raise ValueError(f"GPU suite failed for mode {mode}")
        row = re.search(r"^outputs (\d+), RGBA ([0-9a-f]+), colored (\d+)$",
                        (root / f"replay-{mode}.log").read_text(), re.M)
        if not row or int(row[1]) < 3 or int(row[3]) < 10000:
            raise ValueError(f"Missing ordered replay image for mode {mode}")
        results[str(mode)] = {"outputs": int(row[1]), "rgba_hash": row[2], "colored_pixels": int(row[3])}
    if any(results["0"][key] != results["1"][key] for key in ("rgba_hash", "colored_pixels")):
        raise ValueError("Specialization changed the ordered replay framebuffer")
    return results


def diagnostics(folder, mode):
    window = json.loads((folder / "window.json").read_text())
    by_end, by_stream = LIFE["render_submissions"](folder / "submissions.ns", window["pid"])
    frames, _ = LIFE["read_frames"](folder, window, by_end, False)
    if len(frames) != window["verified_completed_renders"] or len(frames) < 3:
        raise ValueError("Missing joined full raid outputs")
    first, last = frames[0].final.end, frames[-1].final.end
    records = [tuple(map(int, row)) for row in LIFE["rows"](folder / "alpha-variant.tsv", 5)]
    if any(not (0 <= selected <= eligible <= wrapped <= calls) for _, calls, wrapped, eligible, selected in records):
        raise ValueError("Alpha preparation counters are inconsistent")
    if any(a[0] >= b[0] or b[1] - a[1] != 4096 or any(before > after for before, after in zip(a[2:], b[2:]))
           for a, b in zip(records, records[1:])):
        raise ValueError("The preparation trace is not one monotonic counter stream")
    selected = [record for record in records if first <= record[0] <= last]
    if len(selected) < 2:
        raise ValueError("Alpha counters do not cover the verified raid window")
    _, prepared, wrapped, eligible, specialized = (b - a for a, b in zip(selected[0], selected[-1]))
    compiled = (folder / "client.log").read_text().count(COMPILED)
    if not 0 < eligible <= wrapped <= prepared or specialized != eligible * mode or (compiled > 0) != bool(mode):
        raise ValueError("The shader specialization did not take the expected path")
    intervals = LIFE["explain_intervals"](frames, False, by_stream[frames[0].final.stream])
    times = LIFE["summarize"](intervals)
    return {
        "prepared_draws": prepared,
        "legacy_fragment_wrapper": wrapped,
        "inactive_alpha_eligible": eligible,
        "variant_selected": specialized,
        "fraction_of_all_preparations_eligible": eligible / prepared,
        "fraction_of_wrapped_preparations_eligible": eligible / wrapped,
        "compiled_variants_in_process": compiled,
        "counter_samples_within_gpu_window": len(selected),
        "joined_completion_gaps": times["completion_intervals"],
        "final_gpu_span_ns": times["metrics"]["final_gpu_span_ns"],
        "tail_median_final_gpu_span_ns": times["tail_medians"]["final_gpu_span_ns"],
        "producer_submit_interval_ns": times["metrics"]["final_submit_interval_ns"],
    }


def summarize(root):
    reference = json.loads((HERE / "results/raid-aarchx-runtime-pgo-ab.json").read_text())
    artifacts = json.loads((root / "artifact-sha256.json").read_text())
    if (sha(root / "artifacts/candidate/ocerz") != artifacts["runtime_unsigned_sha256"] or
            sha(root / "artifacts/candidate/libwinemetalgl.dylib") != artifacts["renderer_sha256"] or
            artifacts["runtime_unsigned_sha256"] != reference["runtime_artifact_sha256"]["candidate"]):
        raise ValueError("Wrong tested binaries or accepted runtime")
    replay = verify_gpu_tests(root)
    profiles = {}
    for mode in (0, 1):
        folder = root / f"diagnostic-{mode}"
        profiles[str(mode)] = {
            "verified_gpu_window": verify_run(folder, mode, 1, 25, artifacts),
            **diagnostics(folder, mode),
        }
    timed = [
        {"order": order, "specialized_inactive_alpha": bool(mode),
         **verify_run(root / f"timed-{order}-{mode}", mode, 0, 60, artifacts)}
        for order, mode in enumerate((0, 1, 1, 0), 1)
    ]
    compared = {}
    for mode, name in enumerate(("dynamic_alpha", "specialized_inactive_alpha")):
        arms = [run for run in timed if run["specialized_inactive_alpha"] == mode]
        compared[name] = {
            "median_gpu_completed_fps": median(arm[FPS] for arm in arms),
            "median_p99_completion_gap_ns": median(arm[P99] for arm in arms),
        }
    return {
        "artifact_sha256": artifacts,
        "focused_color_depth_and_user_discard_checks": "passed in both modes",
        "gpu_tests_per_mode": 344,
        "indexed_replay": replay,
        "diagnostics": profiles,
        "timed_runs": timed,
        "comparison": compared,
        "paired_specialized_minus_dynamic": [
            {"gpu_completed_fps": timed[t][FPS] - timed[c][FPS], "p99_completion_gap_ns": timed[t][P99] - timed[c][P99]}
            for c, t in ((0, 1), (3, 2))
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_root", type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize(args.capture_root), indent=2))
