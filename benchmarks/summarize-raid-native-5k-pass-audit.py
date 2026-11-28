"""Join native 5K draw and attachment state to Metal fragment intervals."""

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import re
from runpy import run_path
from statistics import median

from metal_submission_log import interval_coverage


HERE = Path(__file__).resolve().parent
VERIFY = run_path(str(HERE / "verify-raid-native-5k.py"))["verify"]
METAL = run_path(str(HERE / "summarize-raid-deferred-tail.py"))
FRAMES = METAL["LIFE"]
SYSTEM = METAL["SYSTEM"]
LABEL = re.compile(r"(?:^|:)WMGL pass audit (\d+)(?= +\()")
LOAD = ("discard", "load", "clear")
STORE = ("discard", "store", "resolve", "store-and-resolve")
END = ("other", "target", "blit", "compute", "visibility", "commit", "barrier", "clear")
FLAGS = ("depth_test", "depth_write", "stencil_test", "blend", "alpha_test", "sample_shading", "alpha_to_coverage")


def read_audit(path, pid):
    begin, end, draws = {}, {}, defaultdict(list)
    with path.open() as source:
        for line in source:
            if not line.endswith("\n"):
                break
            kind, label, *fields = line.split()
            key = int(label)
            row = tuple(map(int, fields))
            if kind == "P" and len(row) == 8 and row[0] == pid and key not in begin:
                begin[key] = row[1:]
            elif (kind == "D" and len(row) == 8 and key in begin and key not in end and
                    0 <= row[4] < 1 << len(FLAGS) and row[6] > 0 and row[7] >= 0):
                draws[key].append(row)
            elif kind == "E" and len(row) == 5 and key in begin and key not in end and row[1] >= 0:
                end[key] = row
            else:
                raise ValueError(f"Invalid pass audit row for encoder {label}")
    return begin, end, draws


def read_fragment_activity(path, pid, legacy_pid):
    columns = {"start", "duration", "channel-name", "state", "process", "cmdbuffer-id", "event-label"}
    buffers, labeled = defaultdict(list), defaultdict(list)
    for row in SYSTEM["exported_rows"](path, "metal-gpu-intervals", columns):
        if row["state"].text != "Active":
            continue
        process = row["process"].get("fmt", "")
        if process.endswith(f" ({legacy_pid})"):
            raise ValueError("The earlier test was active during the Metal trace")
        if not process.endswith(f" ({pid})") or row["channel-name"].text != "Fragment":
            continue
        start = int(row["start"].text)
        stop = start + int(row["duration"].text)
        if start >= stop:
            continue
        buffer = int(row["cmdbuffer-id"].text or 0)
        if buffer:
            buffers[buffer].append((start, stop, "Fragment", None))
        label = LABEL.search(row["event-label"].get("fmt", ""))
        if label:
            labeled[int(label[1])].append((start, stop, buffer))
    return buffers, labeled


def report_groups(groups):
    result = []
    for (load, store, cause, resolve), passes in sorted(groups.items()):
        flags = Counter()
        programs = Counter()
        references = 0
        total_draws = reported_draws = 0
        for _, _, count, work in passes:
            total_draws += count
            for program, fragment, instructions, texture_ops, mask, color, draw_count, refs in work:
                references += refs
                reported_draws += draw_count
                programs[program, fragment, instructions, texture_ops] += draw_count
                active = {name for i, name in enumerate(FLAGS) if mask & (1 << i)}
                flags.update({name: draw_count for name in active})
                if not color:
                    flags["color_mask_off"] += draw_count
                if {"depth_test", "alpha_test"} <= active and active.isdisjoint({"depth_write", "stencil_test"}):
                    flags["alpha_test_with_read_only_depth"] += draw_count
        result.append({
            "load_color_depth_stencil": load,
            "store_color_depth_stencil": store,
            "pass_end": cause,
            "color_resolve": resolve,
            "pass_count": len(passes),
            "fragment_active_ns": FRAMES["distribution"]([row[1] for row in passes]),
            "draws_per_pass": FRAMES["distribution"]([row[2] for row in passes]),
            "total_draws": total_draws,
            "attributed_direct_draws": reported_draws,
            "attributed_vertex_references": references,
            "direct_draws_by_active_state": {
                name: flags[name] for name in (*FLAGS, "color_mask_off", "alpha_test_with_read_only_depth")
            },
            "fragment_programs_by_draws": [
                {"linked_program": program, "arb_load_serial": serial, "arb_instructions": instructions,
                 "arb_texture_instructions": texture_ops, "draws": count}
                for (program, serial, instructions, texture_ops), count in programs.most_common(8)
            ],
        })
    return result


def match_outputs(frames, buffers, labeled, begin, end):
    final_by_serial = {frame.final.serial: frame for frame in frames}
    fragment_ends = {buffer: max(stop for _, stop, _, _ in spans) for buffer, spans in buffers.items()}
    candidates = {}
    for label, events in labeled.items():
        if label not in end or end[label][2] not in (2, 3):
            continue
        frame = final_by_serial.get(begin[label][0])
        if frame is None:
            continue
        stop = max(stop for _, stop, _ in events)
        buffer_ids = {buffer for _, _, buffer in events}
        if len(buffer_ids) != 1:
            raise ValueError(f"Audited pass {label} mapped to multiple Metal command buffers")
        buffer = buffer_ids.pop()
        if buffer and stop == fragment_ends[buffer]:
            if frame.sequence in candidates:
                raise ValueError(f"Multiple resolved Metal buffers matched output {frame.sequence}")
            candidates[frame.sequence] = (buffer, frame.final.end - stop)
    if len(candidates) < 24:
        raise ValueError("Too few traced output resolves are linked to final render serials")
    offset = round(median(value for _, value in candidates.values()))
    tolerance = 250
    matches = {sequence: buffer for sequence, (buffer, value) in candidates.items() if abs(value - offset) <= tolerance}
    if len(matches) < 24 or len(matches) * 2 < len(candidates):
        raise ValueError("Audited output resolves did not align to the submission log")
    evidence = {"matched_endpoints": len(matches), "maximum_error_ns": max(
        abs(value - offset) for sequence, (_, value) in candidates.items() if sequence in matches
    )}
    return offset, tolerance, matches, evidence


def summarize_case(case, budget, baseline, legacy_pid):
    run = VERIFY(case, case / "graphics-settings.lua", measurement_seconds=25)
    if (run["graphics"]["issues"] or run["graphics"]["actual"] != baseline["graphics"]["actual"]
            or (case / "pass-mode.txt").read_text().split() != [str(budget), "1"]
            or (case / "render-end-mode.txt").read_text().split() != ["deferred", "0"]
            or any(run["artifact_sha256"][key] != baseline["artifact_sha256"][key]
                   for key in ("runtime_unsigned_sha256", "runtime_signed_sha256"))):
        raise ValueError("The audited renderer used a different fixture, runtime or graphics profile")
    window = run["verified_gpu_window"]
    pid = window["pid"]
    witness = json.loads((case / "trace-witness.json").read_text())["metal"]
    if (witness["attach_pid"] != pid or witness["returncode"] or not witness["trace_exists"]
            or not witness["begin_uptime_ns"] < witness["end_uptime_ns"]):
        raise ValueError("Missing Metal trace witness for the audit")
    by_end, _ = FRAMES["render_submissions"](case / "submissions.ns", pid)
    frames, _ = FRAMES["read_frames"](case, window, by_end, False)
    if len({frame.final.stream for frame in frames}) != 1:
        raise ValueError("The fixture did not complete on one render stream")
    buffers, labeled = read_fragment_activity(case / "gpu-intervals.xml", pid, legacy_pid)
    begin, end, work = read_audit(case / "pass-audit.tsv", pid)
    offset, tolerance, matches, alignment = match_outputs(frames, buffers, labeled, begin, end)
    selected = METAL["continuous_stage_frames"](frames, matches)
    if len(selected) < 2:
        raise ValueError("The trace contains no continuous run of verified output")
    bounds = [frame.final.end - offset for frame in selected]
    final_buffers = {matches[frame.sequence]: frame.final.end - offset for frame in selected}
    groups = defaultdict(list)
    clipped = []
    for label, events in labeled.items():
        spans = [(a, b) for a, b, _ in events]
        start = min(a for a, _ in spans)
        stop = max(b for _, b in spans)
        if stop > bounds[0] and start < bounds[-1]:
            clipped.extend(spans)
        if start < bounds[0] or stop > bounds[-1]:
            continue
        serial, width, height, samples, *load = begin[label]
        reason, count, *store = end[label]
        if ((width, height, samples) != (5120, 2880, 4) or not serial or
                any(not 0 <= value < len(LOAD) for value in load) or
                any(not 0 <= value < len(STORE) for value in store) or not 0 <= reason < len(END) or
                sum(row[6] for row in work[label]) > count):
            raise ValueError(f"Invalid native 5K attachment or draw witnesses for the audited pass {label}")
        buffers_for_label = {buffer for _, _, buffer in events}
        if len(buffers_for_label) != 1:
            raise ValueError(f"Audited pass {label} mapped to multiple Metal command buffers")
        buffer = buffers_for_label.pop()
        resolve = "none"
        if store[0] in (2, 3):
            resolve = "output" if buffer in final_buffers and abs(stop - final_buffers[buffer]) <= tolerance else "intermediate"
        key = tuple(LOAD[action] for action in load), tuple(STORE[action] for action in store), END[reason], resolve
        groups[key].append((label, interval_coverage(spans)(start, stop), count, work[label]))
    if not groups:
        raise ValueError("No tagged render passes were matched to Metal fragment activity")
    coverage = interval_coverage(clipped)
    return {
        "verified_gpu_window": window,
        "artifact_sha256": run["artifact_sha256"],
        "trace_witness": witness,
        "matched_outputs": len(selected),
        "matched_fragment_clock_alignment": alignment,
        "native_target_fragment_active_per_completion_gap_ns": FRAMES["distribution"]([
            coverage(a, b) for a, b in zip(bounds, bounds[1:])
        ]),
        "passes_by_attachment_actions_and_end": report_groups(groups),
    }


def summarize(root, legacy_pid):
    baseline = json.loads((HERE / "results/raid-native-5k-max-baseline.json").read_text())
    captures = {
        budget: summarize_case(root / f"native-5k-audit-{budget}", budget, baseline, legacy_pid)
        for budget in (64, 2048)
    }
    if captures[64]["artifact_sha256"] != captures[2048]["artifact_sha256"]:
        raise ValueError("The two diagnostic launches used different binaries")
    validation = root / "pass-audit"
    expected = json.loads((HERE / "results/raid-native-5k-natural-pass-ab.json").read_text())["renderer_validation"]["64"]
    tests = (validation / "gpu-tests.log").read_text().splitlines()[-1]
    replay = (validation / "replay.log").read_text()
    expected_readback = f"RGBA {expected['readback_hash']}, colored {expected['draw_colored_pixels']}"
    if (tests != f"{expected['gpu_tests_passed']} tests, 0 failed checks"
            or "viewport 5120x2880, samples 4" not in replay or expected_readback not in replay):
        raise ValueError("The audit renderer changed the regression or native 5K replay result")
    return {"viewport": baseline["viewport"], "multisample_count": baseline["multisample_count"],
            "verified_settings": baseline["graphics"]["actual"],
            "audit_renderer_validation": {"gpu_tests_passed": expected["gpu_tests_passed"],
                                          "native_replay_hash": expected["readback_hash"]},
            "diagnostics_by_draw_budget": captures}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_root", type=Path)
    parser.add_argument("--legacy-pid", type=int, required=True)
    args = parser.parse_args()
    print(json.dumps(summarize(args.capture_root, args.legacy_pid), indent=2))
