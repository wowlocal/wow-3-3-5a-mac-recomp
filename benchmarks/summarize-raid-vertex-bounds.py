"""Check the original i386 bounds updates against their recorded inputs."""

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
import json
from pathlib import Path
from runpy import run_path
from statistics import median
import struct


HERE = Path(__file__).resolve().parent
STACK = run_path(str(HERE / "summarize-raid-guest-native-stacks.py"))
WINDOW = STACK["GUEST"]["read_stack_window_bounds"]
ROWS = STACK["read_samples"]
VERIFY_WINDOW = STACK["READ_VERIFIED_WINDOW"]
SITES = ((0x881709, 0x881755), (0x8817F1, 0x88183D),
         (0x8818EC, 0x881934), (0x8819CF, 0x881A17))
EMISSION_RANGE = (0x8816DA, 0x881AA0)
PROBE_SITES = {pc: (group, edge) for group, pair in enumerate(SITES) for edge, pc in enumerate(pair)}
CAPACITY = 1 << 19
STRIDE = 128
RECORD = struct.Struct("<QQQ12I")
FLOAT = struct.Struct("<f")
BITS = struct.Struct("<I")


def unpack_float(word):
    return FLOAT.unpack(BITS.pack(word))[0]


def update_bounds(old, candidates):
    xyz = tuple(map(unpack_float, candidates))
    lower = [new if coord < unpack_float(prior) else prior
             for prior, new, coord in zip(old[:3], candidates, xyz)]
    upper = [new if coord > unpack_float(prior) else prior
             for prior, new, coord in zip(old[3:], candidates, xyz)]
    return (*lower, *upper)


def read_records(capture, first, end):
    raw = (capture / "vertex-bounds.bin").read_bytes()
    gate = json.loads((capture / "bounds-gate.json").read_text())
    count, enabled = struct.unpack_from("<QQ", raw)
    if len(raw) != (CAPACITY + 1) * STRIDE or enabled or count != gate["captured_rows"]:
        raise ValueError(f"Invalid probe header in {capture}")
    if not count:
        raise ValueError(f"Missing probe records in {capture}")

    def records():
        for index in range(max(0, count - CAPACITY), count):
            offset = (index % CAPACITY + 1) * STRIDE
            sequence, ticks, cpu, pc, bounds, frame, *words = RECORD.unpack_from(raw, offset)
            if sequence != index + 1 or not first <= ticks < end or pc not in PROBE_SITES or not cpu:
                raise ValueError(f"Invalid probe row {index} in {capture}")
            yield ticks, cpu, pc, bounds, frame, tuple(words[:3]), tuple(words[3:])

    return count, records()


def read_swaps(capture, first, end):
    result = []
    with (capture / "agl.ns").open() as stream:
        for line in stream:
            if not line.endswith("\n"):
                break
            start, stop = map(int, line.split())
            if first <= start < end:
                if stop < start or (result and start <= result[-1]):
                    raise ValueError(f"Invalid swap timing in {capture}")
                result.append(start)
    return result


def summarize(capture):
    verify = VERIFY_WINDOW(capture, 25)
    _, first, end = WINDOW(capture)
    total_rows, rows = read_records(capture, first, end)
    swaps = read_swaps(capture, first, end)
    pending = {}
    last_by_cpu = {}
    quad_by_cpu = {}
    by_group = Counter()
    active = Counter()
    changed_components = Counter()
    chains = Counter()
    chains_replay = Counter()
    quads = Counter()
    positions_by_swap = defaultdict(lambda: defaultdict(list))
    individual_changes_in_quads = 0
    ambiguous_frames = set()
    ambiguous_pairs = 0
    first_tick, last_tick = end, first
    unmatched_ends = 0
    for row in rows:
        ticks, cpu, pc, bounds, frame, candidates, state = row
        first_tick, last_tick = min(first_tick, ticks), max(last_tick, ticks)
        group, edge = PROBE_SITES[pc]
        key = (cpu, bounds, frame, group)
        if edge == 0:
            if key in pending:
                raise ValueError(f"Missing end or overlapping bounds update in {capture}")
            pending[key] = row
            continue
        start = pending.pop(key, None)
        if start is None:
            unmatched_ends += 1
            continue
        if start[0] > ticks or start[5] != candidates:
            raise ValueError(f"Unexpected bounds inputs at row for {pc:#x} in {capture}")
        expected = update_bounds(start[6], candidates)
        if expected != state:
            raise ValueError(f"Scalar replay differs from original bounds at {pc:#x} in {capture}")
        changed = sum(a != b for a, b in zip(start[6], state))
        changed_components[changed] += 1
        by_group[group] += 1
        begin_swap = bisect_right(swaps, start[0])
        end_swap = bisect_right(swaps, ticks)
        if begin_swap != end_swap:
            ambiguous_pairs += 1
            ambiguous_frames.update(range(begin_swap, end_swap + 1))
            last_by_cpu.pop(cpu, None)
            quad_by_cpu.pop(cpu, None)
            continue
        active[begin_swap] += 1

        previous = last_by_cpu.get(cpu)
        continuous = False
        if previous and previous[:3] == (bounds, frame, begin_swap) and previous[3] + 1 == group:
            edge_name = f"{group}->{group + 1}"
            chains[edge_name] += 1
            continuous = previous[4] == start[6]
            chains_replay[edge_name] += continuous
        last_by_cpu[cpu] = (bounds, frame, begin_swap, group, state)
        if group == 0:
            quad_by_cpu[cpu] = (start[6], changed, candidates)
        elif not continuous:
            quad_by_cpu.pop(cpu, None)
        elif cpu in quad_by_cpu:
            initial, individual_changes, positions = quad_by_cpu[cpu]
            positions += candidates
            if group == len(SITES) - 1:
                quads[sum(a != b for a, b in zip(initial, state))] += 1
                individual_changes_in_quads += individual_changes + changed
                positions_by_swap[begin_swap][(cpu, bounds)].append(positions)
                quad_by_cpu.pop(cpu)
            else:
                quad_by_cpu[cpu] = (initial, individual_changes + changed, positions)

    if not last_by_cpu or len(pending) + unmatched_ends > len(last_by_cpu) * 2:
        raise ValueError(f"Insufficient complete pairs in {capture}")
    if len(by_group) != len(SITES):
        raise ValueError(f"Expected all four groups in {capture}")

    full_frames = [
        i for i in range(1, len(swaps))
        if first_tick < swaps[i - 1] < swaps[i] < last_tick and i not in ambiguous_frames
    ]
    if len(full_frames) < 2:
        raise ValueError(f"Need two complete frames inside the probe window in {capture}")
    compared_quads = prior_positions = prior_stream = aligned_positions = 0
    for earlier, later in zip(full_frames, full_frames[1:]):
        if later != earlier + 1:
            continue
        for key, current in positions_by_swap[later].items():
            previous = positions_by_swap[earlier].get(key, [])
            compared_quads += len(current)
            aligned_positions += min(len(current), len(previous))
            prior_positions += sum(a == b for a, b in zip(current, previous))
            if current == previous:
                prior_stream += len(current)

    return {
        "diagnostic_gpu_window": verify,
        "legacy_paused": json.loads((capture / "legacy_witness.json").read_text())["legacy_paused"],
        "capture_start_offset_ns": first_tick - first,
        "capture_span_ns": last_tick - first_tick,
        "probe_rows_written": total_rows,
        "probe_rows_retained": min(total_rows, CAPACITY),
        "threads": len(last_by_cpu),
        "matched_updates": sum(by_group.values()),
        "updates_straddling_swap_timestamp": ambiguous_pairs,
        "unmatched_before_gate_closed": len(pending),
        "unmatched_after_gate_opened": unmatched_ends,
        "updates_by_group": {str(group + 1): by_group[group] for group in range(len(SITES))},
        "number_of_changed_components": dict(sorted(changed_components.items())),
        "adjacent_updates_to_same_bounds": dict(sorted(chains.items())),
        "adjacent_updates_with_continuous_bounds": dict(sorted(chains_replay.items())),
        "quads_by_number_of_final_changed_components": {n: quads[n] for n in range(7)},
        "individual_changed_components_in_complete_quads": individual_changes_in_quads,
        "complete_frame_intervals_between_first_and_last_probe": len(full_frames),
        "median_updates_per_complete_frame": median(active[i] for i in full_frames),
        "max_updates_per_complete_frame": max(active[i] for i in full_frames),
        "quad_positions_compared_to_previous_complete_frame": compared_quads,
        "quad_positions_aligned_by_address_and_ordinal": aligned_positions,
        "matching_quad_positions_by_address_and_ordinal": prior_positions,
        "quads_in_matching_position_streams_by_address": prior_stream,
    }


def coverage(capture):
    samples = ROWS(capture)
    denominator = sum(row.weight for row in samples)
    def weighted_share(ranges):
        return sum(row.weight for row in samples if any(a <= row.leaf < b for a, b in ranges)) / denominator
    return {
        "all_sampled_cpu_usec": denominator,
        "share_in_each_bounds_update": [weighted_share((site,)) for site in SITES],
        "share_in_all_four_bounds_updates": weighted_share(SITES),
        "share_in_vertex_emission_region": weighted_share((EMISSION_RANGE,)),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("captures", nargs="+", type=Path)
    parser.add_argument("--cpu-profile", action="append", default=[], type=Path)
    args = parser.parse_args()
    print(json.dumps({
        "bounds_regions": [list(map(hex, pair)) for pair in SITES],
        "vertex_emission_region": list(map(hex, EMISSION_RANGE)),
        "probe_launches": [summarize(path) for path in args.captures],
        "prior_cpu_profiles": [coverage(path) for path in args.cpu_profile],
    }, indent=2))


if __name__ == "__main__":
    main()
