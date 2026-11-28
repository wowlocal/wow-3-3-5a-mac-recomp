"""Export live native 5K quads and compare native kernels at observed wave sizes."""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
from runpy import run_path


HERE = Path(__file__).resolve().parent
QUADS = run_path(str(HERE / "export-raid-rotated-quads.py"))
VERIFY = run_path(str(HERE / "verify-raid-native-5k.py"))["verify"]


def export(root, binary, footprints, case_file):
    baseline = json.loads((HERE / "results/raid-native-5k-max-baseline.json").read_text())
    profile = json.loads((HERE / "results/raid-native-5k-guest-profile.json").read_text())
    layout = json.loads(footprints.read_text())
    output = root / "quad-5k"
    artifacts = json.loads((output / "artifact-sha256.json").read_text())
    original = dict(baseline["artifact_sha256"], renderer_sha256=profile["artifact_sha256"]["renderer_sha256"])
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    if (digest != profile["guest_image_sha256"] or digest != layout["guest_image_sha256"]
            or layout["artifact_sha256"]["renderer_sha256"] != artifacts["renderer_sha256"]
            or artifacts != original):
        raise ValueError("The input packets or measured waves used a different client or renderer")
    expected = {QUADS["BEGIN"]: ("movss", "-0x2c(%ebp), %xmm0"),
                QUADS["END"]: ("movl", "0x134(%esi), %ebx")}
    instructions = QUADS["SCENE"]["PHASE"]["GUEST"](binary, tuple(expected))
    if {pc: (op, args.strip()) for pc, (op, args) in instructions.items()} != expected:
        raise ValueError("The rotated quad input or output site changed")

    shapes = Counter()
    for capture in layout["captures"]:
        for size, count in capture["admitted_wave_quad_histogram"].items():
            shapes[int(size)] += count
    waves = sorted(size for size, count in shapes.items() for _ in range(count))
    if not waves or waves[len(waves) // 2] <= 0:
        raise ValueError("Missing a measured positive parallel wave size")
    stats = QUADS["LIFE"]["distribution"](waves)
    batch_sizes = sorted({round(stats["median"]), stats["p99"]})

    captures = []
    with case_file.open("wb") as file:
        for index in (1, 2):
            case = root / f"native-5k-quad-packets-{index}"
            run = VERIFY(case, case / "graphics-settings.lua", measurement_seconds=45)
            if (run["graphics"]["issues"] or run["graphics"]["actual"] != baseline["graphics"]["actual"]
                    or run["artifact_sha256"] != artifacts or
                    (case / "color-only-msaa-mode.txt").read_text().split() != ["1", "0"]
                    or (case / "pass-mode.txt").read_text().split() != ["2048", "1"]):
                raise ValueError("The native 5K packet recording differs from the measured raid")
            captures.append({"verified_gpu_window": run["verified_gpu_window"], **QUADS["read_capture"](case, file)})
    count = sum(run["all_gate_counts"]["exported_replay_cases"] for run in captures)
    if case_file.stat().st_size != count * QUADS["CASE"].size:
        raise ValueError("The exported native replay size does not match the verified packet count")
    return {
        "artifact_sha256": artifacts, "guest_image_sha256": digest,
        "case_file_sha256": hashlib.sha256(case_file.read_bytes()).hexdigest(), "exported_replay_cases": count,
        "admitted_wave_quad_count": len(waves), "admitted_wave_quad_sizes": stats,
        "live_batch_sizes": batch_sizes, "captures": captures,
    }


def report(root, export_log):
    evidence = json.loads(export_log.read_text())
    results = []
    for batch in evidence["live_batch_sizes"]:
        sample = QUADS["report"](export_log, root / f"replay-{batch}.log", root / "sanitizer.log", live_size=batch)
        results.append({
            "live_wave_quads": batch,
            "verified_replay_modes": sample["verified_replay_modes"],
            "native_replay_isolation": sample["native_replay_isolation"],
            "native_packet_timings": sample["native_packet_timings"],
        })
    return {**evidence, "native_replays": results}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    prepare = commands.add_parser("export")
    for name in ("capture_root", "i386_executable", "footprint_summary", "case_file"):
        prepare.add_argument(name, type=Path)
    finish = commands.add_parser("report")
    finish.add_argument("replay_root", type=Path)
    finish.add_argument("export_log", type=Path)
    args = parser.parse_args()
    if args.operation == "export":
        result = export(args.capture_root, args.i386_executable, args.footprint_summary, args.case_file)
    else:
        result = report(args.replay_root, args.export_log)
    print(json.dumps(result, indent=2))
