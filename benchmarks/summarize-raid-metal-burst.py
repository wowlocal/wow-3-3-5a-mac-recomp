"""Match a consecutive raid capture to image fetches and Metal replay activity."""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import plistlib
import re
from runpy import run_path
import struct
import subprocess

from metal_submission_log import interval_coverage


HERE = Path(__file__).resolve().parent
VERIFY = run_path(str(HERE / "summarize-raid-renderer-pgo.py"))["verify"]
CAPTURE = run_path(str(HERE / "summarize-metal-frame-capture.py"))
SYSTEM = run_path(str(HERE / "summarize-raid-system-tail.py"))
BEGIN = re.compile(r"WMGL_CAPTURE begin stream=(\d+) after_sequence=(\d+) draws=(\d+)")
FRAME = re.compile(r"WMGL_CAPTURE frame sequence=(\d+) draws=(\d+)")
END = re.compile(r"WMGL_CAPTURE end sequence=(\d+) draws=(\d+)")
VIEWPORT = (1365, 768)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def command_layout(session):
    def children(*commands):
        args = ["gpudebug", "--json", "-s", str(session)]
        for command in commands:
            args.extend(("-c", command))
        output = subprocess.check_output(args, text=True)
        return json.loads(output.splitlines()[-1])["children"]

    commands = []
    for buffer in children("go /commands", "list --all"):
        path = f"/commands/{buffer['name']}"
        encoders = children(f"go {path}", "list --all")
        if len(encoders) != 1:
            raise ValueError("Expected one encoder in each archived command buffer")
        name = encoders[0]["name"]
        if name == "be0":
            commands.append({"index": int(buffer["name"][2:]), "encoder": "blit", "draws": 0})
        elif name == "re0":
            draws = children(f"go {path}/{name}", "list --all")
            commands.append({
                "index": int(buffer["name"][2:]),
                "encoder": "render",
                "draws": sum(bool(re.fullmatch(r"draw\d+", item["name"])) for item in draws),
                "color_attachments": [item["values"][-1]["value"] for item in draws if item["name"] == "color0"],
            })
        else:
            raise ValueError(f"Unexpected archive encoder {name}")
    return commands


def read_burst(capture, commands):
    window = json.loads((capture / "window.json").read_text())
    verified = VERIFY(capture, "candidate", seconds=35)
    if tuple(window["window_size"]) != VIEWPORT:
        raise ValueError("Expected the full raid viewport")
    text = (capture / "client.log").read_text()
    begin = list(re.finditer(rf"^{BEGIN.pattern}$", text, re.M))
    end = list(re.finditer(rf"^{END.pattern}$", text, re.M))
    if len(begin) != 1 or len(end) != 1:
        raise ValueError("Expected one complete burst in the client log")
    stream, prior_seq, prior_draws = map(int, begin[0].groups())
    final_seq, final_draws = map(int, end[0].groups())
    if window["stream"] != stream or final_seq != prior_seq + 4:
        raise ValueError("Expected four consecutive outputs on the verified stream")
    if len(commands) != 12:
        raise ValueError("Expected three archived command buffers per output")
    output_records = CAPTURE["read_completions"](capture / "gpu.ns", window["pid"], stream,
                                                  VIEWPORT[0] << 32 | VIEWPORT[1])
    if output_records[prior_seq][1] != prior_draws or output_records[final_seq][1] != final_draws:
        raise ValueError("Capture markers disagree with GPU completions")
    labels = Counter()
    frames = []
    counts = Counter()
    for line in text[begin[0].end():end[0].start()].splitlines():
        if line.startswith("WMGL_CAPTURE_DRAW "):
            counts[line.removeprefix("WMGL_CAPTURE_DRAW ")] += 1
            continue
        match = FRAME.fullmatch(line)
        if not match:
            continue
        sequence, draws = map(int, match.groups())
        index = len(frames) * 3
        group = commands[index:index + 3]
        if (sequence != prior_seq + len(frames) + 1 or sum(counts.values()) != draws or
                output_records[sequence][1] != draws or
                [row["encoder"] for row in group] != ["blit", "render", "render"] or
                sum(row["draws"] for row in group) != draws):
            raise ValueError("Archive draw split does not match the live frame")
        for row in group[1:]:
            if row["index"] not in (index + 1, index + 2) or not any(
                "1365x768" in value for value in row["color_attachments"]
            ):
                raise ValueError("Unexpected replay output attachment")
        frames.append({"capture_sequence": sequence, "draws": draws,
                       "render_draw_split": [row["draws"] for row in group[1:]],
                       "shader_combinations": len(counts)})
        labels.update(counts)
        counts.clear()
    if len(frames) != 4 or counts:
        raise ValueError("Expected four complete labeled draw lists")
    archive = capture / "raid-burst.gputrace"
    metadata = plistlib.loads((archive / "metadata").read_bytes())
    api_calls = (archive / "capture").read_bytes()
    if any(api_calls.count(label.encode()) < count for label, count in labels.items()):
        raise ValueError("Missing draw signposts in the archive")
    return {"frames": frames, "verified_live_window": verified,
            "archive_frame_count_metadata": metadata["DYCaptureEngine.captured_frames_count"],
            "archive_command_stream_sha256": hashlib.sha256(api_calls).hexdigest()}


def union_ns(spans):
    return interval_coverage(spans)(min(a for a, _ in spans), max(b for _, b in spans)) if spans else 0


def windowserver_intervals(path):
    columns = {"start", "duration", "state", "process"}
    spans = []
    for row in SYSTEM["exported_rows"](path, "metal-gpu-intervals", columns):
        if (row["process"].get("fmt", "").startswith("WindowServer (") and
                row["state"].get("fmt") == "Active"):
            start = int(row["start"].text)
            end = start + int(row["duration"].text)
            if end > start:
                spans.append((start, end))
    return spans


def png_digest(path):
    png = path.read_bytes()
    if png[:8] != b"\x89PNG\r\n\x1a\n" or png[12:16] != b"IHDR" or struct.unpack_from(">II", png, 16) != VIEWPORT:
        raise ValueError(f"Invalid full-size replay image in {path}")
    return hashlib.sha256(png).hexdigest()


def measure_replay(replay, frames, legacy_pid):
    witness = json.loads((replay / "fetch-witness.json").read_text())
    if witness["legacy_outputs_during_requests"] or [row["command_path"] for row in witness["fetches"]] != [
        "/commands/cb11/re0", "/commands/cb1/re0/draw0", "/commands/cb11/re0",
    ]:
        raise ValueError("Wrong replay requests or legacy output during replay")
    final = png_digest(replay / "burst-final-first.png")
    if final != png_digest(replay / "burst-final-second.png"):
        raise ValueError("Repeated final replay differs")
    hashes = [png_digest(replay / f"frame-{index}.png") for index in (1, 2, 3)] + [final]
    if len(set(hashes)) != 4:
        raise ValueError("Expected four distinct frame images")

    pid = witness["replayer_pid"]
    gpu_path = replay / "burst-fetch-gpu.xml"
    buffers, activity, counts = SYSTEM["gpu_activity"](gpu_path, pid, legacy_pid)
    desktop = windowserver_intervals(gpu_path)
    render = [(buffer, spans) for buffer, spans in buffers.items()
              if {stage for _, _, stage, _ in spans} == {"Vertex", "Fragment"}]
    render.sort(key=lambda item: min(start for start, _, _, _ in item[1]))
    if len(render) != 17:
        raise ValueError("Expected two eight-buffer replays and one rewind render buffer")
    encoding = {}
    columns = {"start", "process", "cmdbuffer-id", "event-type", "encoder-label"}
    for row in SYSTEM["exported_rows"](replay / "burst-fetch-encoders.xml",
                                       "metal-application-encoders-list", columns):
        if (row["process"].get("fmt", "").endswith(f" ({pid})") and row["event-type"].text == "Encoding" and
                (row["encoder-label"].text or "").startswith("Render Command")):
            encoding[int(row["cmdbuffer-id"].text)] = int(row["start"].text)
    encoding_groups = [[encoding[buffer] for buffer, _ in group if buffer in encoding]
                       for group in (render[:8], render[8:9], render[9:])]
    if any(not group or group != sorted(group) for group in encoding_groups):
        raise ValueError("Replay encoding order did not match GPU buffer order")
    encoding_gaps = [b[0] - a[-1] for a, b in zip(encoding_groups, encoding_groups[1:])]
    internal_gaps = [b - a for group in encoding_groups for a, b in zip(group, group[1:])]
    if not internal_gaps or min(encoding_gaps) <= max(internal_gaps):
        raise ValueError("Expected separately identifiable full replay and rewind requests")
    legacy = interval_coverage(activity["legacy"])
    groups = []
    for burst in (render[:8], render[9:]):
        result = []
        for index, frame in enumerate(frames):
            windows = [window for _, spans in burst[index * 2:index * 2 + 2] for window in spans]
            start = min(a for a, _, _, _ in windows)
            end = max(b for _, b, _, _ in windows)
            if legacy(start, end):
                raise ValueError("Earlier client GPU activity overlapped the replay frame")
            active = [(a, b) for a, b, _, _ in windows]
            desktop_in_span = [(max(a, start), min(b, end)) for a, b in desktop if a < end and b > start]
            render_ns = union_ns(active)
            result.append({
                "draws": frame["draws"],
                "vertex_active_ns": union_ns([(a, b) for a, b, stage, _ in windows if stage == "Vertex"]),
                "fragment_active_ns": union_ns([(a, b) for a, b, stage, _ in windows if stage == "Fragment"]),
                "render_active_union_ns": render_ns,
                "windowserver_overlap_ns": render_ns + union_ns(desktop_in_span) - union_ns(active + desktop_in_span),
                "elapsed_from_first_render_to_last_render_ns": end - start,
            })
        groups.append(result)
    return {"replay_requests": witness["fetches"], "replayed_image_sha256": hashes,
            "same_final_image_after_rewind": True, "replayer_gpu_intervals": counts["client"],
            "render_buffers_with_application_encoding": [len(group) for group in encoding_groups],
            "application_encoding_breaks_between_requests_ns": encoding_gaps,
            "gpu_render_frames": groups}


def summarize(root, legacy_pid):
    renderer = json.loads((root / "burst-artifacts.json").read_text())
    if renderer["renderer_sha256"] != sha256(root / "artifacts" / "candidate" / "libwinemetalgl.dylib"):
        raise ValueError("Captured renderer artifact changed")
    tests = (root / "gpu-tests.log").read_text()
    passed = re.search(r"(\d+) tests, 0 failed checks", tests)
    if not passed:
        raise ValueError("Renderer GPU tests did not pass")
    replay = root / "quiet-replay"
    commands = json.loads((replay / "commands.json").read_text())
    burst = read_burst(root / "live-burst-quiet", commands)
    return {"base_commit": "98fb312", "hardware": "Apple M4 Pro", "viewport": VIEWPORT,
            "vsync": False, "caster_count": 24, "boss_count": 1, "artifacts": renderer,
            "gpu_tests_passed": int(passed[1]), "live_burst": burst,
            "full_gpu_replay": measure_replay(replay, burst["frames"], legacy_pid)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    tasks = parser.add_subparsers(dest="mode", required=True)
    index = tasks.add_parser("commands")
    index.add_argument("session", type=int)
    report = tasks.add_parser("summary")
    report.add_argument("capture_root", type=Path)
    report.add_argument("--legacy-pid", type=int, required=True)
    args = parser.parse_args()
    result = command_layout(args.session) if args.mode == "commands" else summarize(args.capture_root, args.legacy_pid)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
