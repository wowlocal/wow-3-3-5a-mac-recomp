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
for match in re.finditer(r"^\s*([0-9a-f]+):\s+([^\s;]+)",decoded,re.M):
    index=int(match[1],16)//4
    if 0<=index<len(words):
        lookup[words[index]]=match[2].lower()
assert len(lookup)>=len(words)*.95
groups=defaultdict(Counter)
for (guest,word),weight in samples.items():
    groups[guest][lookup.get(word,"unknown")]+=weight
report={
    "warm_cpu_periods":len(periods),
    "guest_operations":{
        guest:[{"arm_mnemonic":op,"cpu_usec":weight,"share":weight/sum(hist.values())}
               for op,weight in hist.most_common(18)]
        for guest,hist in sorted(groups.items())
    },
}
print(json.dumps(report,indent=2))
