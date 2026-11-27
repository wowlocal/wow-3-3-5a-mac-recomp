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


def read_stack_window(capture):
    window = json.loads((capture / 'window.json').read_text())
    if window['missing_sequences'] != 0:
        raise ValueError('Stack window requires consecutive GPU completions')
    with (capture / 'gpu.ns').open() as stream:
        anchors = []
        for line in stream:
            if not line.endswith('\n'):
                break
            pid, stream_id, sequence, time, _, _ = map(int, line.split())
            if (pid, stream_id, sequence) == (window['pid'], window['stream'], window['first_sequence']):
                anchors.append(time)
    if len(anchors) != 1:
        raise ValueError('Expected one GPU timestamp at the stack window start')
    start = anchors[0]
    end = start + window['measurement_duration_ns']
    samples = []
    first = None
    last = None
    with (capture / 'i386-stacks.ns').open() as stream:
        for line in stream:
            if not line.endswith('\n'):
                break
            time, weight, leaf, *returns = line.split()
            time, weight = int(time), int(weight)
            if (last is not None and time < last) or weight <= 0:
                raise ValueError('Expected ordered stack samples with positive CPU weights')
            if first is None:
                first = time
            last = time
            if start <= time < end:
                samples.append((weight, int(leaf, 16), [int(pc, 16) for pc in returns]))
    if not samples or first > start or last < end:
        raise ValueError('Stack samples do not cover the GPU measurement window')
    return window, samples


def find_i386_call_sites(binary, return_addresses):
    command = ['xcrun', 'llvm-objdump', '--arch=i386', '-d', str(binary)]
    call = re.compile(r'^\s*([0-9a-f]+):\t([0-9a-f ]+)\tcalll\s')
    sites = {}
    with subprocess.Popen(command, stdout=subprocess.PIPE, text=True) as process:
        in_text = False
        for line in process.stdout:
            if line.startswith('(') and ' section' in line:
                in_text = line.strip() == '(__TEXT,__text) section'
            if not in_text or '\tcalll\t' not in line or not (match := call.match(line)):
                continue
            pc = int(match[1], 16)
            return_address = pc + len(match[2].split())
            if return_address in return_addresses:
                sites[return_address] = pc
        if process.wait():
            raise subprocess.CalledProcessError(process.returncode, command)
    return sites


def summarize_stack_samples(samples, ranges, call_sites, compared_entries=()):
    starts = [start for start, _ in ranges]

    def containing_range(pc):
        if pc is None:
            return None
        index = bisect_right(starts, pc) - 1
        if index >= 0 and pc < ranges[index][1]:
            return ranges[index][0]
        return None

    total_weight = sum(weight for weight, _, _ in samples)
    frame_depth = Counter()
    leaf_weights = Counter()
    inclusive_weights = Counter()
    descendant_weights = {}
    adjacent_weights = {}
    overlap_weight = 0
    translated_weight = 0
    for weight, leaf, returns in samples:
        if not leaf:
            continue
        translated_weight += weight
        leaf_entry = containing_range(leaf)
        if leaf_entry is not None:
            leaf_weights[leaf_entry] += weight
        entries = [leaf_entry]
        for address in returns:
            call = call_sites.get(address)
            if call is None:
                break
            entries.append(containing_range(call))
        frame_depth[len(entries) - 1] += weight
        known_entries = set(entries) - {None}
        for entry in known_entries:
            inclusive_weights[entry] += weight
        if compared_entries and known_entries.issuperset(compared_entries):
            overlap_weight += weight
        for callee, caller in set(zip(entries, entries[1:])):
            if callee is not None and caller is not None and callee != caller:
                adjacent_weights.setdefault(caller, Counter())[callee] += weight
        for caller in set(entries[1:]) - {None}:
            if leaf_entry is not None and leaf_entry != caller:
                descendant_weights.setdefault(caller, Counter())[leaf_entry] += weight
    if not inclusive_weights or not sum(weight for depth, weight in frame_depth.items() if depth):
        raise ValueError('No validated i386 caller frames in the capture')

    def fraction(weight):
        return weight / total_weight

    def ranked(weights, entry):
        return [
            {'entry': hex(child), 'cpu_usec': amount}
            for child, amount in weights.get(entry, Counter()).most_common(5)
        ]

    report = {
        'samples': len(samples),
        'total_cpu_usec': total_weight,
        'translated_cpu_usec': translated_weight,
        'cpu_usec_by_validated_call_depth': dict(sorted(frame_depth.items())),
        'share_of_all_cpu_with_validated_caller': fraction(
            sum(weight for depth, weight in frame_depth.items() if depth)
        ),
        'hottest_inclusive_unwind_ranges': [
            {
                'entry': hex(entry),
                'inclusive_cpu_usec': weight,
                'exclusive_cpu_usec': leaf_weights[entry],
                'inclusive_share_of_all_sampled_cpu': fraction(weight),
                'exclusive_share_of_all_sampled_cpu': fraction(leaf_weights[entry]),
                'top_sampled_callees': ranked(adjacent_weights, entry),
                'top_descendant_leaves': ranked(descendant_weights, entry),
            }
            for entry, weight in inclusive_weights.most_common(24)
        ],
    }
    if compared_entries:
        first, second = compared_entries
        union_weight = inclusive_weights[first] + inclusive_weights[second] - overlap_weight
        report['compared_entries'] = {
            'entries': [hex(first), hex(second)],
            'inclusive_cpu_usec': [inclusive_weights[first], inclusive_weights[second]],
            'overlap_cpu_usec': overlap_weight,
            'share_of_all_cpu_in_either_entry': fraction(union_weight),
        }
    return report


def stack_report(capture, binary, compared_entries):
    window, samples = read_stack_window(capture)
    return_addresses = {address for _, _, returns in samples for address in returns}
    ranges = read_unwind_ranges(binary)
    call_sites = find_i386_call_sites(binary, return_addresses)
    return {'gpu_window': window, **summarize_stack_samples(samples, ranges, call_sites, compared_entries)}


def main():
    parser = argparse.ArgumentParser(description='Group weighted i386 guest PCs by Mach-O unwind range')
    parser.add_argument('capture', type=Path, help='directory containing the headless client.log')
    parser.add_argument('binary', type=Path, help='local Mach-O game executable')
    parser.add_argument('--stacks', action='store_true', help='attribute i386-stacks.ns in the verified GPU window')
    parser.add_argument('--compare-entries', nargs=2, type=lambda value: int(value, 0), default=(),
                        metavar='ADDRESS', help='measure overlap of two unwind entries in stack samples')
    options = parser.parse_args()
    if options.compare_entries and not options.stacks:
        parser.error('--compare-entries requires --stacks')
    if options.stacks:
        print(json.dumps(stack_report(options.capture, options.binary, options.compare_entries), indent=2))
        return

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
