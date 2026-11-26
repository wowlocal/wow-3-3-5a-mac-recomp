#include <OpenGL/OpenGL.h>
#include <OpenGL/gl.h>
#include <OpenGL/glext.h>
#include <dlfcn.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

enum { WIDTH = 1365, HEIGHT = 768, STORE = 2 * 1024 * 1024,
       STEP = 2048, BATCHES = 48, PAYLOAD = 1584 };

static void *(*map_range)(GLenum, GLintptr, GLsizeiptr, GLbitfield);
static void (*flush_range)(GLenum, GLintptr, GLsizeiptr);

static void require(int success, const char *message)
{
    if (!success) {
        fprintf(stderr, "%s (GL error %#x)\n", message, glGetError());
        exit(1);
    }
}

static void update(int map, GLintptr offset, GLsizeiptr size, const void *payload)
{
    if (!map) {
        glBufferSubData(GL_ARRAY_BUFFER, offset, size, payload);
        return;
    }
    if (map_range) {
        enum { WRITE = 0x0002, INVALIDATE_RANGE = 0x0004, EXPLICIT_FLUSH = 0x0010 };
        void *range = map_range(GL_ARRAY_BUFFER, offset, size, WRITE | INVALIDATE_RANGE | EXPLICIT_FLUSH);
        require(range != NULL, "glMapBufferRange failed");
        memcpy(range, payload, (size_t)size);
        flush_range(GL_ARRAY_BUFFER, 0, size);
    } else {
        unsigned char *base = glMapBuffer(GL_ARRAY_BUFFER, GL_WRITE_ONLY);
        require(base != NULL, "glMapBuffer failed");
        memcpy(base + offset, payload, (size_t)size);
        glFlushMappedBufferRangeAPPLE(GL_ARRAY_BUFFER, offset, size);
    }
    require(glUnmapBuffer(GL_ARRAY_BUFFER), "glUnmapBuffer failed");
}

static void draw(int map, GLintptr *offset)
{
    glClearColor(0.1f, 0.15f, 0.2f, 1.0f);
    glClear(GL_COLOR_BUFFER_BIT);
    const GLsizeiptr sizes[] = { 1056, 1296, 1584 };
    GLfloat payload[PAYLOAD / sizeof(GLfloat)] = {0};
    for (int batch = 0; batch < BATCHES; batch++) {
        if (*offset + STEP > STORE) {
            glBufferData(GL_ARRAY_BUFFER, STORE, NULL, GL_DYNAMIC_DRAW);
            *offset = 0;
        }
        GLfloat center = ((batch % 8) - 3.5f) * 0.24f;
        GLfloat height = ((batch / 8) - 2.5f) * 0.30f;
        payload[0] = center - 0.08f;
        payload[1] = height - 0.08f;
        payload[2] = center + 0.08f;
        payload[3] = height - 0.08f;
        payload[4] = center;
        payload[5] = height + 0.08f;
        update(map, *offset, sizes[batch % 3], payload);
        GLint binding = 0;
        glGetIntegerv(GL_ARRAY_BUFFER_BINDING, &binding);
        require(binding != 0, "missing vertex buffer");
        glColor4ub(227, (GLubyte)(batch + 40), 83, 255);
        glVertexPointer(2, GL_FLOAT, 0, (const void *)(uintptr_t)*offset);
        glDrawArrays(GL_TRIANGLES, 0, 3);
        *offset += STEP;
    }
    glFlush();
}

static void verify(void)
{
    const size_t size = WIDTH * HEIGHT * 4;
    unsigned char *control = malloc(size), *candidate = malloc(size);
    require(control && candidate, "could not allocate comparison buffers");
    GLintptr offset = STORE;
    draw(0, &offset);
    glReadPixels(0, 0, WIDTH, HEIGHT, GL_RGBA, GL_UNSIGNED_BYTE, control);
    offset = STORE;
    draw(1, &offset);
    glReadPixels(0, 0, WIDTH, HEIGHT, GL_RGBA, GL_UNSIGNED_BYTE, candidate);
    size_t colored = 0;
    for (size_t i = 0; i < size; i += 4)
        colored += candidate[i] == 227;
    require(glGetError() == GL_NO_ERROR && colored > 10000 && !memcmp(control, candidate, size),
            "custom draw did not match the control");
    free(candidate);
    free(control);
    printf("Identical rendered output (%zu colored pixels)\n", colored);
}

static void run(int map, unsigned frames)
{
    GLintptr offset = STORE;
    glFinish();
    uint64_t start = clock_gettime_nsec_np(CLOCK_MONOTONIC);
    for (unsigned i = 0; i < frames; i++)
        draw(map, &offset);
    glFinish();
    uint64_t elapsed = clock_gettime_nsec_np(CLOCK_MONOTONIC) - start;
    require(glGetError() == GL_NO_ERROR, "timed run failed");
    printf("%s %.3f ms/frame\n", map ? "explicit-flush" : "subdata", (double)elapsed / frames / 1e6);
}

int main(void)
{
    const CGLPixelFormatAttribute attributes[] = { kCGLPFAAccelerated, 0 };
    CGLPixelFormatObj format = NULL;
    CGLContextObj context = NULL;
    GLint count = 0;
    require(CGLChoosePixelFormat(attributes, &format, &count) == kCGLNoError && format,
            "no accelerated OpenGL profile");
    require(CGLCreateContext(format, NULL, &context) == kCGLNoError &&
            CGLSetCurrentContext(context) == kCGLNoError, "could not create context");
    CGLDestroyPixelFormat(format);
    const char *extensions = (const char *)glGetString(GL_EXTENSIONS);
    int range_supported = extensions && strstr(extensions, "GL_ARB_map_buffer_range");
    int apple_flush_supported = extensions && strstr(extensions, "GL_APPLE_flush_buffer_range");
    printf("%s, map-buffer-range %d, Apple flush-buffer-range %d\n", glGetString(GL_VERSION),
           range_supported, apple_flush_supported);
    require(range_supported || apple_flush_supported, "explicit range flushing not supported");
    if (range_supported) {
        map_range = (void *(*)(GLenum, GLintptr, GLsizeiptr, GLbitfield))dlsym(RTLD_DEFAULT, "glMapBufferRange");
        flush_range = (void (*)(GLenum, GLintptr, GLsizeiptr))dlsym(RTLD_DEFAULT, "glFlushMappedBufferRange");
        require(map_range && flush_range, "OpenGL library did not export range map entry points");
    }
    require(CGLEnable(context, kCGLCEMPEngine) == kCGLNoError, "could not enable threaded driver");

    GLuint fb, color, stream;
    glGenFramebuffersEXT(1, &fb);
    glBindFramebufferEXT(GL_FRAMEBUFFER_EXT, fb);
    glGenRenderbuffersEXT(1, &color);
    glBindRenderbufferEXT(GL_RENDERBUFFER_EXT, color);
    glRenderbufferStorageEXT(GL_RENDERBUFFER_EXT, GL_RGBA8, WIDTH, HEIGHT);
    glFramebufferRenderbufferEXT(GL_FRAMEBUFFER_EXT, GL_COLOR_ATTACHMENT0_EXT, GL_RENDERBUFFER_EXT, color);
    require(glCheckFramebufferStatusEXT(GL_FRAMEBUFFER_EXT) == GL_FRAMEBUFFER_COMPLETE_EXT,
            "could not create render target");
    glViewport(0, 0, WIDTH, HEIGHT);
    glGenBuffers(1, &stream);
    glBindBuffer(GL_ARRAY_BUFFER, stream);
    if (!map_range)
        glBufferParameteriAPPLE(GL_ARRAY_BUFFER, GL_BUFFER_FLUSHING_UNMAP_APPLE, GL_FALSE);
    glEnableClientState(GL_VERTEX_ARRAY);
    verify();
    run(0, 30);
    run(1, 30);
    for (int i = 0; i < 3; i++) {
        run(i % 2 != 0, 200);
        run(i % 2 == 0, 200);
    }
    CGLSetCurrentContext(NULL);
    CGLDestroyContext(context);
    return 0;
}
