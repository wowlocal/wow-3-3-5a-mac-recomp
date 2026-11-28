"""Compare the same native 5K arena with 24 and 12 active casters."""

import argparse
from bisect import bisect_left
from collections import Counter
import json
from pathlib import Path
from runpy import run_path
from statistics import median

from metal_submission_log import interval_coverage


HERE = Path(__file__).resolve().parent
VERIFIER = run_path(str(HERE / "verify-raid-native-5k.py"))
CPU_PROFILE = run_path(str(HERE / "summarize-raid-native-5k-profile.py"))["cpu_profile"]
SYSTEM = run_path(str(HERE / "summarize-raid-system-tail.py"))
LIFE = SYSTEM["LIFECYCLE"]
FIXTURE = VERIFIER["FIXTURE"]
TOLERANCE_NS = 250
FPS, P99 = "average_completed_fps", "p99_gpu_completion_gap_ns"


def read_capture(case, artifact_hashes, graphics, casters):
    run = VERIFIER["verify"](case, case / "graphics-settings.lua", casters=casters)
    if (run["graphics"]["issues"] or run["graphics"]["actual"] != graphics
            or run["artifact_sha256"] != artifact_hashes
            or (case / "color-only-msaa-mode.txt").read_text().split() != ["1", "0"]
            or (case / "pass-mode.txt").read_text().split() != ["2048", "0"]
            or (case / "render-end-mode.txt").read_text().split() != ["deferred", "0"]
            or (case / "submissions.ns").exists()):
        raise ValueError(f"{case.name} differs from the other native 5K runs")
    return run


def verify_spawns(root, before, diagnostic, restored):
    initial, reduced, final = [
        json.loads((case / "spawn-masks.json").read_text()) for case in (before, diagnostic, restored)
    ]
    fixture_ids = FIXTURE["IDS"]
    enabled = {fixture_ids[index] for index in FIXTURE["HALF_RAID"]}
    if (len(initial) != 25 or {row[1] for row in initial} != set(fixture_ids)
            or any(row[2:] != [0, 1, 1] for row in initial) or final != initial):
        raise ValueError("The complete fixture or its restored spawn masks differ")
    expected = [[guid, entry, 0, int(entry in enabled), 1] for guid, entry, *_ in initial]
    if reduced != expected:
        raise ValueError("The reduced raid changed rows outside the exact caster subset")
    evidence = json.loads((root / "caster-sweep-state/restoration.json").read_text())
    if (set(evidence) != {"spawns", "templates", "models", "scripts"}
            or any(row["before_sha256"] != row["restored_sha256"] for row in evidence.values())):
        raise ValueError("The original fixture rows were not restored")
    return enabled, evidence


def trace_witness(case, window):
    pid, stream, last_sequence = (window[name] for name in ("pid", "stream", "last_sequence"))
    frames = [(seq, tick, draws, dimensions) for process, source, seq, tick, draws, dimensions in (
        tuple(map(int, row)) for row in LIFE["rows"](case / "gpu.ns", 6)
    ) if (process, source) == (pid, stream) and seq >= last_sequence]
    witness = json.loads((case / "trace-witness.json").read_text())
    if not frames or frames[0][0] != last_sequence or set(witness) != {"cpu", "metal"}:
        raise ValueError("Missing trace or GPU measurement cutoff")
    width, height = window["window_size"]
    if any(draws <= 0 or dimensions != ((width << 32) | height) for _, _, draws, dimensions in frames):
        raise ValueError("The post-window GPU stream used a different render size")
    previous = frames[0][1]
    for label, seconds in (("cpu", 8), ("metal", 6)):
        record = witness[label]
        if (record["attach_pid"] != pid or record["returncode"] or not record["trace_exists"]
                or record["requested_seconds"] != seconds
                or not previous < record["begin_uptime_ns"] < record["end_uptime_ns"]):
            raise ValueError("Trace timing overlapped the FPS measurement or another trace")
        previous = record["end_uptime_ns"]
    return witness, frames[1:]


def match_output_clock(outputs, endpoints):
    # Repeated differences between the same GPU endpoints identify the offset.
    # Intermediate render passes need not end at an output completion.
    votes = Counter((tick - relative) // TOLERANCE_NS
                    for _, tick, _, _ in outputs[::max(1, len(outputs) // 32)]
                    for relative in endpoints)

    def matches(offset):
        matched = []
        for row in outputs:
            tick = row[1] - offset
            index = bisect_left(endpoints, tick)
            near = endpoints[max(0, index - 1):index + 1]
            if near:
                endpoint = min(near, key=lambda item: abs(item - tick))
                if abs(endpoint - tick) <= TOLERANCE_NS:
                    matched.append((row, endpoint))
        return matched

    offsets = [bucket * TOLERANCE_NS + TOLERANCE_NS // 2 for bucket, _ in votes.most_common(8)]
    if not offsets:
        raise ValueError("No recorded fragment endpoints align with the output stream")
    _, initial = max(((offset, matches(offset)) for offset in offsets), key=lambda pair: (
        len(pair[1]), -sum(abs(row[1] - end - pair[0]) for row, end in pair[1])))
    if len(initial) < 30:
        raise ValueError("Too few fragment endpoints match a native completion")
    offset = round(median(row[1] - endpoint for row, endpoint in initial))
    all_matches = matches(offset)
    longest, contiguous = [], []
    for row, end in all_matches:
        if contiguous and (row[0] != contiguous[-1][0] + 1 or end <= contiguous[-1][1]):
            contiguous = []
        contiguous.append((row[0], end))
        if len(contiguous) > len(longest):
            longest = contiguous
    if len(longest) < 30:
        raise ValueError("The native completion sequence is discontinuous in the GPU trace")
    evidence = {"matched_endpoints": len(all_matches), "consecutive_matches": len(longest),
                "maximum_alignment_error_ns": max(abs(row[1] - end - offset) for row, end in all_matches)}
    return [end for _, end in longest], evidence


def compare_gpu(case, window, witness, frames, legacy_pid):
    outputs = [row for row in frames if witness["begin_uptime_ns"] <= row[1] <= witness["end_uptime_ns"]]
    buffers, spans, counts = SYSTEM["gpu_activity"](case / "gpu-intervals.xml", window["pid"], legacy_pid)
    if counts.get("legacy"):
        raise ValueError("The earlier test was active in the paired GPU trace")
    endpoints = sorted({max(fragments) for rows in buffers.values()
                        if (fragments := [end for _, end, stage, _ in rows if stage == "Fragment"])})
    clock, evidence = match_output_clock(outputs, endpoints)
    metrics = {}
    for stage in ("client", "Vertex", "Fragment"):
        covered = interval_coverage(spans[stage])
        metrics[stage] = LIFE["distribution"]([covered(a, b) for a, b in zip(clock, clock[1:])])
    return {**evidence, "active_time_ns_per_completion_gap": metrics,
            "matched_completion_span_ns": clock[-1] - clock[0]}


def profile(case, window, legacy_pid):
    witness, frames = trace_witness(case, window)
    return {
        "trace_witness": witness,
        "cpu": CPU_PROFILE(case / "time-profile.xml", window["pid"]),
        "gpu": compare_gpu(case, window, witness["metal"], frames, legacy_pid),
    }


def summarize(root, legacy_pid):
    snapshot = json.loads((HERE / "results/raid-native-5k-max-baseline.json").read_text())
    renderer = json.loads((HERE / "results/raid-native-5k-color-only-msaa-ab.json").read_text())
    artifacts = dict(snapshot["artifact_sha256"], renderer_sha256=renderer["artifact_sha256"]["renderer_sha256"])
    before, diagnostic, restored = [root / f"native-5k-casters-{suffix}" for suffix in (
        "before-24", "diagnostic-12", "restored-24")]
    enabled, restoration = verify_spawns(root, before, diagnostic, restored)
    runs = [
        read_capture(case, artifacts, snapshot["graphics"]["actual"], casters)
        for case, casters in ((before, 24), (diagnostic, 12), (restored, 24))
    ]
    first, middle, final = [run["verified_gpu_window"] for run in runs]
    full = {metric: median((first[metric], final[metric])) for metric in (FPS, P99)}
    return {
        "enabled_entries_with_12_casters": sorted(enabled), "fixture_restoration": restoration,
        "native_5k_24_casters_before": runs[0], "native_5k_12_casters_diagnostic": runs[1],
        "native_5k_24_casters_restored": runs[2],
        "bracketing_24_caster_median": full,
        "12_caster_change_from_full_raid": {metric: middle[metric] - full[metric] for metric in (FPS, P99)},
        "post_window_profiles": {
            "24": profile(before, first, legacy_pid),
            "12": profile(diagnostic, middle, legacy_pid),
        },
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_root", type=Path)
    parser.add_argument("--legacy-pid", type=int, required=True)
    args = parser.parse_args()
    print(json.dumps(summarize(args.capture_root, args.legacy_pid), indent=2))
