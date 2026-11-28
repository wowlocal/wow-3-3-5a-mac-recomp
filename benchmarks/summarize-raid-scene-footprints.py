"""Record the direct-call closure and check dependencies within complete scene passes."""

from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass, field, replace
import hashlib
import json
import mmap
from pathlib import Path
import re
from runpy import run_path
from statistics import median
import struct
import subprocess
import tempfile


HERE = Path(__file__).resolve().parent
SCENE = run_path(str(HERE / "summarize-raid-scene-work.py"))
PASS, UPDATE, CONSUMER = (SCENE[name] for name in ("PASS", "UPDATE", "CONSUMER"))
RETURNS = {call + 5: call for call in (PASS, UPDATE, CONSUMER)}
ENTRIES = {PASS: 0x97E390, UPDATE: 0x964CA0, CONSUMER: 0x967F20}
ARGUMENT_SETUP = {
    0x980647: ("leal", "-0x9c(%ebp), %eax"),
    0x980689: ("movl", "%eax, 0xc(%esp)"),
    0x98068D: ("leal", "-0xd8(%ebp), %eax"),
    0x980693: ("movl", "$0x3f800000, 0x14(%esp)"),
    0x98069B: ("movl", "$0x3f800000, 0x10(%esp)"),
    0x9806A3: ("movl", "%eax, 0x8(%esp)"),
    0x9806A7: ("movl", "%esi, 0x4(%esp)"),
    0x9806AB: ("movl", "%ebx, (%esp)"),
}
ROW = struct.Struct("<QQQI9I")
CAPACITY = 1 << 25


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def text_section(data):
    if data[:4] != b"\xce\xfa\xed\xfe" or struct.unpack_from("<I", data, 4)[0] != 7:
        raise ValueError("Expected a thin i386 Mach-O image")
    count, size = struct.unpack_from("<II", data, 16)
    offset = 28
    for _ in range(count):
        command, length = struct.unpack_from("<II", data, offset)
        if length < 8 or offset + length > 28 + size:
            raise ValueError("Invalid Mach-O load command")
        if command == 1:
            sections, = struct.unpack_from("<I", data, offset + 48)
            if 56 + sections * 68 > length:
                raise ValueError("Truncated Mach-O segment")
            for section in range(offset + 56, offset + 56 + sections * 68, 68):
                if data[section:section + 32] == b"__text".ljust(16, b"\0") + b"__TEXT".ljust(16, b"\0"):
                    address, size, start = struct.unpack_from("<III", data, section + 32)
                    if start + size > len(data):
                        raise ValueError("Truncated i386 text section")
                    return address, data[start:start + size]
        offset += length
    raise ValueError("Missing i386 text section")


def instruction_table(guest):
    from capstone import CS_ARCH_X86, CS_GRP_CALL, CS_GRP_JUMP, CS_GRP_RET, CS_MODE_32, Cs
    from capstone.x86_const import X86_INS_JMP, X86_INS_LOOP, X86_INS_LOOPE, X86_INS_LOOPNE, X86_OP_IMM

    with tempfile.TemporaryDirectory(prefix="raid-i386-scene.") as folder:
        thin = Path(folder) / "guest-i386"
        subprocess.run(["xcrun", "lipo", "-thin", "i386", str(guest), "-output", str(thin)], check=True)
        base, text = text_section(thin.read_bytes())
    decoder = Cs(CS_ARCH_X86, CS_MODE_32)
    decoder.detail = True
    loops = {X86_INS_LOOP, X86_INS_LOOPE, X86_INS_LOOPNE}

    def closure(entry, depth):
        pending = [(entry, depth)]
        visited, unresolved = {}, set()
        while pending:
            pc, remaining = pending.pop()
            if not base <= pc < base + len(text) or visited.get(pc, -1) >= remaining:
                continue
            start = pc - base
            instruction = next(decoder.disasm(text[start:start + 15], pc, count=1), None)
            if instruction is None:
                continue
            visited[pc] = remaining
            if len(visited) > 100_000:
                raise ValueError("The scene call graph exceeded the probe limit")
            if instruction.group(CS_GRP_RET):
                continue
            call = instruction.group(CS_GRP_CALL)
            branch = instruction.group(CS_GRP_JUMP) or instruction.id in loops
            if call or branch:
                operand = instruction.operands[0]
                immediate = operand.type == X86_OP_IMM and base <= operand.imm < base + len(text)
                if immediate and (not call or remaining):
                    pending.append((operand.imm, remaining - int(call)))
                else:
                    unresolved.add(pc)
                if instruction.id == X86_INS_JMP:
                    continue
            pending.append((pc + instruction.size, remaining))
        return visited, unresolved

    functions = {"scene_caller": (ENTRIES[PASS], 0), "update": (ENTRIES[UPDATE], 5),
                 "consumer": (ENTRIES[CONSUMER], 3)}
    pcs, config = set(), {}
    for name, (entry, depth) in functions.items():
        visited, unresolved = closure(entry, depth)
        if entry not in visited:
            raise ValueError(f"Could not decode the {name} entry")
        pcs.update(visited)
        config[name] = {"entry": hex(entry), "direct_call_depth": depth, "decoded_pcs": len(visited),
                        "potential_escape_sites": len(unresolved)}
    return sorted(pcs), config


def configure(guest, path):
    pcs, branches = instruction_table(guest)
    path.write_bytes(struct.pack(f"<{len(pcs)}I", *pcs))
    return {"pc_table_sha256": digest(path), "pc_count": len(pcs), "closures": branches}


@dataclass
class Footprint:
    item: int = 0
    root_esp: int = 0
    start: int = 0
    elapsed: int = 0
    inputs: dict = field(default_factory=dict)
    outputs: dict = field(default_factory=dict)
    issues: Counter = field(default_factory=Counter)
    accesses: int = 0
    argument_spans: tuple = ()

    def mark(self, kind, address, width, mode, esp, pc):
        if kind in (2, 5):
            self.issues[pc, "uncovered_transfer"] += 1
        if kind == 3 or (kind in (1, 5) and mode == 4):
            self.issues[pc, "unsupported_access"] += 1
        if kind not in (1, 5) or mode == 4:
            return
        if not 1 <= width <= 16 or mode not in (1, 2, 3) or not 0 < address <= (1 << 32) - width:
            raise ValueError(f"Invalid guest memory access at {pc:#x}")
        self.accesses += 1
        if esp <= address and address + width <= self.root_esp:
            return
        for byte in range(address, address + width):
            if mode != 2 and byte not in self.outputs:
                self.inputs.setdefault(byte, pc)
            if mode != 1:
                self.outputs[byte] = pc


@dataclass
class Pass:
    start: int
    stack: tuple
    update: list = field(default_factory=list)
    consumer: list = field(default_factory=list)
    between: list = field(default_factory=list)
    caller: Footprint = field(default_factory=Footprint)
    active: tuple | None = None


def dependency(left, right):
    raw = left.outputs.keys() & right.inputs.keys()
    war = left.inputs.keys() & right.outputs.keys()
    waw = left.outputs.keys() & right.outputs.keys()
    return raw, war, waw


def record_edges(edges, left, right, names, conflicts):
    pc_maps = ((left.outputs, right.inputs), (left.inputs, right.outputs), (left.outputs, right.outputs))
    for kind, addresses, (first, second) in zip(names, conflicts, pc_maps):
        edges.update((kind, a, b) for a, b in {(first[p], second[p]) for p in addresses})


def schedule_updates(updates, gaps, counts, edges, distribution):
    """Stop a wave at each recorded conflict or uncovered operation."""
    nonconflicting_intervals = 0
    waves = []
    wave = []
    earlier_updates = Footprint()

    def close_wave():
        if wave:
            waves.append(list(wave))
            wave.clear()
            earlier_updates.inputs.clear()
            earlier_updates.outputs.clear()

    # Count risks against every still-unpublished update, not just its neighbor.
    for update, gap in zip(updates, gaps):
        if update.issues:
            close_wave()
            waves.append([update.elapsed])
            continue
        hits = dependency(earlier_updates, update)
        counts["updates_with_conflict_in_current_wave"] += any(hits)
        record_edges(edges, earlier_updates, update,
                     ("read_after_write", "write_after_read", "write_after_write"), hits)
        if any(hits):
            close_wave()
        wave.append(update.elapsed)
        earlier_updates.inputs.update(update.inputs)
        earlier_updates.outputs.update(update.outputs)
        gap_conflicts = dependency(earlier_updates, gap)
        counts["caller_intervals_with_dependency_on_pending_updates"] += any(gap_conflicts)
        record_edges(edges, earlier_updates, gap,
                     ("caller_reads_update", "caller_overwrites_update_input",
                      "caller_overwrites_update_output"), gap_conflicts)
        if gap.issues or any(gap_conflicts):
            close_wave()
        else:
            nonconflicting_intervals += 1
    close_wave()
    counts["largest_observed_update_wave"] = max(counts["largest_observed_update_wave"], max(map(len, waves)))
    counts["update_waves"] += len(waves)
    counts["updates_in_multi_item_waves"] += sum(len(wave) for wave in waves if len(wave) > 1)
    counts["covered_nonconflicting_post_update_intervals"] += nonconflicting_intervals
    work = sum(task.elapsed for task in updates)
    bounds = sum(max(max(wave), sum(wave) / 4) for wave in waves)
    distribution.setdefault("four_worker_instrumented_work_left_fraction", []).append(bounds / work)
    distribution.setdefault("largest_update_wave", []).append(max(map(len, waves)))


def analyze_pass(scene, counts, issues, edges, distribution, owned_counts, owned_edges, owned_distribution):
    if scene.active or not scene.update or not scene.consumer or scene.update[-1].start >= scene.consumer[0].start:
        raise ValueError("A scene pass does not contain the expected completed update and consumer waves")
    if len(scene.between) != len(scene.update):
        raise ValueError("Missing caller intervals between updates")
    counts["complete_passes"] += 1
    counts["update_calls"] += len(scene.update)
    counts["consumer_calls"] += len(scene.consumer)
    produced_by_update = {}
    update_inputs = set()
    frame_bytes = {"update_input_bytes": 0, "update_output_bytes": 0, "consumer_input_bytes": 0}
    for name, tasks in (("update", scene.update), ("consumer", scene.consumer), ("between_updates", scene.between)):
        for task in tasks:
            issues.update({(name, *key): n for key, n in task.issues.items()})
            if name == "between_updates":
                continue
            counts[f"{name}_without_uncovered_operations"] += not task.issues
            counts[f"{name}_explicit_accesses"] += task.accesses
            frame_bytes[f"{name}_input_bytes"] += len(task.inputs)
            if name == "update":
                frame_bytes["update_output_bytes"] += len(task.outputs)
                update_inputs.update(task.inputs)
                produced_by_update.update(task.outputs)
            else:
                prior = produced_by_update.keys() & task.inputs.keys()
                counts["consumers_reading_addresses_written_by_updates"] += bool(prior)
                edges.update(("consumer_reads_update", a, b) for a, b in
                             {(produced_by_update[p], task.inputs[p]) for p in prior})

    frame_bytes["distinct_update_input_bytes"] = len(update_inputs)
    frame_bytes["distinct_update_output_bytes"] = len(produced_by_update)
    for key, value in frame_bytes.items():
        distribution.setdefault(key, []).append(value)
    for key, tasks in (("update", scene.update), ("consumer", scene.consumer)):
        distribution.setdefault(f"{key}_calls_per_pass", []).append(len(tasks))

    schedule_updates(scene.update, scene.between, counts, edges, distribution)
    isolated = []
    for task in scene.update:
        stack_bytes = {byte for start, size in task.argument_spans for byte in range(start, start + size)}
        if task.issues or not produced_by_update.keys().isdisjoint(stack_bytes):
            isolated.append(task)
            owned_counts["updates_without_verified_argument_isolation"] += 1
            continue
        owned_counts["argument_bytes_read_at_original_locations"] += len(task.inputs.keys() & stack_bytes)
        owned_counts["updates_eligible_to_copy_argument_bytes"] += 1
        isolated.append(replace(task, inputs={a: pc for a, pc in task.inputs.items() if a not in stack_bytes}))
    schedule_updates(isolated, scene.between, owned_counts, owned_edges, owned_distribution)


def top_edges(edges):
    return [{"kind": kind, "first_pc": hex(a), "second_pc": hex(b), "count": n}
            for (kind, a, b), n in edges.most_common(24)]


def read_capture(folder, pcs):
    window = json.loads((folder / "window.json").read_text())
    by_end, _ = SCENE["LIFE"]["render_submissions"](folder / "submissions.ns", window["pid"])
    frames, _ = SCENE["LIFE"]["read_frames"](folder, window, by_end, False)
    submits = [frame.final.submit for frame in frames]
    manifest = json.loads((folder / "scene-gates.json").read_text())
    if manifest["armed_ms"] != 135 or manifest["offsets"] != [2, 15, 28] or len(manifest["gates"]) != 3:
        raise ValueError("Unexpected scene probe windows")
    counts, issues, edges, distribution = Counter(), Counter(), Counter(), {}
    owned_counts, owned_edges, owned_distribution = Counter(), Counter(), {}
    gates = []
    last_index = 0
    with (folder / "raid-prep.bin").open("rb") as file, mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ) as ring:
        count, armed = struct.unpack_from("<QQ", ring)
        if len(ring) != (CAPACITY + 1) * ROW.size or armed or not 0 < count == manifest["gates"][-1][1] < CAPACITY:
            raise ValueError("Invalid scene ring")
        for start, end in manifest["gates"]:
            if not last_index == start < end <= count:
                raise ValueError("The scene ring has missing rows")
            last_index = end
            first_tick, = struct.unpack_from("<Q", ring, (start + 1) * ROW.size + 8)
            final_tick, = struct.unpack_from("<Q", ring, end * ROW.size + 8)
            if not submits[0] < first_tick < final_tick < submits[-1]:
                raise ValueError("Scene gate outside the verified GPU window")
            valid_frames = range(bisect_right(submits, first_tick) + 1, bisect_right(submits, final_tick))
            pending, previous, selected = {}, {}, set()
            passes_before = counts["complete_passes"]
            for index in range(start, end):
                seq, tick, cpu, pc, *words, kind = ROW.unpack_from(ring, (index + 1) * ROW.size)
                if seq != index + 1 or not cpu or not previous.get(cpu, first_tick) <= tick <= final_tick:
                    raise ValueError(f"Invalid scene trace at record {index + 1}")
                previous[cpu] = tick
                scene = pending.get(cpu)
                if kind:
                    if kind not in (1, 2, 3, 5) or pc not in pcs:
                        raise ValueError(f"Invalid instruction footprint at {pc:#x}")
                    if scene is not None:
                        scope = scene.active[2] if scene.active else scene.caller
                        scope.mark(kind, *words[:4], pc)
                    continue
                ebx, ebp, esp = words[1], words[6], words[7]
                if pc == PASS:
                    if scene is not None:
                        raise ValueError("Nested scene passes reuse a capture scope")
                    pending[cpu] = Pass(tick, (ebp, esp))
                elif pc == PASS + 5:
                    if scene is not None:
                        del pending[cpu]
                        frame = bisect_right(submits, tick)
                        if scene.stack != (ebp, esp):
                            raise ValueError("The scene caller stack changed")
                        if frame in valid_frames and frame == bisect_right(submits, scene.start):
                            analyze_pass(scene, counts, issues, edges, distribution,
                                         owned_counts, owned_edges, owned_distribution)
                            selected.add(frame)
                elif pc in (UPDATE, CONSUMER):
                    if scene is not None:
                        if scene.active or not ebx:
                            raise ValueError("Nested scene tasks reuse a capture scope")
                        if pc == UPDATE and scene.consumer:
                            raise ValueError("The update wave continued after consumer work")
                        spans = ((esp, 24), (ebp - 0xD8, 12), (ebp - 0x9C, 12)) if pc == UPDATE else ()
                        scene.active = pc, (ebp, esp), Footprint(ebx, esp, tick, argument_spans=spans)
                elif pc in RETURNS:
                    if scene is not None:
                        if scene.active is None or scene.active[:2] != (RETURNS[pc], (ebp, esp)):
                            raise ValueError("Missing matching scene task entry")
                        _, _, task = scene.active
                        if task.item != ebx or task.accesses == 0:
                            raise ValueError("Scene task identity or memory coverage changed")
                        task.elapsed = tick - task.start
                        if task.elapsed <= 0:
                            raise ValueError("Scene task time did not advance")
                        scene.active = None
                        if pc == UPDATE + 5:
                            if scene.update:
                                scene.between.append(scene.caller)
                            scene.update.append(task)
                            scene.caller = Footprint()
                        else:
                            scene.consumer.append(task)
                        if pc == CONSUMER + 5 and len(scene.consumer) == 1:
                            scene.between.append(scene.caller)
                            scene.caller = Footprint()
                else:
                    raise ValueError(f"Unexpected scene boundary at {pc:#x}")
            gates.append({"rows": end - start, "complete_producer_intervals": len(selected),
                          "complete_scene_passes": counts["complete_passes"] - passes_before})
    if not counts["complete_passes"]:
        raise ValueError("No complete scene passes in the verified interval")
    return {
        "gates": gates,
        "counts": counts,
        "median_per_pass": {key: median(values) for key, values in distribution.items()},
        "uncovered_sites": [{"scope": scope, "pc": hex(pc), "kind": kind, "count": n}
                            for (scope, pc, kind), n in issues.most_common()],
        "top_dependency_sites": top_edges(edges),
        "owned_argument_model": {
            "counts": owned_counts,
            "median_per_pass": {key: median(values) for key, values in owned_distribution.items()},
            "top_dependency_sites": top_edges(owned_edges),
        },
    }


def report(guest, root, capture_names):
    table = root / "scene-pcs.bin"
    with tempfile.TemporaryDirectory(prefix="raid-scene-check.") as folder:
        config = configure(guest, Path(folder) / "scene-pcs.bin")
    if digest(table) != config["pc_table_sha256"]:
        raise ValueError("The captured instruction table does not match the original i386 image")
    pcs = set(struct.unpack(f"<{config['pc_count']}I", table.read_bytes()))
    if not (root / "general.log").read_text().endswith("test_m32: 805 checks, 0 failed\n"):
        raise ValueError("Missing general i386 checks")
    artifacts = json.loads((root / "artifact-sha256.json").read_text())
    accepted = json.loads((HERE / "results/raid-winemetalgl-defer-render-end-pgo-ab.json").read_text())
    original = json.loads((HERE / "results/raid-aarchx-scene-work.json").read_text())
    if (digest(guest) != original["guest_image_sha256"] or
            digest(root / "artifacts/candidate/ocerz") != artifacts["runtime_unsigned_sha256"] or
            digest(root / "artifacts/candidate/libwinemetalgl.dylib") != artifacts["renderer_sha256"] or
            artifacts["renderer_sha256"] != accepted["artifact_sha256"]["renderer_sha256_by_role"]["candidate"]):
        raise ValueError("Wrong original game executable or diagnostic binaries")
    expected = {**ARGUMENT_SETUP, **{pc: ("calll", hex(target)) for pc, target in ENTRIES.items()}}
    requested = expected.keys() | RETURNS.keys()
    instructions = SCENE["PHASE"]["GUEST"](guest, requested)
    annotated = {pc: (op, operands.strip()) for pc, (op, operands) in instructions.items()}
    if not requested <= annotated.keys() or any(annotated[pc] != annotation for pc, annotation in expected.items()):
        raise ValueError("The original preparation call boundaries or argument setup differ")
    captures = []
    for name in capture_names:
        case = root / name
        verified, _ = SCENE["TAIL"]["verify_capture"](case, artifacts)
        if (json.loads((case / "legacy.json").read_text()) !=
                {"legacy_paused": True, "legacy_completed_frames_during_launch": 0}):
            raise ValueError("The earlier local client was not isolated")
        captures.append({"verified_gpu_window": verified, **read_capture(case, pcs)})
    return {
        "guest_image_sha256": digest(guest),
        "artifacts": artifacts,
        **config,
        "general_i386_checks": 805,
        "captures": captures,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    setup = actions.add_parser("configure")
    setup.add_argument("guest", type=Path)
    setup.add_argument("table", type=Path)
    summary = actions.add_parser("report")
    summary.add_argument("guest", type=Path)
    summary.add_argument("root", type=Path)
    summary.add_argument("captures", nargs="+")
    args = parser.parse_args()
    result = configure(args.guest, args.table) if args.action == "configure" else report(
        args.guest, args.root, args.captures)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
