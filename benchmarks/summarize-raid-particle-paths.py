"""Measure mapped particle batches and their ordered geometry publication."""

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
import json
from pathlib import Path
from runpy import run_path
from typing import NamedTuple


SCENE = run_path(str(Path(__file__).with_name("summarize-raid-scene-work.py")))
PHASE, LIFE = SCENE["PHASE"], SCENE["LIFE"]
DISPATCH = (0x97DC66, 0x97DC9C)
PARTICLE, GROUP, STANDALONE = 0x97D552, 0x97CB41, 0x97CDA2
GROUP_MAP, GROUP_UNMAP = 0x97C8BD, 0x97C906
FILL, ADVANCE, QUADS = 0x97C9FB, 0x97CA30, 0x8831A9
FILL_TAIL = 0x883343
DIRECT = {
    **dict.fromkeys(DISPATCH, 0x97CEF0),
    0x97D3A1: 0x979130, 0x97D4DE: 0x97B970, PARTICLE: 0x97CAA0,
    GROUP: 0x97C340, STANDALONE: 0x8832A0, FILL: 0x8832A0, QUADS: 0x882C80,
    0x97C8A1: 0x8D68B0, 0x97C992: 0x979B80, 0x88321C: 0x87AC90,
    0x88324D: 0x8FF080, 0x88326D: 0x8D68B0,
}
INDIRECT = {
    GROUP_MAP: "*0xdc(%edx)", GROUP_UNMAP: "*0xe0(%edx)",
    0x8831DB: "*0xe0(%edx)", 0x88328F: "*0xdc(%edx)",
}
RETURNS = {**{site + 5: site for site in DIRECT}, **{site + 6: site for site in INDIRECT}}
CALLS = DIRECT.keys() | INDIRECT.keys()


class Call(NamedTuple):
    site: int
    start: int
    end: int
    item: int
    output: int
    eax: int

    @property
    def duration(self):
        return self.end - self.start


class Advance(NamedTuple):
    tick: int
    item: int
    output: int
    index_bytes: int


def read_paths(capture):
    gate = json.loads((capture / "phase-gate.json").read_text())
    calls, advances, pending = defaultdict(list), defaultdict(list), {}
    missing = Counter()
    first, last = None, 0
    for tick, cpu, pc, eax, _, _, edx, esi, edi, ebp, esp in SCENE["read_boundary_records"](
        capture / "particle-paths.bin", gate
    ):
        first = tick if first is None else min(first, tick)
        last = max(last, tick)
        if pc == ADVANCE:
            advances[cpu].append(Advance(tick, edi, esi, edx))
        elif pc in CALLS:
            key = cpu, pc, ebp, esp
            if key in pending:
                raise ValueError(f"Repeated call with no return at {pc:#x}")
            pending[key] = tick, edi, esi
        else:
            site = RETURNS[pc]
            entry = pending.pop((cpu, site, ebp, esp), None)
            if entry is None:
                missing[pc] += 1
                continue
            start, item, output = entry
            if site == FILL and (not item or (item, output) != (edi, esi)):
                raise ValueError("Fill changed the caller's input item or mapped cursor")
            calls[cpu].append(Call(site, start, tick, item, output, eax))
    active = [cpu for cpu, rows in calls.items() if any(row.site in DISPATCH for row in rows)]
    if len(active) != 1:
        raise ValueError("Expected one particle dispatcher CPU")
    cpu, = active
    return calls[cpu], advances[cpu], first, last, {
        **gate, "dispatch_cpus": len(active), "total_guest_cpus": len(calls),
        "unpaired_entries_by_pc": {hex(pc): n for pc, n in Counter(key[1] for key in pending).items()},
        "unpaired_returns_by_pc": {hex(pc): n for pc, n in sorted(missing.items())},
    }


def contained(parent, children, starts):
    i = bisect_right(starts, parent.start)
    j = bisect_right(starts, parent.end, lo=i)
    if any(child.end > parent.end for child in children[i:j]):
        raise ValueError("Call extends beyond its parent")
    return children[i:j]


def plan_batches(calls, moves):
    groups = sorted(calls[GROUP], key=lambda call: call.start)
    fills = sorted(calls[FILL], key=lambda call: call.start)
    maps = sorted(calls[GROUP_MAP], key=lambda call: call.start)
    unmaps = sorted(calls[GROUP_UNMAP], key=lambda call: call.start)
    fill_starts, map_starts, unmap_starts = ([c.start for c in path] for path in (fills, maps, unmaps))
    if len(fills) != len(moves):
        raise ValueError("Incomplete mapped-buffer cursor updates")
    bytes_by_start = {}
    for fill, move in zip(fills, moves):
        if (fill.end > move.tick or fill.item != move.item or not fill.output or
                fill.output > move.output):
            raise ValueError("Invalid cursor update after emitter fill")
        bytes_by_start[fill.start] = move
    for group in groups:
        selected = contained(group, fills, fill_starts)
        if not selected:
            continue
        opening, = contained(group, maps, map_starts)
        closing, = contained(group, unmaps, unmap_starts)
        if (opening.end > selected[0].start or opening.eax != selected[0].output or
                bytes_by_start[selected[-1].start].tick > closing.start):
            raise ValueError("Fill range falls outside its mapped-buffer lifetime")
        for before, after in zip(selected, selected[1:]):
            move = bytes_by_start[before.start]
            if move.tick > after.start or move.output != after.output:
                raise ValueError("Mapped-buffer cursors are not contiguous")
        yield [(fill, bytes_by_start[fill.start]) for fill in selected]


def measure_frames(starts, calls, advances):
    groups = SCENE["window_calls"](calls, starts)
    moves = [[] for _ in groups]
    for move in advances:
        index = bisect_right(starts, move.tick) - 1
        if 0 <= index < len(groups):
            moves[index].append(move)
    previous_lengths = {}
    for begin, end, calls, moves_in_frame in zip(starts, starts[1:], groups, moves):
        if not any(calls[site] for site in DISPATCH):
            raise ValueError("Missing dispatcher in a complete frame")
        batches = list(plan_batches(calls, moves_in_frame))
        if sum(map(len, batches)) != len(calls[FILL]):
            raise ValueError("Some mapped fills are outside an original batch")
        sizes = {}
        compared = matches = zeroes = grouped_quad_ns = 0
        quads = sorted(calls[QUADS], key=lambda call: call.start)
        quad_starts = [call.start for call in quads]
        for batch in batches:
            for fill, move in batch:
                vertex_bytes = move.output - fill.output
                index_bytes = move.index_bytes
                zeroes += vertex_bytes == 0
                size = (vertex_bytes, index_bytes)
                samples = sizes.setdefault(fill.item, [])
                previous = previous_lengths.get(fill.item, [])
                index = len(samples)
                if index < len(previous):
                    compared += 1
                    matches += size == previous[index]
                samples.append(size)
                grouped_quad_ns += sum(q.duration for q in contained(fill, quads, quad_starts))
        previous_lengths = sizes
        quad_ns = sum(q.duration for q in quads)
        saving = sum(SCENE["batch_saving"]([call for call, _ in batch], 8, 500) for batch in batches)
        yield {
            "producer_interval_ns": end - begin,
            **{f"call_{site:x}_ns": sum(call.duration for call in calls[site]) for site in sorted(CALLS)},
            "quad_batch_ns": quad_ns,
            "quad_batch_inside_mapped_fill_ns": grouped_quad_ns,
            "mapped_groups": len(batches),
            "mapped_fills": len(calls[FILL]),
            "zero_vertex_byte_fills": zeroes,
            "largest_mapped_group": max(map(len, batches), default=0),
            "mapped_vertex_bytes": sum(move.output - fill.output for batch in batches for fill, move in batch),
            "logical_index_bytes": sum(move.index_bytes for batch in batches for _, move in batch),
            "compared_sizes_with_previous_frame": compared,
            "unchanged_sizes_from_previous_frame": matches,
            "optimistic_8_worker_500ns_delivery_saving_ns": saving,
        }


def summarize(root, guest):
    artifacts, guest_sha, verified = SCENE["verify_trial"](root, guest)
    expected = {**{pc: hex(target) for pc, target in DIRECT.items()}, **INDIRECT}
    instructions = {pc: (op, operands.strip())
                    for pc, (op, operands) in PHASE["GUEST"](guest, CALLS | {ADVANCE - 2, ADVANCE, FILL_TAIL}).items()}
    expected_instructions = {
        **{pc: ("calll", operands) for pc, operands in expected.items()},
        ADVANCE - 2: ("addl", "%eax, %esi"),
        ADVANCE: ("movl", "-0xe0(%ebp), %eax"),
        FILL_TAIL: ("jmp", "0x882f20"),
    }
    if instructions != expected_instructions:
        raise ValueError("Original particle call sites differ")
    capture = root / "phase"
    calls, advances, first, last, counters = read_paths(capture)
    window = json.loads((capture / "window.json").read_text())
    by_end, _ = LIFE["render_submissions"](capture / "submissions.ns", window["pid"])
    frames, _ = LIFE["read_frames"](capture, window, by_end, False)
    starts = [frame.final.submit for frame in frames if first <= frame.final.submit <= last]
    if len(starts) < 2:
        raise ValueError("No overlap with verified producer intervals")
    rows = list(measure_frames(starts, calls, advances))
    return {
        "artifact_sha256": artifacts, "guest_image_sha256": guest_sha, "general_i386_checks": 805,
        "diagnostic_gpu_window": verified, "trace": counters, "joined_producer_intervals": len(rows),
        "metrics": {key: LIFE["distribution"]([row[key] for row in rows]) for key in rows[0]},
        "totals": {key: sum(row[key] for row in rows) for key in (
            "mapped_groups", "mapped_fills", "zero_vertex_byte_fills", "mapped_vertex_bytes",
            "logical_index_bytes", "compared_sizes_with_previous_frame", "unchanged_sizes_from_previous_frame",
        )},
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_root", type=Path)
    parser.add_argument("guest_binary", type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize(args.capture_root, args.guest_binary), indent=2))
