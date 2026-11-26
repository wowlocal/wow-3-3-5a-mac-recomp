#!/usr/bin/env python3
"""Set the classic Mac client's saved content size while the client is closed."""

import argparse
import os
import re
import tempfile
from pathlib import Path

WINDOW_BOUNDS = re.compile(
    rb"(?m)^(\$Current User\\World of Warcraft\\Client\\WindowBounds\r?\n)"
    rb"(-?\d+),(-?\d+),-?\d+,-?\d+(\r?)$"
)


def resize(contents, width, height):
    if width <= 0 or height <= 0:
        raise ValueError("The window dimensions must be positive")

    def replace(match):
        left, top = int(match[2]), int(match[3])
        right, bottom = left + width, top + height
        if not all(-32768 <= edge <= 32767 for edge in (left, top, right, bottom)):
            raise ValueError("The window bounds exceed the classic Mac coordinate range")
        return match[1] + f"{left},{top},{right},{bottom}".encode("ascii") + match[4]

    updated, count = WINDOW_BOUNDS.subn(replace, contents)
    if count != 1:
        raise ValueError("Expected exactly one classic WoW WindowBounds entry")
    return updated


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("width", type=int)
    parser.add_argument("height", type=int)
    args = parser.parse_args()
    prefs = Path.home() / "Library/Preferences/com.blizzard.World of Warcraft.prefs"
    updated = resize(prefs.read_bytes(), args.width, args.height)
    descriptor, path = tempfile.mkstemp(prefix=".wow-window-", dir=prefs.parent)
    temporary = Path(path)
    try:
        with os.fdopen(descriptor, "wb") as output:
            os.fchmod(output.fileno(), prefs.stat().st_mode & 0o777)
            output.write(updated)
        temporary.replace(prefs)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"Saved window content size {args.width}×{args.height}")


if __name__ == "__main__":
    main()
