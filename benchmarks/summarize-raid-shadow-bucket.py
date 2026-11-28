"""Compare the empty-bucket shortcut with the original shadow scan."""

import argparse
import json
from pathlib import Path
import re
from runpy import run_path


HERE = Path(__file__).resolve().parent
PGO = run_path(str(HERE / "summarize-raid-renderer-pgo.py"))
BUCKETS = re.compile(r"^ocerz: GL shadow buckets empty=(\d+) occupied=(\d+)$", re.M)


def verify(capture, kind, enabled, logging, seconds=60):
    if (capture / "shadow-bucket-mode.txt").read_text().split() != [str(enabled), str(logging)]:
        raise ValueError(f"Wrong bucket shortcut setting in {capture}")
    text = (capture / "client.log").read_text()
    if bool(BUCKETS.search(text)) != bool(logging):
        raise ValueError(f"Unexpected bucket counter output in {capture}")
    return {
        **PGO["verify"](capture, kind, renderer_kind="candidate", seconds=seconds),
        "signed_runtime_sha256": (capture / "runtime-sha256.txt").read_text().strip(),
    }


def diagnostic_counts(folder):
    text = (folder / "client.log").read_text()
    _, marker, after = text.rpartition("saved Metal trial backbuffer readback")
    rows = [(int(empty), int(occupied)) for empty, occupied in BUCKETS.findall(after)]
    if not marker or len(rows) < 2:
        raise ValueError("Missing post-readback bucket counters")
    if any(c < a or d < b for (a, b), (c, d) in zip(rows, rows[1:])):
        raise ValueError("Bucket counters went backwards")
    empty, occupied = (b - a for a, b in zip(rows[0], rows[-1]))
    if empty + occupied <= 0:
        raise ValueError("No bucket checks in the observed interval")
    return {"complete_counter_rows_after_world_readback": len(rows),
            "empty_bucket_checks": empty, "occupied_bucket_checks": occupied,
            "empty_bucket_share": empty / (empty + occupied)}


def summarize(root):
    binaries = json.loads((root / "timed-artifacts.json").read_text())
    reference = json.loads((HERE / "results/raid-winemetalgl-renderer-pgo-ab.json").read_text())
    if binaries["renderer"] != reference["renderer_artifacts"]["candidate"]["sha256"]:
        raise ValueError("Renderer differs from the accepted profiled renderer")
    training = [
        {"enabled": bool(enabled), **verify(root / f"training-{enabled}", "training", enabled, 0)}
        for enabled in (0, 1)
    ]
    if len({row["signed_runtime_sha256"] for row in training}) != 1:
        raise ValueError("Training used different runtimes")
    diag_folder = root / "diagnostic"
    diagnostic = verify(diag_folder, "candidate", 1, 1, seconds=25)
    order = (0, 1, 1, 0)
    timed = [
        {"order": index, "mode": "candidate" if enabled else "control", "enabled": bool(enabled),
         **verify(root / f"timed-{index}-{enabled}", "candidate", enabled, 0)}
        for index, enabled in enumerate(order, 1)
    ]
    if {row["signed_runtime_sha256"] for row in [diagnostic, *timed]} != {binaries["signed_runtime"]}:
        raise ValueError("The runtime changed between timing arms")
    return {
        "renderer_sha256": binaries["renderer"],
        "training_captures": training,
        "diagnostic_gpu_window": diagnostic,
        "diagnostic_bucket_counts": diagnostic_counts(diag_folder),
        **PGO["summarize_timing"](timed),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_root", type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize(args.capture_root), indent=2))


if __name__ == "__main__":
    main()
