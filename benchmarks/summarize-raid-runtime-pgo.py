"""Compare the AArchX builds while using the same profiled renderer."""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
from runpy import run_path


HERE = Path(__file__).resolve().parent
PGO = run_path(str(HERE / "summarize-raid-renderer-pgo.py"))


def summarize_native(folder, artifact_shas, renderer):
    state = json.loads((folder / "profile-mode.json").read_text())
    if state["runtime_sha256"] != artifact_shas[state["runtime_mode"]] or state["renderer_sha256"] != renderer:
        raise ValueError("The CPU trace used different binaries")
    read_window = PGO["VERIFY_WINDOW"]
    window = read_window(folder, 34)
    pid = json.loads((folder / "window.json").read_text())["pid"]
    profiler = run_path(str(HERE / "summarize-raid-cpu-trace.py"))
    native, weights = Counter(), Counter()
    total = 0
    columns = {"thread", "thread-state", "weight", "stack"}
    groups = {
        "shadow_lookup_or_allocation": {"shadow_for"},
        "guest_memory_leaf_helpers": {"leaf_memmove", "leaf_memset", "leaf_bzero"},
        "guest_gl_map_or_unmap": {"sp_glMapBuffer", "sp_glMapBufferRange", "sp_glUnmapBuffer", "map_common"},
    }
    for row in profiler["read_table"](folder / "time-profile.xml", "time-profile", columns):
        if not profiler["is_main_thread"](row["thread"][0], pid) or row["thread-state"][1] != "Running":
            continue
        weight = int(row["weight"][1])
        total += weight
        frames = row["stack"]
        if frames and frames[0][2] == "ocerz":
            native[frames[0][1] or "Unattributed"] += weight
        stack = {(fn or "").lstrip("_") for _, fn, image in frames if image == "ocerz"}
        for group, names in groups.items():
            mapped_lookup = (group == "shadow_lookup_or_allocation" and "lookup" in stack
                             and groups["guest_gl_map_or_unmap"].intersection(stack))
            if names.intersection(stack) or mapped_lookup:
                weights[group] += weight
    if not total:
        raise ValueError("No running main-thread profile samples")
    return {
        **state,
        "diagnostic_gpu_window": window,
        "main_thread_running_weight_ns": total,
        "selected_native_call_groups": {
            group: {"inclusive_sample_weight_ns": weights[group], "share_of_running_samples": weights[group] / total}
            for group in groups
        },
        "highest_weight_native_leaves": [
            {"symbol": symbol, "weight_ns": weight, "share_of_running_samples": weight / total}
            for symbol, weight in native.most_common(12)
        ],
    }


def summarize(root):
    renderer = (root / "renderer-sha256.txt").read_text().strip()
    previous = json.loads((HERE / "results/raid-winemetalgl-renderer-pgo-ab.json").read_text())
    if renderer != previous["renderer_artifacts"]["candidate"]["sha256"]:
        raise ValueError("The renderer differs from the accepted profiled library")
    artifact_shas = {
        kind: hashlib.sha256((root / "artifacts" / kind / "ocerz").read_bytes()).hexdigest()
        for kind in ("training", "control", "candidate")
    }

    def capture(folder, mode):
        window = PGO["verify"](folder, mode, renderer_kind="candidate")
        witness = json.loads((folder / "runtime-sha256.json").read_text())
        if witness["artifact"] != artifact_shas[mode] or len(witness["signed_app"]) != 64:
            raise ValueError(f"Wrong AArchX artifact in {folder}")
        return {**window, "signed_runtime_sha256": witness["signed_app"]}

    training = [capture(root / f"training-{index}", "training") for index in (1, 2)]
    order = ("control", "candidate", "candidate", "control")
    timed = [
        {"order": index, "mode": mode, **capture(root / f"timed-{index}-{mode}", mode)}
        for index, mode in enumerate(order, 1)
    ]
    for mode in ("control", "candidate"):
        if len({row["signed_runtime_sha256"] for row in timed if row["mode"] == mode}) != 1:
            raise ValueError(f"The signed runtime changed between {mode} runs")
    result = {
        "runtime_artifact_sha256": artifact_shas,
        "fixed_renderer_sha256": renderer,
        "runtime_build_validation": json.loads((root / "build-flags.json").read_text()),
        "training_captures": training,
        **PGO["summarize_timing"](timed),
    }
    if (root / "native-candidate" / "time-profile.xml").exists():
        result["follow_up_native_profile"] = summarize_native(root / "native-candidate", artifact_shas, renderer)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_root", type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize(args.capture_root), indent=2))


if __name__ == "__main__":
    main()
