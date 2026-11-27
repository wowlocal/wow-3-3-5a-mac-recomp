import argparse
from bisect import bisect_right
from collections import Counter
import json
from pathlib import Path
import re
import subprocess

from i386_guest_profile import read_guest_pc_periods


def read_unwind_ranges(binary):
    command = ['xcrun', 'llvm-dwarfdump', '--arch=i386', '--eh-frame', str(binary)]
    range_pattern = re.compile(r'\bFDE\b.*\bpc=([0-9a-fA-F]+)\.\.\.([0-9a-fA-F]+)')
    ranges = set()
    with subprocess.Popen(command, stdout=subprocess.PIPE, text=True) as process:
        for line in process.stdout:
            match = range_pattern.search(line)
            if match:
                start, end = (int(value, 16) for value in match.groups())
                if start < end:
                    ranges.add((start, end))
        if process.wait():
            raise subprocess.CalledProcessError(process.returncode, command)

    ordered = sorted(ranges)
    if not ordered or any(left[1] > right[0] for left, right in zip(ordered, ordered[1:])):
        raise ValueError('Expected non-overlapping i386 unwind ranges')
    return ordered


def attribute_weights(guest_pcs, ranges):
    starts = [start for start, _ in ranges]
    weights = Counter()
    for pc, weight in guest_pcs.items():
        index = bisect_right(starts, pc) - 1
        if index >= 0 and pc < ranges[index][1]:
            weights[ranges[index]] += weight
    return weights


def main():
    parser = argparse.ArgumentParser(description='Group weighted i386 guest PCs by Mach-O unwind range')
    parser.add_argument('capture', type=Path, help='directory containing the headless client.log')
    parser.add_argument('binary', type=Path, help='local Mach-O game executable')
    options = parser.parse_args()

    periods = read_guest_pc_periods(options.capture)
    total_weight = sum(cpu_usec for cpu_usec, _ in periods)
    guest_pcs = Counter()
    for _, pcs in periods:
        guest_pcs.update(pcs)
    translated_weight = sum(guest_pcs.values())

    ranges = read_unwind_ranges(options.binary)
    range_weights = attribute_weights(guest_pcs, ranges)
    matched_weight = sum(range_weights.values())
    if not matched_weight:
        raise ValueError('No sampled addresses match the supplied executable')

    report = {
        'warm_periods': len(periods),
        'total_sampled_cpu_usec': total_weight,
        'translated_cpu_usec': translated_weight,
        'distinct_guest_pcs': len(guest_pcs),
        'unwind_ranges': len(ranges),
        'sampled_unwind_ranges': len(range_weights),
        'matched_translated_cpu_usec': matched_weight,
        'matched_fraction_of_translated_cpu': matched_weight / translated_weight,
        'unmatched_translated_cpu_usec': translated_weight - matched_weight,
        'cumulative_share_of_all_sampled_cpu': {
            count: sum(weight for _, weight in range_weights.most_common(count)) / total_weight
            for count in (1, 4, 8, 16, 32, 64)
        },
        'hottest_matched_ranges': [
            {
                'start': hex(start),
                'end_exclusive': hex(end),
                'cpu_usec': weight,
                'share_of_all_sampled_cpu': weight / total_weight,
            }
            for (start, end), weight in range_weights.most_common(16)
        ],
    }
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
