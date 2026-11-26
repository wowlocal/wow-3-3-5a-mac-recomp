#!/bin/zsh
set -eu

if (( $# != 6 )) || [[ "$4" != 120 && "$4" != 600 ]]; then
    print -u2 'Usage: capture-northshire.sh PID FRAME_LOG REPORT 120|600 KEY_HELPER MOUSE_HELPER'
    exit 2
fi
repo_dir=${0:A:h:h}
pid=$1
report=${3:A}
duration=$4
key_helper=${5:A}
mouse_helper=${6:A}
if [[ -e "$report" ]]; then
    print -u2 'Choose an unused report path.'
    exit 1
fi

movement_cycle() {
    "$key_helper" "$pid" w 700
    "$key_helper" "$pid" s 1090
    "$key_helper" "$pid" q 640
    "$key_helper" "$pid" e 640
    "$key_helper" "$pid" a 410
    "$key_helper" "$pid" d 410
}

mouse_cycle() {
    "$key_helper" "$pid"
    "$mouse_helper" "$pid" 0.62 0.66 left 1100 0.73 0.66
    "$mouse_helper" "$pid" 0.73 0.66 left 1100 0.62 0.66
    "$mouse_helper" "$pid" 0.62 0.66 right 1200
}

"$key_helper" "$pid"
report_dir=$(mktemp -d "${report:h}/.northshire.XXXXXX")
temporary_report="$report_dir/result.json"
measurement=
cleanup() {
    if [[ -n "$measurement" ]] && kill -0 "$measurement" 2>/dev/null; then
        kill -INT "$measurement" 2>/dev/null || true
        wait "$measurement" 2>/dev/null || true
    fi
    if [[ -f "$temporary_report" ]]; then
        unlink "$temporary_report"
    fi
    rmdir "$report_dir"
}
trap cleanup EXIT

deadline=$((SECONDS + duration))
python3 "$repo_dir/scripts/measure.py" "$pid" "$2" "$temporary_report" --seconds "$duration" &
measurement=$!
sleep 1
cycle=0
while (( SECONDS + 5 < deadline )); do
    if ! kill -0 "$measurement" 2>/dev/null; then
        print -u2 'The frame recorder stopped before the route finished.'
        exit 1
    fi
    movement_cycle
    cycle=$((cycle + 1))
    if (( cycle % 5 == 0 && (cycle <= 10 || duration == 600) && SECONDS + 4 < deadline )); then
        mouse_cycle
    fi
done
wait "$measurement"
measurement=
"$key_helper" "$pid"
python3 -c 'import os, sys; os.link(sys.argv[1], sys.argv[2])' "$temporary_report" "$report"
