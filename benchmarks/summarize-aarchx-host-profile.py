from collections import Counter, defaultdict
from pathlib import Path
import json, re, subprocess, sys, tempfile

run=Path(sys.argv[1])
log=(run/"client.log").read_text().rsplit("saved Metal trial backbuffer readback",1)[-1]
periods=[]
current=None
for line in log.splitlines():
    if re.search(r"GUESTPROF\[\d+\] \d+s cpu_ms=",line):
        current=Counter()
        periods.append(current)
        continue
    row=re.search(r"host_word guest=(\S+) arm64=([0-9a-f]{8}) cpu_us=(\d+)",line)
    if row and current is not None:
        current[(row[1],int(row[2],16))]+=int(row[3])
periods=periods[1:-1]
assert len(periods)>=4 and all(periods)
samples=Counter()
for period in periods: samples.update(period)
words=sorted({word for _,word in samples})
with tempfile.TemporaryDirectory(prefix="headless-jit-decode.") as path:
    folder=Path(path)
    asm=folder/"words.s"
    obj=folder/"words.o"
    asm.write_text(".text\n.p2align 2\n"+"".join(f".long 0x{word:08x}\n" for word in words))
    subprocess.run(["xcrun","clang","-arch","arm64","-c",str(asm),"-o",str(obj)],check=True,capture_output=True)
    decoded=subprocess.check_output(["xcrun","llvm-objdump","-d","--no-show-raw-insn",str(obj)],text=True)
lookup={}
for match in re.finditer(r"^[ \t]*([0-9a-f]+):[ \t]+([^\s;]+)[ \t]*(.*)$",decoded,re.M):
    index=int(match[1],16)//4
    if 0<=index<len(words):
        lookup[words[index]]=(match[2].lower(),match[3])
assert len(lookup)>=len(words)*.95
groups=defaultdict(Counter)
jcc_offsets = defaultdict(Counter)
for (guest,word),weight in samples.items():
    opcode, operands = lookup.get(word, ("unknown", ""))
    groups[guest][opcode] += weight
    if guest == "jcc" and opcode in ("stp", "ldr"):
        address = re.search(r"\[x20(?:,\s*#(-?(?:0x[0-9a-f]+|[0-9]+)))?\]", operands)
        if address:
            offset = int(address[1], 0) if address[1] else 0
            jcc_offsets[opcode][offset] += weight
report={
    "warm_cpu_periods":len(periods),
    "jcc_x20_memory_offsets": {
        opcode: [
            {"byte_offset": offset, "cpu_usec": weight,
             "share_of_arm_opcode": weight / groups["jcc"][opcode]}
            for offset, weight in offsets.most_common()
        ]
        for opcode, offsets in sorted(jcc_offsets.items())
    },
    "guest_operations":{
        guest:[{"arm_mnemonic":op,"cpu_usec":weight,"share":weight/sum(hist.values())}
               for op,weight in hist.most_common(18)]
        for guest,hist in sorted(groups.items())
    },
}
print(json.dumps(report,indent=2))
