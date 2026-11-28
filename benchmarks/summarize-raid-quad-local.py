"""Check local i386 conditional stores against the full raid and scalar bounds."""

import argparse
import hashlib
import json
from pathlib import Path
import re
from runpy import run_path


HERE = Path(__file__).resolve().parent
TIMING = run_path(str(HERE / "summarize-raid-renderer-pgo.py"))
BOUNDS = run_path(str(HERE / "summarize-raid-vertex-bounds.py"))
SITES = {"0x881709", "0x8817f1", "0x8818ec", "0x8819cf"}
LOG = re.compile(r"^ocerz: quad local stores rip=(0x[0-9a-f]+) joins=(\d+)$", re.M)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_mode(capture, mode, logging, seconds):
    if (capture / "quad-local-mode.txt").read_text().split() != [str(mode), str(logging)]:
        raise ValueError(f"Different local-store settings in {capture}")
    return TIMING["verify"](capture, "candidate", seconds=seconds)


def diagnostic(root):
    capture = root / "in-world-bounds"
    read_mode(capture, 1, 1, 25)
    observed = LOG.findall((capture / "client.log").read_text())
    if {pc for pc, _ in observed} != SITES or any(count != "6" for _, count in observed):
        raise ValueError("Expected six local branches at each of the four live sites")
    artifact = root / "artifacts" / "validation" / "ocerz"
    return {"site_join_counts": {pc: 6 for pc in sorted(SITES)},
            "runtime_sha256": sha256(artifact),
            "signed_runtime_sha256": (capture / "runtime-sha256.txt").read_text().strip(),
            **BOUNDS["summarize"](capture)}


def unit_results(root):
    results = {}
    for mode in (0, 1):
        for index in range(6):
            log = (root / f"unit-{mode}-{index}.log").read_text()
            if "  ok  quad stores" not in log or bool(LOG.search(log)) != bool(mode) or "FAIL" in log:
                raise ValueError("A focused interpreter comparison failed")
            if mode and LOG.findall(log) != [("0x400000", "2")]:
                raise ValueError("The focused comparison must execute both local branches")
        kind = "candidate" if mode else "control"
        general = (root / f"general-{kind}.log").read_text()
        match = re.search(r"test_m32: (\d+) checks, 0 failed", general)
        if not match or not int(match[1]):
            raise ValueError("General i386 checks did not pass")
        results[kind] = {"focused_interpreter_comparisons": 6, "general_checks": int(match[1])}
    return results


def summarize(root, guest_binary):
    baseline = json.loads((HERE / "results" / "raid-aarchx-quad-native-replay.json").read_text())
    guest_hash = sha256(guest_binary)
    if guest_hash != baseline["guest_image_sha256"]:
        raise ValueError("Recorded sites belong to a different guest image")
    artifacts = json.loads((root / "timed-artifacts.json").read_text())
    if (artifacts["renderer"] != baseline["layout_artifacts_sha256"]["renderer"] or
            artifacts["unsigned_runtime"] != sha256(root / "artifacts" / "candidate" / "ocerz")):
        raise ValueError("Different timed binaries")
    runs = []
    for order, mode in enumerate((0, 1, 1, 0), 1):
        capture = root / f"timed-{order}-{mode}"
        window = read_mode(capture, mode, 0, 60)
        if LOG.search((capture / "client.log").read_text()):
            raise ValueError("Unexpected code-generation logging during timing")
        if (capture / "runtime-sha256.txt").read_text().strip() != artifacts["signed_runtime"]:
            raise ValueError("The timed comparison changed runtime binaries")
        runs.append({"order": order, "mode": "candidate" if mode else "control", **window})
    settings = ("hardware", "macos", "viewport", "vsync", "caster_count", "boss_count", "camera")
    return {
        **{name: baseline[name] for name in settings},
        "base_commit": "05284a3",
        "guest_image_sha256": guest_hash,
        "timed_artifacts_sha256": artifacts,
        "unit_tests": unit_results(root),
        "diagnostic": diagnostic(root),
        **TIMING["summarize_timing"](runs),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_root", type=Path)
    parser.add_argument("guest_binary", type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize(args.capture_root, args.guest_binary), indent=2))


if __name__ == "__main__":
    main()
