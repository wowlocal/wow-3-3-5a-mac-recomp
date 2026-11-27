"""Test the patched transient allocator with real Metal storage and a simulated queue."""

import argparse
from pathlib import Path
import subprocess
import tempfile


HEADERS = {
    "core/metal/queue.h": """
#pragma once
#include <cstdint>
namespace wmgl {
class Queue {
public:
    bool isComplete(uint64_t serial) { return serial != 7 || completed; }
    bool completed = false;
};
}
""",
    "core/metal/device.h": """
#pragma once
#import <Metal/Metal.h>
namespace wmgl {
class Device {
public:
    static Device& get() { static Device device; return device; }
    id<MTLDevice> mtl() { return device_; }
private:
    id<MTLDevice> device_ = MTLCreateSystemDefaultDevice();
};
}
""",
    "core/stats.h": """
#pragma once
#include <cstdint>
namespace wmgl::stats {
struct Frame { uint64_t transientBytes; };
inline void count(uint64_t Frame::*, uint64_t) {}
}
""",
    "core/util/log.h": """
#pragma once
namespace wmgl {
enum class LogLevel { Warn };
enum class LogCategory { Gpu };
inline void logOnce(LogLevel, LogCategory, const char*, const char*, ...) {}
}
""",
}

PROBE = """
#import <Foundation/Foundation.h>
#include <cassert>
#include <cstdio>
#include "core/metal/transient.h"

int main() {
    @autoreleasepool {
        constexpr size_t chunkSize = 4u << 20;
        wmgl::Queue queue;
        wmgl::TransientAllocator allocator(queue);
        id<MTLBuffer> first;
        {
            wmgl::TransientAllocator::Reservation pending(allocator, chunkSize, 4);
            assert(pending.allocation().data);
            first = pending.allocation().buffer;
            auto* bytes = static_cast<uint8_t*>(pending.allocation().data);
            bytes[0] = 0x75;
            bytes[chunkSize - 1] = 0x3b;
            const auto second = allocator.allocate(chunkSize, 1, 4);
            const auto third = allocator.allocate(chunkSize, 2, 4);
            assert(second.buffer && third.buffer);
            assert(second.buffer != first && third.buffer != first);
            pending.usedBy(7);
            assert(bytes[0] == 0x75 && bytes[chunkSize - 1] == 0x3b);
        }
        const auto outstanding = allocator.allocate(chunkSize, 3, 4);
        assert(outstanding.buffer && outstanding.buffer != first);
        queue.completed = true;
        const auto recycled = allocator.allocate(chunkSize, 8, 4);
        assert(recycled.buffer == first && recycled.offset == 0);
        std::puts("Reservation protected its storage until the attached serial completed");
    }
}
"""


def run(renderer):
    source = renderer / "src"
    with tempfile.TemporaryDirectory(prefix="wmgl-reservation-") as tmp:
        root = Path(tmp)
        for name, contents in HEADERS.items():
            header = root / name
            header.parent.mkdir(parents=True, exist_ok=True)
            header.write_text(contents)
        probe = root / "probe.mm"
        probe.write_text(PROBE)
        binary = root / "probe"
        subprocess.run([
            "xcrun", "clang++", "-std=c++20", "-fobjc-arc", "-O2",
            "-I", str(root), "-I", str(source),
            str(probe), str(source / "core/metal/transient.mm"),
            "-framework", "Foundation", "-framework", "Metal", "-o", str(binary),
        ], check=True)
        subprocess.run([str(binary)], check=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("renderer", type=Path, help="WineMetalGL checkout with the reservation patch applied")
    run(parser.parse_args().renderer.resolve())
