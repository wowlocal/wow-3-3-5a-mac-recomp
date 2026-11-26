#!/usr/bin/env python3
"""Measure one repeatable Wrath route using the client swap log and macOS process counters."""

import argparse
import ctypes
import json
import math
from pathlib import Path
from statistics import median
import time


NANOSECONDS = 1_000_000_000


class ProcessUsage(ctypes.Structure):
    # rusage_info_v0 from the macOS SDK's sys/resource.h.
    _fields_ = [("uuid", ctypes.c_uint8 * 16)] + [
        (name, ctypes.c_uint64)
        for name in (
            "user_ticks", "system_ticks", "package_wakes", "interrupt_wakes", "page_ins",
            "wired_bytes", "resident_bytes", "footprint_bytes", "started", "exited",
        )
    ]


def monotonic_ns():
    return time.clock_gettime_ns(time.CLOCK_MONOTONIC)


def process_usage(pid):
    usage = ProcessUsage()
    proc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    result = proc.proc_pid_rusage(pid, 0, ctypes.byref(usage))  # RUSAGE_INFO_V0
    if result:
        raise RuntimeError(f"Unable to read resource usage for PID {pid} (errno {ctypes.get_errno()})")
    return usage


def cpu_nanoseconds_per_tick():
    timebase = (ctypes.c_uint32 * 2)()
    if ctypes.CDLL(None).mach_timebase_info(ctypes.byref(timebase)) or not timebase[1]:
        raise RuntimeError("Unable to read the macOS Mach timebase")
    return timebase[0] / timebase[1]


def percentile(sorted_values, quantile):
    return sorted_values[math.ceil(quantile * len(sorted_values)) - 1]


def summarize_frames(frame_times, start, stop):
    selected = [stamp for stamp in frame_times if start <= stamp <= stop]
    if selected != sorted(selected):
        raise RuntimeError("Frame log has out-of-order timestamps")
    gaps = sorted(right - left for left, right in zip(selected, selected[1:]))
    if not gaps:
        raise RuntimeError("The client produced fewer than two measured swaps during the route")
    spans = [selected[0] - start, *gaps, stop - selected[-1]]
    return {
        "swaps": len(selected),
        "duration_ns": stop - start,
        "average_swap_fps": round(len(selected) * NANOSECONDS / (stop - start), 3),
        "median_fps": round(NANOSECONDS / median(gaps), 2),
        "p50_frame_ms": round(median(gaps) / 1_000_000, 6),
        "p95_frame_ms": round(percentile(gaps, 0.95) / 1_000_000, 6),
        "p99_frame_ms": round(percentile(gaps, 0.99) / 1_000_000, 6),
        "stalls_over_250_ms": sum(gap > 250_000_000 for gap in spans),
        "longest_gap_ms": round(max(spans) / 1_000_000, 2),
    }


def read_swap_rows(path):
    with path.open() as stream:
        rows = [tuple(map(int, line.split())) for line in stream if line.endswith("\n")]
    if rows and (len(rows[0]) not in (1, 2) or any(len(row) != len(rows[0]) for row in rows)):
        raise RuntimeError("Expected a swap return timestamp or an entry/return pair on each line")
    return rows


def summarize_swap_split(rows, start, stop):
    selected = [(entered, returned) for entered, returned in rows if start <= returned <= stop]
    elapsed = [returned - entered for entered, returned in selected]
    outside = [entered - previous[1] for previous, (entered, _) in zip(selected, selected[1:])]
    if min(elapsed) < 0 or min(outside) < 0:
        raise RuntimeError("The swap split requires one sequential on-screen swap stream")
    paired_swap = elapsed[1:]
    spans = sorted(zip(paired_swap, outside), key=sum)
    slow = spans[-math.ceil(0.05 * len(spans)):]
    return {
        "median_inside_swap_ms": round(median(paired_swap) / 1_000_000, 2),
        "p95_inside_swap_ms": round(percentile(sorted(paired_swap), 0.95) / 1_000_000, 2),
        "median_between_swaps_ms": round(median(outside) / 1_000_000, 2),
        "p95_between_swaps_ms": round(percentile(sorted(outside), 0.95) / 1_000_000, 2),
        "time_inside_swap_percent": round(100 * sum(paired_swap) / (sum(paired_swap) + sum(outside)), 2),
        "slowest_5pct_time_inside_swap_percent": round(100 * sum(swap for swap, _ in slow) / sum(map(sum, slow)), 2),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pid", type=int)
    parser.add_argument("frame_log", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--seconds", type=float, default=120)
    options = parser.parse_args()
    if not math.isfinite(options.seconds) or options.seconds <= 0:
        parser.error("--seconds must be a positive duration")
    if not options.frame_log.is_file():
        parser.error("frame log is missing. Launch with --profile and wait for the game window")

    initial = process_usage(options.pid)
    start = monotonic_ns()
    print(f"Start the route now. Recording {options.seconds:g} seconds.", flush=True)
    peak = initial.footprint_bytes
    sample = initial
    deadline = start + int(options.seconds * NANOSECONDS)
    while (remaining := deadline - monotonic_ns()) > 0:
        time.sleep(min(remaining / NANOSECONDS, 0.5))
        sample = process_usage(options.pid)
        if sample.started != initial.started:
            raise RuntimeError("The client process changed during recording")
        peak = max(peak, sample.footprint_bytes)
    stop = monotonic_ns()
    used_ticks = sample.user_ticks + sample.system_ticks - initial.user_ticks - initial.system_ticks
    used_ns = used_ticks * cpu_nanoseconds_per_tick()
    rows = read_swap_rows(options.frame_log)
    report = summarize_frames([row[-1] for row in rows], start, stop)
    if len(rows[0]) == 2:
        report.update(summarize_swap_split(rows, start, stop))
    report.update({
        "duration_seconds": round((stop - start) / NANOSECONDS, 3),
        "average_client_cpu_percent": round(100 * used_ns / (stop - start), 2),
        "peak_sampled_client_footprint_mib": round(peak / 2**20, 2),
        "footprint_sample_interval_seconds": 0.5,
    })
    with options.output.open("x") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
