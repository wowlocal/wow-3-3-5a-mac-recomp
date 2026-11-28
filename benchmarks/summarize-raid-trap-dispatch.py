"""Measure real FPCR crossings and compare diagnostic dispatch in the live raid."""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
from runpy import run_path


HERE = Path(__file__).resolve().parent
TIMING = run_path(str(HERE / "summarize-raid-renderer-pgo.py"))
CAPTURE = run_path(str(HERE / "summarize-metal-frame-capture.py"))
CROSS_BEGIN = re.compile(r"AFP_CROSS begin thread=(\d+) swaps=(\d+) uptime_ns=(\d+) callbacks=(\d+) "
                         r"leaf_overflow=(\d+) other_traps=(\d+)")
CROSS_END = re.compile(r"AFP_CROSS end thread=(\d+) swaps=(\d+)")
TRAP = re.compile(r"AFP_CROSS trap (runtime|special|generic) (\S+) (\d+)")
LEAF = re.compile(r"AFP_CROSS leaf (\S+) (\d+)")
REPORT_SWAPS = 256


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_reports(path):
    reports = []
    report = None
    for line in path.read_text().splitlines():
        if match := CROSS_BEGIN.fullmatch(line):
            if report is not None:
                raise ValueError("Overlapping crossing reports")
            thread, swaps, timestamp, callbacks, overflow, other = map(int, match.groups())
            if overflow or other:
                raise ValueError("Unclassified leaf or trap transitions")
            report = {"thread": thread, "swaps": swaps, "timestamp": timestamp,
                      "callbacks": callbacks, "traps": Counter(), "leaves": Counter()}
        elif match := TRAP.fullmatch(line):
            if report is None or not int(match[3]):
                raise ValueError("Trap outside a complete crossing report")
            report["traps"][(match[1], match[2])] += int(match[3])
        elif match := LEAF.fullmatch(line):
            if report is None or not int(match[2]):
                raise ValueError("Leaf outside a complete crossing report")
            report["leaves"][match[1]] += int(match[2])
        elif match := CROSS_END.fullmatch(line):
            if report is None or (report["thread"], report["swaps"]) != tuple(map(int, match.groups())):
                raise ValueError("Crossing report identity changed")
            reports.append(report)
            report = None
    return reports  # The process may stop while writing the last report.


def read_crossing(capture, artifacts, transition_delta):
    if json.loads((capture / "artifact-sha256.json").read_text()) != artifacts:
        raise ValueError("AFP crossing runtime changed between captures")
    if (capture / "afp-mode.txt").read_text().split() != ["1", "1"]:
        raise ValueError("AFP and crossing instrumentation must be enabled")
    window = TIMING["verify"](capture, "candidate", seconds=35)
    original_window = json.loads((capture / "window.json").read_text())
    width, height = window["window_size"]
    completions = CAPTURE["read_completions"](capture / "gpu.ns", original_window["pid"],
                                              original_window["stream"], width << 32 | height)
    start = completions[original_window["first_sequence"]][0]
    end = completions[original_window["last_sequence"]][0]
    reports = read_reports(capture / "client.log")
    if len({row["thread"] for row in reports}) != 1:
        raise ValueError("Expected exactly one AGL swap thread")
    if any(right["swaps"] - left["swaps"] != REPORT_SWAPS or right["timestamp"] <= left["timestamp"]
           for left, right in zip(reports, reports[1:])):
        raise ValueError("Nonconsecutive crossing report boundaries")
    complete = [right for left, right in zip(reports, reports[1:])
                if start <= left["timestamp"] < right["timestamp"] <= end]
    if len(complete) < 3:
        raise ValueError("Expected several complete crossing intervals after the fixture warmed")
    swaps = len(complete) * REPORT_SWAPS
    traps = sum((row["traps"] for row in complete), Counter())
    leaves = sum((row["leaves"] for row in complete), Counter())
    callbacks = sum(row["callbacks"] for row in complete)
    leaf_count = sum(leaves.values())
    crossings = sum(traps.values()) + leaf_count + callbacks
    gl = sum(count for (_, name), count in traps.items() if name.startswith("_gl")) + sum(
        count for name, count in leaves.items() if name.startswith("_gl")
    )
    return {
        "gpu_window": window, "complete_report_intervals": len(complete), "counted_swaps": swaps,
        "measured_pairs": crossings, "measured_pairs_per_swap": crossings / swaps,
        "estimated_transition_cost_ns_per_swap": crossings / swaps * transition_delta,
        "opengl_symbol_pairs_per_swap": gl / swaps,
        "leaf_pairs_per_swap": leaf_count / swaps,
        "callback_pairs_per_swap": callbacks / swaps,
        "trap_pairs_per_swap": {kind: sum(count for (role, _), count in traps.items() if role == kind) / swaps
                                for kind in ("generic", "special", "runtime")},
        "top_trap_pairs_per_swap": [{"kind": kind, "name": name, "pairs_per_swap": count / swaps}
                                    for (kind, name), count in traps.most_common(20)],
        "top_leaf_pairs_per_swap": [{"name": name, "pairs_per_swap": count / swaps}
                                    for name, count in leaves.most_common(20)],
    }


def read_dispatch(root, renderer_hash):
    artifacts = json.loads((root / "artifact-sha256.json").read_text())
    if artifacts["renderer_sha256"] != renderer_hash:
        raise ValueError("Trap dispatch experiment changed the renderer")
    checks = {}
    for mode in (0, 1):
        focused = (root / f"focused-{mode}.log").read_text()
        general = (root / f"general-{mode}.log").read_text()
        focused_count = re.search(r"test_m32 trap-diagnostics: (\d+) checks, 0 failed", focused)
        general_count = re.search(r"test_m32: (\d+) checks, 0 failed", general)
        if not focused_count or not general_count:
            raise ValueError("Diagnostic dispatch tests did not pass")
        checks[str(mode)] = {"focused": int(focused_count[1]), "general": int(general_count[1])}
    runs = []
    for order, mode in enumerate((0, 1, 1, 0), 1):
        capture = root / f"timed-{order}-{mode}"
        if json.loads((capture / "artifact-sha256.json").read_text()) != artifacts:
            raise ValueError("Runtime or renderer changed during dispatch timing")
        if (capture / "trap-dispatch-mode.txt").read_text().strip() != str(mode):
            raise ValueError("Wrong diagnostic dispatch setting")
        window = TIMING["verify"](capture, "candidate")
        if "AFP_CROSS " in (capture / "client.log").read_text():
            raise ValueError("Crossing instrumentation was active during timing")
        runs.append({"order": order, "mode": "candidate" if mode else "control", **window})
    return {"artifacts": artifacts, "tests": checks, **TIMING["summarize_timing"](runs)}


def summarize(root, renderer):
    prior = json.loads((HERE / "results" / "raid-aarchx-i386-afp-ab.json").read_text())
    artifacts = json.loads((root / "artifact-sha256.json").read_text())
    renderer_hash = sha256(renderer)
    if (renderer_hash != prior["artifacts"]["renderer_sha256"] or renderer_hash != artifacts["renderer_sha256"] or
            sha256(root / "artifacts" / "candidate" / "ocerz") != artifacts["runtime_unsigned_sha256"]):
        raise ValueError("Unexpected instrumentation runtime or renderer")
    delta = prior["isolated_host_transition_pair"]["afp_minus_control_ns"]
    comparison = prior["comparison"]
    added_time = 1e9 / comparison["candidate"]["median_gpu_completed_fps"] - 1e9 / comparison["control"]["median_gpu_completed_fps"]
    settings = ("hardware", "macos", "viewport", "vsync", "caster_count", "boss_count", "camera")
    return {
        **{key: prior[key] for key in settings}, "base_commit": "3cc8c28",
        "crossing_artifacts": artifacts, "prior_transition_pair_delta_ns": delta,
        "prior_afp_average_frame_time_increase_ns": added_time,
        "crossing_captures": [read_crossing(root / f"crossing-{index}", artifacts, delta) for index in (1, 2)],
        "dispatch_experiment": read_dispatch(root / "dispatch", renderer_hash),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_root", type=Path)
    parser.add_argument("renderer", type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize(args.capture_root, args.renderer), indent=2))


if __name__ == "__main__":
    main()
