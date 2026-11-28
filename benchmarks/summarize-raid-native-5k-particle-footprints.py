"""Check offset plans and memory dependencies before parallel particle fills."""

from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass, field
import hashlib
import json
import mmap
from pathlib import Path
from runpy import run_path
import struct


HERE = Path(__file__).resolve().parent
PLAN = run_path(str(HERE / "summarize-raid-particle-plans.py"))
VERIFY = run_path(str(HERE / "verify-raid-native-5k.py"))["verify"]
TRACE = run_path(str(HERE / "summarize-raid-scene-footprints.py"))
LIFE = PLAN["LIFE"]
LAYOUT_SIZE = 36
MATRIX_SIZE = 64
NORMAL_SIZE = 12
EMITTER_COUNT = 0x1C
EMITTER_BOUNDS = 0x218
BOUNDS_SIZE = 24
SHARED_SORT_WORD = 0xF4ACFC
LOCAL_ROTATES = {0x880855: ("rorl", "$0x1e, -0x70(%ebp)"),
                 0x881B82: ("rorl", "$0x1e, -0x200(%ebp)"),
                 0x881C9E: ("rorl", "$0x1e, -0x200(%ebp)")}
MEMORY = struct.Struct("<QQQI9I")
CAPACITY = 1 << 25
U32 = 1 << 32


def merge(spans):
    result = []
    for start, end in sorted(spans):
        if result and start <= result[-1][1]:
            result[-1] = result[-1][0], max(end, result[-1][1])
        else:
            result.append((start, end))
    return result


def intersects(first, second):
    i = j = 0
    while i < len(first) and j < len(second):
        a, b = first[i], second[j]
        if a[1] <= b[0]:
            i += 1
        elif b[1] <= a[0]:
            j += 1
        else:
            return True
    return False


def subtract(spans, owned):
    remaining = []
    for lo, hi in spans:
        for start, stop in owned:
            if stop <= lo:
                continue
            if start >= hi:
                break
            if lo < start:
                remaining.append((lo, start))
            lo = max(lo, stop)
            if lo >= hi:
                break
        if lo < hi:
            remaining.append((lo, hi))
    return remaining


@dataclass
class Job:
    cpu: int
    fill: object
    reads: set = field(default_factory=set)
    writes: set = field(default_factory=set)
    issues: Counter = field(default_factory=Counter)
    accesses: int = 0
    private_rotations: int = 0

    @property
    def start(self):
        return self.fill.quad_before[0]

    @property
    def stop(self):
        return self.fill.quad_after[0]

    def record(self, kind, pc, address, width, mode, esp):
        end = address + width
        root_sp = self.fill.quad_before[1][4]
        if (kind == 1 and mode == 4 and pc in LOCAL_ROTATES and width == 4
                and 0 < esp <= address < end <= root_sp):
            self.accesses += 1
            self.private_rotations += 1
            return
        if kind in (2, 5):
            self.issues["escaped_call_or_jump", pc] += 1
        if kind == 3 or (kind in (1, 5) and mode == 4):
            self.issues["unclassified_memory_operand", pc] += 1
        if kind not in (1, 5) or mode == 4:
            return
        if not 1 <= width <= 16 or not 0 < address < end <= U32 or mode not in (1, 2, 3):
            self.issues["invalid_memory_span", pc] += 1
            return
        self.accesses += 1
        if esp <= address and end <= root_sp:
            return
        if mode != 2:
            self.reads.add((address, end))
        if mode != 1:
            self.writes.add((address, end))


def count_waves(work, counts, key, quads_per_wave):
    pending, quads, reads, writes = 0, 0, [], []
    waves = []
    for task, generated_quads in work:
        if task is None:
            if pending:
                waves.append((pending, quads))
            pending, quads, reads, writes = 0, 0, [], []
            continue
        read, write = task
        if intersects(writes, read) or intersects(writes, write) or intersects(reads, write):
            waves.append((pending, quads))
            pending, quads, reads, writes = 0, 0, [], []
        pending += 1
        quads += generated_quads
        reads = merge(reads + read)
        writes = merge(writes + write)
    if pending:
        waves.append((pending, quads))
    multi = [(width, amount) for width, amount in waves if width > 1]
    counts[f"{key}_groups_with_multi_job_wave"] += bool(multi)
    counts[f"{key}_jobs_in_multi_job_waves"] += sum(width for width, _ in multi)
    counts[f"{key}_largest_wave"] = max(counts[f"{key}_largest_wave"], max((width for width, _ in waves), default=0))
    quads_per_wave[key].extend(amount for _, amount in multi)


def classify(job, counts, issues):
    counts["quad_jobs"] += 1
    counts["explicit_memory_accesses"] += job.accesses
    counts["classified_private_stack_rotations"] += job.private_rotations
    issues.update(job.issues)
    if job.issues or not job.accesses:
        counts["jobs_without_complete_memory_classification"] += 1
        return None, None, None

    a, b = job.fill.quad_before[1], job.fill.quad_after[1]
    emitter, layout, matrix, root_sp = a[0], a[2], a[3], a[4]
    args = [(root_sp, root_sp + 16)]
    read, write = merge(job.reads), merge(job.writes)
    original = subtract(read, args), write
    if (a[:5] != b[:5] or a[10:14] != b[10:14] or not all((emitter, layout, matrix, root_sp))
            or any(last != (first + step * job.fill.vertices) % U32
                   for first, step, last in zip(a[6:10], a[10:14], b[6:10]))):
        counts["jobs_with_unmodeled_layout_advance"] += 1
        return original, None, None
    layout_span = (layout, layout + LAYOUT_SIZE)
    matrix_span = (matrix, matrix + MATRIX_SIZE)
    counts["jobs_reading_layout"] += intersects(read, [layout_span])
    counts["jobs_reading_matrix"] += intersects(read, [matrix_span])
    if intersects(write, [matrix_span]) or intersects(write, args):
        counts["jobs_writing_matrix_or_argument_slots"] += 1
        return original, None, None
    owned = args + [layout_span, matrix_span]
    if a[11] == 0:
        counts["jobs_with_zero_stride_normal"] += 1
        if a[7] != b[7] or not a[7] or a[15:18] != b[15:18]:
            counts["jobs_with_changed_shared_normal_words"] += 1
            return original, None, None
        counts["jobs_with_equal_normal_words_at_boundaries"] += 1
        owned.append((a[7], a[7] + NORMAL_SIZE))
    owned = merge(owned)
    local_read, local_write = subtract(read, owned), subtract(write, owned)
    copied = local_read, local_write
    counts["jobs_with_modeled_private_inputs"] += 1
    output = (job.fill.entry[1], job.fill.end[1])
    count_field = (emitter + EMITTER_COUNT, emitter + EMITTER_COUNT + 4)
    bounds = (emitter + EMITTER_BOUNDS, emitter + EMITTER_BOUNDS + BOUNDS_SIZE)
    counts["jobs_writing_original_emitter_count"] += intersects(local_write, [count_field])
    counts["jobs_writing_original_emitter_bounds"] += intersects(local_write, [bounds])
    known = merge([output, count_field, bounds])
    extra = subtract(local_write, known)
    if extra:
        counts["jobs_writing_beyond_mapped_output_and_emitter_fields"] += 1
        counts["jobs_touching_shared_sort_state"] += intersects(extra, [(SHARED_SORT_WORD, SHARED_SORT_WORD + 4)])
        issues["write_outside_modeled_output_with_emitter_flags", a[18]] += 1
        return original, copied, None
    counts["jobs_with_writes_in_admission_ranges"] += 1
    return original, copied, copied


def check_gate(layout, memory, gate, submits, pcs, counts, issues, sizes, paths, failures, quads_per_wave):
    start, end = gate["layout"]
    first, = struct.unpack_from("<Q", layout, (start + 1) * PLAN["ROW"].size + 8)
    last, = struct.unpack_from("<Q", layout, end * PLAN["ROW"].size + 8)
    if not submits[0] < first < last < submits[-1]:
        raise ValueError("The layout gate is outside the verified producer window")
    complete = range(bisect_right(submits, first) + 1, bisect_right(submits, last))
    groups, jobs = [], []
    for cpu, group, tick in PLAN["mapped_groups"](layout, start, end, quad_layout=True):
        index = bisect_right(submits, tick)
        if index not in complete or index != bisect_right(submits, group.start):
            continue
        exact_before = sizes["nonempty_groups_with_exact_prefix_positions"]
        PLAN["check_group"](group, sizes, paths, failures)
        if sizes["nonempty_groups_with_exact_prefix_positions"] == exact_before:
            continue
        group_jobs = []
        for fill in group.fills:
            if (fill.quad_before is None) != (fill.quad_after is None):
                raise ValueError("Missing paired quad snapshots")
            job = Job(cpu, fill) if fill.quad_before else None
            if job:
                if not first <= job.start < job.stop <= last:
                    raise ValueError("The paired quad call has invalid timestamps")
                jobs.append(job)
            group_jobs.append(job)
        groups.append(group_jobs)

    jobs.sort(key=lambda task: task.start)
    if any(a.stop > b.start for a, b in zip(jobs, jobs[1:])):
        raise ValueError("Overlapping quad calls on the traced producer")
    start, end = gate["accesses"]
    index = 0
    previous = 0
    view = memoryview(memory)[(start + 1) * MEMORY.size:(end + 1) * MEMORY.size]
    try:
        for expected, row in enumerate(MEMORY.iter_unpack(view), start + 1):
            seq, tick, cpu, pc, address, width, mode, esp, _, *_, kind = row
            if seq != expected or tick < previous or not cpu or pc not in pcs or kind not in (1, 2, 3, 5):
                raise ValueError(f"Invalid memory ring record {expected}")
            previous = tick
            while index < len(jobs) and tick >= jobs[index].stop:
                index += 1
            if index < len(jobs) and jobs[index].start <= tick < jobs[index].stop:
                job = jobs[index]
                if cpu != job.cpu:
                    raise ValueError("A memory event belongs to another guest CPU inside the quad call")
                job.record(kind, pc, address, width, mode, esp)
    finally:
        view.release()
    for group in groups:
        modes = [classify(job, counts, issues) if job else (None, None, None) for job in group]
        for index, name in enumerate(("original_shared", "copied_inputs", "known_output_ranges")):
            count_waves([(mode[index], job.fill.vertices // 4 if job else 0)
                         for job, mode in zip(group, modes)], counts, name, quads_per_wave)
    return {"mapped_groups_checked": len(groups), "quad_calls": len(jobs), "memory_rows": end - start}


def read_case(case, pcs, baseline, artifacts):
    verified = VERIFY(case, case / "graphics-settings.lua", measurement_seconds=45)
    if (verified["graphics"]["issues"] or verified["graphics"]["actual"] != baseline["graphics"]["actual"]
            or verified["artifact_sha256"] != artifacts
            or (case / "color-only-msaa-mode.txt").read_text().split() != ["1", "0"]
            or (case / "pass-mode.txt").read_text().split() != ["2048", "1"]):
        raise ValueError("The diagnostic did not use the full native 5K fixture and selected renderer")
    window = verified["verified_gpu_window"]
    pid = window["pid"]
    by_end, _ = LIFE["render_submissions"](case / "submissions.ns", pid)
    frames, _ = LIFE["read_frames"](case, window, by_end, False)
    submits = [frame.final.submit for frame in frames]
    manifest = json.loads((case / "particle-memory-gates.json").read_text())
    if manifest["armed_ms"] != 160 or manifest["offsets"] != [3, 16, 29] or len(manifest["gates"]) != 3:
        raise ValueError("Unexpected particle diagnostic gates")
    counts, sizes, paths, failures, issues = (Counter() for _ in range(5))
    quads_per_wave = {key: [] for key in ("original_shared", "copied_inputs", "known_output_ranges")}
    reports = []
    with (case / "particle-plan.bin").open("rb") as plan, (case / "particle-memory.bin").open("rb") as raw:
        with mmap.mmap(plan.fileno(), 0, access=mmap.ACCESS_READ) as layout:
            with mmap.mmap(raw.fileno(), 0, access=mmap.ACCESS_READ) as memory:
                layout_count, layout_arm, busy = struct.unpack_from("<QQQ", layout)
                memory_count, memory_arm = struct.unpack_from("<QQ", memory)
                if (layout_arm or memory_arm or not 0 < layout_count < PLAN["CAPACITY"]
                        or not 0 < memory_count < CAPACITY or len(memory) != MEMORY.size * (CAPACITY + 1)
                        or len(layout) != PLAN["ROW"].size * (PLAN["CAPACITY"] + 1)):
                    raise ValueError("The particle rings are armed, incomplete or overwritten")
                lo = hi = 0
                for gate in manifest["gates"]:
                    a, b = gate["layout"]
                    c, d = gate["accesses"]
                    if not lo == a < b <= layout_count or not hi == c < d <= memory_count:
                        raise ValueError("The two ring index ranges are discontinuous")
                    reports.append(check_gate(layout, memory, gate, submits, pcs, counts, issues,
                                              sizes, paths, failures, quads_per_wave))
                    lo, hi = b, d
                if (lo, hi) != (layout_count, memory_count):
                    raise ValueError("The last diagnostic gate did not cover the recorded buffer")
    if not counts["quad_jobs"]:
        raise ValueError("No complete particle geometry calls matched a GPU producer interval")
    return {"verified_gpu_window": window, "gates": reports, "snapshot_busy": busy, "counts": counts,
            "quads_in_multi_job_waves": {key: {"waves": len(quads), **LIFE["distribution"](quads)}
                                          for key, quads in quads_per_wave.items() if quads},
            "admitted_wave_quad_histogram": dict(sorted(Counter(quads_per_wave["known_output_ranges"]).items())),
            "validated_size_counts": sizes, "prediction_paths": paths, "prediction_failures": sum(failures.values()),
            "uncovered_operations": [{"kind": kind, "guest_pc": hex(pc), "events": n}
                                     for (kind, pc), n in issues.most_common(20)
                                     if kind != "write_outside_modeled_output_with_emitter_flags"],
            "emitter_flags_with_unmodeled_writes": {hex(flags): n for (kind, flags), n in issues.items()
                                                   if kind == "write_outside_modeled_output_with_emitter_flags"}}


def summarize(root, binary):
    baseline = json.loads((HERE / "results/raid-native-5k-max-baseline.json").read_text())
    reference = json.loads((HERE / "results/raid-native-5k-guest-profile.json").read_text())
    output = root / "particle-5k"
    artifacts = json.loads((output / "artifact-sha256.json").read_text())
    restored = json.loads((output / "restored-runtime.json").read_text())
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    if (digest != reference["guest_image_sha256"]
            or restored != reference["restored_runtime_sha256"]
            or artifacts["renderer_sha256"] != reference["artifact_sha256"]["renderer_sha256"]
            or not (output / "general.log").read_text().endswith("test_m32: 812 checks, 0 failed\n")):
        raise ValueError("The guest image, runtime, test result or selected renderer differs")
    closure = {"mapped_quad_generator": (0x882C80, 6)}
    pcs, info = TRACE["instruction_table"](binary, closure)
    table = struct.pack(f"<{len(pcs)}I", *pcs)
    if table != (output / "particle-pcs.bin").read_bytes():
        raise ValueError("The original i386 instruction closure differs from the recorder table")
    expected = {**PLAN["EXPECTED"], **LOCAL_ROTATES}
    decoded = {key: (mnemonic, operands.strip()) for key, (mnemonic, operands)
               in PLAN["SCENE"]["PHASE"]["GUEST"](binary, expected).items()}
    if decoded != expected:
        raise ValueError("The original map, culling or geometry call sites differ")
    runs = [read_case(root / f"native-5k-particle-memory-{index}", set(pcs), baseline, artifacts)
            for index in (1, 2)]
    return {"viewport": baseline["viewport"], "multisample_count": baseline["multisample_count"],
            "verified_settings": baseline["graphics"]["actual"], "artifact_sha256": artifacts,
            "restored_runtime_sha256": restored, "guest_image_sha256": digest,
            "general_i386_checks": 812, "pc_table_sha256": hashlib.sha256(table).hexdigest(),
            "closures": info, "captures": runs}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_root", type=Path)
    parser.add_argument("i386_executable", type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize(args.capture_root, args.i386_executable), indent=2))
