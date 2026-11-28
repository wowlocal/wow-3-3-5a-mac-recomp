"""Order the same profiled i386 blocks by temporal affinity and verify the live A/B."""

import argparse
from collections import Counter, defaultdict, deque
import hashlib
import json
from pathlib import Path
from runpy import run_path
from statistics import median
import xml.etree.ElementTree as ElementTree


HERE = Path(__file__).resolve().parent
LAYOUT = run_path(str(HERE / "summarize-raid-hot-layout.py"))
READ_WINDOW = run_path(str(HERE / "summarize-raid-shadow-reuse.py"))["read_window"]
NAMES = ("frequency", "affinity")
PROXIMITY_NS = 12_000_000
RANK_BAND = 64


def read_order(path):
    header, *lines = path.read_text().splitlines()
    entries = [int(line, 16) for line in lines]
    if not entries or len(entries) != len(set(entries)):
        raise ValueError(f"Missing or duplicate addresses in {path}")
    return header, entries


def proximity_weights(samples, selected):
    recent = deque()
    weights = Counter()
    for timestamp, _, block, _ in samples:
        while recent and timestamp - recent[0][0] > PROXIMITY_NS:
            recent.popleft()
        if block not in selected:
            continue
        for prior_time, prior_block in recent:
            if block != prior_block:
                pair = tuple(sorted((prior_block, block)))
                weights[pair] += 1 + (PROXIMITY_NS - (timestamp - prior_time)) // 1_000_000
        recent.append((timestamp, block))
    if not weights:
        raise ValueError("No sampled proximity between selected blocks")
    return weights


def affinity_order(frequency, edges):
    ranks = {block: index for index, block in enumerate(frequency)}
    order = list(frequency)
    neighbors = defaultdict(list)
    for (a, b), weight in edges.items():
        neighbors[a].append((b, weight))
        neighbors[b].append((a, weight))

    def move_cost(block, other, source, dest):
        return sum(weight * (min(abs(dest - ranks[peer]), RANK_BAND)
                             - min(abs(source - ranks[peer]), RANK_BAND))
                   for peer, weight in neighbors[block] if peer != other)

    # Preserve the hotness ordering between bands and improve proximity inside them.
    for _ in range(2):
        for start in range(0, len(order), RANK_BAND):
            stop = min(start + RANK_BAND, len(order))
            for i in range(start, stop):
                a = order[i]
                best_delta = 0
                partner = None
                for j in range(i + 1, stop):
                    b = order[j]
                    change = move_cost(a, b, i, j) + move_cost(b, a, j, i)
                    if change < best_delta:
                        best_delta, partner = change, j
                if partner is not None:
                    b = order[partner]
                    order[i], order[partner] = b, a
                    ranks[a], ranks[b] = partner, i
    return order


def proximity_report(order, edges):
    indices = {block: index for index, block in enumerate(order)}
    total = sum(edges.values())
    return {
        "mean_weighted_rank_distance": sum(abs(indices[a] - indices[b]) * w for (a, b), w in edges.items()) / total,
        "weight_within_32_ranks_fraction": sum(w for (a, b), w in edges.items()
                                               if abs(indices[a] - indices[b]) <= 32) / total,
    }


def prepare(root, previous):
    path = previous / "selected-order.txt"
    header, frequency = read_order(path)
    seeds = set(frequency)
    training = LAYOUT["read_timed_samples"](previous / "training")
    holdout = LAYOUT["read_timed_samples"](previous / "packed-profile")
    edges = proximity_weights(training, seeds)
    ordered = affinity_order(frequency, edges)
    if len(ordered) != len(frequency) or set(ordered) != seeds:
        raise ValueError("Changed the set of precompiled blocks")
    root.mkdir(parents=True, exist_ok=True)
    (root / "frequency-order.txt").write_bytes(path.read_bytes())
    (root / "affinity-order.txt").write_text(header + "\n" + "".join(f"{block:x}\n" for block in ordered))
    held_out_edges = proximity_weights(holdout, seeds)
    return {
        "seed_count": len(seeds),
        "proximity_window_ns": PROXIMITY_NS,
        "frequency_rank_band": RANK_BAND,
        "training_proximity_pairs": len(edges),
        "held_out_proximity_pairs": len(held_out_edges),
        "training_proximity": {name: proximity_report(order, edges)
                               for name, order in zip(NAMES, (frequency, ordered))},
        "held_out_proximity": {name: proximity_report(order, held_out_edges)
                               for name, order in zip(NAMES, (frequency, ordered))},
    }


def verify(capture, name, hashes, seeds, seconds):
    witness = json.loads((capture / "order-witness.json").read_text())
    if witness != {"legacy_paused": True, "order": name, "order_sha256": hashes[name]}:
        raise ValueError(f"Incorrect order or legacy witness in {capture}")
    if (capture / "layout-mode.txt").read_text().split() != ["1", "0"]:
        raise ValueError(f"Unexpected layout settings in {capture}")
    if (capture / "map-shadow-mode.txt").read_text().split() != ["1", "0"]:
        raise ValueError(f"Unexpected shadow settings in {capture}")
    rows = LAYOUT["ORDER_LOG"].findall((capture / "client.log").read_text())
    if len(rows) != 1:
        raise ValueError(f"Missing pretranslation witness in {capture}")
    requested, compiled, present, span, elapsed = map(int, rows[0])
    if (requested, compiled, present) != (seeds, seeds, 0):
        raise ValueError(f"The run did not pretranslate the complete seed set in {capture}")
    return {
        "pretranslation": {"compiled_blocks": compiled, "arena_span_bytes": span, "elapsed_ns": elapsed},
        **READ_WINDOW(capture, seconds),
    }


def summarize(root):
    a, b = (read_order(root / f"{name}-order.txt") for name in NAMES)
    if a[0] != b[0] or set(a[1]) != set(b[1]):
        raise ValueError("The mode order files must contain the same seed set")
    for start in range(0, len(a[1]), RANK_BAND):
        stop = start + RANK_BAND
        if set(a[1][start:stop]) != set(b[1][start:stop]):
            raise ValueError("Affinity ordering must preserve membership of the frequency bands")
    hashes = {name: hashlib.sha256((root / f"{name}-order.txt").read_bytes()).hexdigest() for name in NAMES}
    runs = []
    for index, name in enumerate(("frequency", "affinity", "affinity", "frequency"), 1):
        path = root / "timed" / f"{index}-{name}"
        runs.append({"order": index, "mode": name, **verify(path, name, hashes, len(a[1]), 60)})
    groups = {}
    for name in NAMES:
        group = [row for row in runs if row["mode"] == name]
        groups[name] = {
            "median_completed_fps": median(row["average_completed_fps"] for row in group),
            "median_p99_gap_ns": median(row["p99_gpu_completion_gap_ns"] for row in group),
        }
    traces = []
    for name in NAMES:
        capture = root / "delivery" / name
        toc = ElementTree.parse(capture / "toc.xml").find("./run/info/summary")
        if toc.findtext("end-reason") != "Time limit reached" or toc.findtext("template-name") != "CPU Counters":
            raise ValueError(f"Incomplete instruction delivery trace in {capture}")
        cpu = json.loads((capture / "summary.json").read_text())["counters"]
        if not cpu["complete_intervals"] or cpu["complete_intervals_by_core_type"] != {
            "Performance": cpu["complete_intervals"]
        }:
            raise ValueError(f"Expected complete main-thread samples on performance cores in {capture}")
        traces.append({
            "mode": name,
            "duration_seconds": float(toc.findtext("duration")),
            "diagnostic_gpu_window": verify(capture, name, hashes, len(a[1]), 32),
            "counters": cpu,
        })
    return {
        "seed_count": len(a[1]),
        "order_sha256": hashes,
        "blocks_at_different_ranks": sum(x != y for x, y in zip(a[1], b[1])),
        "timed_runs": runs,
        "comparison": groups,
        "paired_affinity_minus_frequency": [
            {
                "completed_fps": runs[t]["average_completed_fps"] - runs[c]["average_completed_fps"],
                "p99_gap_ns": runs[t]["p99_gpu_completion_gap_ns"] - runs[c]["p99_gpu_completion_gap_ns"],
            }
            for c, t in ((0, 1), (3, 2))
        ],
        "delivery_traces": traces,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_root", type=Path)
    parser.add_argument("--prepare-from", type=Path)
    args = parser.parse_args()
    result = prepare(args.capture_root, args.prepare_from) if args.prepare_from else summarize(args.capture_root)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
