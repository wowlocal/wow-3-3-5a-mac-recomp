from collections import Counter
import re


def read_guest_pc_periods(capture):
    """Read the complete in-world periods of an instruction-level capture."""
    marker = 'saved Metal trial backbuffer readback'
    _, separator, in_world = (capture / 'client.log').read_text().rpartition(marker)
    if not separator:
        raise ValueError('The capture has no in-world readback marker')

    periods = []
    header = re.compile(r'\] \d+s cpu_ms=(\d+) translated=')
    translation = re.compile(r'\] +translated_pc=(0x[0-9a-f]+) cpu_us=(\d+)')
    for line in in_world.splitlines():
        match = header.search(line)
        if match:
            periods.append((int(match[1]) * 1_000, Counter()))
        elif periods and (match := translation.search(line)):
            periods[-1][1][int(match[1], 16)] += int(match[2])

    complete = periods[1:-1]
    if len(complete) < 4 or any(not cpu_us or not pcs for cpu_us, pcs in complete):
        raise ValueError('Expected at least four complete in-world periods with guest PCs')
    return complete
