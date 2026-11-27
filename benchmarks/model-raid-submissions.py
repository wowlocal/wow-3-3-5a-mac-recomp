#!/usr/bin/env python3
"""Model draw and triangle limits from a saved raid call-order log."""

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median


CALL = re.compile(r"^ocerz: m32: calllog \[t(\d+)\] (_aglSwapBuffers|_glDraw\w+)\(([^)]*)\)")


def captured_frames(path):
    pending = {}
    completed = defaultdict(list)
    with path.open() as trace:
        for line in trace:
            match = CALL.match(line)
            if not match:
                continue
            thread, name, arguments = match.groups()
            if name == "_aglSwapBuffers":
                if thread in pending:
                    completed[thread].append(pending[thread])
                pending[thread] = []
            elif thread in pending:
                pending[thread].append((name, arguments))

    frames = max(completed.values(), key=lambda batch: sum(map(len, batch)), default=[])
    if not any(frames):
        raise ValueError("No draw calls between complete swaps")
    return [[primitive_counts(name, arguments) for name, arguments in frame] for frame in frames]


def primitive_counts(name, arguments):
    if name != "_glDrawRangeElementsEXT":
        raise ValueError(f"Unsupported draw call {name}")
    mode, _, _, count = (int(argument, 0) for argument in arguments.split(",")[:4])
    if count < 0:
        raise ValueError("Negative element count")
    if mode == 0x0004:
        triangles = count // 3
        return triangles, triangles
    if mode == 0x0005:
        return max(0, count - 2), 0
    raise ValueError(f"Unsupported draw topology {mode:#x}")


def partitions(frame, draw_setting, triangle_budget, include_strips):
    buffers = []
    draws = triangles = budget_triangles = 0
    for primitives, list_primitives in frame:
        draws += 1
        triangles += primitives
        budget_triangles += primitives if include_strips else list_primitives
        # The continuing-pass path checks these counts before the next draw.
        # Flushing at swap accounts for the final group, including exact cuts.
        draw_cap_reached = draws >= 8 * draw_setting
        triangle_budget_reached = triangle_budget and budget_triangles >= triangle_budget
        if draw_cap_reached or (draws >= draw_setting and triangle_budget_reached):
            buffers.append({"draws": draws, "triangles": triangles})
            draws = triangles = budget_triangles = 0
    if draws:
        buffers.append({"draws": draws, "triangles": triangles})
    return buffers


def sweep(frames, draw_settings, triangle_budget, include_strips, example_frame):
    results = []
    for setting in draw_settings:
        groups = [partitions(frame, setting, triangle_budget, include_strips) for frame in frames]
        results.append({
            "draw_setting": setting,
            "continuing_pass_draw_cap": setting * 8,
            "frames_by_buffer_count": dict(sorted(Counter(map(len, groups)).items())),
            "max_draws_per_buffer": max(buffer["draws"] for frame in groups for buffer in frame),
            **({"example_buffers": groups[example_frame - 1]} if example_frame is not None else {}),
        })
    return {
        "first_tested_setting_with_at_most_two_buffers": next((
            row["draw_setting"] for row in results if max(row["frames_by_buffer_count"]) <= 2
        ), None),
        "results": results,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("call_log", type=Path)
    parser.add_argument("--triangle-budget", type=int, default=65000)
    parser.add_argument("--draw-settings", nargs="+", type=int, default=list(range(64, 97)))
    parser.add_argument("--example-frame", type=int, help="One-based index of a complete swap interval")
    args = parser.parse_args()
    if args.triangle_budget < 0 or min(args.draw_settings) <= 0:
        parser.error("triangle budget must be nonnegative and draw settings must be positive")
    draw_settings = sorted(set(args.draw_settings))
    try:
        frames = captured_frames(args.call_log)
        if args.example_frame is not None and not 1 <= args.example_frame <= len(frames):
            raise ValueError("example frame must be a complete swap interval in the capture")
        report = {
            "model": "Continuing render pass with direct draw limits and swap at the end",
            "complete_intervals": len(frames),
            "median_draws_per_interval": median(map(len, frames)),
            "median_triangles_per_interval": median(sum(draw[0] for draw in frame) for frame in frames),
            "triangle_budget": args.triangle_budget,
            "example_frame": args.example_frame,
            "counting_triangle_strips": sweep(frames, draw_settings, args.triangle_budget, True, args.example_frame),
            "counting_direct_triangle_lists_only": sweep(frames, draw_settings, args.triangle_budget, False,
                                                         args.example_frame),
        }
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
