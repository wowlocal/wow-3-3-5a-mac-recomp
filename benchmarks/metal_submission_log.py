from bisect import bisect_right
from collections import namedtuple


Submission = namedtuple("Submission", "stream serial draws submit start end kernel_start kernel_end callback")


def read_submissions(path, pid):
    with path.open() as log:
        for line in log:
            if not line.endswith("\n"):
                break
            process, kind, *fields = line.split()
            if int(process) != pid:
                continue
            if kind not in ("render", "present") or len(fields) != len(Submission._fields):
                raise ValueError("Invalid command-buffer submission record")
            entry = Submission(*map(int, fields))
            if not 0 < entry.submit <= entry.start <= entry.end <= entry.callback:
                raise ValueError(f"Unordered timestamps for {kind} submission {entry.serial}")
            yield kind, entry


def interval_coverage(intervals):
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            first, last = merged[-1]
            merged[-1] = first, max(last, end)
        else:
            merged.append((start, end))
    ends = [end for _, end in merged]

    def covered(start, end):
        total = 0
        for i in range(bisect_right(ends, start), len(merged)):
            first, last = merged[i]
            if first >= end:
                break
            total += min(end, last) - max(start, first)
        return total

    return covered
