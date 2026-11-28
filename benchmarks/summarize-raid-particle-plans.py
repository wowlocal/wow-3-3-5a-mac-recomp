"""Compare post-map particle size plans with the original writes and cursor advances."""

from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass, field
import json
import mmap
from pathlib import Path
from runpy import run_path
import struct


HERE = Path(__file__).resolve().parent
SCENE = run_path(str(HERE / "summarize-raid-scene-work.py"))
TAIL, LIFE = SCENE["TAIL"], SCENE["LIFE"]
PLAN = 1
MAP, FILL, ADVANCE, UNMAP = 0x97C8C3, 0x97C9FB, 0x97CA30, 0x97C906
TEXTURE, QUAD, QUAD_RETURN = 0x88303B, 0x8831A9, 0x8831AE
SITE = {PLAN, MAP, FILL, ADVANCE, UNMAP, TEXTURE, QUAD, QUAD_RETURN}
ROW = struct.Struct("<QQQ26I")
CAPACITY = 1 << 22
EXPECTED = {
    MAP - 6: ("calll", "*0xdc(%edx)"),
    MAP: ("movl", "$0x0, -0xd8(%ebp)"),
    0x97C9A0: ("movl", "0x14(%ebp), %ebx"),
    0x97C9A3: ("addl", "-0xfc(%ebp), %ebx"),
    0x97C9CD: ("movl", "0x18(%eax,%edx), %edi"),
    FILL: ("calll", "0x8832a0"),
    0x97CA00: ("movl", "0x1c(%edi), %edx"),
    0x97CA0B: ("imull", "0x8c(%edi), %ecx"),
    0x97CA18: ("imull", "0x90(%edi), %edx"),
    ADVANCE - 2: ("addl", "%eax, %esi"),
    ADVANCE: ("movl", "-0xe0(%ebp), %eax"),
    UNMAP: ("calll", "*0xe0(%edx)"),
    0x8832E2: ("movl", "0x50(%ecx), %eax"),
    0x883324: ("movl", "0x98(%ecx), %eax"),
    0x882F2C: ("movl", "0xda2e00, %eax"),
    TEXTURE - 5: ("calll", "0x8955f0"),
    TEXTURE: ("testl", "%eax, %eax"),
    0x883043: ("movl", "0xd2ff58, %eax"),
    0x88304A: ("divl", "0x8c(%esi)"),
    0x883056: ("movl", "0x50(%esi), %eax"),
    0x88305F: ("cmovbl", "-0x110(%ebp), %eax"),
    QUAD: ("calll", "0x882c80"),
    QUAD_RETURN: ("movl", "-0x10c(%ebp), %eax"),
    0x882E0C: ("movl", "0x20(%edx), %eax"),
    0x882E11: ("divl", "0x8c(%ecx)"),
    0x89560A: ("testb", "$0x4, 0x28(%ebx)"),
    0x895610: ("movl", "0x40(%ebx), %esi"),
    0x895656: ("movl", "0x44(%ebx), %eax"),
    0x89569F: ("movl", "0x5c(%ebx), %eax"),
    0x8956A6: ("testb", "$0x1, 0x2(%eax)"),
    0x8956AC: ("movl", "0x18(%eax), %eax"),
    0x880C7F: ("movss", "0xd2f8b8, %xmm2"),
    0x881496: ("movss", "(%ecx), %xmm0"),
    0x88149A: ("mulss", "0x13c(%ebx), %xmm0"),
    0x8814B0: ("fistpl", "-0x28(%ebp)"),
    0x8814B6: ("shrl", "$0x5, %ecx"),
    0x8814BD: ("andl", "$0x7f, %ecx"),
    0x8814C6: ("movss", "0x140(%ebx), %xmm1"),
    0x8814D7: ("movss", "0xf4aae0(,%ecx,4), %xmm0"),
    0x8814E2: ("ucomiss", "%xmm1, %xmm0"),
    0x8814E5: ("ja", "0x88147c"),
}


@dataclass
class Fill:
    entry: tuple
    end: tuple | None = None
    texture: int | None = None
    selected: int | None = None
    vertices: int | None = None
    quad_before: tuple | None = None
    quad_after: tuple | None = None


@dataclass
class Group:
    start: int
    frame: int
    mapping: tuple
    plans: list = field(default_factory=list)
    fills: list = field(default_factory=list)
    active: Fill | None = None
    gap: bool = False


def predict(plan):
    if plan is None:
        return "unreadable_snapshot", None, None, None
    (_, stride, enabled, limit, live, mode, old, vp, ip, flags, shape, texture,
     texture_flags, pending, primary, fallback, fallback_flags, fallback_result) = plan[:18]
    if live == 0:
        return "empty_emitter", (0, 0), None, None
    if mode:
        return "retained_emitter_count", (old * vp * stride, old * ip), None, None
    if not enabled:
        return "particle_path_disabled", (0, 0), None, None
    if not texture:
        resource = 0
    elif texture_flags & 4:
        if pending:
            resource = 0
        elif not fallback:
            resource = primary
        elif not fallback_flags & 1:
            resource = fallback_result
        else:
            return "resource_resolution_required", None, None, None
    elif not pending:
        resource = primary
    else:
        return "resource_resolution_required", None, None, None
    if not resource:
        return "texture_result_zero", (0, 0), 0, None
    if not vp:
        return "invalid_vertex_factor", None, resource, None
    selected = min(live, limit // vp)
    contributing, cull_mode = plan[18:20]
    if cull_mode == 2:
        return "culling_requires_other_inputs", None, resource, selected
    if cull_mode not in (0, 1) or (cull_mode == 1 and contributing > selected):
        raise ValueError("Invalid culling prediction")
    vertices = 4 * (contributing if cull_mode == 1 else selected)
    path = "generated_with_culling_count" if cull_mode == 1 else "generated"
    return path, (vertices // vp * vp * stride, vertices // vp * ip), resource, selected


def check_group(group, counts, paths, failures):
    if group.active or group.gap:
        counts["groups_excluded_for_missing_boundaries_or_reads"] += 1
        return
    start, size, stride = group.mapping[:3]
    if len(group.fills) != size:
        raise ValueError("The original group did not visit its recorded emitter count")
    counts["complete_groups"] += 1
    counts["empty_groups"] += not size
    counts["groups_with_multiple_fills"] += size > 1
    if size and not start:
        raise ValueError("The complete group has a null original vertex mapping")
    if group.plans and len(group.plans) != size:
        raise ValueError("The planner omitted some original group members")
    actual_cursor = predicted_cursor = start
    group_exact = True
    group_vertex_bytes = group_index_bytes = 0
    for index, fill in enumerate(group.fills):
        emitter, before, original_stride = fill.entry[:3]
        item, after, index_bytes, output_count, vp, ip = fill.end[:6]
        if (not emitter or emitter != item or before != actual_cursor or before > after or stride != original_stride
                or after - before != output_count * vp * stride or index_bytes != output_count * ip):
            raise ValueError("Original mapped cursor or byte advances disagree")
        if (fill.vertices is None) != (fill.selected is None):
            raise ValueError("The original quad call and return were not paired")
        actual_cursor = after
        sizes = after - before, index_bytes
        group_vertex_bytes += sizes[0]
        group_index_bytes += sizes[1]
        counts["original_fills"] += 1
        plan = group.plans[index] if group.plans else None
        if plan is not None and (plan[0] != emitter or plan[1] != stride):
            raise ValueError("The pre-fill mapping plan changed emitter identity or stride")
        path, size_plan, texture, selected = predict(plan)
        paths[path] += 1
        if fill.selected is not None:
            counts["quad_calls_with_original_vertex_count"] += 1
            if fill.vertices == 4 * fill.selected:
                counts["quad_calls_with_four_vertices_per_selected_particle"] += 1
            elif path != "generated_with_culling_count" or fill.vertices != 4 * plan[18]:
                failures["quad_vertex_ratio", plan[9] if plan else 0,
                         plan[10] if plan else 0, fill.selected, fill.vertices] += 1
            if path == "generated_with_culling_count":
                counts["culling_count_checks"] += 1
                counts["culling_vertex_count_matches"] += fill.vertices == 4 * plan[18]
                counts["particles_removed_by_culling_plan"] += selected - plan[18]
        if texture is not None:
            counts["texture_results_checked"] += 1
            if texture != fill.texture:
                counts["texture_result_mismatches"] += 1
        if selected is not None:
            counts["size_clamp_checks"] += 1
            if selected != fill.selected:
                counts["size_clamp_mismatches"] += 1
        if size_plan is None:
            group_exact = False
            continue
        counts["fills_with_predicted_sizes"] += 1
        if size_plan != sizes:
            group_exact = False
            counts["fills_with_different_sizes"] += 1
            failures[("fill_sizes", path, plan[9], plan[10], *size_plan, *sizes)] += 1
            continue
        counts["fills_with_exact_sizes"] += 1
        if group_exact:
            if predicted_cursor != before:
                raise ValueError("An exact prefix disagrees with the next original fill position")
            predicted_cursor += size_plan[0]
    counts["mapped_vertex_bytes"] += group_vertex_bytes
    counts["logical_index_bytes"] += group_index_bytes
    if size and group_exact:
        if predicted_cursor != actual_cursor:
            raise ValueError("The original group ended at a different vertex cursor")
        counts["nonempty_groups_with_exact_prefix_positions"] += 1
        counts["fills_in_exact_groups"] += size
        counts["vertex_bytes_in_exact_groups"] += group_vertex_bytes
        counts["logical_index_bytes_in_exact_groups"] += group_index_bytes
        if size > 1:
            counts["exact_groups_with_multiple_fills"] += 1
            counts["fills_in_exact_groups_with_multiple_fills"] += size


def mapped_groups(raw, start, end, *, quad_layout=False):
    active, ticks = {}, {}
    for index in range(start, end):
        seq, tick, cpu, site, frame, ordinal, ready, *words = ROW.unpack_from(raw, (index + 1) * ROW.size)
        if (seq != index + 1 or not cpu or not ticks.get(cpu, 0) <= tick or site not in SITE
                or ready not in (0, 1)):
            raise ValueError(f"Invalid particle plan row {index + 1}")
        ticks[cpu] = tick
        group = active.get(cpu)
        if site == MAP:
            if group is not None:
                raise ValueError("Nested or unmatched original particle map")
            active[cpu] = Group(tick, frame, tuple(words), gap=not ready)
            continue
        if group is None:
            continue
        if site == PLAN:
            if group.frame != frame or ordinal != len(group.plans) or group.fills:
                raise ValueError("The planned emitters do not precede the mapped fills")
            group.plans.append(tuple(words) if ready else None)
        elif site == UNMAP:
            del active[cpu]
            if group.frame != frame:
                raise ValueError("Mapped group changed caller frame")
            group.gap |= not ready
            yield cpu, group, tick
        elif site == FILL:
            if group.frame != frame or group.active:
                raise ValueError("Unexpected mapped fill nesting")
            group.gap |= not ready
            if ready and not group.gap and ordinal != len(group.fills):
                raise ValueError("The original fill order changed")
            group.active = Fill(tuple(words))
        elif site == ADVANCE:
            if group.frame != frame or group.active is None:
                raise ValueError("An original mapped advance lacks its fill")
            group.gap |= not ready
            if ready and not group.gap and ordinal != len(group.fills):
                raise ValueError("The original advance order changed")
            group.active.end = tuple(words)
            group.fills.append(group.active)
            group.active = None
        elif group.active:
            group.gap |= not ready
            if ready and not group.gap:
                if words[0] != group.active.entry[0]:
                    raise ValueError("An original fill callback changed emitter")
                attr = {TEXTURE: "texture", QUAD: "selected", QUAD_RETURN: "vertices"}[site]
                if getattr(group.active, attr) is not None:
                    raise ValueError("Repeated callback inside one original fill")
                value = words[1]
                if quad_layout and site == QUAD:
                    group.active.quad_before = tick, tuple(words)
                elif quad_layout and site == QUAD_RETURN:
                    if group.active.quad_before is None:
                        raise ValueError("Quad-layout return lacks the call")
                    group.active.quad_after = tick, tuple(words)
                    value = words[14] - group.active.quad_before[1][14]
                    if value < 0:
                        raise ValueError("The recorded vertex counter moved backwards")
                setattr(group.active, attr, value)


def count_groups(raw, start, end, submits, counts, paths, failures):
    first, = struct.unpack_from("<Q", raw, (start + 1) * ROW.size + 8)
    last, = struct.unpack_from("<Q", raw, end * ROW.size + 8)
    if not submits[0] < first < last < submits[-1]:
        raise ValueError("A particle-plan gate fell outside verified GPU submissions")
    full_frames = range(bisect_right(submits, first) + 1, bisect_right(submits, last))
    matched_frames = set()
    for _, group, tick in mapped_groups(raw, start, end):
        interval = bisect_right(submits, tick)
        if interval in full_frames and interval == bisect_right(submits, group.start):
            check_group(group, counts, paths, failures)
            matched_frames.add(interval)
    return len(matched_frames)


def capture_report(folder, artifacts):
    verified, _ = TAIL["verify_capture"](folder, artifacts)
    window = json.loads((folder / "window.json").read_text())
    by_end, _ = LIFE["render_submissions"](folder / "submissions.ns", window["pid"])
    frames, _ = LIFE["read_frames"](folder, window, by_end, False)
    submits = [frame.final.submit for frame in frames]
    manifest = json.loads((folder / "particle-plan-gates.json").read_text())
    if manifest["armed_ms"] != 2000 or manifest["offsets"] != [2, 16, 30] or len(manifest["gates"]) != 3:
        raise ValueError("Unexpected particle plan capture gates")
    counts, paths, failures, gates = Counter(), Counter(), Counter(), []
    with (folder / "particle-plan.bin").open("rb") as file, mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ) as raw:
        count, armed, busy = struct.unpack_from("<QQQ", raw)
        if (len(raw) != (CAPACITY + 1) * ROW.size or armed or
                not 0 < count == manifest["gates"][-1][1] < CAPACITY):
            raise ValueError("The particle plan ring has missing or overwritten records")
        previous = 0
        for start, end in manifest["gates"]:
            if not previous == start < end <= count:
                raise ValueError("The particle plan gate ranges are discontinuous")
            matched = count_groups(raw, start, end, submits, counts, paths, failures)
            gates.append({"rows": end - start, "complete_producer_intervals_with_groups": matched})
            previous = end
    if not counts["original_fills"]:
        raise ValueError("No complete original fills were captured")
    return {
        "verified_gpu_window": verified,
        "gates": gates,
        "mapping_guard_busy": busy,
        "counts": counts,
        "prediction_paths": paths,
        "top_failure_inputs": [{"shape": shape, "count": n} for shape, n in failures.most_common(20)],
    }


def run_report(root, guest, checks):
    sha = TAIL["QUEUED"]["sha"]
    prior = json.loads((HERE / "results/raid-aarchx-particle-paths.json").read_text())
    artifacts = json.loads((root / "artifact-sha256.json").read_text())
    if (sha(guest) != prior["guest_image_sha256"] or
            sha(root / "artifacts/candidate/ocerz") != artifacts["runtime_unsigned_sha256"] or
            sha(root / "artifacts/candidate/libwinemetalgl.dylib") != artifacts["renderer_sha256"] or
            artifacts["renderer_sha256"] != prior["artifact_sha256"]["renderer_sha256"]):
        raise ValueError("Unexpected guest executable or trial binaries")
    if not (root / "general.log").read_text().endswith("test_m32: 812 checks, 0 failed\n"):
        raise ValueError("The general i386 checks failed")
    if checks and (root / "culling-math.log").read_text() != "Particle culling arithmetic: 1048576 comparisons, 0 failed\n":
        raise ValueError("Missing the guarded particle arithmetic comparisons")
    captures = [capture_report(root / f"phase-{mode}", artifacts) for mode in (0, 1)]
    total = sum((Counter(capture["counts"]) for capture in captures), Counter())
    return {
        "artifact_sha256": artifacts,
        "guest_image_sha256": prior["guest_image_sha256"],
        "general_i386_checks": 812,
        "captures": captures,
        "total_counts": total,
    }


def summarize(root, initial, guest):
    decoded = {pc: (op, operands.strip()) for pc, (op, operands) in SCENE["PHASE"]["GUEST"](guest, EXPECTED).items()}
    if decoded != EXPECTED:
        raise ValueError("The original mapped particle or resource instructions differ")
    return {
        "without_culling_prediction": run_report(initial, guest, False),
        "with_culling_prediction": run_report(root, guest, True),
        "native_phase_index_comparisons": 1048576,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_root", type=Path)
    parser.add_argument("guest_binary", type=Path)
    parser.add_argument("--initial-root", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(summarize(args.capture_root, args.initial_root, args.guest_binary), indent=2))
