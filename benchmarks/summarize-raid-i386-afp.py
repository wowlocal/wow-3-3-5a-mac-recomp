"""Verify i386 hardware floating-point behavior and compare the live raid."""

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import re
from runpy import run_path
from statistics import median


HERE = Path(__file__).resolve().parent
TIMING = run_path(str(HERE / "summarize-raid-renderer-pgo.py"))
BLOCK = re.compile(r"^BLOCK rip=(0x[0-9a-f]+) .* n_insns=(\d+) .* pin_class=(\d+)")
INSN = re.compile(r"^  INSN (\d+) off=\d+ words=(\d+)  (.+)$")
FP = re.compile(r"^(?:v)?(?:add|sub|mul|div|sqrt)(?:ss|sd|ps|pd)$")


def word_shapes(path):
    shapes = defaultdict(set)
    block = None
    instructions = []
    for line in path.read_text().splitlines():
        if match := BLOCK.match(line):
            if block is not None:
                raise ValueError("JIT dump ended a block without its epilogue")
            block = match.group(1), int(match.group(2)), int(match.group(3))
            instructions = []
        elif match := INSN.match(line):
            index = int(match.group(1))
            if block is None or index != len(instructions):
                raise ValueError("Unexpected JIT instruction index")
            instructions.append((match.group(3), int(match.group(2))))
        elif line.startswith("  EPI "):
            if block is None or len(instructions) != block[1]:
                raise ValueError("Incomplete JIT block in diagnostic dump")
            text, words = zip(*instructions)
            shapes[(block[0], block[2], text)].add(words)
            block = None
    if block is not None or not shapes:
        raise ValueError("Incomplete or missing JIT dump")
    return shapes


def compare_words(control, candidate):
    common = control.keys() & candidate.keys()
    totals = defaultdict(lambda: {"sites": 0, "control_words": 0, "candidate_words": 0})
    matched = 0
    for shape in common:
        before, after = control[shape], candidate[shape]
        if len(before) != 1 or len(after) != 1:
            continue
        matched += 1
        for text, n, m in zip(shape[2], next(iter(before)), next(iter(after))):
            opcode = text.split()[0].lower()
            if not FP.fullmatch(opcode):
                continue
            row = totals[opcode]
            row["sites"] += 1
            row["control_words"] += n
            row["candidate_words"] += m
    if not totals:
        raise ValueError("No matching floating-point instruction shapes in the hot routine")
    return {"guest_routine": ["0x880c70", "0x882c7f"],
            "control_blocks": len(control), "candidate_blocks": len(candidate),
            "stable_shared_blocks": matched,
            "common_unstable_blocks": len(common) - matched,
            "floating_point_arm64_words": dict(sorted(totals.items()))}


def check_tests(root):
    cases = {}
    hand = []
    for mode in ("control", "afp"):
        differential = (root / f"diff32-{mode}.log").read_text()
        counts = [int(count) for count in re.findall(r"^differential32: (\d+) passed, 0 failed", differential, re.M)]
        if counts != [40044, 40044] or ("Differential JIT uses hardware AFP" in differential) != (mode == "afp"):
            raise ValueError(f"The {mode} i386 comparison is incomplete")
        cases[mode] = counts
        log = (root / f"fp-state-{mode}.log").read_text()
        details = re.findall(r"^(HAND_STATE\b.+)$", log, re.M)
        hashes = re.findall(r"^HAND_STATE_TOTAL ([0-9a-f]{16})$", log, re.M)
        if len(details) != 88 or len(hashes) != 2:
            raise ValueError(f"Missing the 44 hand-written states in both {mode} layouts")
        hand.append((details, hashes))
    if hand[0] != hand[1]:
        raise ValueError("Guest state changed between software and hardware floating-point checks")
    m32 = re.search(r"test_m32: (\d+) checks, 0 failed", (root / "m32-tests.log").read_text())
    if not m32:
        raise ValueError("General i386 compatibility tests failed")
    return {"differential_cases_offset_and_low_shadow": cases,
            "hand_corpus_identical_state_hashes": hand[0][1], "general_i386_checks": int(m32[1])}


def transition_probe(path):
    rows = []
    for line in path.read_text().splitlines():
        match = re.fullmatch(r"afp=([01]) pairs=(\d+) elapsed_ns=(\d+)", line)
        if not match or int(match[2]) != 2_000_000 or int(match[3]) <= 0:
            raise ValueError("Unexpected floating-point transition probe output")
        rows.append({"afp_enabled": bool(int(match[1])), "ns_per_pair": int(match[3]) / int(match[2])})
    if [int(row["afp_enabled"]) for row in rows] != [0, 1, 1, 0, 0, 1, 1, 0]:
        raise ValueError("Incomplete interleaved floating-point transition probe")
    control = median(row["ns_per_pair"] for row in rows if not row["afp_enabled"])
    enabled = median(row["ns_per_pair"] for row in rows if row["afp_enabled"])
    return {"samples": rows, "control_median_ns": control, "afp_median_ns": enabled,
            "afp_minus_control_ns": enabled - control}


def read_capture(capture, mode, diagnostic, artifacts):
    if json.loads((capture / "artifact-sha256.json").read_text()) != artifacts:
        raise ValueError(f"Changed runtime or renderer in {capture}")
    if (capture / "afp-mode.txt").read_text().split() != [str(mode), str(int(diagnostic))]:
        raise ValueError(f"Wrong floating-point mode in {capture}")
    window = TIMING["verify"](capture, "candidate", seconds=25 if diagnostic else 60)
    log = (capture / "client.log").read_text()
    active = re.findall(r"^ocerz: i386 AFP trial enabled=([01])$", log, re.M)
    dumps = list(capture.glob("jit.*"))
    if active != ([str(mode)] if diagnostic else []) or len(dumps) != int(diagnostic):
        raise ValueError(f"Unexpected floating-point instrumentation in {capture}")
    return window, dumps


def summarize(root, renderer):
    baseline = json.loads((HERE / "results" / "raid-winemetalgl-renderer-pgo-ab.json").read_text())
    artifacts = json.loads((root / "artifact-sha256.json").read_text())
    runtime = root / "artifacts" / "candidate" / "ocerz"
    renderer_digest = hashlib.sha256(renderer.read_bytes()).hexdigest()
    if (hashlib.sha256(runtime.read_bytes()).hexdigest() != artifacts["runtime_unsigned_sha256"] or
            renderer_digest != artifacts["renderer_sha256"] or
            renderer_digest != baseline["renderer_artifacts"]["candidate"]["sha256"]):
        raise ValueError("Unexpected runtime or renderer artifact")
    diagnostics = [read_capture(root / f"diagnostic-{mode}", mode, True, artifacts) for mode in (0, 1)]
    shapes = [word_shapes(dumps[0]) for _, dumps in diagnostics]
    timed = []
    for index, mode in enumerate((0, 1, 1, 0), 1):
        window, _ = read_capture(root / f"timed-{index}-{mode}", mode, False, artifacts)
        timed.append({"order": index, "mode": "candidate" if mode else "control", **window})
    settings = ("hardware", "macos", "viewport", "vsync", "caster_count", "boss_count", "camera")
    capabilities = (root / "layout-standard.log").read_text().splitlines()[0]
    probe = re.fullmatch(r"page_size=(\d+) kernel_query_symbol=(\d+) kernel_query=(\d+) tso_symbol=(\d+) tso_result=(-?\d+)",
                         capabilities)
    if not probe:
        raise ValueError("Missing the ad-hoc host capability probe")
    page_size, query_available, supported, tso_available, tso_result = map(int, probe.groups())
    return {
        **{key: baseline[key] for key in settings}, "base_commit": "e08d948",
        "ordinary_host_process": {"page_bytes": page_size, "cross_arch_query_available": bool(query_available),
                                  "cross_arch_supported": bool(supported), "tso_api_available": bool(tso_available),
                                  "tso_api_result": tso_result},
        "artifacts": artifacts, "tests": check_tests(root),
        "diagnostics": [{"afp_enabled": bool(mode), "gpu_window": row[0]} for mode, row in enumerate(diagnostics)],
        "jit_word_comparison": compare_words(*shapes),
        "isolated_host_transition_pair": transition_probe(root / "transitions.log"),
        **TIMING["summarize_timing"](timed),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_root", type=Path)
    parser.add_argument("renderer", type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize(args.capture_root, args.renderer), indent=2))


if __name__ == "__main__":
    main()
