"""Compare fixed raid replays with and without a bounded Metal workload."""

import argparse
from collections import defaultdict
import json
from pathlib import Path
import re
from runpy import run_path
from statistics import median

from metal_submission_log import interval_coverage


HERE = Path(__file__).resolve().parent
SYSTEM = run_path(str(HERE / "summarize-raid-system-tail.py"))
BURST = run_path(str(HERE / "summarize-raid-metal-burst.py"))
ORDER = ("control", "loaded", "loaded", "control", "control", "loaded", "loaded", "control")
STAGE_NAMES = {"Vertex", "Fragment"}
MARKER = re.compile(r"^Raid contention marker (\d+):")
PULSE = re.compile(r"^Raid contention (\d+) buffer (\d+):")


def collect_intervals(path, replayer_pid, load_pid, legacy_pid):
    columns = {"state", "start", "duration", "process", "cmdbuffer-id", "channel-name", "event-label"}
    render_buffers = defaultdict(list)
    markers = defaultdict(list)
    pulses = defaultdict(lambda: defaultdict(list))
    other_spans = defaultdict(list)
    processes = {f" ({pid})": kind for pid, kind in
                 ((replayer_pid, "replay"), (load_pid, "load"), (legacy_pid, "legacy"))}
    for row in SYSTEM["exported_rows"](path, "metal-gpu-intervals", columns):
        if row["state"].get("fmt") != "Active":
            continue
        start = int(row["start"].text)
        end = start + int(row["duration"].text)
        if end <= start:
            continue
        label = row["process"].get("fmt", "")
        kind = next((name for suffix, name in processes.items() if label.endswith(suffix)), None)
        if kind == "replay":
            if row["cmdbuffer-id"].text:
                render_buffers[int(row["cmdbuffer-id"].text)].append((start, end, row["channel-name"].text))
        elif kind == "load":
            event_label = row["event-label"].get("fmt", "")
            if match := MARKER.match(event_label):
                markers[int(match[1])].append((start, end))
            elif match := PULSE.match(event_label):
                pulses[int(match[1])][int(match[2])].append((start, end))
        elif kind == "legacy" or label.startswith("WindowServer ("):
            other_spans["legacy" if kind else "desktop"].append((start, end))
    if not render_buffers or not markers or not pulses:
        raise ValueError("Missing replay or test queue hardware intervals")
    return render_buffers, markers, pulses, other_spans


def marker_witness(samples, traced):
    offsets = []
    for index, sample in enumerate(samples):
        for expected, edge in zip((index * 2, index * 2 + 1), ("begin", "end")):
            marker = sample[edge]
            if marker["index"] != expected or not traced[expected]:
                raise ValueError(f"Missing Metal marker {expected}")
            offsets.append(marker["gpu_end_ns"] - max(last for _, last in traced[expected]))
    return {"matched_markers": len(offsets), "end_timestamp_offset_range_ns": max(offsets) - min(offsets)}


def overlap_ns(spans, other):
    first = min(a for a, _ in spans)
    last = max(b for _, b in spans)
    clipped = [(max(a, first), min(b, last)) for a, b in other if a < last and b > first]
    union = BURST["union_ns"]
    return union(spans) + union(clipped) - union(spans + clipped)


def read_sample(index, sample, buffers, markers, pulses, other_spans, png, archived):
    if BURST["png_digest"](png) != archived["full_gpu_replay"]["replayed_image_sha256"][-1]:
        raise ValueError("Final frame differs from the archived replay")
    first = max(b for _, b in markers[index * 2])
    last = min(a for a, _ in markers[index * 2 + 1])
    selected = []
    for windows in buffers.values():
        stages = {stage for _, _, stage in windows}
        if stages != STAGE_NAMES:
            continue
        start, end = min(a for a, _, _ in windows), max(b for _, b, _ in windows)
        if first <= start and end <= last:
            selected.append((start, windows))
        elif first < end and start < last:
            raise ValueError("A replay render buffer crossed a measurement boundary")
    selected.sort()
    frames = archived["live_burst"]["frames"]
    if len(selected) != 2 * len(frames):
        raise ValueError(f"Incomplete raid replay in {sample['name']}: {len(selected)} render buffers")
    if set(pulses) != set(range(len(sample["load_buffers"]))):
        raise ValueError("Metal did not trace every completed test queue buffer")
    load = [span for windows in pulses.values() for span in windows]
    load_during_request = interval_coverage(load)(first, last)
    if (sample["mode"] == "loaded") != bool(load_during_request):
        raise ValueError("Recorded test queue work differs from the requested condition")
    results = []
    union = BURST["union_ns"]
    for index, frame in enumerate(frames):
        windows = selected[index * 2][1] + selected[index * 2 + 1][1]
        spans = [(a, b) for a, b, _ in windows]
        start, end = min(a for a, _ in spans), max(b for _, b in spans)
        if overlap_ns(spans, other_spans["legacy"]):
            raise ValueError("Earlier client GPU work overlapped a replay frame")
        results.append({
            "draws": frame["draws"],
            "vertex_active_ns": union([(a, b) for a, b, stage in windows if stage == "Vertex"]),
            "fragment_active_ns": union([(a, b) for a, b, stage in windows if stage == "Fragment"]),
            "render_active_ns": union(spans), "elapsed_render_span_ns": end - start,
            "independent_queue_overlap_ns": overlap_ns(spans, load),
            "windowserver_overlap_ns": overlap_ns(spans, other_spans["desktop"]),
        })
    burst_span = max(end for _, windows in selected for _, end, _ in windows) - selected[0][0]
    return {"mode": sample["mode"], "fetch_wall_ns": sample["fetch_wall_ns"],
            "load_buffers": len(pulses), "independent_queue_active_during_request_ns": load_during_request,
            "elapsed_render_burst_ns": burst_span, "frames": results}


def summarize(root, legacy_pid):
    archived = json.loads((HERE / "results" / "raid-metal-burst-replay.json").read_text())
    witness = json.loads((root / "witness.json").read_text())
    samples = witness["samples"]
    if tuple(sample["mode"] for sample in samples) != ORDER or witness["legacy_outputs_during_experiment"]:
        raise ValueError("Expected interleaved load and control with the earlier client idle")
    digest = BURST["sha256"](Path(witness["archive"]) / "capture")
    if digest != archived["live_burst"]["archive_command_stream_sha256"]:
        raise ValueError("The replay command stream differs from the verified raid archive")
    buffers, markers, pulses, other = collect_intervals(root / "gpu.xml", witness["replayer_pid"],
                                                       witness["load_pid"], legacy_pid)
    clock = marker_witness(samples, markers)
    runs = [read_sample(index, sample, buffers, markers, pulses[index], other,
                        root / f"{sample['name']}.png", archived) for index, sample in enumerate(samples)]
    measurements = ("render_active_ns", "elapsed_render_span_ns", "windowserver_overlap_ns",
                    "independent_queue_overlap_ns")
    comparison = {}
    for mode in ("control", "loaded"):
        arm = [run for run in runs if run["mode"] == mode]
        frames = [frame for run in arm for frame in run["frames"]]
        comparison[mode] = {
            "frames": len(frames), "above_2ms_render_active": sum(row["render_active_ns"] > 2_000_000 for row in frames),
            "median_elapsed_render_burst_ns": median(run["elapsed_render_burst_ns"] for run in arm),
            "median_independent_queue_active_during_request_ns": median(
                run["independent_queue_active_during_request_ns"] for run in arm),
            **{f"median_{key}": median(row[key] for row in frames) for key in measurements},
        }
    pairs = [(0, 1), (3, 2), (4, 5), (7, 6)]
    paired_deltas = [
        [b["render_active_ns"] - a["render_active_ns"] for a, b in zip(runs[left]["frames"], runs[right]["frames"])]
        for left, right in pairs
    ]
    load_times = [end - start for sample in samples for start, end in sample["load_buffers"]]
    return {"base_commit": "694e96e", "hardware": archived["hardware"], "viewport": archived["viewport"],
            "archive_command_stream_sha256": digest,
            "replayed_final_image_sha256": archived["full_gpu_replay"]["replayed_image_sha256"][-1],
            "load_draws_per_buffer": witness.get("load_draws_per_buffer", 8),
            "load_median_gpu_command_buffer_ns": median(load_times),
            "gpu_marker_witness": clock, "comparison": comparison, "paired_render_active_delta_ns": paired_deltas,
            "runs": runs}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_root", nargs="+", type=Path)
    parser.add_argument("--legacy-pid", type=int, required=True)
    args = parser.parse_args()
    results = [summarize(root, args.legacy_pid) for root in args.capture_root]
    print(json.dumps(results[0] if len(results) == 1 else results, indent=2))


if __name__ == "__main__":
    main()
