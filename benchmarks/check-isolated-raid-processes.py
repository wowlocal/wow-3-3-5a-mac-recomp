import argparse
from pathlib import Path
import subprocess
import sys
import time


def active_trials(client_root):
    rows = subprocess.check_output(["/bin/ps", "-ax", "-o", "pid=,comm="], text=True)
    trials = []
    for row in rows.splitlines():
        fields = row.split(None, 1)
        if len(fields) != 2:
            continue
        pid, image = fields
        if not image.endswith(".app/Contents/MacOS/ocerz"):
            continue
        path = Path(image).resolve()
        if path.is_relative_to(client_root):
            trials.append((int(pid), path.parents[2].name))
    return trials


def main():
    parser = argparse.ArgumentParser(description="Check that an isolated raid client has no running trial instances.")
    parser.add_argument("client_root", type=Path)
    options = parser.parse_args()
    client_root = options.client_root.resolve(strict=True)
    deadline = time.monotonic() + 3
    trials = active_trials(client_root)
    while trials and time.monotonic() < deadline:
        time.sleep(0.15)
        trials = active_trials(client_root)
    for pid, bundle in trials:
        print(f"Isolated raid instance still running: {pid} {bundle}", file=sys.stderr)
    return int(bool(trials))


if __name__ == "__main__":
    sys.exit(main())
