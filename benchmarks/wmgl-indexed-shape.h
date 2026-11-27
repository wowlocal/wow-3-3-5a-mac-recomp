#ifndef WMGL_INDEXED_SHAPE_H
#define WMGL_INDEXED_SHAPE_H

#include <stdint.h>

enum {
    WIDTH = 1365, HEIGHT = 768, DRAWS = 826, WRITES = 363, STORE = 2 * 1024 * 1024,
    MAX_SUBDIVISIONS = 32, MAX_IN_FLIGHT = 8, DEFAULT_IN_FLIGHT = 3, WARMUP = 120
};

// The same 32-bit data layout is read by the native and translated replays.
typedef struct { uint32_t count, vertices, indices, end; } Draw;
typedef struct { uint32_t from, to; } Upload;

#ifndef WMGL_I386_FRAME
static Draw draws[DRAWS];
static Upload uploads[WRITES];
static unsigned char vertex_bytes[STORE];
static size_t index_bytes;
static uint16_t indices[3 * MAX_SUBDIVISIONS * (MAX_SUBDIVISIONS + 1) * (2 * MAX_SUBDIVISIONS + 1) / 6];

static unsigned subdivision(unsigned triangles)
{
    for (unsigned n = 1; n <= MAX_SUBDIVISIONS; n++)
        if (n * n >= triangles) return n;
    return 0;
}

static void build_geometry(const char *histogram)
{
    FILE *file = fopen(histogram, "r");
    require(file != NULL, "could not open the triangle histogram");
    unsigned counts[DRAWS];
    unsigned used = 0, triangles = 0, count, copies;
    while (fscanf(file, "%u%u", &count, &copies) == 2) {
        require(count && subdivision(count) && copies <= DRAWS - used, "invalid triangle histogram entry");
        for (unsigned i = 0; i < copies; i++) counts[used++] = count;
        triangles += count * copies;
    }
    require(feof(file) && used == DRAWS, "the triangle histogram must cover all draws");
    fclose(file);

    // One index template per subdivision. Draws use just the required prefix.
    unsigned index_count = 0, index_start[MAX_SUBDIVISIONS + 1];
    for (unsigned n = 1; n <= MAX_SUBDIVISIONS; n++) {
        index_start[n] = index_count;
        unsigned row = 0;
        for (unsigned y = 0; y < n; y++) {
            const unsigned next = row + n - y + 1;
            for (unsigned x = 0; x < n - y; x++) {
                indices[index_count++] = (uint16_t)(row + x);
                indices[index_count++] = (uint16_t)(row + x + 1);
                indices[index_count++] = (uint16_t)(next + x);
                if (x + y + 1 < n) {
                    indices[index_count++] = (uint16_t)(row + x + 1);
                    indices[index_count++] = (uint16_t)(next + x + 1);
                    indices[index_count++] = (uint16_t)(next + x);
                }
            }
            row = next;
        }
        require(index_count - index_start[n] == 3 * n * n, "broken mesh template");
    }
    index_bytes = index_count * sizeof(indices[0]);

    size_t offset = 0;
    for (unsigned i = 0; i < DRAWS; i++) {
        const unsigned slot = i * WRITES / DRAWS;
        if (!i || (i - 1) * WRITES / DRAWS != slot)
            uploads[slot].from = (uint32_t)offset;
        // This permutation distributes large draws without using game meshes.
        const unsigned triangle_count = counts[(i * 593) % DRAWS];
        const unsigned n = subdivision(triangle_count);
        const unsigned vertices = (n + 1) * (n + 2) / 2;
        draws[i] = (Draw){ triangle_count * 3, (uint32_t)offset,
                          (uint32_t)(index_start[n] * sizeof(indices[0])), vertices - 1 };
        const float cx = ((slot % 8) - 3.5f) * 0.24f, cy = ((slot / 8 % 6) - 2.5f) * 0.30f;
        for (unsigned y = 0; y <= n; y++) {
            for (unsigned x = 0; x <= n - y; x++) {
                const float position[] = {cx - 0.08f + 0.16f * x / n + 0.08f * y / n,
                                          cy - 0.08f + 0.16f * y / n};
                require(offset + sizeof position <= STORE, "vertex stream exceeds the 2 MiB store");
                memcpy(vertex_bytes + offset, position, sizeof position);
                offset += sizeof position;
            }
        }
        uploads[slot].to = (uint32_t)offset;
    }
    printf("shape %u draws, %u triangles, %u uploads, %zu vertex bytes\n", DRAWS, triangles, WRITES, offset);
}

#endif

#endif
