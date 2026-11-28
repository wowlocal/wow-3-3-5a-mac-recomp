"""Compare indexed and linear shadow lookups in a single profiled runtime."""

import argparse
import json
from pathlib import Path
import re
from runpy import run_path


HERE = Path(__file__).resolve().parent
PGO = run_path(str(HERE / "summarize-raid-renderer-pgo.py"))
INDEX = re.compile(r"^ocerz: GL shadow index hits=(\d+) misses=(\d+)$", re.M)


def verify(capture, kind, indexed, logging, seconds=60):
    if (capture / "shadow-index-mode.txt").read_text().split() != [str(indexed), str(logging)]:
        raise ValueError(f"Wrong shadow-index setting in {capture}")
    text = (capture / "client.log").read_text()
    if bool(INDEX.search(text)) != bool(logging):
        raise ValueError(f"Unexpected index counter output in {capture}")
    return {
        **PGO["verify"](capture, kind, renderer_kind="candidate", seconds=seconds),
        "signed_runtime_sha256": (capture / "runtime-sha256.txt").read_text().strip(),
    }


def diagnostic_counts(folder):
    text = (folder / "client.log").read_text()
    _, marker, after = text.rpartition("saved Metal trial backbuffer readback")
    rows = [(int(hits), int(misses)) for hits, misses in INDEX.findall(after)]
    if not marker or len(rows) < 2:
        raise ValueError("Missing post-readback index counters")
    if any(c < a or d < b for (a, b), (c, d) in zip(rows, rows[1:])):
        raise ValueError("Shadow-index counters went backwards")
    hits, misses = (b - a for a, b in zip(rows[0], rows[-1]))
    if hits + misses <= 0:
        raise ValueError("No index probes in the observed interval")
    return {"complete_counter_rows_after_world_readback": len(rows),
            "index_key_hits": hits, "index_key_misses": misses,
            "key_hit_share": hits / (hits + misses)}


def summarize(root):
    binaries = json.loads((root / "timed-artifacts.json").read_text())
    reference = json.loads((HERE / "results/raid-winemetalgl-renderer-pgo-ab.json").read_text())
    if binaries["renderer"] != reference["renderer_artifacts"]["candidate"]["sha256"]:
        raise ValueError("Renderer differs from the accepted profiled renderer")
    training = [
        {"indexed": bool(indexed), **verify(root / f"training-{indexed}", "training", indexed, 0)}
        for indexed in (0, 1)
    ]
    if len({row["signed_runtime_sha256"] for row in training}) != 1:
        raise ValueError("Training used different runtimes")
    diag_folder = root / "diagnostic"
    diagnostic = verify(diag_folder, "candidate", 1, 1, seconds=25)
    order = (0, 1, 1, 0)
    timed = [
        {"order": index, "mode": "candidate" if indexed else "control", "indexed": bool(indexed),
         **verify(root / f"timed-{index}-{indexed}", "candidate", indexed, 0)}
        for index, indexed in enumerate(order, 1)
    ]
    if {row["signed_runtime_sha256"] for row in [diagnostic, *timed]} != {binaries["signed_runtime"]}:
        raise ValueError("The runtime changed between timing arms")
    return {
        "renderer_sha256": binaries["renderer"],
        "training_captures": training,
        "diagnostic_gpu_window": diagnostic,
        "diagnostic_index_counts": diagnostic_counts(diag_folder),
        **PGO["summarize_timing"](timed),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_root", type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize(args.capture_root), indent=2))


if __name__ == "__main__":
    main()
