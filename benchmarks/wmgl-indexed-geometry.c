#include "winemetalgl/driver.h"

#include <OpenGL/gl.h>
#include <OpenGL/glext.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <sys/mman.h>

#define FUNCTIONS(X) \
    X(glBindBuffer) X(glBindProgramARB) X(glBufferData) X(glBufferParameteriAPPLE) X(glBufferSubData) \
    X(glClear) X(glClearColor) X(glClientWaitSync) X(glColor4ub) X(glDeleteSync) X(glDrawRangeElements) \
    X(glEnable) X(glEnableClientState) X(glFenceSync) X(glFlushMappedBufferRangeAPPLE) \
    X(glGenBuffers) X(glGenProgramsARB) X(glGetError) X(glLoadIdentity) X(glMapBuffer) X(glMatrixMode) \
    X(glProgramStringARB) X(glReadPixels) X(glTranslatef) X(glUnmapBuffer) X(glVertexPointer) X(glViewport)

#define DECLARE(name) static __typeof__(&name) run_##name;
FUNCTIONS(DECLARE)
#undef DECLARE

static void require(int condition, const char *description)
{
    if (!condition) {
        fprintf(stderr, "%s\n", description);
        exit(1);
    }
}

static void load(void)
{
#define RESOLVE(name) \
    run_##name = (__typeof__(run_##name))wmgl_get_proc_address(#name); \
    require(run_##name != NULL, "missing " #name);
    FUNCTIONS(RESOLVE)
#undef RESOLVE
}

#include "wmgl-indexed-shape.h"

static void program(GLenum target, const char *source)
{
    GLuint name;
    run_glGenProgramsARB(1, &name);
    run_glBindProgramARB(target, name);
    run_glProgramStringARB(target, GL_PROGRAM_FORMAT_ASCII_ARB, (GLsizei)strlen(source), source);
    run_glEnable(target);
    require(run_glGetError() == GL_NO_ERROR, "could not load the synthetic ARB program");
}

static int profile_storage;
static int select_matrix_mode;
static unsigned allocated, released, startup_allocations, measured;
static uint64_t storage_nanos[60000];

static void *allocate_storage(size_t bytes)
{
    if (bytes != STORE) return NULL;
    const size_t alignment = 65536;
    void *mapped = mmap(NULL, bytes + alignment, PROT_READ | PROT_WRITE, MAP_ANON | MAP_PRIVATE, -1, 0);
    require(mapped != MAP_FAILED, "could not allocate probe vertex storage");
    const uintptr_t start = (uintptr_t)mapped;
    const uintptr_t aligned = (start + alignment - 1) & ~(alignment - 1);
    const size_t prefix = aligned - start;
    if (prefix)
        require(munmap(mapped, prefix) == 0, "could not trim the start of vertex storage");
    require(munmap((void *)(aligned + bytes), alignment - prefix) == 0,
            "could not trim the end of vertex storage");
    ++allocated;
    return (void *)aligned;
}

static void release_storage(void *storage, size_t bytes)
{
    require(munmap(storage, bytes) == 0, "could not release vertex storage");
    ++released;
}

static int compare_nanos(const void *left, const void *right)
{
    uint64_t a = *(const uint64_t *)left, b = *(const uint64_t *)right;
    return (a > b) - (a < b);
}

static uint64_t time_ns(void) { return clock_gettime_nsec_np(CLOCK_UPTIME_RAW); }

static void render(unsigned stamp)
{
    if (select_matrix_mode) {
        run_glMatrixMode(GL_PROJECTION);
        run_glLoadIdentity();
        run_glMatrixMode(GL_MODELVIEW);
        run_glLoadIdentity();
        run_glTranslatef(stamp % 2 ? -0.015625f : 0.015625f, 0, 0);
    }
    run_glClear(GL_COLOR_BUFFER_BIT);
    const int sample_storage = profile_storage && stamp >= WARMUP;
    const uint64_t before = sample_storage ? time_ns() : 0;
    run_glBufferData(GL_ARRAY_BUFFER, STORE, NULL, GL_DYNAMIC_DRAW);
    if (sample_storage) {
        require(measured < sizeof(storage_nanos) / sizeof(storage_nanos[0]), "too many storage timing samples");
        storage_nanos[measured++] = time_ns() - before;
    }
    unsigned previous = WRITES;
    for (unsigned i = 0; i < DRAWS; i++) {
        unsigned slot = i * WRITES / DRAWS;
        if (slot != previous) {
            const Upload upload = uploads[slot];
            const GLsizeiptr size = upload.to - upload.from;
            if (slot % 16 == 0) {
                run_glBufferSubData(GL_ARRAY_BUFFER, upload.from, size, vertex_bytes + upload.from);
            } else {
                unsigned char *base = run_glMapBuffer(GL_ARRAY_BUFFER, GL_WRITE_ONLY);
                require(base != NULL, "could not map a vertex range");
                memcpy(base + upload.from, vertex_bytes + upload.from, (size_t)size);
                run_glFlushMappedBufferRangeAPPLE(GL_ARRAY_BUFFER, upload.from, size);
                require(run_glUnmapBuffer(GL_ARRAY_BUFFER), "could not unmap a vertex range");
            }
            previous = slot;
        }
        if (select_matrix_mode) run_glMatrixMode(i % 2 ? GL_MODELVIEW : GL_PROJECTION);
        const Draw d = draws[i];
        run_glVertexPointer(2, GL_FLOAT, 0, (const void *)(uintptr_t)d.vertices);
        run_glColor4ub(227, (GLubyte)(slot + 40), (GLubyte)stamp, 255);
        run_glDrawRangeElements(GL_TRIANGLES, 0, d.end, d.count, GL_UNSIGNED_SHORT,
                               (const void *)(uintptr_t)d.indices);
    }
}

static void wait_frame(GLsync fence)
{
    GLenum status = run_glClientWaitSync(fence, 0, UINT64_C(2000000000));
    require(status == GL_ALREADY_SIGNALED || status == GL_CONDITION_SATISFIED, "GPU output did not complete");
    run_glDeleteSync(fence);
}

int main(int argc, char **argv)
{
    require(argc == 2 || argc == 3, "usage: wmgl-indexed-geometry TRIANGLE_HISTOGRAM [IN_FLIGHT]");
    unsigned flight = DEFAULT_IN_FLIGHT;
    if (argc == 3) {
        char *end = NULL;
        unsigned long value = strtoul(argv[2], &end, 10);
        require(end && !*end && value > 0 && value <= MAX_IN_FLIGHT, "in-flight limit must be 1 to 8");
        flight = (unsigned)value;
    }
    require(wmgl_initialize(WMGL_DRIVER_ABI_VERSION) == 0, "could not initialize WineMetalGL");
    const char *record_storage = getenv("OCERZ_WMGL_PROBE_STORAGE");
    profile_storage = record_storage && strcmp(record_storage, "1") == 0;
    if (profile_storage) wmgl_set_optional_buffer_memory(allocate_storage, release_storage);
    const char *matrix_mode = getenv("OCERZ_WMGL_PROBE_MATRIX_MODE");
    select_matrix_mode = matrix_mode && strcmp(matrix_mode, "1") == 0;
    load();
    struct wmgl_pixel_format formats[256];
    unsigned available = wmgl_get_pixel_formats(formats, 256, NULL);
    int format = 0;
    for (unsigned i = 0; i < available && i < 256; i++) {
        const struct wmgl_pixel_format choice = formats[i];
        if (choice.draw_to_pbuffer && choice.red_bits == 8 && choice.alpha_bits == 8 &&
            choice.depth_bits == 24 && choice.stencil_bits == 8 && choice.samples <= 1 &&
            !choice.srgb_capable && !choice.float_components) {
            format = (int)i + 1;
            break;
        }
    }
    const int attributes[] = { 0x2091, 2, 0x2092, 1, 0 };
    int error = 0;
    wmgl_context *context = format ? wmgl_context_create(format, NULL, attributes, &error) : NULL;
    wmgl_surface *surface = context ? wmgl_surface_create_pbuffer(format, WIDTH, HEIGHT) : NULL;
    require(context && surface && wmgl_make_current(context, surface, surface), "could not make the pbuffer current");

    GLuint ids[2];
    run_glGenBuffers(2, ids);
    run_glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, ids[1]);
    build_geometry(argv[1]);
    run_glBufferData(GL_ELEMENT_ARRAY_BUFFER, index_bytes, indices, GL_STATIC_DRAW);
    run_glBindBuffer(GL_ARRAY_BUFFER, ids[0]);
    run_glBufferParameteriAPPLE(GL_ARRAY_BUFFER, GL_BUFFER_FLUSHING_UNMAP_APPLE, GL_FALSE);
    run_glBufferParameteriAPPLE(GL_ARRAY_BUFFER, GL_BUFFER_SERIALIZED_MODIFY_APPLE, GL_FALSE);
    run_glViewport(0, 0, WIDTH, HEIGHT);
    run_glClearColor(0.1f, 0.15f, 0.2f, 1.0f);
    run_glEnableClientState(GL_VERTEX_ARRAY);
    if (!select_matrix_mode) {
        program(GL_VERTEX_PROGRAM_ARB, "!!ARBvp1.0\nMOV result.position, vertex.position;\nMOV result.color, vertex.color;\nEND\n");
        program(GL_FRAGMENT_PROGRAM_ARB, "!!ARBfp1.0\nMOV result.color, fragment.color;\nEND\n");
    }

    GLsync fences[MAX_IN_FLIGHT] = {0};
    uint64_t start = 0;
    unsigned frames = 0;
    while (!start || time_ns() - start < UINT64_C(8000000000)) {
        unsigned slot = frames % flight;
        if (fences[slot]) wait_frame(fences[slot]);
        if (frames == WARMUP) {
            startup_allocations = allocated;
            start = time_ns();
        }
        render(frames);
        fences[slot] = run_glFenceSync(GL_SYNC_GPU_COMMANDS_COMPLETE, 0);
        require(fences[slot] && wmgl_surface_swap(surface), "could not submit an offscreen output");
        frames++;
    }
    for (unsigned slot = 0; slot < flight; slot++) wait_frame(fences[slot]);
    require(run_glGetError() == GL_NO_ERROR, "the measured draw stream incurred a GL error");

    if (profile_storage) {
        require(measured > 0, "could not time storage replacement");
        qsort(storage_nanos, measured, sizeof(storage_nanos[0]), compare_nanos);
        printf("storage %u measured replacements, %u startup allocations, %u further allocations, %u released; median %" PRIu64
               " ns, p99 %" PRIu64 " ns, max %" PRIu64 " ns\n", measured, startup_allocations,
               allocated - startup_allocations, released, storage_nanos[measured / 2],
               storage_nanos[(measured * 99 + 99) / 100 - 1], storage_nanos[measured - 1]);
    }

    render(83);
    unsigned char *pixels = malloc(WIDTH * HEIGHT * 4);
    require(pixels != NULL, "could not allocate framebuffer readback memory");
    run_glReadPixels(0, 0, WIDTH, HEIGHT, GL_RGBA, GL_UNSIGNED_BYTE, pixels);
    uint64_t hash = UINT64_C(14695981039346656037);
    size_t colored = 0;
    for (size_t i = 0; i < WIDTH * HEIGHT * 4; i++) {
        hash = (hash ^ pixels[i]) * UINT64_C(1099511628211);
        colored += i % 4 == 0 && pixels[i] == 227;
    }
    free(pixels);
    require(run_glGetError() == GL_NO_ERROR && colored > 10000, "representative framebuffer is incomplete");
    printf("outputs %u, RGBA %016" PRIx64 ", colored %zu\n", frames, hash, colored);
    wmgl_make_current(NULL, NULL, NULL);
    wmgl_surface_destroy(surface);
    wmgl_context_destroy(context);
    return 0;
}
