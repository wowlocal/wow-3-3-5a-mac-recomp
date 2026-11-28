"""Attribute GPU stage activity to labeled render targets in the native 5K raid."""

import argparse
from collections import defaultdict
import json
from pathlib import Path
import re
from runpy import run_path
from typing import NamedTuple

from metal_submission_log import interval_coverage


HERE = Path(__file__).resolve().parent
VERIFY = run_path(str(HERE / "verify-raid-native-5k.py"))
METAL = run_path(str(HERE / "summarize-raid-deferred-tail.py"))
FRAMES = METAL["LIFE"]
SYSTEM = METAL["SYSTEM"]
PASS_LABEL = re.compile(r"(?:^|:)WMGL pass (\d+) (\d+x\d+) c\d+(?: d)?(?: clear)?(?= +\()")
STAGES = ("Fragment", "Vertex")


class Activity(NamedTuple):
    start: int
    end: int
    stage: str
    buffer: int
    encoder: int
    target: str
    pass_number: int


def read_labeled_activity(path, pid, legacy_pid):
    columns = {"start", "duration", "channel-name", "state", "process", "cmdbuffer-id",
               "encoder-id", "event-label"}
    buffers = defaultdict(list)
    activity = []
    for row in SYSTEM["exported_rows"](path, "metal-gpu-intervals", columns):
        if row["state"].text != "Active":
            continue
        process = row["process"].get("fmt", "")
        if process.endswith(f" ({legacy_pid})"):
            raise ValueError("An earlier trial had GPU activity during the capture")
        stage = row["channel-name"].text
        if not process.endswith(f" ({pid})") or stage not in STAGES:
            continue
        start = int(row["start"].text)
        end = start + int(row["duration"].text)
        if end <= start:
            continue
        buffer = int(row["cmdbuffer-id"].text or 0)
        encoder = int(row["encoder-id"].text or 0)
        label = PASS_LABEL.search(row["event-label"].get("fmt", ""))
        pass_number, target = (int(label[1]), label[2]) if label else (-1, "unknown")
        activity.append(Activity(start, end, stage, buffer, encoder, target, pass_number))
        if buffer:
            buffers[buffer].append((start, end, stage, encoder))
    return buffers, activity


def summarize(case, legacy_pid):
    run = VERIFY["verify"](case, case / "graphics-settings.lua")
    baseline = json.loads((HERE / "results/raid-native-5k-max-baseline.json").read_text())
    if (run["graphics"]["issues"] or run["graphics"]["actual"] != baseline["graphics"]["actual"]
            or run["artifact_sha256"] != baseline["artifact_sha256"]):
        raise ValueError("The encoder capture differs from the verified native 5K baseline")

    window = run["verified_gpu_window"]
    pid = window["pid"]
    witness = json.loads((case / "trace-witness.json").read_text())["metal"]
    if (witness["attach_pid"] != pid or witness["returncode"] or not witness["trace_exists"]
            or not witness["begin_uptime_ns"] < witness["end_uptime_ns"]):
        raise ValueError("The Metal capture lacks a valid trace witness")
    by_end, by_stream = FRAMES["render_submissions"](case / "submissions.ns", pid)
    frames, _ = FRAMES["read_frames"](case, window, by_end, False)
    submissions = by_stream[frames[0].final.stream]
    buffers, activity = read_labeled_activity(case / "gpu-intervals.xml", pid, legacy_pid)
    offset, tolerance, endpoints, alignment = SYSTEM["ALIGN_FRAGMENT_ENDS"](buffers, submissions)
    matches = METAL["buffer_matches"](frames, endpoints, offset, tolerance)
    selected = METAL["continuous_stage_frames"](frames, matches)
    if len(selected) < 2:
        raise ValueError("The capture has no continuous verified GPU window")

    bounds = [frame.final.end - offset for frame in selected]
    grouped = defaultdict(lambda: defaultdict(list))
    identities = defaultdict(lambda: defaultdict(set))
    native_passes = defaultdict(lambda: defaultdict(list))
    for start, end, stage, buffer, encoder, target, pass_number in activity:
        if end <= bounds[0] or start >= bounds[-1]:
            continue
        grouped[stage][target].append((start, end))
        if buffer and encoder:
            identities[stage][target].add((buffer, encoder))
        if target == "5120x2880":
            native_passes[stage][pass_number].append((start, end))

    def timings(spans):
        covered = interval_coverage(spans)
        return FRAMES["distribution"]([covered(start, end) for start, end in zip(bounds, bounds[1:])])

    stages = {}
    for stage in STAGES:
        targets = grouped[stage]
        if "5120x2880" not in targets:
            raise ValueError(f"Native 5K pass labels are missing in the {stage} stage")
        all_times = timings([span for spans in targets.values() for span in spans])
        tagged_times = timings([span for target, spans in targets.items() if target != "unknown" for span in spans])
        stages[stage] = {
            "all_client_active_ns": all_times,
            "known_target_union_active_ns": tagged_times,
            "known_target_share_of_union": tagged_times["mean"] / all_times["mean"],
            "targets": {
                target: {"active_ns": timings(spans), "encoder_identities": len(identities[stage][target])}
                for target, spans in sorted(targets.items())
            },
            "native_5k_pass_numbers": {
                number: timings(spans) for number, spans in sorted(native_passes[stage].items())
            },
        }
    return {
        "diagnostic_run": run,
        "metal_trace_witness": witness,
        "matched_output_count": len(selected),
        "matched_fragment_clock_alignment": alignment,
        "matched_completion_span_ns": bounds[-1] - bounds[0],
        "stages_in_matched_completion_gaps": stages,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    parser.add_argument("--legacy-pid", type=int, required=True)
    args = parser.parse_args()
    print(json.dumps(summarize(args.capture, args.legacy_pid), indent=2))
