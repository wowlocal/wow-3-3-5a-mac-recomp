"""Check replay results and validate live guest quad buffer addresses and bytes."""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
from runpy import run_path
from statistics import median
import struct


HERE = Path(__file__).resolve().parent
VERIFY = run_path(str(HERE / "summarize-raid-renderer-pgo.py"))["verify"]
BOUNDS = run_path(str(HERE / "summarize-raid-vertex-bounds.py"))
STRIDE = 256
CAPACITY = 1 << 20
ROW = struct.Struct("<QQQ36I")
BEGIN, END = 0x8816DA, 0x881AAD
VERTICES = (0x881709, 0x8817F1, 0x8818EC, 0x8819CF)
WINDOWS = {BEGIN, END, *VERTICES}
LIMIT = 1 << 32
STORE_WIDTHS = (12, 12, 4, 8)


def read_gates(capture, start, end):
    raw = (capture / "quad-layout.bin").read_bytes()
    count, enabled = struct.unpack_from("<QQ", raw)
    if len(raw) != (CAPACITY + 1) * STRIDE or enabled or not 0 < count < CAPACITY:
        raise ValueError(f"Invalid quad ring in {capture}")
    gates = json.loads((capture / "prep-gates.json").read_text())
    if len(gates) != 3 or gates[0][0] or gates[-1][1] != count:
        raise ValueError(f"Invalid quad gate bounds in {capture}")
    previous = 0
    last_tick = start
    for first, after in gates:
        if first != previous or not first < after <= count:
            raise ValueError(f"Invalid row coverage in {capture}")
        rows = []
        for index in range(first, after):
            seq, tick, cpu, *data = ROW.unpack_from(raw, (index + 1) * STRIDE)
            if seq != index + 1 or not last_tick <= tick < end or not cpu or data[0] not in WINDOWS:
                raise ValueError(f"Invalid quad row {index} in {capture}")
            rows.append((cpu, data))
            last_tick = tick
        yield rows
        previous = after


def interval(start, size):
    if not 0 < start < start + size <= LIMIT:
        return None
    return start, start + size


def overlaps(first, other):
    return first[0] < other[1] and other[0] < first[1]


def layout(before, before_end, points, counts, strides):
    _, stack, cursor, context, color_source = before[:5]
    first_addresses = before[5:9]
    steps = before[9:13]
    after_addresses = before_end[5:9]
    strides[tuple(steps)] += 1
    counts["complete_quads"] += 1
    if (before[1:5] != before_end[1:5] or steps != before_end[9:13] or
            [(address + 4 * step) % LIMIT for address, step in zip(first_addresses, steps)] != after_addresses or
            (before[13] + 4) % LIMIT != before_end[13]):
        raise ValueError("Actual cursor changed differently from four serial vertex writes")

    normal_stable = before[14:17] == before_end[14:17]
    counts["normal_source_unchanged_at_edges"] += normal_stable
    counts["zero_normal_stride"] += not steps[1]
    regions = []
    for stream, (first, step, width) in enumerate(zip(first_addresses, steps, STORE_WIDTHS)):
        writes = [interval(first + i * step, width) for i in range(4)]
        if None in writes:
            counts["address_wrap_or_null"] += 1
            return
        if stream == 1 and not step:
            writes = writes[:1]
        regions.extend(writes)

    context_write = interval(context + 0x218, 24)
    cursor_write = interval(cursor, 36)
    readable = [interval(stack - 0x1FC, 0x210), interval(context + 0x10, 8),
                interval(0xF4AD00, 12), interval(color_source, 4)]
    mutable = [*regions, context_write, cursor_write]
    if None in mutable or None in readable:
        counts["address_wrap_or_null"] += 1
        return
    mutable.sort()
    alias = (any(overlaps(first, second) for first, second in zip(mutable, mutable[1:])) or
             any(overlaps(output, source) for output in mutable for source in readable))
    counts["disjoint_with_single_normal_write"] += not alias
    all_one_page = all((first >> 12) == ((first + 3 * step + size - 1) >> 12)
                       for first, step, size in zip(first_addresses, steps, STORE_WIDTHS))
    counts["each_stream_inside_one_guest_page"] += all_one_page
    counts["guard_ready_with_single_normal_write"] += not alias and all_one_page and normal_stable and not steps[1]

    for vertex, position in enumerate(points):
        offset = 20 + 4 * vertex
        address, *actual = before_end[offset:offset + 4]
        if not address:
            counts["position_load_guard_skips"] += 1
            continue
        if address != first_addresses[0] + vertex * steps[0]:
            raise ValueError("Readback used a different position address")
        counts["position_vertices_read_back"] += 1
        matches = actual == position
        counts["position_vertices_matching_registers"] += matches
        if not matches and not alias:
            raise ValueError("A disjoint buffer differs from the original position registers")


def summarize_gate(rows):
    pending = {}
    counts = Counter()
    strides = Counter()
    for cpu, data in rows:
        pc, stack = data[:2]
        key = cpu, stack
        if pc == BEGIN:
            if key in pending:
                counts["unpaired_begin"] += 1
            pending[key] = data, []
        elif pc == END:
            started = pending.pop(key, None)
            if not started or len(started[1]) != len(VERTICES):
                counts["unpaired_end"] += 1
                continue
            layout(started[0], data, started[1], counts, strides)
        elif key in pending:
            _, points = pending[key]
            if len(points) >= len(VERTICES) or pc != VERTICES[len(points)]:
                pending.pop(key)
                counts["unpaired_vertex"] += 1
                continue
            points.append(data[17:20])
    if not counts["complete_quads"] or not counts["position_vertices_read_back"]:
        raise ValueError("No whole quad with a verified memory readback")
    return {
        "probe_rows": len(rows),
        "counts": dict(sorted(counts.items())),
        "stream_strides_bytes": [{"strides": shapes, "quads": count} for shapes, count in strides.most_common()],
    }


def read_replay(log, count):
    rows = {"all": [], "hot": []}
    verified = False
    for line in log.read_text().splitlines():
        mode, *fields = line.split()
        if mode == "verified":
            verified = tuple(map(int, fields)) == (count, 32768)
            continue
        order, size, repetitions = map(int, fields[:3])
        scalar, neon = map(float, fields[3:])
        if order != len(rows[mode]) + 1 or size != (count if mode == "all" else 2048) or min(scalar, neon) <= 0:
            raise ValueError(f"Unexpected replay row in {log}")
        rows[mode].append({"case_count": size, "repetitions": repetitions, "scalar_ns": scalar, "neon_ns": neon})
    if not verified or any(len(group) != 8 for group in rows.values()):
        raise ValueError(f"Incomplete replay in {log}")
    return {mode: {"median_scalar_ns": median(row["scalar_ns"] for row in group),
                   "median_neon_ns": median(row["neon_ns"] for row in group),
                   "samples": group} for mode, group in rows.items()}


def summarize(root, guest_binary):
    baseline_path = HERE / "results" / "raid-aarchx-i386-item-preparation.json"
    baseline = json.loads(baseline_path.read_text())
    artifacts = json.loads((root / "artifact-sha256.json").read_text())
    instrumented_runtime = root / "artifacts" / "observe" / "ocerz"
    guest_hash = hashlib.sha256(guest_binary.read_bytes()).hexdigest()
    if guest_hash != baseline["guest_image_sha256"]:
        raise ValueError("Probe addresses require the profiled guest image")
    if hashlib.sha256(instrumented_runtime.read_bytes()).hexdigest() != artifacts["instrumented_runtime"]:
        raise ValueError("The instrumented runtime artifact changed")
    if artifacts["renderer"] != baseline["artifacts_sha256"]["renderer"]:
        raise ValueError("Wrong renderer for the full raid layout capture")
    exports = json.loads((root / "export.json").read_text())
    count = sum(capture["complete_quads"] for capture in exports)
    fixture = (root / "quads.bin").read_bytes()
    if len(fixture) != 96 * count or (root / "replay-sanitized.txt").read_text().split() != ["verified", str(count), "32768"]:
        raise ValueError("The replay or sanitizer verification is incomplete")
    captures = []
    for index in (1, 2):
        capture = root / f"layout-{index}"
        window = VERIFY(capture, "candidate", seconds=35)
        _, start, end = BOUNDS["WINDOW"](capture)
        captures.append({"verified_gpu_window": window,
                         "gates": [summarize_gate(rows) for rows in read_gates(capture, start, end)]})
    settings = ("hardware", "macos", "viewport", "vsync", "caster_count", "boss_count", "camera")
    return {
        **{name: baseline[name] for name in settings},
        "base_commit": "8df2820",
        "guest_image_sha256": guest_hash,
        "layout_artifacts_sha256": artifacts,
        "raw_replay_sha256": hashlib.sha256(fixture).hexdigest(),
        "archived_capture_exports": exports,
        "verified_archived_quads": count,
        "verified_edge_cases": 32768,
        "sanitizer_check_passed": True,
        "replay": {
            "explicit_simd_only": read_replay(root / "replay.txt", count),
            "with_compiler_vectorization": read_replay(root / "replay-autovec.txt", count),
        },
        "layout_captures": captures,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_root", type=Path)
    parser.add_argument("guest_binary", type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize(args.capture_root, args.guest_binary), indent=2))


if __name__ == "__main__":
    main()
