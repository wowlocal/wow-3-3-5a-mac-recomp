"""Verify a completed native 5K raid run and its in-client graphics settings."""

import argparse
import hashlib
import json
from pathlib import Path
import re


WIDTH, HEIGHT = 5120, 2880
SETTINGS = {
    "farclip": 1277, "terrainDetail": 1, "shadowLevel": 0, "particleDensity": 1, "environmentDetail": 1.5,
    "groundEffectDensity": 64, "groundEffectDist": 140, "baseMip": 0, "extShadowQuality": 5,
    "textureFilteringMode": 5, "weatherDensity": 3, "componentTextureLevel": 9,
    "specular": 1, "ffxGlow": 1, "ffxDeath": 1, "projectedTextures": 1, "gxResolution": "5120x2880",
    "gxVSync": 0, "gxMultisample": 4, "maxFPS": 0, "maxFPSBk": 0,
}
TOKEN = re.compile(r'--[^\n]*|"(?:(?:\\.)|[^"\\])*"|[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?|\w+|[{}\[\]=,]')


def read_saved_variables(path):
    tokens = iter(token for token in TOKEN.findall(path.read_text()) if not token.startswith("--"))

    def literal(raw):
        if raw.startswith('"'):
            return json.loads(raw)
        if raw in ("nil", "true", "false"):
            return {"nil": None, "true": True, "false": False}[raw]
        if raw == "[":
            raise ValueError("Unexpected Lua indexed value")
        return float(raw) if "." in raw or "e" in raw.lower() else int(raw)

    def value(raw):
        if raw != "{":
            return literal(raw)
        contents = {}
        sequential = 1
        for field in tokens:
            if field == "}":
                return [contents[i] for i in range(1, sequential)] if sequential > 1 else contents
            if field == "[":
                key = literal(next(tokens))
                if next(tokens) != "]" or next(tokens) != "=":
                    raise ValueError("Malformed saved variable key")
                contents[key] = value(next(tokens))
            else:
                contents[sequential] = value(field)
                sequential += 1
            if next(tokens) != ",":
                raise ValueError("Malformed saved variable separator")
        raise ValueError("Incomplete Lua settings table")

    if next(tokens, None) != "WrathNative5KReport" or next(tokens, None) != "=":
        raise ValueError("Missing graphics witness variable")
    return value(next(tokens))


def agrees(actual, wanted):
    if isinstance(wanted, str):
        return actual == wanted
    try:
        return abs(float(actual) - wanted) < 1e-5
    except (TypeError, ValueError):
        return False


def check_graphics(state):
    if state is None:
        return {"captured": False, "issues": ["The in-client settings witness is empty"]}
    issues = []
    if state.get("marker") != "complete":
        issues.append("The add-on did not finish reading the live settings")
    settings = dict(SETTINGS)
    limits = {}
    shadow_limit = state.get("shadowLimit")
    if shadow_limit not in (None, "", "?"):
        limits["extShadowQuality"] = float(shadow_limit)
        settings["extShadowQuality"] = min(settings["extShadowQuality"], limits["extShadowQuality"])
    for name, expected in settings.items():
        if not agrees(state.get(name), expected):
            issues.append(f"{name} reported {state.get(name)!r}, expected {expected!r}")
    if "terrainDetailBefore" in state and not agrees(state["terrainDetailBefore"], settings["terrainDetail"]):
        issues.append(f"Terrain detail before the witness ran was {state['terrainDetailBefore']!r}")
    formats = state.get("formats", [])
    selected = int(state.get("format", 0))
    if (not isinstance(formats, list) or len(formats) % 3 or not 1 <= selected <= len(formats) // 3 or
            max(formats[2::3]) != settings["gxMultisample"] or formats[3 * selected - 1] != settings["gxMultisample"]):
        issues.append("The client did not select its highest listed multisample format")
    return {"captured": True, "actual": state, "expected": settings, "client_limits": limits, "issues": issues}


def fixture_matches(rows):
    if len(rows) != 28 or rows[1] != "25":
        return False
    online, map_id, zone, x, y, z, angle = rows[0].split("\t")
    if ((online, map_id, zone) != ("1", "0", "12") or
            any(abs(float(observed) - expected) >= tolerance for observed, expected, tolerance in (
                (x, -8947.22, .02), (y, -133.353, .02), (z, 83.6899, .02), (angle, 6.11982, .001),
            ))):
        return False
    spells = (11113, 1449, 15237, 26573, 6343)
    expected = [(990500, 1, 1, 1100, 1100, 2600, 2600, 5, 36, 0, 1)]
    for actor in range(25):
        delay = 350 + 135 * actor
        spell = spells[actor % len(spells)]
        cycle = 5100 if spell == 26573 else 1500 + 110 * (actor % 5)
        expected.append((990500 + actor, 0, 1, delay, delay, cycle, cycle, 11, spell, 2, 1))
    return [tuple(map(int, row.split("\t"))) for row in rows[2:]] == sorted(expected)


def verify(case, saved, *, measurement_seconds=60):
    window = json.loads((case / "window.json").read_text())
    monitor = json.loads((case / "monitor.log").read_text().splitlines()[-1])
    isolation = json.loads((case / "legacy.json").read_text())
    fixture = (case / "fixture.tsv").read_text().splitlines()
    client = (case / "client.log").read_text()
    attach = f"trial WineMetalGL attached a {WIDTH}x{HEIGHT} offscreen drawable"
    viewport = f"native trial GL viewport 0 0 {WIDTH} {HEIGHT} draw-fbo 0 samples 4 buffers 1"
    if (window["window_size"] != [WIDTH, HEIGHT]
            or window["measurement_duration_ns"] != measurement_seconds * 1_000_000_000
            or window["missing_sequences"] or window["verified_completed_renders"] < 60 or
            attach not in client or viewport not in client):
        raise ValueError("The timed run lacks a native 5K, 4× MSAA GPU completion and viewport witness")
    if (not fixture_matches(fixture)
            or monitor["seen"] != [window["pid"]] or monitor["activationNotifications"]
            or monitor["maxVisibleWindows"] or isolation != {
                "legacy_paused": True, "legacy_completed_frames_during_launch": 0,
            }):
        raise ValueError("The timed fixture, visibility or process isolation differs")
    image = (case / "world.ppm").read_bytes()
    header = f"P6\n{WIDTH} {HEIGHT}\n255\n".encode("ascii")
    if not image.startswith(header) or len(image) != len(header) + WIDTH * HEIGHT * 3:
        raise ValueError("The full-resolution world readback is missing")
    corners = [
        max(image[i:i + 3]) for y in (HEIGHT // 8, 7 * HEIGHT // 8) for x in (WIDTH // 8, 7 * WIDTH // 8)
        for i in [len(header) + (y * WIDTH + x) * 3]
    ]
    if any(not light for light in corners):
        raise ValueError("The world readback has an uncovered corner")
    graphics = check_graphics(read_saved_variables(saved) if saved else None)
    return {
        "viewport": [0, 0, WIDTH, HEIGHT],
        "multisample_count": 4,
        "world_readback_sha256": hashlib.sha256(image).hexdigest(),
        "graphics": graphics,
        "verified_cast_rows": 26,
        "verified_gpu_window": window,
        "artifact_sha256": json.loads((case / "artifact-sha256.json").read_text()),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    parser.add_argument("--settings", type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.capture, args.settings), indent=2))
