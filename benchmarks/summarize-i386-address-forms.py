from collections import Counter
from pathlib import Path
import json
import re
import subprocess
import sys

from i386_guest_profile import read_guest_pc_periods

capture=Path(sys.argv[1])
binary=Path(sys.argv[2])
periods = read_guest_pc_periods(capture)
total = sum(cpu_usec for cpu_usec, _ in periods)
weights = Counter()
for _, pcs in periods:
    weights.update(pcs)

selected={}
instruction=re.compile(r'^\s*([0-9a-f]+):\s+([^\s]+)\s*(.*)$')
arguments=['xcrun','llvm-objdump','--arch=i386','-d','--no-show-raw-insn',str(binary)]
with subprocess.Popen(arguments, stdout=subprocess.PIPE, text=True) as process:
    in_text = False
    for line in process.stdout:
        if line.startswith('(') and ' section' in line:
            in_text = line.strip() == '(__TEXT,__text) section'
            continue
        if not in_text:
            continue
        row = instruction.match(line)
        if not row:
            continue
        address = int(row[1], 16)
        if address in weights:
            selected[address] = row[2], row[3].split('#', 1)[0].strip()
    assert process.wait() == 0

def operands(string):
    parts=[]
    head=0
    parens=0
    for index,char in enumerate(string):
        if char=='(':
            parens+=1
        elif char==')':
            parens-=1
        elif char==',' and not parens:
            parts.append(string[head:index].strip())
            head=index+1
    parts.append(string[head:].strip())
    return parts

families=Counter()
kinds=Counter()
frame_moves=Counter()
for address, (mnemonic, text) in selected.items():
    if mnemonic.startswith(("lea","nop")): continue
    amount=weights[address]
    arguments=operands(text)
    is_move=bool(re.fullmatch(r'mov[blwq]',mnemonic))
    direction=('load' if '(' in arguments[0] else 'store') if is_move and len(arguments)==2 else 'other'
    for argument in arguments:
        actual=re.search(r'(-?(?:0x[0-9a-f]+|[0-9]+))?\(%([a-z0-9]+)(,.*?)?\)',argument)
        if not actual: continue
        disp=int(actual[1],0) if actual[1] else 0
        base=actual[2]
        mode='indexed' if actual[3] else 'base_only'
        family=(base if base in ('ebp','esp') else 'other', mode)
        families[family]+=amount
        kinds[(mnemonic,direction,*family)]+=amount
        if base=='ebp' and mode=='base_only' and is_move:
            frame_moves[(direction,'negative' if disp<0 else 'nonnegative')]+=amount
        break

matched=sum(weights[address] for address in selected)
assert matched>.7*sum(weights.values())

def rows(values, names, limit=None):
    return [
        {**dict(zip(names,key)),'cpu_usec':weight,'share_of_all_sampled_cpu':weight/total}
        for key,weight in values.most_common(limit)
    ]

report={
    'warm_periods':len(periods),
    'total_sampled_cpu_usec':total,
    'translated_cpu_usec':sum(weights.values()),
    'matched_guest_address_samples_cpu_usec':matched,
    'matched_fraction_of_translated_cpu':matched/sum(weights.values()),
    'distinct_guest_pcs_matched_to_static_instructions':len(selected),
    'memory_address_base':rows(families,('base','indexing')),
    'high_weight_guest_address_forms':rows(kinds,('opcode','move_direction','base','indexing'),12),
    'ebp_move_without_index':rows(frame_moves,('direction','displacement_sign')),
}
print(json.dumps(report,indent=2))
