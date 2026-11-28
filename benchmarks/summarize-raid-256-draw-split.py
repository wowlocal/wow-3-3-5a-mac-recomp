"""Compare two continuing-pass submission budgets in the full raid."""

import argparse
import hashlib
import json
from pathlib import Path
import re
from runpy import run_path


HERE = Path(__file__).resolve().parent
SPLIT = run_path(str(HERE / "summarize-raid-single-render-pass.py"))
TIMING = run_path(str(HERE / "summarize-raid-renderer-pgo.py"))
ORDER = (64, 32, 32, 64)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_timed(root, artifacts):
    runs = []
    for order, setting in enumerate(ORDER, 1):
        capture = root / f"timed-{order}-{setting}"
        window = SPLIT["verify"](capture, setting, False)
        if (capture / "pgo-mode.txt").read_text().split() != ["candidate", "0"]:
            raise ValueError(f"Expected the PGO runtime with profiling off in {capture}")
        if "/artifacts/candidate/libwinemetalgl.dylib" not in (capture / "renderer-map.txt").read_text():
            raise ValueError(f"Wrong renderer loaded in {capture}")
        if json.loads((capture / "artifact-sha256.json").read_text()) != artifacts:
            raise ValueError(f"Changed runtime or renderer in {capture}")
        runs.append({"order": order, "mode": "control" if setting == 64 else "candidate",
                     "draw_setting": setting, **window})
    return TIMING["summarize_timing"](runs)


def replay_checks(root, renderer_hash):
    checks = []
    for setting in (64, 32):
        output = (root / f"replay-{setting}.log").read_text()
        match = re.search(r"outputs (\d+), RGBA ([0-9a-f]{16}), colored (\d+)", output)
        if not match or not int(match[1]) or not int(match[3]):
            raise ValueError(f"Missing deterministic output for draw setting {setting}")
        manifest = (root / f"replay-{setting}-renderer.sha256").read_text().strip()
        if manifest != renderer_hash:
            raise ValueError(f"Replay used a different renderer for draw setting {setting}")
        checks.append({"draw_setting": setting, "outputs": int(match[1]),
                       "rgba_hash": match[2], "colored_pixels": int(match[3])})
    if any(checks[0][key] != checks[1][key] for key in ("rgba_hash", "colored_pixels")):
        raise ValueError("The draw budget changed deterministic framebuffer pixels")
    return checks


def summarize(root, renderer):
    artifacts = json.loads((root / "artifact-sha256.json").read_text())
    baseline = json.loads((HERE / "results" / "raid-winemetalgl-renderer-pgo-ab.json").read_text())
    digest = sha256(renderer)
    if artifacts["renderer_sha256"] != digest or digest != baseline["renderer_artifacts"]["candidate"]["sha256"]:
        raise ValueError("Expected the accepted PGO WineMetalGL build in both arms")
    diagnostics = [SPLIT["diagnose"](root / f"diagnostic-{setting}", setting, include_fragment=True)
                   for setting in (64, 32)]
    control, candidate = [row["draw_bearing_submissions_per_output"] for row in diagnostics]
    if max(candidate, key=candidate.get) <= max(control, key=control.get):
        raise ValueError("The smaller budget did not increase typical draw-bearing submissions")
    config = ("hardware", "macos", "viewport", "vsync", "caster_count", "boss_count", "camera")
    return {
        **{key: baseline[key] for key in config}, "base_commit": "49582d4",
        "artifacts": artifacts, "diagnostics": diagnostics,
        "deterministic_replay": replay_checks(root, artifacts["renderer_sha256"]),
        **read_timed(root, artifacts),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_root", type=Path)
    parser.add_argument("renderer", type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize(args.capture_root, args.renderer), indent=2))


if __name__ == "__main__":
    main()
