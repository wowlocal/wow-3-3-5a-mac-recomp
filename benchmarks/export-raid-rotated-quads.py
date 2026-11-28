"""Export paired rotated-quad inputs and complete original vertex packets."""

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
import hashlib
import json
import mmap
from pathlib import Path
import re
from runpy import run_path
from statistics import median
import struct


HERE = Path(__file__).resolve().parent
SCENE = run_path(str(HERE / "summarize-raid-scene-work.py"))
TAIL, LIFE = SCENE["TAIL"], SCENE["LIFE"]
LAYOUT = run_path(str(HERE / "summarize-raid-quad-layout.py"))
BEGIN, END = 0x881651, 0x881AAD
STRIDE, CAPACITY, STORE_WIDTHS = 256, 1 << 20, (12, 12, 4, 8)
ROW = struct.Struct("<QQQ58I")
CASE = struct.Struct("<63I")
U32 = 1 << 32


def packet_inputs_and_outputs(before, after, counts):
    if not before[4] or not after[4]:
        counts["pairs_missing_same_page_snapshot"] += 1
        return None
    if before[1:4] != after[1:4]:
        raise ValueError("The frame, context or cursor changed during the quad")
    addresses, steps = before[6:10], before[10:14]
    if (steps != after[10:14] or any((start + 4 * step) % U32 != end
                                    for start, step, end in zip(addresses, steps, after[6:10])) or
            (before[14] + 4) % U32 != after[14]):
        raise ValueError("The actual cursor did not advance by four vertices")
    counts["pairs_with_verified_cursor"] += 1
    frame, context, cursor = before[1:4]
    spans = []
    for stream, (address, step, width) in enumerate(zip(addresses, steps, STORE_WIDTHS)):
        # A zero-stride normal is rewritten with the same three words.
        vertices = 1 if stream == 1 and not step else 4
        spans.extend(LAYOUT["interval"](address + i * step, width) for i in range(vertices))
    spans.extend((LAYOUT["interval"](context + 0x218, 24), LAYOUT["interval"](cursor, 36)))
    readable = (
        LAYOUT["interval"](frame - 0x1C8, 0x1DC),
        LAYOUT["interval"](context + 0x10, 8),
        LAYOUT["interval"](0xF4AD00, 12), LAYOUT["interval"](before[36], 4),
    )
    if None in spans or None in readable:
        counts["pairs_with_wrapped_or_null_regions"] += 1
        return None
    spans.sort()
    overlaps = LAYOUT["overlaps"]
    if any(overlaps(lo, hi) for lo, hi in zip(spans, spans[1:])) or any(
        overlaps(read, write) for write in spans for read in readable
    ):
        counts["pairs_with_aliasing"] += 1
        return None
    if before[5] & 0xffc0 != 0x1f80 or before[37] != 0:
        counts["pairs_outside_default_float_controls"] += 1
        return None
    # The first eleven payload words participate in arithmetic. Normal and
    # color are opaque copies. Infinities in the old bounds compare normally.
    math_words = before[21:32]
    if any((word & 0x7f800000) == 0x7f800000 for word in math_words):
        counts["pairs_with_nonfinite_arithmetic_input"] += 1
        return None
    counts["zero_normal_stride_pairs"] += steps[1] == 0
    return CASE.pack(*before[21:36], *before[15:21], *after[21:57], *after[15:21])


def read_capture(folder, output):
    manifest = json.loads((folder / "packet-gates.json").read_text())
    if manifest["armed_ms"] != 160 or manifest["offsets"] != [2, 15, 28] or len(manifest["gates"]) != 3:
        raise ValueError("The three expected capture gates were not used")
    window = json.loads((folder / "window.json").read_text())
    by_end, _ = LIFE["render_submissions"](folder / "submissions.ns", window["pid"])
    frames, _ = LIFE["read_frames"](folder, window, by_end, False)
    submits = [frame.final.submit for frame in frames]
    counts, gates, layouts = Counter(), [], Counter()
    counts_for_full_frames = []
    previous_index, last_tick = 0, 0
    cpus = set()
    with (folder / "quad-packets.bin").open("rb") as file, mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ) as log:
        count, armed = struct.unpack_from("<QQ", log)
        if armed or len(log) != (CAPACITY + 1) * STRIDE or count != manifest["gates"][-1][1] or not 0 < count < CAPACITY:
            raise ValueError("Invalid quad snapshot ring")
        for start, stop in manifest["gates"]:
            if start != previous_index or not start < stop <= count:
                raise ValueError("The quad snapshot gate counts are discontinuous")
            pending, gate_counts, frame_counts = {}, Counter(), Counter()
            first_tick, = struct.unpack_from("<Q", log, (start + 1) * STRIDE + 8)
            final_tick, = struct.unpack_from("<Q", log, stop * STRIDE + 8)
            if not submits[0] < first_tick < final_tick < submits[-1]:
                raise ValueError("A quad gate falls outside the verified GPU outputs")
            full_frames = range(bisect_right(submits, first_tick) + 1, bisect_right(submits, final_tick))
            for index in range(start, stop):
                sequence, tick, cpu, *state = ROW.unpack_from(log, (index + 1) * STRIDE)
                pc, frame = state[:2]
                if sequence != index + 1 or tick < last_tick or not cpu or pc not in (BEGIN, END) or state[4] not in (0, 1):
                    raise ValueError(f"Invalid rotated-quad observation at record {index + 1}")
                cpus.add(cpu)
                last_tick = tick
                key = cpu, frame
                if pc == BEGIN:
                    if key in pending:
                        raise ValueError("Two overlapping invocations on the same emitter frame")
                    pending[key] = tick, state
                    continue
                entry = pending.pop(key, None)
                if entry is None:
                    gate_counts["exits_before_a_captured_entry"] += 1
                    continue
                entered, before = entry
                gate_counts["complete_begin_end_pairs"] += 1
                frame_index = bisect_right(submits, entered)
                if frame_index != bisect_right(submits, tick):
                    gate_counts["pairs_crossing_a_frame_submission"] += 1
                    continue
                packet = packet_inputs_and_outputs(before, state, gate_counts)
                if packet is not None:
                    output.write(packet)
                    layouts[tuple(before[10:14])] += 1
                    gate_counts["exported_replay_cases"] += 1
                    if frame_index in full_frames:
                        frame_counts[frame_index] += 1
            gate_counts["entries_without_captured_exit"] = len(pending)
            counts.update(gate_counts)
            counts_for_full_frames.extend(frame_counts[i] for i in full_frames)
            gates.append({"rows": stop - start, "producer_intervals_fully_inside_gate": len(full_frames),
                          **gate_counts})
            previous_index = stop
    if len(cpus) != 1 or not counts["exported_replay_cases"]:
        raise ValueError("No complete replay cases on the original rendering guest CPU")
    return {
        "gates": gates,
        "all_gate_counts": dict(counts),
        "verified_strides": [{"strides": stride, "cases": n} for stride, n in layouts.most_common()],
        "rotated_quads_per_fully_observed_producer_interval": counts_for_full_frames,
    }


def export(root, guest, case_file):
    accepted = json.loads((HERE / "results/raid-winemetalgl-defer-render-end-pgo-ab.json").read_text())
    shape = json.loads((HERE / "results/raid-aarchx-context-values.json").read_text())
    artifacts = json.loads((root / "artifact-sha256.json").read_text())
    sha = TAIL["QUEUED"]["sha"]
    if (sha(guest) != shape["guest_image_sha256"] or
            sha(root / "artifacts/candidate/ocerz") != artifacts["runtime_unsigned_sha256"] or
            sha(root / "artifacts/candidate/libwinemetalgl.dylib") != artifacts["renderer_sha256"] or
            artifacts["renderer_sha256"] != accepted["artifact_sha256"]["renderer_sha256_by_role"]["candidate"]):
        raise ValueError("Wrong game executable or diagnostic binaries")
    if not (root / "general.log").read_text().endswith("test_m32: 812 checks, 0 failed\n"):
        raise ValueError("The diagnostic runtime failed the general i386 checks")
    instructions = SCENE["PHASE"]["GUEST"](guest, (BEGIN, END))
    expected = {BEGIN: ("movss", "-0x2c(%ebp), %xmm0"), END: ("movl", "0x134(%esi), %ebx")}
    if {pc: (op, args.strip()) for pc, (op, args) in instructions.items()} != expected:
        raise ValueError("The rotated vertex path differs from the original code")
    captures = []
    with case_file.open("wb") as output:
        for i in (0, 1):
            folder = root / f"packet-{i}"
            verified, _ = TAIL["verify_capture"](folder, artifacts)
            captures.append({"verified_gpu_window": verified, **read_capture(folder, output)})
    count = sum(run["all_gate_counts"]["exported_replay_cases"] for run in captures)
    if case_file.stat().st_size != count * CASE.size:
        raise ValueError("Exported case file size differs from the validated records")
    full_frames = [n for run in captures for n in run["rotated_quads_per_fully_observed_producer_interval"]]
    if not full_frames or median(full_frames) <= 0:
        raise ValueError("Missing fully captured intervals for live batch sizing")
    return {"guest_image_sha256": shape["guest_image_sha256"], "artifact_sha256": artifacts,
            "general_i386_checks": 812, "captures": captures,
            "case_file_sha256": sha(case_file), "exported_replay_cases": count,
            "rotated_quads_per_full_interval": LIFE["distribution"](full_frames)}


def report(export_log, timed, sanitized, *, live_size=None):
    result = json.loads(export_log.read_text())
    isolation = json.loads((timed.parent / "native-isolation.json").read_text())
    if isolation != {"legacy_paused": True, "legacy_gpu_completions": 0}:
        raise ValueError("Expected the earlier client to remain paused during the native replay")
    witness = re.compile(r"^verified (\d+) quads, (\d+) distinct threads in the parallel check$", re.M)
    text = timed.read_text()
    for log in (text, sanitized.read_text()):
        row = witness.search(log)
        if not row or int(row[1]) != result["exported_replay_cases"] or int(row[2]) < 2:
            raise ValueError("Missing original vertex equality or native worker witness")
    timings = defaultdict(list)
    pattern = re.compile(r"^batch (\d+) run (\d+) scalar ([0-9.]+) neon ([0-9.]+) parallel ([0-9.]+) ns_per_quad$", re.M)
    for batch, order, scalar, neon, parallel in pattern.findall(text):
        timings[int(batch)].append({"order": int(order), "scalar": float(scalar), "neon": float(neon), "parallel": float(parallel)})
    if live_size is None:
        live_size = round(result["rotated_quads_per_full_interval"]["median"])
    if set(timings) != {256, 4096, 32768, live_size} or any([row["order"] for row in rows] != list(range(1, 8))
                                                           for rows in timings.values()):
        raise ValueError("Missing alternate-order native worker timings")
    result["verified_replay_modes"] = ["scalar", "neon", "parallel_neon", "address_and_undefined_behavior_sanitizer"]
    result["native_replay_isolation"] = isolation
    result["native_packet_timings"] = [
        {"quads": batch, "median_ns_per_quad": {mode: median(row[mode] for row in rows)
                                               for mode in ("scalar", "neon", "parallel")}, "runs": rows}
        for batch, rows in sorted(timings.items())
    ]
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    operations = parser.add_subparsers(dest="command", required=True)
    make = operations.add_parser("export")
    make.add_argument("capture_root", type=Path)
    make.add_argument("guest_binary", type=Path)
    make.add_argument("case_file", type=Path)
    stats = operations.add_parser("report")
    stats.add_argument("export_log", type=Path)
    stats.add_argument("timing_log", type=Path)
    stats.add_argument("sanitizer_log", type=Path)
    args = parser.parse_args()
    summary = export(args.capture_root, args.guest_binary, args.case_file) if args.command == "export" else report(
        args.export_log, args.timing_log, args.sanitizer_log)
    print(json.dumps(summary, indent=2))
