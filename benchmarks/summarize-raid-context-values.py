"""Compare early context words with the original dispatcher's late publication."""

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass, field
import json
import mmap
from pathlib import Path
from runpy import run_path
import struct
from typing import NamedTuple


HERE = Path(__file__).resolve().parent
SCENE = run_path(str(HERE / "summarize-raid-scene-work.py"))
TAIL, LIFE = SCENE["TAIL"], SCENE["LIFE"]
RECORD = struct.Struct("<QQQI8II12I4I")
STRIDE, CAPACITY = 128, 1 << 22
BEGIN, PUBLISH, PUBLISHED, END = 0x97D31D, 0x97D3B3, 0x97D3FE, 0x97D404
EARLY = (0x97D382, 0x97D38D, 0x979190, 0x9791AD, 0x97B9D7, 0x97CB1C)
CONTEXT_REGISTER = {
    BEGIN: 0, 0x97D382: 2, 0x97D38D: 3, 0x979190: 5, 0x9791AD: 5,
    0x97B9D7: 5, 0x97CB1C: 5, PUBLISH: 0, PUBLISHED: 3,
}
INSTRUCTIONS = {
    BEGIN: ("movl", "%edx, 0x50(%eax)"),
    0x97D382: ("calll", "0x957c10"),
    0x97D38D: ("cmpl", "$0x5, (%eax)"),
    0x979190: ("calll", "0x89f920"),
    0x9791AD: ("movl", "0x1121200, %eax"),
    0x97B9D7: ("leal", "-0xb8(%ebp), %eax"),
    0x97CB1C: ("testb", "$-0x80, 0x4(%eax)"),
    PUBLISH: ("movl", "0x8(%ebp), %edx"),
    PUBLISHED: ("movl", "%eax, 0x9c(%edx)"),
    END: ("incl", "%ebx"),
}
FIELDS = (0x58, 0x60, 0x68, 0x74, 0x78, 0x80, 0x88, 0x90, 0x98)
DESTINATIONS = (0x5C, 0x64, 0x94)
DESTINATION_SOURCES = (0x58, 0x60, 0x90)


class Snapshot(NamedTuple):
    tick: int
    registers: tuple
    values: tuple
    item_words: tuple = ()


@dataclass
class Item:
    begin: Snapshot
    context: int
    cpu: int
    early: dict = field(default_factory=dict)
    publish: Snapshot = None
    published: Snapshot = None
    end: Snapshot = None

    @property
    def tag(self):
        return self.begin.registers[2]


def events(capture, gate):
    last_tick = {}
    with (capture / "context-values.bin").open("rb") as source:
        with mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as trace:
            count, armed = struct.unpack_from("<QQ", trace)
            if armed or not 0 < count == gate["rows"] < CAPACITY or len(trace) != (CAPACITY + 1) * STRIDE:
                raise ValueError("The probe ring is incomplete")
            for index in range(count):
                sequence, tick, cpu, pc, *payload = RECORD.unpack_from(trace, (index + 1) * STRIDE)
                registers, copied, has_item = tuple(payload[:8]), payload[8], payload[24]
                if (sequence != index + 1 or not cpu or pc not in INSTRUCTIONS or
                        tick < last_tick.get(cpu, 1) or copied not in (0, 1) or (pc == END and copied) or
                        has_item not in (0, 1) or (has_item and pc != BEGIN)):
                    raise ValueError(f"Invalid probe record {index + 1}")
                last_tick[cpu] = tick
                yield cpu, pc, Snapshot(tick, registers, tuple(payload[9:21]) if copied else (),
                                       tuple(payload[21:24]) if has_item else ())


def trace_bounds(capture, gate):
    count = gate["rows"]
    if not 0 < count < CAPACITY:
        raise ValueError("Invalid probe count")
    with (capture / "context-values.bin").open("rb") as source:
        source.seek(STRIDE + 8)
        first, = struct.unpack("<Q", source.read(8))
        source.seek(count * STRIDE + 8)
        last, = struct.unpack("<Q", source.read(8))
    if not 0 < first < last:
        raise ValueError("Invalid probe time span")
    return first, last


def decode_items(capture, gate, counts):
    pending = {}
    active_cpus = set()
    for cpu, pc, record in events(capture, gate):
        if pc != END and not record.values:
            counts["snapshots_skipped_at_page_boundary"] += 1
        stack = record.registers[6:]
        key = cpu, stack
        if pc == BEGIN:
            if key in pending:
                raise ValueError("Two active dispatcher items have the same caller frame")
            pending[key] = Item(record, record.registers[0], cpu)
            active_cpus.add(cpu)
            continue
        if pc == END:
            item = pending.pop(key, None)
            if item is None:
                counts["dispatcher_ends_without_a_recorded_entry"] += 1
            else:
                item.end = record
                yield item
            continue
        context = record.registers[CONTEXT_REGISTER[pc]]
        if pc in (0x97D382, 0x97D38D, PUBLISH, PUBLISHED):
            item = pending.get(key)
        else:
            owners = [item for (running_cpu, _), item in pending.items()
                      if running_cpu == cpu and context == item.context]
            if len(owners) > 1:
                raise ValueError("A nested checkpoint has multiple active dispatcher callers")
            item = owners[0] if owners else None
        if item is None:
            counts["checkpoints_without_a_recorded_entry"] += 1
            continue
        if item.context != context or not context:
            raise ValueError("The dispatcher context changed at a parent checkpoint")
        if pc == PUBLISH:
            if item.publish is not None:
                raise ValueError("The dispatcher published the same item twice")
            item.publish = record
        elif pc == PUBLISHED:
            if item.publish is None or item.published is not None:
                raise ValueError("Unexpected end of a context publication")
            item.published = record
        else:
            if pc in item.early:
                raise ValueError(f"Repeated early checkpoint {pc:#x}")
            item.early[pc] = record
    if len(active_cpus) != 1:
        raise ValueError("Expected one active guest dispatcher CPU within the gate")
    counts["entries_at_end_of_gate"] = len(pending)


def predict_context(item):
    if not item.begin.item_words or item.tag not in (0, 1, 2, 3, 4):
        return ()
    tag, material, metadata = item.begin.item_words
    if tag != item.tag:
        raise ValueError("Original item tag differs from the dispatch register")
    return tag, material, metadata if tag <= 2 else 0


def compare_values(items, submits):
    counts, delays, stable_delays = defaultdict(Counter), defaultdict(list), defaultdict(list)
    actual, previous = Counter(), {}
    predictions, prediction_leads = defaultdict(Counter), defaultdict(list)
    complete_outputs = set()
    active_frame = None
    for item in items:
        frame = bisect_right(submits, item.begin.tick)
        if not 0 < frame < len(submits) or not item.end.tick < submits[frame]:
            continue
        complete_outputs.add(frame)
        if active_frame != frame:
            active_frame = frame
            previous.clear()
        tag = str(item.tag)
        actual[f"paired_items_tag_{tag}"] += 1
        if item.publish is None or item.published is None or not item.publish.values or not item.published.values:
            actual["items_without_a_complete_publication_snapshot"] += 1
            continue
        source = tuple(item.publish.values[FIELDS.index(field)] for field in DESTINATION_SOURCES)
        if item.publish.values[:len(FIELDS)] != item.published.values[:len(FIELDS)] or source != item.published.values[-3:]:
            raise ValueError("The original instructions did not publish the expected words")
        actual["verified_publications"] += 1
        actual[f"verified_publications_tag_{tag}"] += 1
        actual["published_0x58_matches_entry_tag"] += item.publish.values[0] == item.tag
        actual["published_tasks_that_adjust_iteration_index"] += item.end.registers[1] != item.begin.registers[1]
        guessed = predict_context(item)
        prediction = predictions[tag]
        prediction["publications"] += 1
        if guessed:
            prediction["input_words_captured"] += 1
            prediction["all_three_values_match"] += guessed == source
            prediction_leads[tag].append(item.publish.tick - item.begin.tick)
            for field, value, published in zip(DESTINATIONS, guessed, source):
                prediction[f"matches_{field:#x}"] += value == published
        key = frame, item.cpu, item.begin.registers[6], item.context
        prior = previous.get(key)
        if (prior is not None and prior.end.registers[1] + 1 == item.begin.registers[1] and item.begin.values):
            actual["adjacent_publication_to_next_item_pairs"] += 1
            observed = item.begin.values[-3:]
            actual["adjacent_pairs_with_all_previous_fields_visible"] += prior.published.values[-3:] == observed
            for field, expected, got in zip(DESTINATIONS, prior.published.values[-3:], observed):
                actual[f"adjacent_pairs_matching_{field:#x}"] += expected == got
            guess = predict_context(prior)
            if guess:
                actual["adjacent_pairs_with_an_early_input_prediction"] += 1
                actual["adjacent_pairs_matching_the_early_prediction"] += guess == observed
        previous[key] = item
        for pc, snapshot in item.early.items():
            if not snapshot.values:
                continue
            if snapshot.tick >= item.publish.tick:
                raise ValueError("An early checkpoint occurred after publication began")
            ident = tag, hex(pc)
            row = counts[ident]
            row["snapshots"] += 1
            equal = [a == b for a, b in zip(snapshot.values[:len(FIELDS)], item.publish.values[:len(FIELDS)])]
            row["all_source_words_equal"] += all(equal)
            for index, field in enumerate(FIELDS):
                row[f"equal_{field:#x}"] += equal[index]
            if pc in (0x979190, 0x9791AD, 0x97B9D7, 0x97CB1C):
                esp, ebp = snapshot.registers[7], snapshot.registers[6]
                row["source_0x98_inside_the_callee_stack"] += esp <= snapshot.values[FIELDS.index(0x98)] < ebp + 8
            offset = item.publish.tick - snapshot.tick
            delays[ident].append(offset)
            if equal[FIELDS.index(0x58)]:
                stable_delays[ident].append(offset)
    if not counts or not actual["verified_publications"]:
        raise ValueError("No early and late context snapshots in complete verified frames")
    return {
        "complete_producer_intervals": len(complete_outputs),
        "publication_checks": dict(actual),
        "predictions_from_the_original_item_before_setup": [
            {"tag": int(tag), **counts, "lead_before_publication_ns": LIFE["distribution"](prediction_leads[tag])
             if prediction_leads[tag] else {}}
            for tag, counts in sorted(predictions.items(), key=lambda pair: int(pair[0]))
        ],
        "checkpoints": [
            {
                "tag": int(tag), "guest_pc": pc,
                **row,
                "lead_before_publication_ns": LIFE["distribution"](delays[tag, pc]),
                "lead_when_0x58_matches_ns": (
                    LIFE["distribution"](stable_delays[tag, pc]) if stable_delays[tag, pc] else {}
                ),
            }
            for (tag, pc), row in sorted(counts.items(), key=lambda p: (int(p[0][0]), int(p[0][1], 16)))
        ],
    }


def summarize(root, guest):
    artifacts = json.loads((root / "artifact-sha256.json").read_text())
    accepted = json.loads((HERE / "results/raid-winemetalgl-defer-render-end-pgo-ab.json").read_text())
    prior = json.loads((HERE / "results/raid-aarchx-i386-item-critical-path.json").read_text())
    sha = TAIL["QUEUED"]["sha"]
    if (sha(guest) != prior["guest_image_sha256"] or
            sha(root / "artifacts/candidate/ocerz") != artifacts["runtime_unsigned_sha256"] or
            sha(root / "artifacts/candidate/libwinemetalgl.dylib") != artifacts["renderer_sha256"] or
            artifacts["renderer_sha256"] != accepted["artifact_sha256"]["renderer_sha256_by_role"]["candidate"]):
        raise ValueError("Wrong original i386 client or diagnostic artifacts")
    if not (root / "general.log").read_text().endswith("test_m32: 805 checks, 0 failed\n"):
        raise ValueError("The diagnostic failed the general i386 checks")
    instructions = SCENE["PHASE"]["GUEST"](guest, INSTRUCTIONS.keys())
    if INSTRUCTIONS != {pc: (op, text.strip()) for pc, (op, text) in instructions.items()}:
        raise ValueError("A checkpoint differs from the original scatter or dispatch code")
    result = []
    for i, delay in enumerate((2, 19)):
        capture = root / f"phase-{i}"
        verified, _ = TAIL["verify_capture"](capture, artifacts)
        gate = json.loads((capture / "phase-gate.json").read_text())
        if gate["armed_seconds"] != 5 or gate["after_ready_seconds"] != delay:
            raise ValueError("Wrong probe gate")
        first, last = trace_bounds(capture, gate)
        window = json.loads((capture / "window.json").read_text())
        by_end, _ = LIFE["render_submissions"](capture / "submissions.ns", window["pid"])
        frames, _ = LIFE["read_frames"](capture, window, by_end, False)
        submits = [frame.final.submit for frame in frames if first <= frame.final.submit <= last]
        raw_counts = Counter()
        comparison = compare_values(decode_items(capture, gate, raw_counts), submits)
        result.append({
            "verified_gpu_window": verified, "recording_gate": gate, "raw_trace_counts": dict(raw_counts),
            **comparison,
        })
    return {
        "artifact_sha256": artifacts, "guest_image_sha256": prior["guest_image_sha256"],
        "general_i386_checks": 805, "captures": result,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_root", type=Path)
    parser.add_argument("guest_binary", type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize(args.capture_root, args.guest_binary), indent=2))
