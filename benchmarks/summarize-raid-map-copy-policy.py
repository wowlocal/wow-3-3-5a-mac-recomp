"""Measure which native GL map flags account for fallback shadow copies."""

import argparse
import hashlib
import json
from pathlib import Path
import re
from runpy import run_path


HERE = Path(__file__).resolve().parent
VERIFY = run_path(str(HERE / "summarize-raid-renderer-pgo.py"))["verify"]
FIELDS = ("maps", "copies", "input", "whole", "explicit", "invalidate", "unknown",
          "recent_null", "unmap", "flush")
ROW = re.compile(r"ocerz: GL map policy " + " ".join(f"{name}=(\\d+)" for name in FIELDS))
REPORT_INTERVAL = 16384


def read_diagnostic(capture):
    window = VERIFY(capture, "candidate", seconds=35)
    if (capture / "shadow-bucket-mode.txt").read_text().split() != ["0", "0"]:
        raise ValueError(f"Wrong shadow lookup settings in {capture}")
    log = (capture / "client.log").read_text()
    _, marker, after = log.rpartition("saved Metal trial backbuffer readback")
    if not marker:
        raise ValueError(f"Missing world readback in {capture}")
    rows = []
    for line in after.splitlines(keepends=True):
        if not line.startswith("ocerz: GL map policy ") or not line.endswith("\n"):
            continue
        match = ROW.fullmatch(line.rstrip("\n"))
        if not match:
            raise ValueError(f"Incomplete copy-policy counters in {capture}")
        rows.append(dict(zip(FIELDS, map(int, match.groups()))))
    if len(rows) < 2 or rows[0]["maps"] % REPORT_INTERVAL:
        raise ValueError(f"Missing copy-policy counters in {capture}")
    for first, last in zip(rows, rows[1:]):
        if last["maps"] - first["maps"] != REPORT_INTERVAL:
            raise ValueError(f"Counter gap in {capture}")
        if any(last[name] < first[name] for name in FIELDS):
            raise ValueError(f"Counter moved backwards in {capture}")
    delta = {name: rows[-1][name] - rows[0][name] for name in FIELDS}
    if not 0 < delta["copies"] <= delta["maps"] or not delta["input"]:
        raise ValueError(f"Missing input copies in {capture}")
    if (max(delta["whole"], delta["recent_null"]) > delta["input"] or
            sum(delta[name] for name in ("explicit", "invalidate", "unknown")) > delta["input"]):
        raise ValueError(f"Invalid input byte classification in {capture}")
    return {
        "diagnostic_gpu_window": window,
        "post_readback_counter_rows": len(rows),
        "post_readback_first_and_last_map_counts": [rows[0]["maps"], rows[-1]["maps"]],
        "post_readback_counter_delta": delta,
        "mean_input_bytes_per_copy": delta["input"] / delta["copies"],
        "recent_null_share_of_input_bytes": delta["recent_null"] / delta["input"],
    }


def summarize(root):
    artifacts = json.loads((root / "artifact-sha256.json").read_text())
    runtime = root / "artifacts" / "observe" / "ocerz"
    previous = json.loads((HERE / "results" / "raid-winemetalgl-renderer-pgo-ab.json").read_text())
    if hashlib.sha256(runtime.read_bytes()).hexdigest() != artifacts["instrumented_runtime"]:
        raise ValueError("Diagnostic runtime artifact changed")
    if artifacts["renderer"] != previous["renderer_artifacts"]["candidate"]["sha256"]:
        raise ValueError("Different renderer from the accepted baseline")
    config = ("hardware", "macos", "viewport", "vsync", "caster_count", "boss_count", "camera")
    return {
        **{name: previous[name] for name in config},
        "base_commit": "fa15866",
        "scope": "Observe fallback GL shadow copies after in-world readback with the accepted PGO renderer",
        "artifacts_sha256": artifacts,
        "verified_timed_script_rows": 26,
        "diagnostics": [read_diagnostic(root / f"diagnostic-{index}") for index in (1, 2)],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_root", type=Path)
    print(json.dumps(summarize(parser.parse_args().capture_root), indent=2))


if __name__ == "__main__":
    main()
