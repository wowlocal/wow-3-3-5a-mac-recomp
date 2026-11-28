"""Check original and native matrix replays and the live full-raid A/B."""

import argparse
import json
from pathlib import Path
import re
from runpy import run_path
from statistics import median


HERE = Path(__file__).resolve().parent
TIMING = run_path(str(HERE / "summarize-raid-renderer-pgo.py"))
REPLAY = run_path(str(HERE / "export-raid-matrix-replay.py"))


def replay(root, export_name, replay_file, sanitizer_file, cases_name):
    capture = json.loads((root / export_name).read_text())
    count = sum(gate["captured_calls"] for run in capture["captures"] for gate in run["gates"])
    if REPLAY["digest"](root / cases_name) != capture["case_file_sha256"]:
        raise ValueError("Changed matrix replay inputs")
    lines = (root / replay_file).read_text().splitlines()
    tag, verified, declined, aliases = lines[0].split()
    if tag != "verified" or int(verified) + int(declined) != count or not int(verified):
        raise ValueError("Replay did not account for the original calls")
    if (root / sanitizer_file).read_text().split() != lines[0].split():
        raise ValueError("Sanitizer replay differs from the optimized binary")
    timings = []
    for line in lines[1:]:
        marker, index, scalar, neon = line.split()
        if marker != "pair" or int(index) != len(timings) + 1 or min(float(scalar), float(neon)) <= 0:
            raise ValueError("Invalid replay timing pair")
        timings.append({"scalar_ns": float(scalar), "neon_ns": float(neon)})
    if len(timings) not in (0, 8):
        raise ValueError("Incomplete replay timing sample")
    return {**capture, "verified_results_and_xmm": int(verified), "guard_declines": int(declined),
            "observed_output_aliases": int(aliases), "sanitizer_passed": True,
            "timing_pairs": timings,
            "replay_median_ns": {key: median(t[key] for t in timings) for key in timings[0]} if timings else {}}


def summary(root):
    original = replay(root, "matrix-export.json", "matrix-replay.txt", "matrix-sanitized.txt", "matrix-cases.bin")
    timing = root / "timing"
    native = replay(timing, "matrix-export.json", "matrix-replay.txt", "matrix-sanitized.txt", "matrix-cases.bin")
    if native["guest_image_sha256"] != original["guest_image_sha256"]:
        raise ValueError("Original and native runs used different guests")
    witness = json.loads((timing / "artifact-sha256.json").read_text())
    if witness != native["diagnostic_artifacts"]:
        raise ValueError("Different native runtime or renderer in timing")
    diagnostics = (timing / "diagnostic/client.log").read_text()
    native_rows = re.findall(r"^ocerz: raid matrix native calls=(\d+)$", diagnostics, re.MULTILINE)
    if not native_rows or max(map(int, native_rows)) < 32768:
        raise ValueError("Native path did not execute in the diagnostic capture")
    runs = []
    for order, flag in enumerate((0, 1, 1, 0), 1):
        capture = timing / f"timed-{order}-{flag}"
        if (json.loads((capture / "artifact-sha256.json").read_text()) != witness or
                (capture / "matrix-mode.txt").read_text().split() != [str(flag), "0"] or
                (capture / "matrix.bin").exists() or "raid matrix native calls=" in (capture / "client.log").read_text()):
            raise ValueError("Incorrect runtime, renderer or logging in timing")
        window = TIMING["verify"](capture, "candidate", seconds=60)
        runs.append({"order": order, "mode": ("candidate" if flag else "control"), **window})
    if not (root / "m32-trial-tests.log").read_text().endswith("test_m32: 805 checks, 0 failed\n"):
        raise ValueError("The trial runtime did not pass the general i386 tests")
    return {"original_replay": original, "native_replay": native, "native_calls_at_last_largest_witness": max(map(int, native_rows)),
            "general_i386_checks": 805, **TIMING["summarize_timing"](runs)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_root", type=Path)
    print(json.dumps(summary(parser.parse_args().capture_root), indent=2))
