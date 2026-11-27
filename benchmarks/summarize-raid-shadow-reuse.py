"""Compare mapped-buffer storage options in isolated raid captures."""

import argparse
import json
from pathlib import Path
import re
from statistics import median


FIELDS = ("maps", "allocations", "reused", "evictions", "incoming_bytes", "array_bytes", "write_only_bytes")


def read_window(folder, seconds):
    window = json.loads((folder / "window.json").read_text())
    monitor = json.loads((folder / "monitor.log").read_text().splitlines()[-1])
    if monitor != {"activationNotifications": 0, "maxVisibleWindows": 0, "seen": [window["pid"]]}:
        raise ValueError(f"Invalid desktop isolation in {folder}")
    if window["window_size"] != [1365, 768] or window["missing_sequences"] or window["min_draws"] < 600:
        raise ValueError(f"Invalid raid GPU output in {folder}")
    if window["measurement_duration_ns"] != seconds * 1_000_000_000:
        raise ValueError(f"Wrong GPU window duration in {folder}")
    marker = "Saved anchor active. 25 actors. All 26 timed rows verified."
    if marker not in (folder / "run.log").read_text() or not (folder / "world.png").stat().st_size:
        raise ValueError(f"Missing fixture or visual readback in {folder}")
    return {
        key: value for key, value in window.items()
        if key not in {"pid", "stream", "first_sequence", "last_sequence"}
    }


def read_counter_rows(text):
    return [
        {name: int(value) for name, value in re.findall(r"(\w+)=(\d+)", line)}
        for line in text.splitlines(keepends=True)
        if line.startswith("ocerz: GL maps=") and line.endswith("\n")
    ]


def read_post_readback_counters(folder):
    text = (folder / "client.log").read_text()
    before, marker, after = text.rpartition("saved Metal trial backbuffer readback")
    if not marker:
        raise ValueError(f"Missing readback marker in {folder}")
    return read_counter_rows(before), read_counter_rows(after)


def read_diagnostic(folder, reuse):
    if reuse and (folder / "map-shadow-mode.txt").read_text().split() != ["1", "1"]:
        raise ValueError(f"Wrong diagnostic setting in {folder}")
    _, rows = read_post_readback_counters(folder)
    if len(rows) < 2 or any(b["maps"] - a["maps"] != 4096 for a, b in zip(rows, rows[1:])):
        raise ValueError(f"Missing diagnostic counter rows in {folder}")
    delta = {name: rows[-1][name] - rows[0][name] for name in FIELDS}
    if any(value < 0 for value in delta.values()) or bool(delta["reused"]) != bool(reuse):
        raise ValueError(f"Incorrect counter totals in {folder}")
    return {
        "shadow_reuse": bool(reuse),
        "diagnostic_gpu_window": read_window(folder, 25),
        "post_readback_maps_at_first_and_last_counter": [rows[0]["maps"], rows[-1]["maps"]],
        "post_readback_counter_delta": delta,
        "allocations_per_thousand_maps": 1000 * delta["allocations"] / delta["maps"],
        "mean_incoming_bytes_per_map": delta["incoming_bytes"] / delta["maps"],
    }


def verify_small_mode(folder, mode):
    if (folder / "small-storage-mode.txt").read_text().strip() != str(mode):
        raise ValueError(f"Wrong one-page allocation setting in {folder}")
    marker = "allocated its first guest-backed one-page buffer"
    if (marker in (folder / "client.log").read_text()) != bool(mode):
        raise ValueError(f"Unexpected one-page allocation witness in {folder}")


def read_small_diagnostic(folder, mode):
    verify_small_mode(folder, mode)
    if (folder / "map-shadow-mode.txt").read_text().split() != ["1", "1"]:
        raise ValueError(f"Wrong shadow settings in {folder}")
    before, after = read_post_readback_counters(folder)
    previous = before[-1]["maps"] if before else 0
    final = after[-1]["maps"] if after else previous
    # The counter emits at 128 maps and every 4,096 thereafter.
    # At most 4,095 maps sit between each boundary and its preceding report.
    result = {
        "small_guest_storage": bool(mode),
        "diagnostic_gpu_window": read_window(folder, 25),
        "post_window_resident_kib": int((folder / "post-window-rss-kib.txt").read_text()),
        "fallback_maps_after_readback_bounds": [
            max(0, final - previous - 4095), final - previous + 4095,
        ],
        "post_readback_counter_rows": len(after),
    }
    if len(after) >= 2:
        result["between_post_readback_rows"] = {
            key: after[-1][key] - after[0][key] for key in FIELDS
        }
    return result


def summarize(control, trial, ab, small_storage=False):
    policy = "small_guest_storage" if small_storage else "shadow_reuse"
    runs = []
    for index, mode in enumerate((0, 1, 1, 0), 1):
        folder = ab / f"{index}-{mode}"
        shadow_mode = 1 if small_storage else mode
        if (folder / "map-shadow-mode.txt").read_text().split() != [str(shadow_mode), "0"]:
            raise ValueError(f"Wrong comparison settings in {folder}")
        if small_storage:
            verify_small_mode(folder, mode)
        if "ocerz: GL maps=" in (folder / "client.log").read_text():
            raise ValueError(f"Counter logging during timed comparison in {folder}")
        runs.append({"order": index, policy: bool(mode), **read_window(folder, 60)})
    comparison = {}
    for mode, name in ((False, "control"), (True, "one_page" if small_storage else "reuse")):
        active = [run for run in runs if run[policy] == mode]
        comparison[name] = {
            "median_completed_fps": median(run["average_completed_fps"] for run in active),
            "median_p99_gap_ns": median(run["p99_gpu_completion_gap_ns"] for run in active),
        }
    read_diagnostics = read_small_diagnostic if small_storage else read_diagnostic
    return {
        "diagnostics": [read_diagnostics(control, 0), read_diagnostics(trial, 1)],
        "timed_runs": runs,
        "comparison": comparison,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("logged_control", type=Path)
    parser.add_argument("logged_reuse", type=Path)
    parser.add_argument("ab", type=Path)
    parser.add_argument("--small-storage", action="store_true")
    args = parser.parse_args()
    print(json.dumps(summarize(args.logged_control, args.logged_reuse, args.ab, args.small_storage), indent=2))


if __name__ == "__main__":
    main()
