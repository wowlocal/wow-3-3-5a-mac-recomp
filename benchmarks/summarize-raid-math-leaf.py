"""Summarize state-aware libm reuse and direct i386 math leaves in the raid."""

import argparse
import hashlib
import json
from pathlib import Path
import re
from runpy import run_path


HERE = Path(__file__).resolve().parent
TIMING = run_path(str(HERE / "summarize-raid-renderer-pgo.py"))
CAPTURE = run_path(str(HERE / "summarize-metal-frame-capture.py"))
FUNCTIONS = ("sinf", "cosf", "floorf", "atan2f")
FIELDS = ("calls", "eligible", "consecutive_reuse", "cache_hits", "state_differences", "host_samples", "host_ns")
BEGIN = re.compile(r"MATH_PROFILE begin thread=(\d+) swaps=(\d+) uptime_ns=(\d+)")
END = re.compile(r"MATH_PROFILE end thread=(\d+) swaps=(\d+)")
LEAF = re.compile(r"^ocerz: i386 math leaf _(\w+)$", re.M)
REPORT_SWAPS = 256


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_reports(path):
    reports = []
    entry = None
    for line in path.read_text().splitlines():
        if start := BEGIN.fullmatch(line):
            if entry:
                raise ValueError("Overlapping host math reports")
            thread, swaps, timestamp = map(int, start.groups())
            entry = {"thread": thread, "swaps": swaps, "time": timestamp, "calls": {}}
        elif end := END.fullmatch(line):
            if not entry or (entry["thread"], entry["swaps"]) != tuple(map(int, end.groups())):
                raise ValueError("Host math report ended at a different boundary")
            if set(entry["calls"]) != set(FUNCTIONS):
                raise ValueError("Host math report is missing an operation")
            reports.append(entry)
            entry = None
        elif line.startswith("MATH_PROFILE "):
            _, name, *values = line.split()
            if not entry or name not in FUNCTIONS or name in entry["calls"] or len(values) != len(FIELDS):
                raise ValueError("Unexpected host math row")
            entry["calls"][name] = dict(zip(FIELDS, map(int, values)))
    return reports


def verify_artifacts(capture, artifacts):
    if json.loads((capture / "artifact-sha256.json").read_text()) != artifacts:
        raise ValueError(f"Different runtime or renderer used in {capture}")


def read_profile(capture, artifacts):
    verify_artifacts(capture, artifacts)
    if (capture / "math-mode.txt").read_text().split() != ["0", "1"]:
        raise ValueError("Expected observe-only math cache profiling")
    window = TIMING["verify"](capture, "candidate", seconds=25)
    saved = json.loads((capture / "window.json").read_text())
    width, height = saved["window_size"]
    completions = CAPTURE["read_completions"](capture / "gpu.ns", saved["pid"], saved["stream"], width << 32 | height)
    start = completions[saved["first_sequence"]][0]
    end = completions[saved["last_sequence"]][0]
    reports = read_reports(capture / "client.log")
    if len({row["thread"] for row in reports}) != 1 or any(
        b["swaps"] - a["swaps"] != REPORT_SWAPS or b["time"] <= a["time"] for a, b in zip(reports, reports[1:])
    ):
        raise ValueError("Expected one consecutive swap thread in the profile")
    complete = [b for a, b in zip(reports, reports[1:]) if start <= a["time"] < b["time"] <= end]
    if len(complete) < 3:
        raise ValueError("Too few complete math intervals inside the verified GPU window")
    swaps = len(complete) * REPORT_SWAPS
    rows = {}
    for name in FUNCTIONS:
        total = {field: sum(row["calls"][name][field] for row in complete) for field in FIELDS}
        if (not total["calls"] or not total["host_samples"] or total["state_differences"] or
                total["cache_hits"] > total["eligible"] or total["eligible"] > total["calls"]):
            raise ValueError(f"Invalid cache or state comparison for {name}")
        rows[name] = {**total, "calls_per_swap": total["calls"] / swaps,
                      "cache_hit_share": total["cache_hits"] / total["calls"],
                      "consecutive_reuse_share": total["consecutive_reuse"] / total["calls"],
                      "sampled_host_ns_per_call": total["host_ns"] / total["host_samples"]}
    return {"verified_gpu_window": window, "complete_report_intervals": len(complete),
            "counted_swaps": swaps, "operations": rows,
            "estimated_host_ns_per_swap_at_cache_hits": sum(
                row["cache_hits"] * row["sampled_host_ns_per_call"] / swaps for row in rows.values())}


def read_tests(root, name, modes):
    counts = {}
    for mode in modes:
        prefix = "stack-focused" if name == "math-leaf" else "focused"
        focused = (root / f"{prefix}-{mode}.log").read_text()
        general = (root / f"general-{mode}.log").read_text()
        specific = re.search(rf"test_m32 {name}: (\d+) checks, 0 failed", focused)
        standard = re.search(r"test_m32: (\d+) checks, 0 failed", general)
        if not specific or not standard:
            raise ValueError(f"Missing {name} validation in mode {mode}")
        if name == "math-leaf" and set(LEAF.findall(focused)) != (set(FUNCTIONS) if mode else set()):
            raise ValueError("The focused test must compile all four direct math leaves in the candidate")
        counts[str(mode)] = {"focused": int(specific[1]), "general": int(standard[1])}
    return counts


def read_direct(root, renderer_hash):
    artifacts = json.loads((root / "artifact-sha256.json").read_text())
    if (artifacts["renderer_sha256"] != renderer_hash or
            artifacts["runtime_unsigned_sha256"] != sha256(root / "artifacts" / "candidate" / "ocerz")):
        raise ValueError("Wrong direct math binary or renderer")
    diagnostic = root / "diagnostic"
    verify_artifacts(diagnostic, artifacts)
    if (diagnostic / "math-mode.txt").read_text().split() != ["1", "1"]:
        raise ValueError("Wrong direct math diagnostic mode")
    if set(LEAF.findall((diagnostic / "client.log").read_text())) != set(FUNCTIONS):
        raise ValueError("The live client did not compile the four direct math leaves")
    window = TIMING["verify"](diagnostic, "candidate", seconds=25)
    timed = []
    for order, mode in enumerate((0, 1, 1, 0), 1):
        capture = root / f"timed-{order}-{mode}"
        verify_artifacts(capture, artifacts)
        if (capture / "math-mode.txt").read_text().split() != [str(mode), "0"]:
            raise ValueError("Wrong direct math timing settings")
        log = (capture / "client.log").read_text()
        if LEAF.search(log) or "MATH_PROFILE " in log:
            raise ValueError("Unexpected instrumentation during timing")
        timed.append({"order": order, "mode": "candidate" if mode else "control",
                      **TIMING["verify"](capture, "candidate")})
    return {"artifacts": artifacts, "compiled_live_leaves": list(FUNCTIONS), "verified_diagnostic": window,
            "tests": read_tests(root, "math-leaf", (0, 1)), **TIMING["summarize_timing"](timed)}


def summarize(root, renderer):
    prior = json.loads((HERE / "results" / "raid-aarchx-trap-dispatch-ab.json").read_text())
    artifacts = json.loads((root / "artifact-sha256.json").read_text())
    renderer_hash = sha256(renderer)
    if (renderer_hash != prior["crossing_artifacts"]["renderer_sha256"] or
            artifacts["renderer_sha256"] != renderer_hash or
            artifacts["runtime_unsigned_sha256"] != sha256(root / "artifacts" / "candidate" / "ocerz")):
        raise ValueError("Wrong math profile runtime or renderer")
    fields = ("hardware", "macos", "viewport", "vsync", "caster_count", "boss_count", "camera")
    return {
        **{key: prior[key] for key in fields}, "base_commit": "144aa9d",
        "math_profile_artifacts": artifacts,
        "math_profile": [read_profile(root / f"profile-{index}", artifacts) for index in (1, 2)],
        "math_cache_tests": read_tests(root, "math-cache", (0, 15)),
        "direct_math_experiment": read_direct(root / "direct", renderer_hash),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_root", type=Path)
    parser.add_argument("renderer", type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize(args.capture_root, args.renderer), indent=2))


if __name__ == "__main__":
    main()
