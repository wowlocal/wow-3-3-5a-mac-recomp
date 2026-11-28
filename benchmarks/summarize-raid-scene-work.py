"""Measure the work inside the first scene root and bound update batching."""

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
from heapq import heapreplace
import json
import mmap
from pathlib import Path
from runpy import run_path
from statistics import mean
import struct
from typing import NamedTuple

from metal_submission_log import interval_coverage


HERE = Path(__file__).resolve().parent
PHASE = run_path(str(HERE / "summarize-raid-tail-phases.py"))
TAIL, LIFE = PHASE["TAIL"], PHASE["LIFE"]
ROOT, PASS = 0x721DC3, 0x73FDD9
UPDATE, CONSUMER = 0x9806AE, 0x9801A7
SORTS = (0x9800F8, 0x98011B, 0x98013E)
CONTEXT_CALL = 0x97E6F9
MODE_CALLS = (0x980165, 0x980175)
PASS_CALLS = (UPDATE, CONSUMER, CONTEXT_CALL, *SORTS, *MODE_CALLS)
SITES = {
    ROOT: 0x73F8E0, PASS: 0x97E390,
    0x73FCF3: 0x738670, 0x73FDCA: 0x979EF0, 0x73FDF6: 0x737340,
    0x73FE15: 0x738830, 0x74010B: 0x2843E0, 0x7401EF: 0x73DA80,
    UPDATE: 0x964CA0, CONSUMER: 0x967F20, CONTEXT_CALL: 0x96A240,
    **dict.fromkeys(SORTS, 0x957790), **dict.fromkeys(MODE_CALLS, 0x977000),
}
RETURNS = {call + 5: call for call in SITES}
STRIDE, CAPACITY = 64, 1 << 22
RECORD = struct.Struct("<QQQI8I")


class Call(NamedTuple):
    site: int
    start: int
    end: int
    item: int

    @property
    def duration(self):
        return self.end - self.start


def read_boundary_records(path, gate):
    latest = {}
    with path.open("rb") as source:
        with mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as trace:
            count, armed = struct.unpack_from("<QQ", trace)
            if armed or not 0 < count == gate["rows"] < CAPACITY or len(trace) != (CAPACITY + 1) * STRIDE:
                raise ValueError("Incomplete scene-work trace")
            for index in range(count):
                row = RECORD.unpack_from(trace, (index + 1) * STRIDE)
                sequence, tick, cpu = row[:3]
                if sequence != index + 1 or not cpu or tick < latest.get(cpu, 1):
                    raise ValueError(f"Invalid event {index + 1}")
                latest[cpu] = tick
                yield row[1:]


def load_calls(capture):
    gate = json.loads((capture / "phase-gate.json").read_text())
    calls, pending = defaultdict(list), {}
    latest, unpaired = {}, Counter()
    first = None
    for tick, cpu, pc, _, ebx, _, _, _, _, ebp, esp in read_boundary_records(capture / "root-work.bin", gate):
        latest[cpu] = tick
        first = tick if first is None else min(first, tick)
        if pc in SITES:
            key = cpu, pc, ebp, esp
            if key in pending:
                raise ValueError(f"Repeated call with no return at {pc:#x}")
            pending[key] = tick, ebx
        else:
            site = RETURNS[pc]
            entry = pending.pop((cpu, site, ebp, esp), None)
            if entry is None:
                unpaired[pc] += 1
                continue
            start, item = entry
            if tick < start or (site in (UPDATE, CONSUMER) and (not item or item != ebx)):
                raise ValueError(f"Invalid return at {pc:#x}")
            calls[cpu].append(Call(site, start, tick, item))
    active = [entries for entries in calls.values() if any(entry.site == ROOT for entry in entries)]
    if len(active) != 1:
        raise ValueError("Expected scene work on one calling CPU")
    report = {
        **gate, "calling_cpus": len(latest),
        "unpaired_entries_by_pc": {hex(pc): n for pc, n in Counter(key[1] for key in pending).items()},
        "unpaired_returns_by_pc": {hex(pc): n for pc, n in sorted(unpaired.items())},
    }
    return active[0], first, max(latest.values()), report


def window_calls(calls, starts):
    windows = [defaultdict(list) for _ in starts[:-1]]
    for call in calls:
        index = bisect_right(starts, call.start) - 1
        if 0 <= index < len(windows):
            if call.end > starts[index + 1]:
                raise ValueError(f"Scene call {call.site:#x} crosses producer submissions")
            windows[index][call.site].append(call)
    return windows


def batch_saving(updates, workers, delivery_ns):
    # In this optimistic model the caller can enumerate the observed update
    # inputs without waiting for results. It preserves time between calls and
    # waits for every result at the end of the original update pass.
    if not updates:
        return 0
    available = [0] * workers
    ready = 0
    previous_end = updates[0].start
    for update in updates:
        if update.start < previous_end:
            raise ValueError("Overlapping update calls")
        ready += update.start - previous_end + delivery_ns
        heapreplace(available, max(available[0], ready) + update.duration)
        previous_end = update.end
    return updates[-1].end - updates[0].start - max(available)


def covered(group):
    return sum(call.duration for call in group)


def work_per_frame(starts, windows):
    for begin, end, calls in zip(starts, starts[1:], windows):
        root, = calls[ROOT]
        render_pass, = calls[PASS]
        if not begin <= root.start <= render_pass.start <= render_pass.end <= root.end <= end:
            raise ValueError("Scene pass does not nest in the first root")
        inside = [call for site in PASS_CALLS for call in calls[site]]
        if any(not render_pass.start <= call.start <= call.end <= render_pass.end for call in inside):
            raise ValueError("Call outside the scene pass")
        updates, consumers = sorted(calls[UPDATE], key=lambda call: call.start), calls[CONSUMER]
        if updates and consumers and updates[-1].end > min(call.start for call in consumers):
            raise ValueError("Updates are not a discrete pass before consumer calls")
        updated = {call.item for call in updates}
        sites = {f"call_{site:x}_ns": covered(calls[site]) for site in SITES}
        sort_ns = sum(covered(calls[site]) for site in SORTS)
        update_ns = covered(updates)
        explained_pass = interval_coverage((call.start, call.end) for call in inside)
        batch_saving_500ns = batch_saving(updates, 8, 500)
        yield {
            **sites, "producer_interval_ns": end - begin,
            "producer_outside_first_root_ns": end - begin - root.duration,
            "scene_pass_other_ns": render_pass.duration - explained_pass(render_pass.start, render_pass.end),
            "three_sorts_ns": sort_ns, "update_calls": len(updates),
            "consumer_calls": len(consumers), "unique_updated_items": len(updated),
            "consumer_calls_using_an_updated_item": sum(call.item in updated for call in consumers),
            "ideal_update_removal_producer_ns": end - begin - update_ns,
            "ideal_pass_removal_producer_ns": end - begin - render_pass.duration,
            "optimistic_4_worker_update_saving_ns": batch_saving(updates, 4, 0),
            "optimistic_8_worker_update_saving_ns": batch_saving(updates, 8, 0),
            "optimistic_8_worker_500ns_delivery_update_saving_ns": batch_saving_500ns,
            "optimistic_8_worker_500ns_delivery_producer_ns": end - begin - batch_saving_500ns,
        }


def verify_trial(root, guest):
    artifacts = json.loads((root / "artifact-sha256.json").read_text())
    results = HERE / "results"
    sha = TAIL["QUEUED"]["sha"]
    selected = json.loads((results / "raid-winemetalgl-defer-render-end-pgo-ab.json").read_text())
    guest_sha = json.loads((results / "raid-aarchx-i386-item-memory.json").read_text())["guest_image_sha256"]
    if (sha(guest) != guest_sha or
            sha(root / "artifacts/candidate/ocerz") != artifacts["runtime_unsigned_sha256"] or
            sha(root / "artifacts/candidate/libwinemetalgl.dylib") != artifacts["renderer_sha256"] or
            artifacts["renderer_sha256"] != selected["artifact_sha256"]["renderer_sha256_by_role"]["candidate"]):
        raise ValueError("Wrong original client, runtime or selected renderer")
    if not (root / "general.log").read_text().endswith("test_m32: 805 checks, 0 failed\n"):
        raise ValueError("The diagnostic runtime did not pass the general i386 checks")
    verified, _ = TAIL["verify_capture"](root / "phase", artifacts)
    return artifacts, guest_sha, verified


def summarize(root, guest):
    artifacts, guest_sha, verified = verify_trial(root, guest)
    instructions = PHASE["GUEST"](guest, SITES.keys())
    if {pc: ("calll", hex(target)) for pc, target in SITES.items()} != {
        pc: (op, operands.strip()) for pc, (op, operands) in instructions.items()
    }:
        raise ValueError("Original guest call sites differ")
    capture = root / "phase"
    calls, first, last, counters = load_calls(capture)
    window = json.loads((capture / "window.json").read_text())
    by_end, _ = LIFE["render_submissions"](capture / "submissions.ns", window["pid"])
    frames, _ = LIFE["read_frames"](capture, window, by_end, False)
    selected = [frame for frame in frames if first <= frame.final.submit <= last]
    if len(selected) < 2:
        raise ValueError("Scene work and verified Metal frames do not overlap")
    submits = [frame.final.submit for frame in selected]
    groups = window_calls(calls, submits)
    rows = list(work_per_frame(submits, groups))
    return {
        "artifact_sha256": artifacts, "guest_image_sha256": guest_sha, "general_i386_checks": 805,
        "diagnostic_gpu_window": verified, "trace": counters, "joined_producer_intervals": len(rows),
        "recorded_call_counts": {hex(site): sum(len(group[site]) for group in groups) for site in SITES},
        "metrics": {key: LIFE["distribution"]([row[key] for row in rows]) for key in rows[0]},
        "optimistic_producer_throughput_if_cost_removed": {
            burden: 1e9 / mean(row[burden] for row in rows)
            for burden in ("producer_interval_ns", "optimistic_8_worker_500ns_delivery_producer_ns",
                           "ideal_update_removal_producer_ns",
                           "ideal_pass_removal_producer_ns", "producer_outside_first_root_ns")
        },
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_root", type=Path)
    parser.add_argument("guest_binary", type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize(args.capture_root, args.guest_binary), indent=2))
