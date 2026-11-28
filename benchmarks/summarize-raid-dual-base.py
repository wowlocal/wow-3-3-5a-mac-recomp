"""Verify the live comparison of two guarded i386 address bases."""

import argparse
import hashlib
import json
from pathlib import Path
import re
from runpy import run_path
from statistics import median


HERE = Path(__file__).resolve().parent
READ_WINDOW = run_path(str(HERE / "summarize-raid-shadow-reuse.py"))["read_window"]
WITNESS = re.compile(
    r"^ocerz: i386 dual base rip=(0x[0-9a-f]+) "
    r"primary=(\d+):(\d+):(\d+) secondary=(\d+):(\d+):(\d+)$", re.M
)
READBACK = "saved Metal trial backbuffer readback"
FPS = "average_completed_fps"
P99 = "p99_gpu_completion_gap_ns"
FOCUSED = {
    "overlap": ("0x400600", "dual bases preserve overlapping scalar copies"),
    "underflow": ("0x400680", "secondary protected address wraps below zero"),
    "upper": ("0x400700", "secondary protected address near four GiB"),
}


def checksum(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_run(folder, mode, logging, seconds, artifacts):
    settings = {
        "dual-base-mode.txt": [str(mode), str(logging)],
        "pgo-mode.txt": ["candidate", "0"],
        "map-shadow-mode.txt": ["1", "0"],
        "pass-mode.txt": ["64", "1"],
        "render-end-mode.txt": ["deferred", "0"],
    }
    for filename, expected in settings.items():
        if (folder / filename).read_text().split() != expected:
            raise ValueError(f"Wrong settings in {folder / filename}")
    if ((folder / "shared-base.txt").read_text().split()[:2] != ["OCERZ_M32_GENERAL_BASE_HOIST", "1"] or
            "/artifacts/candidate/libwinemetalgl.dylib" not in (folder / "renderer-map.txt").read_text() or
            json.loads((folder / "artifact-sha256.json").read_text()) != artifacts or
            json.loads((folder / "legacy.json").read_text()) != {
                "legacy_paused": True, "legacy_completed_frames_during_launch": 0,
            }):
        raise ValueError(f"Different runtime, renderer or isolation in {folder}")
    text = (folder / "client.log").read_text()
    if bool(WITNESS.search(text)) != bool(logging) or READBACK not in text:
        raise ValueError(f"Unexpected code generation logging or missing readback in {folder}")
    return READ_WINDOW(folder, seconds)


def verify_tests(root):
    if not (root / "general.log").read_text().endswith("test_m32: 805 checks, 0 failed\n"):
        raise ValueError("The general i386 suite failed")
    for mode in (0, 1):
        for name, (site, marker) in FOCUSED.items():
            log = (root / f"focused-{mode}-{name}.log").read_text()
            rows = list(WITNESS.finditer(log))
            guard_bail = f"LOWHOIST bail {site}" in log
            if (f"  ok  {marker}\n" not in log or "FAIL" in log or len(rows) != mode or
                    (mode and rows[0][1] != site) or
                    guard_bail != bool(mode and name != "overlap")):
                raise ValueError(f"Missing focused {name} case evidence in mode {mode}")


def summarize(root):
    reference = json.loads((HERE / "results/raid-winemetalgl-defer-render-end-pgo-ab.json").read_text())
    artifacts = json.loads((root / "artifact-sha256.json").read_text())
    if (checksum(root / "artifacts/candidate/ocerz") != artifacts["runtime_unsigned_sha256"] or
            checksum(root / "artifacts/candidate/libwinemetalgl.dylib") != artifacts["renderer_sha256"] or
            artifacts["renderer_sha256"] != reference["artifact_sha256"]["renderer_sha256_by_role"]["candidate"]):
        raise ValueError("Wrong runtime or selected renderer")
    verify_tests(root)
    diagnostic = root / "diagnostic"
    diagnostic_window = verify_run(diagnostic, 1, 1, 25, artifacts)
    text = (diagnostic / "client.log").read_text()
    sites = {int(row[1], 16) for row in WITNESS.finditer(text)}
    coverage = (0x2AADDF, 0x97B9A7, 0x8C19FA)
    if not all(site in sites for site in coverage):
        raise ValueError("No code-generation witness in the selected scenery paths")
    runs = [
        {"order": index, "dual_base": bool(mode),
         **verify_run(root / f"timed-{index}-{mode}", mode, 0, 60, artifacts)}
        for index, mode in enumerate((0, 1, 1, 0), 1)
    ]
    result = {
        label: {
            "median_gpu_completed_fps": median(row[FPS] for row in runs if row["dual_base"] == mode),
            "median_p99_completion_gap_ns": median(row[P99] for row in runs if row["dual_base"] == mode),
        }
        for mode, label in enumerate(("one_base", "two_bases"))
    }
    return {
        "artifact_sha256": artifacts, "general_i386_checks": 805, "focused_cases_per_mode": len(FOCUSED),
        "diagnostic_gpu_window": diagnostic_window,
        "distinct_emitted_blocks": len(sites),
        "distinct_emitted_after_readback": len({row[1] for row in WITNESS.finditer(text.rsplit(READBACK, 1)[1])}),
        "verified_emitted_blocks_in_selected_paths": [hex(site) for site in coverage],
        "timed_runs": runs, "comparison": result,
        "paired_two_minus_one": [
            {"gpu_completed_fps": runs[t][FPS] - runs[c][FPS], "p99_completion_gap_ns": runs[t][P99] - runs[c][P99]}
            for c, t in ((0, 1), (3, 2))
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_root", type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize(args.capture_root), indent=2))
