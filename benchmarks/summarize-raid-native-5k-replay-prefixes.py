"""Check native 5K Metal replay prefixes against their application and GPU traces."""

import argparse
from collections import Counter, defaultdict
from datetime import datetime
import hashlib
from itertools import groupby
import json
from pathlib import Path
import re
from runpy import run_path
from statistics import median
import struct
import subprocess
from xml.etree import ElementTree

from metal_submission_log import interval_coverage


HERE = Path(__file__).resolve().parent
SYSTEM = run_path(str(HERE / "summarize-raid-system-tail.py"))
CAPTURE = run_path(str(HERE / "summarize-metal-frame-capture.py"))
VERIFY = run_path(str(HERE / "verify-raid-native-5k.py"))["verify"]
SIZE = (5120, 2880)
PREFIXES = (
    (256, 512, 768, 939),
    (939, 768, 512, 256),
    (512, 939, 256, 768),
)


def json_records(text):
    decoder = json.JSONDecoder()
    while (offset := text.find("\n{")) >= 0 or text.startswith("{"):
        offset = 0 if text.startswith("{") else offset + 1
        value, end = decoder.raw_decode(text, offset)
        yield value
        text = text[end:]


def snapshot_draw_states(case, session):
    """Save the pipeline and depth state for every draw in the measured encoder."""
    base = ["gpudebug", "--json", "-s", str(session)]
    status = subprocess.check_output(["gpudebug", "-s", str(session), "-c", "status"], text=True)
    if str((case / "raid-frame.gputrace").resolve()) not in status or "1037 draw calls" not in status:
        raise ValueError("The Metal debugger session is not inspecting the verified raid frame")
    _, _, _, labels = CAPTURE["read_capture_log"](case / "client.log")
    draws, pipelines, depths = [], {}, {}
    for first in range(0, 939, 32):
        indexes = range(first, min(first + 32, 939))
        commands = base + [argument for index in indexes for argument in (
            "-c", f"go /commands/cb1/re0/draw{index}", "-c", "info", "-c", "info pipeline",
        )]
        records = list(json_records(subprocess.check_output(commands, text=True)))
        states = [row for row in records if row.get("text", "").startswith("-[MTLRenderCommandEncoder draw")]
        descriptions = [row for row in records if "colorAttachments" in row and "sampleCount" in row]
        if len(states) != len(indexes) or len(descriptions) != len(indexes):
            raise ValueError(f"Some draws starting at {first} lack their pipeline or encoder state")
        for index, state, info in zip(indexes, states, descriptions):
            name = info["name"]
            if pipelines.setdefault(name, info) != info:
                raise ValueError("A pipeline descriptor differs between archived draws")
            depth = re.match(r"@dss\d+", state["Depth stencil state"])[0]
            depths.setdefault(depth, None)
            draws.append({"draw": index, "shader_label": labels[index], "pipeline": name, "depth_state": depth})
    for depth in depths:
        records = json_records(subprocess.check_output(base + ["-c", f"info {depth}"], text=True))
        depths[depth] = next(row for row in records if "depthCompareFunction" in row)
    return {"draws": draws, "depth": depths, "pipelines": pipelines}


def read_draw_states(profile, labels):
    source = json.loads((profile / "archived-draw-states.json").read_text())
    draws = source["draws"]
    if [row["draw"] for row in draws] != list(range(939)) or [row["shader_label"] for row in draws] != labels[:939]:
        raise ValueError("The state snapshot does not cover the ordered main pass")
    states = []
    for row in draws:
        pipeline = source["pipelines"][row["pipeline"]]
        depth = source["depth"][row["depth_state"]]
        stencil = "Always, fail=Keep, zFail=Keep, pass=Keep"
        if (pipeline["sampleCount"] != "4" or pipeline["depth"] != "Depth32Float_Stencil8"
                or depth["frontFaceStencil"] != stencil or depth["backFaceStencil"] != stencil):
            raise ValueError("The observed main pass has a different MSAA or depth/stencil setup")
        blend = re.search(r"blendEnabled:[ \t]*(yes|no)", pipeline["colorAttachments"])[1] == "yes"
        rgba = re.search(r"writeMask:[ \t]*(\w*)", pipeline["colorAttachments"])[1] == "RGBA"
        states.append((blend, rgba, depth["depthCompareFunction"], depth["depthWriteEnabled"] == "yes"))

    opaque = (False, True, "LessEqual", True)
    runs = []
    for eligible, group in groupby(enumerate(states), key=lambda item: item[1] == opaque):
        if eligible:
            selection = list(group)
            runs.append({"first_draw": selection[0][0], "draws": len(selection)})
    by_range = []
    for first, end in zip((0, 256, 512, 768), (256, 512, 768, 939)):
        values = states[first:end]
        by_range.append({
            "first_draw": first, "draws": len(values),
            "blended_draws": sum(blend for blend, _, _, _ in values),
            "unblended_full_rgba_lequal_depth_write_draws": values.count(opaque),
        })
    return {
        "draws": len(states), "pipeline_states": len(source["pipelines"]), "depth_stencil_states": len(source["depth"]),
        "unblended_full_rgba_lequal_depth_write_runs": runs, "draw_ranges": by_range,
    }


def read_profile(profile):
    def listing(name):
        data = list(json_records((profile / name).read_text()))
        return next(item for item in reversed(data) if "children" in item)["children"]

    isolation = {mode: json.loads((profile / f"profile-{mode}-isolation.json").read_text())
                 for mode in ("overlapping", "serial")}
    for mode, witness in isolation.items():
        log = list(json_records((profile / f"profile-{mode}.log").read_text()))
        if (witness["exit_code"] or not witness["legacy_paused"] or witness["legacy_completed_frames_during_replay"]
                or not any(item.get("success") for item in log)):
            raise ValueError(f"The {mode} profiling collection was unverified")

    encoder = next(row for row in json_records((profile / "serial-encoders-re0.txt").read_text())
                   if row.get("Encoder type") == "Render")
    command_path = next(row["links"]["d"] for row in json_records((profile / "serial-encoders-re0.txt").read_text())
                        if "links" in row)
    serial_encoders = listing(f"session-{isolation['serial']['session']}-encoders.json")
    main = next(row for row in serial_encoders if row["name"] == "re0")
    if command_path != "commands/cb1/re0" or len(main["values"]) != 4:
        raise ValueError("The reported limiter counters refer to a different render encoder")

    values = {}
    for group in ("pre-fragment_stage", "fragment_shader"):
        values.update({row["name"]: row["values"][0]["value"] for row in listing(f"serial-main-{group}.jsonl")
                       if row.get("values")})
    profile_commands = listing(f"session-{isolation['serial']['session']}-commands.json")
    if len(profile_commands) != 1037 or any(item["values"][-1]["type"] != "percentage" for item in profile_commands):
        raise ValueError("The serial profiler did not enumerate the archived draws")
    return {
        "archive_encoder_path": command_path,
        "hardware": "Apple M4 Pro",
        "profiled_mode": "serial at default GPU state",
        "vertex_active_ms": float(main["values"][2]["value"].removesuffix(" ms")),
        "fragment_active_ms": float(main["values"][3]["value"].removesuffix(" ms")),
        "limiter_counters": {key: value for key, value in encoder.items() if key.endswith("Limiter")},
        "raster_and_fragment_counters": {key: values[key] for key in (
            "pixels_rasterized", "prez_test_fails", "fs_invocations", "average_pixel_overdraw",
            "fs_texture_cache_miss_rate", "fs_texture_l1_bytes_read",
        )},
        "draws_with_nonzero_profiled_cost": sum(bool(item["values"][-1]["value"]) for item in profile_commands),
        "isolation": isolation,
    }


def ns_union(spans):
    return interval_coverage(spans)(min(a for a, _ in spans), max(b for _, b in spans)) if spans else 0


def read_prefixes(directory, depths):
    witness = json.loads((directory / "fetch-witness.json").read_text())
    pid = witness["replayer_pid"]
    trace = ElementTree.parse(directory / "toc.xml").getroot()
    target = trace.find(".//target/process")
    if (target is None or target.get("pid") != str(pid) or target.get("name") != "GPUToolsReplayService"
            or witness["trace_status"] or witness["legacy_completed_frames"]):
        raise ValueError(f"The replay was not isolated in {directory.name}")
    begin, end = [witness[f"clock_{name}"] for name in ("before", "after")]
    offsets = [sample["realtime_ns"] - sample["uptime_ns"] for sample in (begin, end)]
    if abs(offsets[0] - offsets[1]) > 1_000_000 or max(begin["uncertainty_ns"], end["uncertainty_ns"]) > 1_000_000:
        raise ValueError("The host clocks changed during the prefix capture")
    anchor = round(datetime.fromisoformat(trace.findtext(".//start-date")).timestamp() * 1_000_000_000) - offsets[0]
    buffers, activity, counts = SYSTEM["gpu_activity"](directory / "gpu.xml", pid, witness["legacy_pid"], True)
    if counts.get("legacy"):
        raise ValueError("The old client used the GPU during the prefix capture")
    columns = {"start", "process", "event-type", "encoder-label", "cmdbuffer-label", "cmdbuffer-id"}
    encoders = defaultdict(list)
    for row in SYSTEM["exported_rows"](directory / "encoders.xml", "metal-application-encoders-list", columns):
        if (row["process"].get("fmt", "").endswith(f" ({pid})") and row["event-type"].text == "Encoding"
                and row["encoder-label"].text == "Render Command 0" and row["cmdbuffer-label"].text == "Command Buffer 0"
                and row["cmdbuffer-id"].text):
            encoders[int(row["cmdbuffer-id"].text)].append(int(row["start"].text))

    requests = witness["fetches"]
    if [row["draw_limit"] for row in requests] != [item for depth in depths for item in (1, depth)]:
        raise ValueError("The archive was not rewound before every measured prefix")
    seen = set()
    results = []
    for request in requests:
        depth = request["draw_limit"]
        if request["command_path"] != f"/commands/cb1/re0/draw{depth - 1}":
            raise ValueError("An unexpected encoder or draw was replayed")
        a, b = (request[f"{phase}_uptime_ns"] - anchor for phase in ("start", "end"))
        if not 0 <= a < b:
            raise ValueError("The replay fetch fell outside its trace")
        candidates = [(key, data) for key, data in buffers.items() if (
            a - 2_000_000 <= min(start for start, _, _, _ in data) and
            max(end for _, end, _, _ in data) <= b + 2_000_000 and
            len(encoders[key]) == 1 and a - 2_000_000 <= encoders[key][0] <= b + 2_000_000 and
            any(stage == "Fragment" for _, _, stage, _ in data)
        )]
        if len(candidates) != 1 or candidates[0][0] in seen:
            raise ValueError(f"The archived render command buffer is ambiguous at draw {depth} in {directory.name}")
        buffer, spans = candidates[0]
        seen.add(buffer)
        vertex = [(a, b) for a, b, stage, _ in spans if stage == "Vertex"]
        fragment = [(a, b) for a, b, stage, _ in spans if stage == "Fragment"]
        desktop_on_fragment = []
        for a, b in fragment:
            desktop_on_fragment.extend((max(a, c), min(b, d)) for c, d in activity["windowserver"] if c < b and d > a)
        png = (directory / request["png"]).read_bytes()
        if (png[:8] != b"\x89PNG\r\n\x1a\n" or png[12:16] != b"IHDR" or tuple(struct.unpack_from(">II", png, 16)) != SIZE
                or request["image_bytes"] != len(png)):
            raise ValueError("The debugger did not return a native 5K render attachment")
        results.append({
            "draws": depth,
            "render_command_buffer": buffer,
            "vertex_active_ns": ns_union(vertex),
            "fragment_active_ns": ns_union(fragment),
            "windowserver_overlap_with_fragment_ns": ns_union(desktop_on_fragment),
            "image_sha256": hashlib.sha256(png).hexdigest(),
        })
    return results


def summarize(case):
    reference = json.loads((HERE / "results/raid-native-5k-metal-frame-capture.json").read_text())
    fixture = VERIFY(case, case / "graphics-settings.lua", measurement_seconds=25)
    if fixture != reference["verified_fixture"]:
        raise ValueError("The replay archive no longer matches the verified native 5K fixture")
    stream, sequence, _, labels = CAPTURE["read_capture_log"](case / "client.log")
    if (stream, sequence, len(labels)) != (reference["stream"], reference["captured_sequence"], reference["captured_output_draws"]):
        raise ValueError("The captured output markers differ from the verified source")
    profile = case / "profiling-5k"
    runs = [read_prefixes(profile / f"prefix-trace-{index}", depths) for index, depths in enumerate(PREFIXES, 1)]
    depths = [1, 256, 512, 768, 939]
    groups = {}
    for depth in depths:
        rows = [row for capture in runs for row in capture if row["draws"] == depth]
        checksums = {row["image_sha256"] for row in rows}
        if len(checksums) != 1:
            raise ValueError(f"Repeated native 5K prefixes produced different images at {depth} draws")
        fragments = [row["fragment_active_ns"] for row in rows]
        groups[depth] = {
            "observations": len(rows), "fragment_active_ns_median": median(fragments),
            "fragment_active_ns_min": min(fragments), "fragment_active_ns_max": max(fragments),
            "without_recorded_windowserver_overlap": sum(not row["windowserver_overlap_with_fragment_ns"] for row in rows),
            "image_sha256": checksums.pop(),
        }
    if len({row["image_sha256"] for row in groups.values()}) != len(depths):
        raise ValueError("Expected the native 5K prefix checkpoints to draw different images")
    return {
        "viewport": reference["viewport"], "multisample_count": fixture["multisample_count"],
        "verified_cast_rows": fixture["verified_cast_rows"], "verified_settings": fixture["graphics"]["actual"],
        "source_output_sequence": sequence,
        "archive_command_stream_sha256": hashlib.sha256((case / "raid-frame.gputrace/capture").read_bytes()).hexdigest(),
        "hardware_profile": read_profile(profile),
        "archived_main_pass_states": read_draw_states(profile, labels),
        "prefix_summary": groups,
        "observations_in_request_order": runs,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("live_frame_capture", type=Path)
    parser.add_argument("--snapshot-draw-states", type=int, metavar="GPUDEBUG_SESSION",
                        help="export the ordered Metal pipeline and depth state")
    args = parser.parse_args()
    result = (snapshot_draw_states(args.live_frame_capture, args.snapshot_draw_states)
              if args.snapshot_draw_states else summarize(args.live_frame_capture))
    print(json.dumps(result, indent=2))
