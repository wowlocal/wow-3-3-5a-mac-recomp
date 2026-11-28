"""Measure how often the raid's sampled i386 reads revisit a local stack slot."""

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re
from runpy import run_path
import subprocess
from typing import NamedTuple


HERE = Path(__file__).resolve().parent
PROFILE = run_path(str(HERE / "summarize-raid-guest-native-stacks.py"))
UNWIND = PROFILE["GUEST"]
INSTRUCTION = PROFILE["INSTRUCTION"]
LOCAL = re.compile(r"(-(?:0x[0-9a-f]+|[0-9]+))\(%ebp\)$")
REGISTER = re.compile(r"%[a-z][a-z0-9]*$")
MOVES = {"movb": 1, "movw": 2, "movl": 4, "movss": 4, "movsd": 8,
         "movaps": 16, "movups": 16, "movdqa": 16, "movdqu": 16}
FLOAT_OPS = {"add", "sub", "mul", "div", "min", "max", "sqrt", "comi", "ucomi"}
ROOTS = {0x73F8E0, 0x97DBC0}
SAFE_PREFIXES = ("mov", "lea", "add", "sub", "inc", "dec", "imul", "and", "or", "xor", "pxor", "cmp", "test",
                 "ucomi", "comi", "mulss", "mulsd", "divss", "divsd", "max", "min", "sqrt", "nop",
                 "set", "cmov", "shl", "shr", "sar", "sal", "neg", "not")


class Instruction(NamedTuple):
    pc: int
    op: str
    operands: tuple


def range_index(ranges):
    starts = [start for start, _ in ranges]

    def find(pc):
        index = bisect_right(starts, pc) - 1
        if index >= 0 and pc < ranges[index][1]:
            return starts[index]
        return None

    return find


def operands(text):
    # Indexed address expressions contain their own commas.
    return tuple(part.strip() for part in re.split(r",\s*(?![^()]*\))", text) if part.strip())


def disassemble(binary, functions, containing):
    selected = defaultdict(list)
    command = ["xcrun", "llvm-objdump", "--arch=i386", "-d", "--no-show-raw-insn", str(binary)]
    with subprocess.Popen(command, stdout=subprocess.PIPE, text=True) as decoder:
        in_text = False
        for line in decoder.stdout:
            if line.startswith("(") and " section" in line:
                in_text = line.strip() == "(__TEXT,__text) section"
            match = INSTRUCTION.match(line) if in_text else None
            if match:
                pc = int(match[1], 16)
                entry = containing(pc)
                if entry in functions:
                    selected[entry].append(Instruction(pc, match[2], operands(match[3].split("##", 1)[0])))
        if decoder.wait():
            raise subprocess.CalledProcessError(decoder.returncode, command)
    return selected


def root_of(sample, call_sites, containing):
    roots = {containing(sample.leaf)}
    for ret in sample.returns:
        caller = call_sites.get(ret)
        if caller is None:
            break
        roots.add(containing(caller))
    return bool(ROOTS & roots)


def local_slot(operand, width):
    match = LOCAL.fullmatch(operand)
    if not match or not width:
        return None
    offset = int(match[1], 0)
    # Usual i386 prologues save EBX, ESI and EDI in the first 12 bytes.
    return (offset, width) if offset + width <= -12 else None


def register_family(operand):
    for registers in (
        ("%eax", "%ax", "%ah", "%al"), ("%ebx", "%bx", "%bh", "%bl"),
        ("%ecx", "%cx", "%ch", "%cl"), ("%edx", "%dx", "%dh", "%dl"),
        ("%esi", "%si"), ("%edi", "%di"), ("%ebp", "%bp"), ("%esp", "%sp"),
    ):
        if operand in registers:
            return registers[0]
    return operand if REGISTER.fullmatch(operand) else None


def access_width(op):
    if op in MOVES:
        return MOVES[op]
    if op[:-2] in FLOAT_OPS:
        return {"ss": 4, "sd": 8}.get(op[-2:])
    return None


def slot_sites(function):
    early = [(ins.op, ins.operands) for ins in function[:5]]
    prologue = [("pushl", ("%ebp",)), ("movl", ("%esp", "%ebp"))]
    if not any(early[index:index + 2] == prologue for index in range(4)):
        return {}, False
    targets = {int(ins.operands[0], 16) for ins in function if ins.op.startswith("j") and ins.operands
               and re.fullmatch(r"0x[0-9a-f]+", ins.operands[0])}
    resident = {}
    result = {}
    frame_address_formed = False
    for pc, op, args in function:
        if pc in targets:
            resident.clear()
        if not args or not op.startswith(SAFE_PREFIXES):
            resident.clear()
            continue
        if op.startswith("lea") and "(%ebp" in args[0]:
            frame_address_formed = True
        if args[0] == "%ebp" and (op, args) != ("movl", ("%ebp", "%esp")):
            frame_address_formed = True
        if len(args) != 2:
            # A single explicit operand can have implicit writes or modify memory.
            resident.clear()
            continue
        source, dest = args
        if not op.startswith("lea"):
            width = access_width(op)
            slot = local_slot(source, width)
            if slot:
                previous = resident.get(slot)
                switches_class = (previous is not None and op in MOVES and REGISTER.fullmatch(dest) and
                                  previous[1] is not None and previous[1].startswith("%xmm") != dest.startswith("%xmm"))
                result[pc] = {
                    "opcode": op, "recent_access": previous[0] if previous else None,
                    "value_register_still_live": previous is not None and previous[1] is not None,
                    "gpr_xmm_transfer": bool(switches_class),
                }
            else:
                slot = None
        else:
            width = slot = None
        modified = None if op.startswith(("cmp", "test", "comi", "ucomi")) else register_family(dest)
        if modified == "%ebp":
            resident.clear()
        elif modified:
            resident = {key: (kind, None if reg == modified else reg) for key, (kind, reg) in resident.items()}

        if op.startswith(("cmp", "test", "comi", "ucomi")):
            continue
        if REGISTER.fullmatch(dest):
            if slot and op in MOVES and modified != "%ebp":
                resident[slot] = "load", modified
            continue
        # Any potentially aliased memory write invalidates a recorded slot.
        written = local_slot(dest, width) if op in MOVES else None
        if not written:
            resident.clear()
            continue
        low, size = written
        resident = {key: data for key, data in resident.items() if key[0] + key[1] <= low or low + size <= key[0]}
        resident[written] = "store", register_family(source)
    return result, frame_address_formed


def attribute(rows, candidates, arm64, frame_address_formed, containing, return_sites):
    totals, roots, per_function = Counter(), Counter(), defaultdict(Counter)
    for row in rows:
        totals["all_cpu_samples_usec"] += row.weight
        category = candidates.get(row.leaf)
        if not category:
            continue
        entry = containing(row.leaf)
        names = ["known_local_stack_read_usec"]
        native_opcode, native_operands = arm64[row.host_instruction]
        native_load = native_opcode.startswith("ld") and not PROFILE["CPU_STATE_MEMORY"].search(native_operands)
        if native_load:
            names.append("native_load_at_local_read_usec")
        if not frame_address_formed[entry]:
            names.append("local_read_in_function_without_visible_frame_address_formation_usec")
        if category["recent_access"]:
            names.append("same_block_prior_access_usec")
            names.append(f"same_block_prior_{category['recent_access']}_usec")
            if native_load:
                names.append("native_load_at_same_block_prior_access_usec")
            if category["value_register_still_live"]:
                names.append("same_block_value_register_still_live_usec")
            if category["gpr_xmm_transfer"]:
                names.append("same_block_gpr_xmm_transfer_usec")
        belongs_to_root = root_of(row, return_sites, containing)
        for name in names:
            totals[name] += row.weight
            per_function[entry][name] += row.weight
            if belongs_to_root:
                roots[name] += row.weight
    return {
        "weighted_cpu_usec": dict(totals),
        "weighted_cpu_usec_in_both_preparation_call_trees": dict(roots),
        "highest_coverage_functions": [
            {"entry": hex(entry), "forms_frame_address": frame_address_formed[entry], **counts}
            for entry, counts in sorted(per_function.items(),
                                        key=lambda pair: -pair[1]["known_local_stack_read_usec"])[:15]
        ],
    }


def summarize(binary, captures):
    baseline = json.loads((HERE / "results/raid-aarchx-i386-item-memory.json").read_text())
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    if digest != baseline["guest_image_sha256"]:
        raise ValueError("Wrong original client image")
    ranges = UNWIND["read_unwind_ranges"](binary)
    containing = range_index(ranges)
    samples = [PROFILE["read_samples"](source) for source in captures]
    selected = {entry for rows in samples for row in rows if row.leaf and (entry := containing(row.leaf))}
    returns = {ret for rows in samples for row in rows for ret in row.returns}
    return_sites = UNWIND["find_i386_call_sites"](binary, returns)
    candidates, frame_address_formed = {}, {}
    functions = disassemble(binary, selected, containing)
    for entry, instructions in functions.items():
        local, formed = slot_sites(instructions)
        candidates.update(local)
        frame_address_formed[entry] = formed
    arm64 = PROFILE["host_instructions"]({row.host_instruction for rows in samples for row in rows
                                         if row.leaf in candidates})
    result = []
    for source, rows in zip(captures, samples):
        result.append({
            "verified_gpu_window": PROFILE["READ_VERIFIED_WINDOW"](source, 60),
            **attribute(rows, candidates, arm64, frame_address_formed, containing, return_sites),
        })
    return {"guest_image_sha256": digest, "decoded_functions": len(functions), "runs": result}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("guest_binary", type=Path)
    parser.add_argument("captures", type=Path, nargs="+")
    args = parser.parse_args()
    print(json.dumps(summarize(args.guest_binary, args.captures), indent=2))
