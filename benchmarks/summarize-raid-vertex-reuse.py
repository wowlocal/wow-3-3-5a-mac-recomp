"""Summarize matching vertex inputs for direct ARB draws in a verified raid window."""

import argparse
import json
from pathlib import Path
from statistics import median
from typing import NamedTuple


class ReuseRow(NamedTuple):
    pid: int
    stream: int
    sequence: int
    draws: int
    direct_arb_draws: int
    direct_arb_references: int
    tracked_draws: int
    tracked_references: int
    complete_references: int
    same_frame_addresses: int
    same_frame_versions: int
    same_frame_vertex_inputs: int
    same_frame_complete: int
    previous_frame_addresses: int
    previous_frame_versions: int
    previous_frame_vertex_inputs: int
    previous_frame_complete: int


def integers(path, width):
    with path.open() as file:
        for line in file:
            if not line.endswith("\n"):
                break
            row = tuple(map(int, line.split()))
            if len(row) != width:
                raise ValueError(f"Unexpected record in {path.name}")
            yield row


def select_rows(capture, window):
    pid, stream = window["pid"], window["stream"]
    first, last = window["first_sequence"], window["last_sequence"]
    expected = list(range(first, last + 1))
    draws = {
        sequence: count
        for process, generation, sequence, _, count, _ in integers(capture / "gpu.ns", 6)
        if (process, generation) == (pid, stream) and first <= sequence <= last
    }
    selected = sorted(
        ReuseRow(*row)
        for row in integers(capture / "vertex-reuse.ns", len(ReuseRow._fields))
        if row[:2] == (pid, stream) and first <= row[2] <= last
    )
    if sorted(draws) != expected or [row.sequence for row in selected] != expected:
        raise ValueError("The output sequences and reuse counters do not match")
    for row in selected:
        if not 0 <= row.tracked_draws <= row.direct_arb_draws <= row.draws == draws[row.sequence]:
            raise ValueError(f"Inconsistent draw counts at output {row.sequence}")
        if not 0 <= row.complete_references <= row.tracked_references <= row.direct_arb_references:
            raise ValueError(f"Inconsistent reference counts at output {row.sequence}")
        for addresses, versions, vertex, complete in (
            (row.same_frame_addresses, row.same_frame_versions, row.same_frame_vertex_inputs, row.same_frame_complete),
            (row.previous_frame_addresses, row.previous_frame_versions,
             row.previous_frame_vertex_inputs, row.previous_frame_complete),
        ):
            if (
                not 0 <= complete <= vertex <= versions <= addresses <= row.direct_arb_references
                or versions > row.tracked_references
                or vertex > row.complete_references
            ):
                raise ValueError(f"Inconsistent matching references at output {row.sequence}")
    return selected


def summarize(capture):
    window = json.loads((capture / "window.json").read_text())
    monitor = json.loads((capture / "monitor.log").read_text().splitlines()[-1])
    if (
        window["missing_sequences"] or monitor["seen"] != [window["pid"]]
        or monitor["activationNotifications"] or monitor["maxVisibleWindows"]
    ):
        raise ValueError("Expected an isolated window with continuous GPU output")
    rows = select_rows(capture, window)
    if len(rows) != window["verified_completed_renders"]:
        raise ValueError("The reuse rows do not cover the measured frame window")
    totals = {key: sum(getattr(row, key) for row in rows) for key in ReuseRow._fields[3:]}
    denominators = {
        "addresses": totals["direct_arb_references"],
        "versions": totals["tracked_references"],
        "vertex_inputs": totals["complete_references"],
        "complete": totals["complete_references"],
    }
    if not all(denominators.values()):
        raise ValueError("The capture did not exercise all three levels of the probe")
    proportions = {
        f"{scope}_{key}": totals[f"{scope}_{key}"] / total
        for scope in ("same_frame", "previous_frame")
        for key, total in denominators.items()
    }
    return {
        "viewport": window["window_size"],
        "measurement_duration_ns": window["measurement_duration_ns"],
        "verified_output_count": len(rows),
        "gpu_missing_sequences": 0,
        "visible_trial_windows": 0,
        "focus_activations": 0,
        "totals": totals,
        "median_per_output": {
            key: median(getattr(row, key) for row in rows)
            for key in ("direct_arb_draws", "direct_arb_references", "tracked_references")
        },
        "fraction_of_eligible_references_matching": proportions,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize(args.capture), indent=2))


if __name__ == "__main__":
    main()
