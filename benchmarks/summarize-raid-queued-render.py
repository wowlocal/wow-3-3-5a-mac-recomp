"""Check the queued Metal encoder's live timing, pixels, and thread samples."""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
from runpy import run_path


HERE = Path(__file__).resolve().parent
TIMING = run_path(str(HERE / "summarize-raid-renderer-pgo.py"))
CPU = run_path(str(HERE / "summarize-raid-cpu-trace.py"))
PIXELS = re.compile(r"^outputs (\d+), RGBA ([0-9a-f]{16}), colored (\d+)$", re.MULTILINE)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_run(root, name, enabled, traced, artifacts):
    capture = root / name
    if (json.loads((capture / "artifact-sha256.json").read_text()) != artifacts or
            (capture / "queued-render-mode.txt").read_text().split() != [str(enabled), str(int(traced))] or
            (capture / "queued-encoder.tsv").is_file() != traced):
        raise ValueError(f"Wrong renderer, runtime or logging in {capture}")
    return TIMING["verify"](capture, "candidate", seconds=25 if traced else 60)


def batches(capture):
    columns = ("queued_commands", "batches", "copied_payload_bytes", "synchronous_forwarded_calls")
    totals = Counter()
    passes = 0
    for line in (capture / "queued-encoder.tsv").read_text().splitlines():
        values = list(map(int, line.split()))
        if len(values) not in (len(columns), len(columns) + 2) or min(values) < 0 or values[0] > 96 * values[1]:
            raise ValueError("Invalid queued encoder counters")
        totals.update(dict(zip(columns, values)))
        passes += 1
    if not passes or not totals["queued_commands"]:
        raise ValueError("No asynchronously queued render commands")
    return {"logged_passes": passes, **{column: totals[column] for column in columns}}


def cpu_samples(path, pid):
    columns = {"time", "thread", "process", "thread-state", "weight", "stack"}
    weights = Counter()
    methods = Counter()
    first = last = None
    for row in CPU["read_table"](path, "time-profile", columns):
        if not (row["process"][0] or "").endswith(f" ({pid})"):
            continue
        tick = int(row["time"][1])
        first = tick if first is None else min(first, tick)
        last = tick if last is None else max(last, tick)
        if row["thread-state"][1] != "Running":
            continue
        weight = int(row["weight"][1])
        stack = [(name or "", image) for _, name, image in row["stack"]]
        main = CPU["is_main_thread"](row["thread"][0], pid)
        weights["main_running_ns" if main else "other_running_ns"] += weight
        worker = any("::consume(void*)" in name and image == "libwinemetalgl.dylib" for name, image in stack)
        if worker:
            weights["main_in_batch_consumer_ns" if main else "worker_in_batch_consumer_ns"] += weight
        if main:
            proxy = next((name for name, image in stack if name.startswith("-[WMGLQueuedRenderEncoder ")
                          and image == "libwinemetalgl.dylib"), None)
            if proxy:
                weights["main_in_encoder_proxy_ns"] += weight
                methods[proxy] += weight
    if first is None or last <= first or not weights["main_running_ns"] or not weights["worker_in_batch_consumer_ns"]:
        raise ValueError("Expected samples from the game and the asynchronous Metal encoder")
    return {"sample_time_span_ns": last - first, **weights,
            "top_main_proxy_symbols_ns": dict(methods.most_common(10))}


def summary(root):
    artifacts = json.loads((root / "artifact-sha256.json").read_text())
    if (sha(root / "artifacts/candidate/ocerz") != artifacts["runtime_unsigned_sha256"] or
            sha(root / "artifacts/candidate/libwinemetalgl.dylib") != artifacts["renderer_sha256"]):
        raise ValueError("The locally timed artifacts changed")
    diagnostic = verify_run(root, "diagnostic", 1, True, artifacts)
    pid = json.loads((root / "diagnostic/window.json").read_text())["pid"]
    runs = [{"order": i, "mode": "candidate" if enabled else "control",
             **verify_run(root, f"timed-{i}-{enabled}", enabled, False, artifacts)}
            for i, enabled in enumerate((0, 1, 1, 0), 1)]
    replay = {}
    for mode, enabled in (("control", 0), ("queued", 1)):
        if not (root / f"gpu-tests-{enabled}.log").read_text().endswith("343 tests, 0 failed checks\n"):
            raise ValueError(f"{mode} GPU tests failed")
        pixels = PIXELS.search((root / f"replay-{enabled}.log").read_text())
        if not pixels or int(pixels[1]) <= 0:
            raise ValueError(f"{mode} did not render the indexed replay")
        replay[mode] = {"outputs": int(pixels[1]), "rgba_hash": pixels[2], "colored_pixels": int(pixels[3])}
    if any(replay["control"][key] != replay["queued"][key] for key in ("rgba_hash", "colored_pixels")):
        raise ValueError("The queued replay changed the rendered pixels")
    return {"artifact_sha256": artifacts, "gpu_tests_per_mode": 343, "indexed_replay": replay,
            "diagnostic_gpu_window": diagnostic, "diagnostic_pass_counters": batches(root / "diagnostic"),
            "diagnostic_cpu_time_profile": cpu_samples(root / "encoder-cpu.xml", pid),
            **TIMING["summarize_timing"](runs)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_root", type=Path)
    print(json.dumps(summary(parser.parse_args().capture_root), indent=2))
