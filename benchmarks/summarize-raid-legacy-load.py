"""Compare verified raid outputs with an earlier test process active or paused."""

import argparse
import json
from pathlib import Path
from runpy import run_path
from statistics import median


READ_WINDOW = run_path(str(Path(__file__).with_name("summarize-raid-shadow-reuse.py")))["read_window"]


def summarize(root):
    restored_state = (root / "legacy-restored").read_text().strip()
    if not restored_state or restored_state.startswith(("T", "Z")):
        raise ValueError("Expected the legacy test process to resume after the captures")
    runs = []
    for index, label in enumerate(("running", "paused", "paused", "running"), 1):
        capture = root / f"{index}-{label}"
        paused = label == "paused"
        witness = json.loads((capture / "legacy.json").read_text())
        completed = witness["legacy_completed_frames_during_launch"]
        if witness["legacy_paused"] != paused or (completed == 0) != paused or completed < 0:
            raise ValueError(f"Legacy completion witness disagrees with the mode in {capture}")
        if (capture / "map-shadow-mode.txt").read_text().split() != ["1", "0"]:
            raise ValueError(f"Unexpected renderer settings in {capture}")
        runs.append({"order": index, "mode": label, **witness, **READ_WINDOW(capture, 60)})
    comparison = {}
    for label in ("running", "paused"):
        mode_runs = [run for run in runs if run["mode"] == label]
        comparison[label] = {
            "median_completed_fps": median(run["average_completed_fps"] for run in mode_runs),
            "median_p99_gap_ns": median(run["p99_gpu_completion_gap_ns"] for run in mode_runs),
        }
    return {
        "timed_runs": runs,
        "comparison": comparison,
        "paired_paused_minus_running": [
            {
                "completed_fps": runs[trial]["average_completed_fps"] - runs[control]["average_completed_fps"],
                "p99_gap_ns": runs[trial]["p99_gpu_completion_gap_ns"] - runs[control]["p99_gpu_completion_gap_ns"],
            }
            for control, trial in ((0, 1), (3, 2))
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("timed_captures", type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize(args.timed_captures), indent=2))


if __name__ == "__main__":
    main()
