"""Export compact position and bounds cases from verified raid probe captures."""

import argparse
from bisect import bisect_right
import json
from pathlib import Path
from runpy import run_path
import struct


HERE = Path(__file__).resolve().parent
BOUNDS = run_path(str(HERE / "summarize-raid-vertex-bounds.py"))
CASE = struct.Struct("<24I")


def export_capture(capture, output):
    window = BOUNDS["VERIFY_WINDOW"](capture, 25)
    _, first, end = BOUNDS["WINDOW"](capture)
    _, rows = BOUNDS["read_records"](capture, first, end)
    swaps = BOUNDS["read_swaps"](capture, first, end)
    pending = {}
    quads = {}
    counts = {"gpu_completed_frames": window["verified_completed_renders"],
              "complete_quads": 0, "changed_bounds_quads": 0, "quads_with_special_float_inputs": 0}
    for tick, cpu, pc, address, stack, position, state in rows:
        group, edge = BOUNDS["PROBE_SITES"][pc]
        frame = bisect_right(swaps, tick)
        key = cpu, stack, address
        group_key = (*key, group)
        if not edge:
            if group_key in pending:
                raise ValueError(f"Unpaired start in {capture}")
            pending[group_key] = frame, position, state
            continue
        start = pending.pop(group_key, None)
        if start is None or start[0] != frame:
            quads.pop(key, None)
            continue
        _, original_position, before = start
        if position != original_position or BOUNDS["update_bounds"](before, position) != state:
            raise ValueError(f"Position or bounds mismatch in {capture}")
        if not group:
            prior = frame, -1, before, (), before
        else:
            prior = quads.pop(key, None)
        if prior is None or prior[:2] != (frame, group - 1):
            continue
        initial, coordinates, previous_bounds = prior[2:]
        if previous_bounds != before:
            raise ValueError(f"Bounds changed between vertices in {capture}")
        coordinates += position
        if group < 3:
            quads[key] = frame, group, initial, coordinates, state
            continue
        output.write(CASE.pack(*initial, *coordinates, *state))
        counts["complete_quads"] += 1
        counts["changed_bounds_quads"] += initial != state
        counts["quads_with_special_float_inputs"] += any(
            word & 0x7F800000 == 0x7F800000 or 0 < (word & 0x7FFFFFFF) < 0x00800000
            for word in (*initial, *coordinates)
        )
    if not counts["complete_quads"]:
        raise ValueError(f"No complete quad records in {capture}")
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case_file", type=Path)
    parser.add_argument("capture", nargs="+", type=Path)
    args = parser.parse_args()
    with args.case_file.open("xb") as output:
        reports = [export_capture(capture, output) for capture in args.capture]
    print(json.dumps(reports, indent=2))


if __name__ == "__main__":
    main()
