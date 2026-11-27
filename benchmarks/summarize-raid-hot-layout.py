"""Select a bounded i386 pretranslation order and verify its live raid measurements."""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
from runpy import run_path
from statistics import median
import subprocess


HERE = Path(__file__).resolve().parent
READ_WINDOW = run_path(str(HERE / "summarize-raid-shadow-reuse.py"))["read_window"]
READ_BOUNDS = run_path(str(HERE / "summarize-i386-unwind-hotspots.py"))["read_stack_window_bounds"]
ORDER_LOG = re.compile(
    r"^ocerz: i386 hot code order seeds=(\d+) compiled=(\d+) present=(\d+) bytes=(\d+) ns=(\d+)$",
    re.M,
)
MAX_SEEDS = 1024


def text_range(client):
    stdout = subprocess.check_output(["otool", "-arch", "i386", "-l", str(client)], text=True)
    matches = re.findall(
        r"sectname __text\s+segname __TEXT\s+addr (0x[0-9a-f]+)\s+size (0x[0-9a-f]+)",
        stdout,
    )
    if len(matches) != 1:
        raise ValueError("Expected one i386 main executable text section")
    start, size = map(lambda value: int(value, 16), matches[0])
    return start, start + size


def read_samples(capture):
    _, start, stop = READ_BOUNDS(capture)
    first = None
    last = None
    samples = []
    with (capture / "i386-blocks.ns").open() as source:
        for line in source:
            if not line.endswith("\n"):
                break
            timestamp, weight, block, host = line.split()
            timestamp, weight = int(timestamp), int(weight)
            block, host = int(block, 16), int(host, 16)
            if (last is not None and timestamp < last) or weight <= 0 or bool(block) != bool(host) or host % 4:
                raise ValueError(f"Malformed block layout sample in {capture}")
            if first is None:
                first = timestamp
            last = timestamp
            if start <= timestamp < stop:
                samples.append((weight, block, host))
    if not samples or first > start or last < stop:
        raise ValueError(f"Block layout samples do not cover the GPU window in {capture}")
    return samples


def prepare(root, bounds):
    samples = read_samples(root / "training")
    hot = Counter()
    for weight, block, _ in samples:
        if bounds[0] <= block < bounds[1]:
            hot[block] += weight
    ranked = sorted(hot, key=lambda block: (-hot[block], block))[:MAX_SEEDS]
    if not ranked:
        raise ValueError("No translated blocks in the original client's text")
    path = root / "selected-order.txt"
    path.write_text(f"{bounds[0]:x} {bounds[1]:x}\n" + "".join(f"{block:x}\n" for block in ranked))
    return {
        "seeds": len(ranked),
        "sampled_client_blocks": len(hot),
        "selected_share_of_client_translated_cpu": sum(hot[block] for block in ranked) / sum(hot.values()),
    }


def evaluate_pages(samples, page_bytes):
    pages = Counter()
    for weight, _, host in samples:
        pages[host // page_bytes] += weight
    total = sum(pages.values())
    if not total:
        raise ValueError("No sampled code pages")
    accumulated = 0
    for count, (_, weight) in enumerate(pages.most_common(), 1):
        accumulated += weight
        if 5 * accumulated >= 4 * total:
            return {
                "sampled_cpu_usec": total,
                "distinct_sampled_host_pages": len(pages),
                "host_pages_to_cover_80_percent": count,
            }


def verify_window(capture, ordered, profile, seed_count):
    if (capture / "layout-mode.txt").read_text().split() != [str(int(ordered)), str(int(profile))]:
        raise ValueError(f"Unexpected hot-order settings in {capture}")
    if (capture / "map-shadow-mode.txt").read_text().split() != ["1", "0"]:
        raise ValueError(f"Unexpected shadow settings in {capture}")
    rows = ORDER_LOG.findall((capture / "client.log").read_text())
    if len(rows) != int(ordered):
        raise ValueError(f"Inconsistent pretranslation log in {capture}")
    result = READ_WINDOW(capture, 60)
    if rows:
        count, compiled, present, code_bytes, elapsed = map(int, rows[0])
        if count != seed_count or compiled <= 0 or compiled + present > count:
            raise ValueError(f"Unexpected pretranslation coverage in {capture}")
        result["pretranslation"] = {
            "requested_blocks": count,
            "compiled_blocks": compiled,
            "previously_present_blocks": present,
            "arena_span_bytes": code_bytes,
            "elapsed_ns": elapsed,
        }
    return result


def summarize(root, bounds, page_bytes):
    order_file = root / "selected-order.txt"
    header, *entries = order_file.read_text().splitlines()
    seeds = {int(value, 16) for value in entries}
    if tuple(int(value, 16) for value in header.split()) != bounds or len(seeds) != len(entries):
        raise ValueError("Seed list does not match the client or contains duplicate entries")
    profiles = []
    selected_samples = []
    for capture, ordered in (("training", False), ("packed-profile", True)):
        folder = root / capture
        samples = read_samples(folder)
        client = [row for row in samples if bounds[0] <= row[1] < bounds[1]]
        selected = [row for row in client if row[1] in seeds]
        selected_samples.append(selected)
        profiles.append({
            "capture": capture,
            "diagnostic_gpu_window": verify_window(folder, ordered, True, len(seeds)),
            "all_sampled_cpu_usec": sum(weight for weight, _, _ in samples),
            "client_translated": evaluate_pages(client, page_bytes),
            "sampled_seed_blocks": len({block for _, block, _ in selected}),
            "seed_blocks_only": evaluate_pages(selected, page_bytes),
        })
    common = {block for _, block, _ in selected_samples[0]} & {block for _, block, _ in selected_samples[1]}
    for report, selected in zip(profiles, selected_samples):
        report["seed_blocks_sampled_in_both_profiles"] = evaluate_pages(
            (row for row in selected if row[1] in common), page_bytes,
        )

    runs = []
    for index, mode in enumerate((0, 1, 1, 0), 1):
        folder = root / "timed" / f"{index}-{mode}"
        runs.append({"order": index, "pretranslated": bool(mode),
                     **verify_window(folder, bool(mode), False, len(seeds))})
    comparison = {}
    for mode, label in ((False, "lazy"), (True, "hot_order")):
        selected = [row for row in runs if row["pretranslated"] == mode]
        comparison[label] = {
            "median_completed_fps": median(row["average_completed_fps"] for row in selected),
            "median_p99_gap_ns": median(row["p99_gpu_completion_gap_ns"] for row in selected),
        }
    return {
        "seed_count": len(seeds),
        "seed_list_sha256": hashlib.sha256(order_file.read_bytes()).hexdigest(),
        "common_seed_blocks_in_profiles": len(common),
        "host_code_page_bytes": page_bytes,
        "profiles": profiles,
        "timed_runs": runs,
        "comparison": comparison,
        "paired_trial_minus_control": [
            {
                "completed_fps": runs[t]["average_completed_fps"] - runs[c]["average_completed_fps"],
                "p99_gap_ns": runs[t]["p99_gpu_completion_gap_ns"] - runs[c]["p99_gpu_completion_gap_ns"],
            }
            for c, t in ((0, 1), (3, 2))
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_root", type=Path)
    parser.add_argument("client", type=Path)
    parser.add_argument("--prepare", action="store_true", help="write selected-order.txt from the training capture")
    parser.add_argument("--host-page-bytes", type=int, default=16384)
    args = parser.parse_args()
    if args.host_page_bytes <= 0:
        parser.error("host-page-bytes must be positive")
    bounds = text_range(args.client)
    result = prepare(args.capture_root, bounds) if args.prepare else summarize(
        args.capture_root, bounds, args.host_page_bytes,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
