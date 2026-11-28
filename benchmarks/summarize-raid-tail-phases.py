"""Join the two scene preparation roots to completion gaps on the selected renderer."""

import argparse
from collections import Counter, defaultdict
import json
import mmap
from pathlib import Path
from runpy import run_path
import struct

from metal_submission_log import interval_coverage


HERE = Path(__file__).resolve().parent
TAIL = run_path(str(HERE / "summarize-raid-deferred-tail.py"))
LIFE = TAIL["LIFE"]
GUEST = run_path(str(HERE / "summarize-raid-guest-native-stacks.py"))["guest_instructions"]
CALLS = {address: 0x97DBC0 for address in (
    0x28434C, 0x284361, 0x61C512, 0x61C57A, 0x61C5A5, 0x61C6C7, 0x61C6E1,
    0x65991D, 0x65992D, 0x6BF92C, 0x6BF93E, 0x8036B3, 0x8036D3,
)}
CALLS[0x721DC3] = 0x73F8E0
RETURNS = {call + 5: call for call in CALLS}
SITES = CALLS.keys() | RETURNS.keys()
CAPACITY = 1 << 20
STRIDE = 64
RECORD = struct.Struct("<QQQI8I")


def phase_intervals(capture):
    gate = json.loads((capture / "phase-gate.json").read_text())
    pending = defaultdict(list)
    completed = defaultdict(lambda: defaultdict(list))
    last_by_cpu = {}
    counts = Counter()
    unpaired_returns = Counter()
    first = None
    last = None
    with (capture / "phase-calls.bin").open("rb") as source, mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as raw:
        count, enabled = struct.unpack_from("<QQ", raw)
        if len(raw) != (CAPACITY + 1) * STRIDE or enabled or not 0 < count < CAPACITY or count != gate["rows"]:
            raise ValueError("Incomplete phase trace")
        for index in range(count):
            sequence, tick, cpu, pc, *registers = RECORD.unpack_from(raw, (index + 1) * STRIDE)
            if sequence != index + 1 or not cpu or tick < last_by_cpu.get(cpu, 0) or pc not in SITES:
                raise ValueError(f"Invalid recorded call boundary {index}")
            first = tick if first is None else min(first, tick)
            last = tick if last is None else max(last, tick)
            last_by_cpu[cpu] = tick
            caller_frame = registers[6]
            if pc in CALLS:
                pending[cpu, pc, caller_frame].append(tick)
                counts["entries"] += 1
            else:
                call = RETURNS[pc]
                stack = pending[cpu, call, caller_frame]
                counts["returns"] += 1
                if stack:
                    start = stack.pop()
                    if start == tick:
                        raise ValueError("Invalid zero-length preparation call")
                    completed[cpu][CALLS[call]].append((start, tick))
                else:
                    counts["unpaired_returns"] += 1
                    unpaired_returns[pc] += 1
    counts["unpaired_entries"] = sum(map(len, pending.values()))
    roots_on_one_cpu = [group for group in completed.values() if set(group) == {0x73F8E0, 0x97DBC0}]
    if len(roots_on_one_cpu) != 1:
        raise ValueError("Expected both preparation roots on one calling CPU")
    return roots_on_one_cpu[0], first, last, {
        **counts, "guest_cpus": len(completed), "armed_seconds": gate["armed_seconds"],
        "unpaired_return_records_by_pc": {hex(pc): count for pc, count in sorted(unpaired_returns.items())},
    }


def summary(root, guest):
    baseline = json.loads((HERE / "results/raid-winemetalgl-defer-render-end-pgo-ab.json").read_text())
    runtime = json.loads((HERE / "results/raid-aarchx-i386-item-memory.json").read_text())
    artifacts = json.loads((root / "artifact-sha256.json").read_text())
    sha = TAIL["QUEUED"]["sha"]
    if (sha(guest) != runtime["guest_image_sha256"] or
            sha(root / "artifacts/candidate/ocerz") != artifacts["runtime_unsigned_sha256"] or
            sha(root / "artifacts/candidate/libwinemetalgl.dylib") != artifacts["renderer_sha256"] or
            artifacts["renderer_sha256"] != baseline["artifact_sha256"]["renderer_sha256_by_role"]["candidate"]):
        raise ValueError("Wrong original client, instrumented runtime or selected renderer")
    if not (root / "general.log").read_text().endswith("test_m32: 805 checks, 0 failed\n"):
        raise ValueError("The instrumented runtime did not pass the general i386 checks")
    instructions = GUEST(guest, set(CALLS))
    if {pc: ("calll", hex(target)) for pc, target in CALLS.items()} != {
        pc: (op, arguments.strip()) for pc, (op, arguments) in instructions.items()
    }:
        raise ValueError("Original preparation call sites differ")
    capture = root / "phase"
    verified, _ = TAIL["verify_capture"](capture, artifacts)
    window = json.loads((capture / "window.json").read_text())
    spans, first, last, counts = phase_intervals(capture)
    by_end, by_stream = LIFE["render_submissions"](capture / "submissions.ns", window["pid"])
    frames, _ = LIFE["read_frames"](capture, window, by_end, False)
    selected = [frame for frame in frames if first <= frame.final.submit < last]
    if len(selected) < 2:
        raise ValueError("Preparation trace and Metal submissions have no common window")
    submissions = by_stream[selected[0].final.stream]
    workers = TAIL["worker_timings"](capture, [row.final for row in selected])
    tracked = interval_coverage(spans[0x73F8E0] + spans[0x97DBC0])
    root_73 = interval_coverage(spans[0x73F8E0])
    intervals = LIFE["explain_intervals"](selected, False, submissions)
    for row, before, after in zip(intervals, selected, selected[1:]):
        TAIL["add_worker_phase"](row, before, after, workers)
        start, stop = before.final.submit, after.final.submit
        first_root = root_73(start, stop)
        second_root = tracked(start, stop) - first_root
        rest = stop - start - first_root - second_root
        parts = row["components"]
        del parts["final_submit_interval_ns"]
        additions = {"root_73f8e0_elapsed_ns": first_root, "root_97dbc0_outside_first_root_elapsed_ns": second_root,
                     "other_producer_interval_ns": rest}
        parts.update(additions)
        row.update(additions)
        row["combined_preparation_elapsed_ns"] = first_root + second_root
        if sum(parts.values()) != row["completion_gap_ns"]:
            raise ValueError("Preparation phases do not add up to the actual completion gap")
    return {"artifact_sha256": artifacts, "guest_image_sha256": runtime["guest_image_sha256"],
            "general_i386_checks": 805, "diagnostic_gpu_window": verified, "phase_trace_counters": counts,
            "calls_by_root": {hex(root): len(group) for root, group in spans.items()},
            "selected_outputs": len(selected), "phase_capture_span_ns": last - first, **LIFE["summarize"](intervals)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_root", type=Path)
    parser.add_argument("guest_binary", type=Path)
    args = parser.parse_args()
    print(json.dumps(summary(args.capture_root, args.guest_binary), indent=2))
