"""Verify the original i386 matrix equations and export captured calls for replay."""

import argparse
from collections import Counter, OrderedDict
import hashlib
import json
import mmap
from pathlib import Path
import re
from runpy import run_path
import struct


HERE = Path(__file__).resolve().parent
LAYOUT = run_path(str(HERE / "summarize-raid-quad-layout.py"))
NATIVE = run_path(str(HERE / "summarize-raid-guest-native-stacks.py"))
BEGIN, END = 0x8C1E9F, 0x8C23DE
RECORD = struct.Struct("<QQQ6I48I")
CASE = struct.Struct("<82I")
MEMORY = re.compile(r"^(-?0x[0-9a-f]+)?\(%([a-z]+)\)$")


def memory(operand):
    displacement, base = MEMORY.fullmatch(operand).groups()
    offset = int(displacement or "0", 0)
    if offset % 4:
        raise ValueError("Unexpected matrix operand width")
    return base, offset // 4


def normalize(expression):
    if len(expression) == 2:
        return expression
    op, left, right = expression
    left, right = normalize(left), normalize(right)
    if op == "mulss":
        # Both operands are finite in the prototype's selected cases.
        left, right = sorted((left, right))
    return op, left, right


def verify_instructions(guest):
    instructions = NATIVE["guest_instructions"](guest, set(range(BEGIN, END + 1)))
    if instructions.get(END, (None,))[0] != "leave":
        raise ValueError("Unexpected original matrix epilogue")
    registers, stack, result = {}, {}, {}

    def read(operand):
        if operand.startswith("%xmm"):
            return registers[operand]
        base, index = memory(operand)
        if base == "ecx" or base == "edx":
            if result or not 0 <= index < 16:
                raise ValueError("Original source reads overlap ordered publication")
            return ("L" if base == "ecx" else "R"), index
        if base != "ebp" or not -26 <= index < 0:
            raise ValueError(f"Unexpected original matrix source {operand}")
        return stack[index]

    for pc, (op, operands) in sorted(instructions.items()):
        if pc == END:
            break
        source, destination = map(str.strip, operands.split(","))
        if op in ("mulss", "addss"):
            registers[destination] = op, read(destination), read(source)
        elif op == "movss":
            value = read(source)
            if destination.startswith("%xmm") and source.startswith("%xmm"):
                raise ValueError("Original matrix moves changed their upper-lane semantics")
            if destination.startswith("%xmm"):
                registers[destination] = value
            else:
                base, index = memory(destination)
                if base == "eax" and index == len(result):
                    result[index] = value
                elif base == "ebp" and -26 <= index < 0:
                    stack[index] = value
                else:
                    raise ValueError(f"Unexpected original matrix destination {destination}")
        else:
            raise ValueError(f"Unexpected {op} in original matrix body")

    for index in range(16):
        row, column = divmod(index, 4)
        terms = [("mulss", ("L", 4 * row + k), ("R", 4 * k + column)) for k in range(4)]
        expected = terms[0]
        for term in terms[1:]:
            expected = "addss", expected, term
        if normalize(result[index]) != expected:
            raise ValueError(f"Original summation order changed in output {index}")
    exits = [result[15], ("mulss", ("L", 11), ("R", 14)), result[3], result[7],
             ("mulss", ("L", 1), ("R", 7)), ("mulss", ("L", 2), ("R", 11)),
             ("mulss", ("L", 3), ("R", 15)), result[11]]
    if [normalize(registers[f"%xmm{i}"]) for i in range(8)] != list(map(normalize, exits)):
        raise ValueError("Original final XMM expressions changed")

    def last_row(column):
        products = [("mulss", ("L", 12 + k), ("R", 4 * k + column)) for k in range(4)]
        _, first, second, third = products
        return [("addss", ("addss", products[0], first), second), first, second, third]

    prefix, p1, p2, p3 = last_row(3)
    scratch = [prefix, ("R", 3), p1, ("R", 7), p2, ("R", 11), p3, ("R", 15)]
    for column in (2, 1, 0):
        scratch.extend(last_row(column))
    scratch.extend(("L", index) for index in (9, 10, 11, 5, 6, 7))
    if [normalize(stack[i]) for i in range(-26, 0)] != list(map(normalize, scratch)):
        raise ValueError("Original final scratch words changed")
    return {"scalar_instructions": len(instructions) - 1, "ordered_results": len(result),
            "checked_xmm_registers": len(registers), "checked_scratch_words": len(scratch)}


def export_gate(raw, first_index, stop_index, first_tick, end_tick, output):
    pending = {}
    counts = Counter()
    latest_by_cpu = {}
    last_inputs = {}
    recent_inputs = {}
    for index in range(first_index, stop_index):
        sequence, tick, cpu, pc, frame, dst, left, right, mxcsr, *words = RECORD.unpack_from(
            raw, (index + 1) * LAYOUT["STRIDE"])
        if (sequence != index + 1 or pc not in (BEGIN, END) or not all((cpu, frame, dst, left, right)) or
                not latest_by_cpu.get(cpu, first_tick) <= tick < end_tick):
            raise ValueError(f"Invalid matrix row {index}")
        latest_by_cpu[cpu] = tick
        key = cpu, frame
        addresses = dst, left, right
        if pc == BEGIN:
            counts["entry_rows"] += 1
            counts["unpaired_entries"] += key in pending
            pending[key] = addresses, mxcsr, words[:32]
            continue
        counts["return_rows"] += 1
        entry = pending.pop(key, None)
        if entry is None:
            counts["unpaired_returns"] += 1
            continue
        if entry[0] != addresses or (entry[1] ^ mxcsr) & ~63:
            raise ValueError("Original matrix changed its arguments or MXCSR controls")
        alias = int(abs(dst - left) < 64) | (int(abs(dst - right) < 64) << 1)
        counts["captured_calls"] += 1
        if alias:
            counts["overlapping_output_calls"] += 1
        counts[f"mxcsr_control_{mxcsr & ~63:#x}"] += 1
        data = CASE.pack(*entry[2], *words, mxcsr, alias)
        signature = data[:128], mxcsr & ~63
        counts["same_input_as_previous_call_on_cpu"] += last_inputs.get(cpu) == signature
        last_inputs[cpu] = signature
        recent = recent_inputs.setdefault(cpu, OrderedDict())
        counts["same_input_in_last_64_distinct_on_cpu"] += signature in recent
        recent[signature] = None
        recent.move_to_end(signature)
        if len(recent) > 64:
            recent.popitem(last=False)
        output.write(data)
    counts["unpaired_entries"] += len(pending)
    if not counts["captured_calls"]:
        raise ValueError("No complete matrix calls in gate")
    return dict(counts)


def export_capture(capture, output, seconds=35):
    window = LAYOUT["VERIFY"](capture, "candidate", seconds=seconds)
    _, first_tick, end_tick = LAYOUT["BOUNDS"]["WINDOW"](capture)
    gates = json.loads((capture / "prep-gates.json").read_text())
    summaries = []
    with (capture / "matrix.bin").open("rb") as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as raw:
        count, enabled = struct.unpack_from("<QQ", raw)
        if (len(raw) != (LAYOUT["CAPACITY"] + 1) * LAYOUT["STRIDE"] or enabled or
                not 0 < count < LAYOUT["CAPACITY"] or len(gates) != 3 or gates[-1][1] != count):
            raise ValueError("Incomplete matrix recorder")
        previous = 0
        for first, stop in gates:
            if first != previous or not first < stop <= count:
                raise ValueError("Missing matrix gate rows")
            summaries.append(export_gate(raw, first, stop, first_tick, end_tick, output))
            previous = stop
    return {"verified_gpu_window": window, "gates": summaries}


def digest(path):
    with path.open("rb") as stream:
        sha = hashlib.sha256()
        for data in iter(lambda: stream.read(1 << 20), b""):
            sha.update(data)
        return sha.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_root", type=Path)
    parser.add_argument("guest_binary", type=Path)
    parser.add_argument("case_file", type=Path)
    parser.add_argument("--native", action="store_true")
    args = parser.parse_args()
    baseline = json.loads((HERE / "results/raid-aarchx-i386-item-preparation.json").read_text())
    artifacts = json.loads((args.capture_root / "artifact-sha256.json").read_text())
    guest = digest(args.guest_binary)
    if args.native:
        runtime, artifact, renderer = "runtime_unsigned_sha256", "candidate", "renderer_sha256"
        names, seconds = ["diagnostic"], 25
        capture = args.capture_root / "diagnostic"
        if (capture.joinpath("matrix-mode.txt").read_text().split() != ["1", "1"]
                or json.loads(capture.joinpath("artifact-sha256.json").read_text()) != artifacts):
            raise ValueError("Wrong matrix runtime mode or binary in diagnostic")
    else:
        runtime, artifact, renderer = "instrumented_runtime", "observe", "renderer"
        names, seconds = ["layout-1", "layout-2"], 35
    if (guest != baseline["guest_image_sha256"] or artifacts[renderer] != baseline["artifacts_sha256"]["renderer"]
            or artifacts[runtime] != digest(args.capture_root / f"artifacts/{artifact}/ocerz")):
        raise ValueError("Wrong guest, diagnostic runtime or renderer")
    proof = verify_instructions(args.guest_binary)
    with args.case_file.open("wb") as output:
        captures = [export_capture(args.capture_root / name, output, seconds) for name in names]
    config = ("hardware", "macos", "viewport", "vsync", "caster_count", "boss_count", "camera")
    print(json.dumps({**{key: baseline[key] for key in config}, "guest_image_sha256": guest,
                      "diagnostic_artifacts": artifacts, "verified_timed_script_rows": 26,
                      "original_matrix_instructions": proof, "case_file_sha256": digest(args.case_file),
                      "captures": captures}, indent=2))


if __name__ == "__main__":
    main()
