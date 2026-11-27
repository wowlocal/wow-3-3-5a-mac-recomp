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
    position_references: int = 0
    same_frame_position: int = 0
    previous_frame_position: int = 0


def integers(path, widths):
    with path.open() as file:
        for line in file:
            if not line.endswith("\n"):
                break
            row = tuple(map(int, line.split()))
            if len(row) not in widths:
                raise ValueError(f"Unexpected record in {path.name}")
            yield row


def reuse_row(values):
    if len(values) == 17:
        return ReuseRow(*values)
    original = values[:9] + values[10:14] + values[15:19]
    return ReuseRow(*original, values[9], values[14], values[19])


def select_rows(capture, window):
    pid, stream = window["pid"], window["stream"]
    first, last = window["first_sequence"], window["last_sequence"]
    expected = list(range(first, last + 1))
    draws = {
        sequence: count
        for process, generation, sequence, _, count, _ in integers(capture / "gpu.ns", (6,))
        if (process, generation) == (pid, stream) and first <= sequence <= last
    }
    selected = sorted(
        reuse_row(row)
        for row in integers(capture / "vertex-reuse.ns", (17, 20))
        if row[:2] == (pid, stream) and first <= row[2] <= last
    )
    if sorted(draws) != expected or [row.sequence for row in selected] != expected:
        raise ValueError("The output sequences and reuse counters do not match")
    for row in selected:
        if not 0 <= row.tracked_draws <= row.direct_arb_draws <= row.draws == draws[row.sequence]:
            raise ValueError(f"Inconsistent draw counts at output {row.sequence}")
        if not 0 <= row.position_references <= row.complete_references <= row.tracked_references <= row.direct_arb_references:
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
        for position, versions in (
            (row.same_frame_position, row.same_frame_versions),
            (row.previous_frame_position, row.previous_frame_versions),
        ):
            if not 0 <= position <= min(versions, row.position_references):
                raise ValueError(f"Inconsistent position references at output {row.sequence}")
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
        raise ValueError("The capture did not exercise the vertex reuse probe")
    if totals["position_references"]:
        denominators["position"] = totals["position_references"]
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
