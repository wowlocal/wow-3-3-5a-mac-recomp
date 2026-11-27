"""Summarize main-thread scheduling and CPU Counters in exported xctrace tables."""

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
from xml.etree import ElementTree


REFERENCE_TAGS = {
    "start-time", "thread", "thread-state", "duration", "process", "core",
    "non-zero-double", "non-zero-uint64", "string", "sample-time", "uint64",
    "backtrace", "tagged-backtrace", "frame", "binary", "weight",
}
COUNTER_CATEGORIES = {
    "bottlenecks": ("useful", "delivery", "processing", "discarded"),
    "processing": (
        "executing", "execution_latency", "memory_miss", "memory_miss_executing",
        "non_critical_memory_miss",
    ),
    "delivery": (
        "delivery_bandwidth", "delivery_latency_icache", "delivery_latency_itlb",
        "delivery_latency_other",
    ),
}


def read_table(path, schema, required_columns):
    references = {}
    columns = None
    node = None

    def resolve(field):
        if "ref" in field.attrib:
            return references[field.attrib["ref"]]
        return field

    def value(field, column):
        field = resolve(field)
        if column in {"backtrace", "stack"}:
            frames = []
            for child in field.findall("frame"):
                frame = resolve(child)
                image = frame.find("binary")
                name = resolve(image).get("name") if image is not None else None
                frames.append((frame.get("addr"), frame.get("name"), name))
            return frames
        return field.get("fmt"), field.text

    for event, element in ElementTree.iterparse(path, events=("start", "end")):
        if event == "start":
            if element.tag == "node":
                node = element
            continue
        if element.tag == "schema":
            if columns is not None or element.get("name") != schema:
                raise ValueError(f"Expected one xctrace {schema} table")
            columns = {col.findtext("mnemonic"): index for index, col in enumerate(element)}
            if not required_columns <= columns.keys():
                raise ValueError(f"Missing columns in {schema}: {required_columns - columns.keys()}")
        if element.tag != "row":
            continue
        if node is None or columns is None:
            raise ValueError("Missing xctrace table header")
        for field in element.iter():
            if field.tag in REFERENCE_TAGS and "id" in field.attrib:
                references[field.attrib["id"]] = field
        yield {name: value(element[columns[name]], name) for name in required_columns}
        node.remove(element)


def summarize_states(path, pid):
    states = defaultdict(Counter)
    running_by_core = defaultdict(Counter)
    columns = {"thread", "state", "duration", "process", "core"}
    for row in read_table(path, "thread-state", columns):
        process, _ = row["process"]
        if process and process.endswith(f" ({pid})"):
            thread, _ = row["thread"]
            _, state = row["state"]
            duration = int(row["duration"][1])
            states[thread][state] += duration
            if state == "Running":
                core, _ = row["core"]
                running_by_core[thread][core or "Unknown"] += duration

    main_threads = [thread for thread in states if thread.startswith("Main Thread ")]
    if len(main_threads) != 1:
        raise ValueError("Expected one recorded main thread for the target PID")
    main = main_threads[0]
    thread_states = states[main]
    total = sum(thread_states.values())
    if not total or not thread_states["Running"]:
        raise ValueError("No running time for the recorded main thread")
    return {
        "recorded_threads": len(states),
        "main_thread_state_ns": dict(thread_states.most_common()),
        "main_thread_state_total_ns": total,
        "main_thread_running_percent": 100 * thread_states["Running"] / total,
        "main_thread_running_ns_by_core": dict(sorted(running_by_core[main].items())),
        "other_threads_running_ns": sum(
            thread_states["Running"]
            for thread, thread_states in states.items()
            if thread != main
        ),
    }


def summarize_counters(path, pid, mode="bottlenecks"):
    samples = defaultdict(dict)
    categories = COUNTER_CATEGORIES[mode]
    expected = {*categories, "cycle"}
    columns = {
        "timestamp", "duration", "thread", "process", "core", "metric-name", "sum", "average",
    }
    for row in read_table(path, "MetricTableForThread", columns):
        process = row["process"][0] or ""
        thread = row["thread"][0] or ""
        if not process.endswith(f" ({pid})") or not thread.startswith("Main Thread "):
            continue
        start_ns = int(row["timestamp"][1])
        duration_ns = int(row["duration"][1])
        core_type = row["core"][1]
        name = row["metric-name"][1]
        if name not in expected:
            continue
        sample = samples[start_ns, duration_ns, core_type]
        if name in sample:
            raise ValueError(f"Repeated {name} at {start_ns} ns")
        if name == "cycle":
            sample[name] = int(row["sum"][1] or 0)
        else:
            sample[name] = float(row["average"][1] or 0)

    weighted = Counter()
    sample_counts = Counter()
    cycles = 0
    duration = 0
    first_start = None
    last_end = None
    for (start_ns, duration_ns, core_type), sample in sorted(samples.items()):
        if set(sample) != expected or sample["cycle"] <= 0:
            continue
        if duration_ns <= 0 or (last_end is not None and start_ns < last_end):
            raise ValueError("Counter intervals overlap or have a nonpositive duration")
        if first_start is None:
            first_start = start_ns
        last_end = start_ns + duration_ns
        sample_counts[core_type] += 1
        duration += duration_ns
        cycles += sample["cycle"]
        for name in categories:
            if not 0 <= sample[name] <= 1:
                raise ValueError(f"Invalid {name} fraction at {start_ns} ns")
            weighted[name] += sample[name] * sample["cycle"]
    if not cycles:
        raise ValueError("No complete, cycle-weighted counters for the main thread")
    share_key = "cycle_weighted_shares" if mode == "processing" else "cycle_weighted_bandwidth_shares"
    return {
        "complete_intervals": sum(sample_counts.values()),
        "incomplete_or_zero_cycle_intervals": len(samples) - sum(sample_counts.values()),
        "complete_intervals_by_core_type": dict(sample_counts),
        "trace_relative_bounds_ns": [first_start, last_end],
        "complete_interval_duration_ns": duration,
        "cycles": cycles,
        share_key: {name: weighted[name] / cycles for name in categories},
    }


def is_main_thread(thread, pid):
    return bool(thread and thread.startswith("Main Thread ") and thread.endswith(f", pid: {pid})"))


def summarize_l1d_samples(path, pid):
    images = defaultdict(Counter)
    memmove = Counter()
    memmove_callers = Counter()
    first = None
    last = None
    columns = {"timestamp", "pmi-event", "thread", "backtrace", "pc"}
    for row in read_table(path, "SamplingModeSamples", columns):
        if not is_main_thread(row["thread"][0], pid):
            continue
        event = row["pmi-event"][1]
        frames = row["backtrace"]
        if not frames or int(frames[0][0], 16) != int(row["pc"][1]):
            raise ValueError("Miss sample has no frame at its sampled PC")
        timestamp = int(row["timestamp"][1])
        if first is None:
            first = timestamp
        if last is not None and timestamp < last:
            raise ValueError("Miss samples are out of time order")
        last = timestamp

        _, symbol, image = frames[0]
        images[event][image or "Unattributed address"] += 1
        if image == "libsystem_platform.dylib" and symbol == "_platform_memmove":
            memmove[event] += 1
            if len(frames) > 1:
                memmove_callers[event] += 1
    if not images:
        raise ValueError("No main-thread cache miss samples for the target PID")
    return {
        "trace_relative_bounds_ns": [first, last],
        "events": {
            event: {
                "sample_rows": sum(counts.values()),
                "sample_rows_by_image": dict(counts.most_common()),
                "platform_memmove_sample_rows": memmove[event],
                "platform_memmove_sample_rows_with_caller": memmove_callers[event],
            }
            for event, counts in sorted(images.items())
        },
    }


def summarize_memmove_time(path, pid):
    running = 0
    callers = Counter()
    columns = {"thread", "thread-state", "weight", "stack"}
    for row in read_table(path, "time-profile", columns):
        if not is_main_thread(row["thread"][0], pid) or row["thread-state"][1] != "Running":
            continue
        weight = int(row["weight"][1])
        running += weight
        frames = row["stack"]
        if frames and frames[0][1:] == ("_platform_memmove", "libsystem_platform.dylib"):
            _, function, image = frames[1] if len(frames) > 1 else (None, None, None)
            callers[image or "Unattributed", function or "Unattributed"] += weight
    if not running:
        raise ValueError("No main-thread running samples for the target PID")
    return {
        "main_thread_running_weight_ns": running,
        "platform_memmove_leaf_weight_ns": sum(callers.values()),
        "platform_memmove_direct_callers": [
            {"image": image, "function": function, "weight_ns": weight}
            for (image, function), weight in callers.most_common()
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("thread_states_xml", type=Path, nargs="?")
    parser.add_argument("thread_metrics_xml", type=Path)
    parser.add_argument("pid", type=int)
    parser.add_argument("--mode", choices=[*COUNTER_CATEGORIES, "l1d_miss_sampling"], default="bottlenecks")
    parser.add_argument("--time-profile", type=Path)
    args = parser.parse_args()
    if args.mode == "l1d_miss_sampling":
        report = {"miss_samples": summarize_l1d_samples(args.thread_metrics_xml, args.pid)}
    else:
        report = {"counters": summarize_counters(args.thread_metrics_xml, args.pid, args.mode)}
    if args.thread_states_xml is not None:
        report["scheduling"] = summarize_states(args.thread_states_xml, args.pid)
    if args.time_profile is not None:
        report["time_profile"] = summarize_memmove_time(args.time_profile, args.pid)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
