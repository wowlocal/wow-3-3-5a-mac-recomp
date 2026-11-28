"""Compare identically built renderer libraries with and without raid PGO."""

import argparse
import hashlib
import json
from pathlib import Path
from runpy import run_path
from statistics import median


HERE = Path(__file__).resolve().parent
VERIFY_WINDOW = run_path(str(HERE / "summarize-raid-shadow-reuse.py"))["read_window"]


def verify(capture, kind, renderer_kind=None):
    training = kind == "training"
    if (capture / "pgo-mode.txt").read_text().split() != [kind, str(int(training))]:
        raise ValueError(f"Wrong PGO or instrumentation setting in {capture}")
    if (capture / "pass-mode.txt").read_text().split() != ["64", "0"]:
        raise ValueError(f"Different rendering policy in {capture}")
    if (capture / "map-shadow-mode.txt").read_text().split() != ["1", "0"]:
        raise ValueError(f"Different buffer reuse settings in {capture}")
    if (capture / "submissions.ns").exists() or (capture / "encoder-work.tsv").exists():
        raise ValueError(f"Unexpected diagnostic logging in {capture}")
    if json.loads((capture / "legacy.json").read_text()) != {
        "legacy_paused": True, "legacy_completed_frames_during_launch": 0,
    }:
        raise ValueError(f"Earlier test ran during {capture}")
    renderer = renderer_kind or kind
    if f"/artifacts/{renderer}/libwinemetalgl.dylib" not in (capture / "renderer-map.txt").read_text():
        raise ValueError(f"Wrong loaded renderer in {capture}")
    profiles = list(capture.glob("*.profraw"))
    if len(profiles) != int(training):
        raise ValueError(f"Unexpected profiling files in {capture}")
    window = VERIFY_WINDOW(capture, 60)
    if training:
        profile = profiles[0].read_bytes()
        if not profile:
            raise ValueError(f"Empty instrumentation profile in {capture}")
        window.update({"raw_profile_size_bytes": len(profile),
                       "raw_profile_sha256": hashlib.sha256(profile).hexdigest()})
    return window


def summarize_timing(timed):
    fps, p99 = "average_completed_fps", "p99_gpu_completion_gap_ns"
    return {
        "timed_runs": timed,
        "comparison": {
            mode: {
                "median_gpu_completed_fps": median(row[fps] for row in timed if row["mode"] == mode),
                "median_p99_completion_gap_ns": median(row[p99] for row in timed if row["mode"] == mode),
            }
            for mode in ("control", "candidate")
        },
        "paired_candidate_minus_control": [
            {"completed_fps": timed[b][fps] - timed[a][fps], "p99_gap_ns": timed[b][p99] - timed[a][p99]}
            for a, b in ((0, 1), (3, 2))
        ],
    }


def summarize(root):
    witness = json.loads((root / "artifact-sha256.json").read_text())
    flags = json.loads((root / "build-flags.json").read_text())
    for mode in ("control", "candidate"):
        artifact = root / "artifacts" / mode / "libwinemetalgl.dylib"
        if hashlib.sha256(artifact.read_bytes()).hexdigest() != witness[mode]:
            raise ValueError(f"Changed renderer in {mode}")
    training = [verify(root / f"training-{index}", "training") for index in (1, 2)]
    order = ("control", "candidate", "candidate", "control")
    timed = [
        {"order": index, "mode": mode, **verify(root / f"timed-{index}-{mode}", mode)}
        for index, mode in enumerate(order, 1)
    ]
    return {"renderer_build_validation": flags, "training_captures": training, **summarize_timing(timed)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_root", type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize(args.capture_root), indent=2))


if __name__ == "__main__":
    main()
