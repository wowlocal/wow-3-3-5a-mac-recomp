// Included by AArchX's test_m32 after its test setup and call helpers.
#include <OpenGL/gl.h>
#include <OpenGL/glext.h>
#include <inttypes.h>
#include "ocerz/jit.h"

static void require(int condition, const char *message)
{
    if (!condition) {
        fprintf(stderr, "wmgl-i386: %s\n", message);
        exit(1);
    }
}

#include "wmgl-i386-frame.h"

static uint32_t replay_data(const void *data, uint32_t size)
{
    uint32_t address = m32_malloc(size);
    require(address != 0, "could not allocate replay data in the guest");
    if (data) memcpy(m32_h(address), data, size);
    return address;
}

static void replay_wait(uint32_t wait, uint32_t release, uint32_t fence)
{
    const uint32_t status = bench_gl_call(wait, 4, (uint32_t[]){ fence, 0, 2000000000, 0 });
    require(status == GL_ALREADY_SIGNALED || status == GL_CONDITION_SATISFIED, "GPU output did not complete");
    bench_gl_call(release, 1, (uint32_t[]){ fence });
}

static uint32_t replay_window(uint32_t agl)
{
    require(dlopen(CARBON, RTLD_LAZY | RTLD_GLOBAL) != NULL, "could not open Carbon for the replay window");
    const char *appkit = "/System/Library/Frameworks/AppKit.framework/Versions/C/AppKit";
    require(dlopen(appkit, RTLD_LAZY | RTLD_GLOBAL) != NULL, "could not open AppKit for the replay window");
    require(((id (*)(id, SEL))objc_msgSend)((id)objc_getClass("NSApplication"),
                                           sel_registerName("sharedApplication")) != nil,
            "could not initialize AppKit for the replay window");
    const int16_t bounds[] = {100, 120, 100 + HEIGHT, 120 + WIDTH};
    memcpy(m32_h(DATA + 0xa00), bounds, sizeof bounds);
    m32_wr(DATA + 0xa10, 0);
    const uint32_t attributes[] = {6 /* kDocumentWindowClass */, 0, DATA + 0xa00, DATA + 0xa10};
    require(cross(CARBON, "_CreateNewWindow", 4, attributes) == 0,
            "could not create the replay window");
    const uint32_t window = m32_rd(DATA + 0xa10);
    require(window != 0, "the replay window is missing");
    cross(CARBON, "_ShowWindow", 1, &window);
    const uint32_t port = call("_GetWindowPort", 1, &window);
    require(port && call("_aglSetDrawable", 2, (uint32_t[]){agl, port}) == 1,
            "could not attach the windowed Metal presenter");
    return window;
}

static void bench_wmgl_i386(const char *gl, uint32_t agl, const char *histogram, const char *code)
{
    const char *depth = getenv("OCERZ_WMGL_I386_DEPTH");
    char *end = NULL;
    unsigned long flight = depth ? strtoul(depth, &end, 10) : DEFAULT_IN_FLIGHT;
    require((!depth || !*end) && flight > 0 && flight <= MAX_IN_FLIGHT, "invalid in-flight limit");
    const char *duration = getenv("OCERZ_WMGL_I386_SECONDS");
    unsigned long seconds = duration ? strtoul(duration, &end, 10) : 8;
    require((!duration || !*end) && seconds > 0 && seconds <= 620, "invalid replay duration");
    const char *mode = getenv("OCERZ_WMGL_I386_WINDOW");
    const uint32_t window = mode && strcmp(mode, "1") == 0 ? replay_window(agl) : 0;
    if (!window) require(m32_wmgl_resize_pbuffer(WIDTH, HEIGHT), "could not resize the test pbuffer");

    const uint32_t entry = CODE + 0x3000;
    FILE *image = fopen(code, "rb");
    require(image != NULL, "could not open the i386 code");
    const size_t length = fread(m32_h(entry), 1, 4096, image);
    require(length > 0 && length < 4096 && feof(image), "invalid i386 code size");
    fclose(image);
    const int ordered = build_geometry(histogram);

    cross(gl, "_glGenBuffers", 2, (uint32_t[]){ 2, DATA + 0xc30 });
    cross(gl, "_glBindBuffer", 2, (uint32_t[]){ GL_ARRAY_BUFFER, m32_rd(DATA + 0xc30) });
    cross(gl, "_glBindBuffer", 2, (uint32_t[]){ GL_ELEMENT_ARRAY_BUFFER, m32_rd(DATA + 0xc34) });
    const uint32_t element_data = replay_data(indices, (uint32_t)index_bytes);
    call("_glBufferData", 4, (uint32_t[]){ GL_ELEMENT_ARRAY_BUFFER, (uint32_t)index_bytes, element_data, GL_STATIC_DRAW });
    m32_free(element_data);
    cross(gl, "_glBufferParameteriAPPLE", 3, (uint32_t[]){ GL_ARRAY_BUFFER, GL_BUFFER_FLUSHING_UNMAP_APPLE, GL_FALSE });
    cross(gl, "_glBufferParameteriAPPLE", 3, (uint32_t[]){ GL_ARRAY_BUFFER, GL_BUFFER_SERIALIZED_MODIFY_APPLE, GL_FALSE });
    cross(gl, "_glViewport", 4, (uint32_t[]){ 0, 0, WIDTH, HEIGHT });
    const float clear[] = {0.1f, 0.15f, 0.2f, 1.0f};
    uint32_t clear_bits[4];
    memcpy(clear_bits, clear, sizeof clear);
    cross(gl, "_glClearColor", 4, clear_bits);
    cross(gl, "_glEnableClientState", 1, (uint32_t[]){ GL_VERTEX_ARRAY });

    const unsigned stages[] = {GL_VERTEX_PROGRAM_ARB, GL_FRAGMENT_PROGRAM_ARB};
    const char *programs[] = {
        "!!ARBvp1.0\nMOV result.position, vertex.position;\nMOV result.color, vertex.color;\nEND\n",
        "!!ARBfp1.0\nMOV result.color, fragment.color;\nEND\n"
    };
    for (unsigned i = 0; i < 2; i++) {
        put_str(DATA + 0xd00, programs[i]);
        cross(gl, "_glGenProgramsARB", 2, (uint32_t[]){ 1, DATA + 0xe00 });
        cross(gl, "_glBindProgramARB", 2, (uint32_t[]){ stages[i], m32_rd(DATA + 0xe00) });
        cross(gl, "_glProgramStringARB", 4,
              (uint32_t[]){ stages[i], GL_PROGRAM_FORMAT_ASCII_ARB, (uint32_t)strlen(programs[i]), DATA + 0xd00 });
        cross(gl, "_glEnable", 1, (uint32_t[]){ stages[i] });
    }
    require(!g_failures && cross(gl, "_glGetError", 0, NULL) == GL_NO_ERROR, "could not configure replay GL state");

    const I386Frame frame = {
        .draws = replay_data(draws, sizeof draws), .uploads = replay_data(uploads, sizeof uploads),
        .vertices = replay_data(vertex_bytes, uploads[WRITES - 1].to), .agl = agl,
        .clear = m32_export(gl, "_glClear"), .buffer_data = m32_export(gl, "_glBufferData"),
        .subdata = m32_export(gl, "_glBufferSubData"), .map = m32_export(gl, "_glMapBuffer"),
        .flush = m32_export(gl, "_glFlushMappedBufferRangeAPPLE"), .unmap = m32_export(gl, "_glUnmapBuffer"),
        .pointer = m32_export(gl, "_glVertexPointer"), .color = m32_export(gl, "_glColor4ub"),
        .draw = m32_export(gl, "_glDrawRangeElementsEXT"), .fence = m32_export(gl, "_glFenceSync"),
        .copy = m32_export("/usr/lib/libSystem.B.dylib", "_memcpy"),
        .swap = m32_export("/System/Library/Frameworks/AGL.framework/Versions/A/AGL", "_aglSwapBuffers"),
    };
    uint32_t entries[sizeof frame / sizeof(uint32_t)];
    memcpy(entries, &frame, sizeof frame);
    for (size_t i = 0; i < sizeof entries / sizeof(entries[0]); i++)
        require(entries[i] != 0, "could not resolve a guest replay input");
    const uint32_t frame_data = replay_data(&frame, sizeof frame);
    const uint32_t wait = m32_export(gl, "_glClientWaitSync");
    const uint32_t release = m32_export(gl, "_glDeleteSync");
    require(wait && release, "could not resolve fence management");

    const int saved_jit = g_vm.jit_enabled, saved_active = m32_active;
    g_vm.jit_enabled = 1;
    m32_active = 1;
    uint32_t fences[MAX_IN_FLIGHT] = {0};
    uint64_t start = 0;
    unsigned output = 0;
    while (!start || clock_gettime_nsec_np(CLOCK_UPTIME_RAW) - start < (uint64_t)seconds * 1000000000) {
        unsigned slot = output % flight;
        if (fences[slot]) replay_wait(wait, release, fences[slot]);
        if (output == WARMUP) {
            require(g_vm.jit && ocerz_jit_blocks(g_vm.jit) > 0, "the replay produced no translated blocks");
            start = clock_gettime_nsec_np(CLOCK_UPTIME_RAW);
        }
        fences[slot] = m32_call(&g_vm, entry, (uint32_t[]){ frame_data, output, 1 }, 3, NULL, NULL);
        require(fences[slot] != 0, "translated frame failed to submit");
        output++;
    }
    for (unsigned slot = 0; slot < flight; slot++) replay_wait(wait, release, fences[slot]);
    require(cross(gl, "_glGetError", 0, NULL) == GL_NO_ERROR, "the translated draw stream incurred a GL error");
    require(m32_call(&g_vm, entry, (uint32_t[]){ frame_data, 83, 0 }, 3, NULL, NULL) == 1,
            "translated reference frame failed");
    m32_active = saved_active;
    g_vm.jit_enabled = saved_jit;

    const uint32_t pixels = replay_data(NULL, WIDTH * HEIGHT * 4);
    cross(gl, "_glReadPixels", 7, (uint32_t[]){ 0, 0, WIDTH, HEIGHT, GL_RGBA, GL_UNSIGNED_BYTE, pixels });
    const unsigned char *rgba = m32_h(pixels);
    uint64_t hash = UINT64_C(14695981039346656037);
    size_t colored = 0;
    for (size_t i = 0; i < WIDTH * HEIGHT * 4; i++) {
        hash = (hash ^ rgba[i]) * UINT64_C(1099511628211);
        colored += i % 4 == 0 && rgba[i] == 227;
    }
    const uint64_t reference_hash = ordered ? UINT64_C(0xb68e77767258e7d5) : UINT64_C(0x199a81aba2d36749);
    const size_t reference_colored = ordered ? 161327 : 161499;
    require(hash == reference_hash && colored == reference_colored &&
            cross(gl, "_glGetError", 0, NULL) == GL_NO_ERROR, "translated reference framebuffer differs from native");
    printf("outputs %u, RGBA %016" PRIx64 ", colored %zu, translated blocks %" PRIu64 "\n",
           output, hash, colored, ocerz_jit_blocks(g_vm.jit));
    m32_free(pixels);
    m32_free(frame_data);
    m32_free(frame.vertices);
    m32_free(frame.uploads);
    m32_free(frame.draws);
    if (window) {
        require(call("_aglSetDrawable", 2, (uint32_t[]){agl, 0}) == 1, "could not detach the replay window");
        cross(CARBON, "_DisposeWindow", 1, &window);
    }
}
