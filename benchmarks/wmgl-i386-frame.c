#define WMGL_I386_FRAME
#include "wmgl-i386-frame.h"
#include <OpenGL/gl.h>
#include <OpenGL/glext.h>

// Compiled into a relocation-free i386 function. The harness supplies guest
// addresses for the geometry and the AArchX import traps.
uint32_t render_frame(const I386Frame *frame, unsigned stamp, int submit)
{
    frame->clear(GL_COLOR_BUFFER_BIT);
    frame->buffer_data(GL_ARRAY_BUFFER, STORE, 0, GL_DYNAMIC_DRAW);
    unsigned previous = WRITES;
    for (unsigned i = 0; i < DRAWS; i++) {
        const unsigned slot = i * WRITES / DRAWS;
        if (slot != previous) {
            const Upload upload = frame->uploads[slot];
            const uint32_t size = upload.to - upload.from;
            const unsigned char *data = frame->vertices + upload.from;
            if (slot % 16 == 0) {
                frame->subdata(GL_ARRAY_BUFFER, upload.from, size, data);
            } else {
                unsigned char *base = frame->map(GL_ARRAY_BUFFER, GL_WRITE_ONLY);
                if (!base) return 0;
                frame->copy(base + upload.from, data, size);
                frame->flush(GL_ARRAY_BUFFER, upload.from, size);
                if (!frame->unmap(GL_ARRAY_BUFFER)) return 0;
            }
            previous = slot;
        }
        const Draw draw = frame->draws[i];
        frame->pointer(2, GL_FLOAT, 0, (const void *)(uintptr_t)draw.vertices);
        frame->color(227, slot + 40, stamp, 255);
        frame->draw(GL_TRIANGLES, 0, draw.end, draw.count, GL_UNSIGNED_SHORT,
                    (const void *)(uintptr_t)draw.indices);
    }
    if (!submit) return 1;
    const uint32_t fence = frame->fence(GL_SYNC_GPU_COMMANDS_COMPLETE, 0);
    if (!fence) return 0;
    frame->swap(frame->agl);
    return fence;
}
