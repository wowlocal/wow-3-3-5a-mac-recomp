#include <CoreFoundation/CoreFoundation.h>
#include <IOSurface/IOSurface.h>
#include <OpenGL/CGLIOSurface.h>
#include <OpenGL/OpenGL.h>
#include <OpenGL/gl.h>
#include <OpenGL/glext.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

enum { WIDTH = 1365, HEIGHT = 768 };

static void require(int ok, const char *reason)
{
    if (!ok) {
        fprintf(stderr, "%s\n", reason);
        exit(1);
    }
}

static IOSurfaceRef surface(void)
{
    int values[] = { WIDTH, HEIGHT, 4 };
    const void *keys[] = { kIOSurfaceWidth, kIOSurfaceHeight, kIOSurfaceBytesPerElement };
    const void *numbers[3];
    for (int i = 0; i < 3; i++)
        numbers[i] = CFNumberCreate(NULL, kCFNumberIntType, &values[i]);
    CFDictionaryRef props = CFDictionaryCreate(NULL, keys, numbers, 3,
        &kCFTypeDictionaryKeyCallBacks, &kCFTypeDictionaryValueCallBacks);
    IOSurfaceRef result = IOSurfaceCreate(props);
    CFRelease(props);
    for (int i = 0; i < 3; i++)
        CFRelease(numbers[i]);
    require(result != NULL, "could not create output IOSurface");
    return result;
}

static GLuint framebuffer(CGLContextObj ctx, IOSurfaceRef output)
{
    GLuint texture, fb;
    glGenTextures(1, &texture);
    glBindTexture(GL_TEXTURE_RECTANGLE_ARB, texture);
    if (output) {
        CGLError error = CGLTexImageIOSurface2D(ctx, GL_TEXTURE_RECTANGLE_ARB, GL_RGBA8,
            WIDTH, HEIGHT, GL_BGRA, GL_UNSIGNED_INT_8_8_8_8_REV, output, 0);
        require(error == kCGLNoError, "could not attach IOSurface texture");
    } else {
        glTexImage2D(GL_TEXTURE_RECTANGLE_ARB, 0, GL_RGBA8, WIDTH, HEIGHT, 0,
                     GL_BGRA, GL_UNSIGNED_INT_8_8_8_8_REV, NULL);
    }
    glGenFramebuffersEXT(1, &fb);
    glBindFramebufferEXT(GL_FRAMEBUFFER_EXT, fb);
    glFramebufferTexture2DEXT(GL_FRAMEBUFFER_EXT, GL_COLOR_ATTACHMENT0_EXT,
                             GL_TEXTURE_RECTANGLE_ARB, texture, 0);
    require(glCheckFramebufferStatusEXT(GL_FRAMEBUFFER_EXT) == GL_FRAMEBUFFER_COMPLETE_EXT,
            "texture framebuffer is incomplete");
    return fb;
}

static void draw(GLuint target, unsigned frame, GLuint copy_target)
{
    glBindFramebufferEXT(GL_FRAMEBUFFER_EXT, target);
    glClearColor(0.125f, 0.25f, 0.5f, 1.0f);
    glClear(GL_COLOR_BUFFER_BIT);
    glColor4ub((GLubyte)frame, 53, 117, 255);
    glDrawArrays(GL_TRIANGLES, 0, 3);
    if (copy_target) {
        glBindFramebufferEXT(GL_READ_FRAMEBUFFER_EXT, target);
        glBindFramebufferEXT(GL_DRAW_FRAMEBUFFER_EXT, copy_target);
        glBlitFramebufferEXT(0, 0, WIDTH, HEIGHT, 0, 0, WIDTH, HEIGHT,
                             GL_COLOR_BUFFER_BIT, GL_NEAREST);
    }
}

static void run(GLuint output, GLuint scratch, int copy, unsigned count, int timer)
{
    GLuint query = 0;
    if (timer)
        glGenQueries(1, &query);
    glFinish();
    uint64_t before = clock_gettime_nsec_np(CLOCK_MONOTONIC);
    if (timer)
        glBeginQuery(GL_TIME_ELAPSED_EXT, query);
    for (unsigned frame = 0; frame < count; frame++) {
        draw(copy ? scratch : output, frame, copy ? output : 0);
        glFlush();
    }
    if (timer)
        glEndQuery(GL_TIME_ELAPSED_EXT);
    glFinish();
    uint64_t elapsed = clock_gettime_nsec_np(CLOCK_MONOTONIC) - before;
    GLuint64EXT gpu_elapsed = 0;
    if (timer) {
        glGetQueryObjectui64vEXT(query, GL_QUERY_RESULT, &gpu_elapsed);
        glDeleteQueries(1, &query);
    }
    require(glGetError() == GL_NO_ERROR, "run failed");
    printf("%s wall=%.3f ms/frame gpu_query=%.3f ms/frame\n", copy ? "blit" : "direct",
           (double)elapsed / count / 1e6, (double)gpu_elapsed / count / 1e6);
}

static void verify(GLuint output, GLuint scratch)
{
    const size_t bytes = WIDTH * HEIGHT * 4;
    unsigned char *copy = malloc(bytes), *direct = malloc(bytes);
    require(copy && direct, "could not allocate result images");
    draw(scratch, 17, output);
    glBindFramebufferEXT(GL_FRAMEBUFFER_EXT, output);
    glReadPixels(0, 0, WIDTH, HEIGHT, GL_RGBA, GL_UNSIGNED_BYTE, copy);
    draw(output, 17, 0);
    glReadPixels(0, 0, WIDTH, HEIGHT, GL_RGBA, GL_UNSIGNED_BYTE, direct);
    require(glGetError() == GL_NO_ERROR && !memcmp(copy, direct, bytes), "rendered images differ");
    const unsigned char *middle = direct + 4 * ((HEIGHT / 2) * WIDTH + WIDTH / 2);
    require(middle[0] == 17 && middle[1] == 53 && middle[2] == 117 && middle[3] == 255,
            "render did not draw the expected triangle");
    free(direct);
    free(copy);
}

int main(void)
{
    const CGLPixelFormatAttribute attrs[] = { kCGLPFAAccelerated, 0 };
    CGLPixelFormatObj format = NULL;
    CGLContextObj ctx = NULL;
    GLint count = 0;
    require(CGLChoosePixelFormat(attrs, &format, &count) == kCGLNoError && format, "no accelerated format");
    require(CGLCreateContext(format, NULL, &ctx) == kCGLNoError, "could not create context");
    require(CGLSetCurrentContext(ctx) == kCGLNoError, "could not make context current");
    CGLDestroyPixelFormat(format);
    IOSurfaceRef output = surface();
    GLuint front = framebuffer(ctx, output), scratch = framebuffer(ctx, NULL);
    static const GLfloat vertices[] = { -0.9f, -0.8f, 0.9f, -0.8f, 0.0f, 0.9f };
    glViewport(0, 0, WIDTH, HEIGHT);
    glEnableClientState(GL_VERTEX_ARRAY);
    glVertexPointer(2, GL_FLOAT, 0, vertices);
    const char *extensions = (const char *)glGetString(GL_EXTENSIONS);
    int timer = extensions && strstr(extensions, "GL_EXT_timer_query") != NULL;
    printf("renderer=%s timer_query=%d\n", glGetString(GL_RENDERER), timer);
    verify(front, scratch);
    run(front, scratch, 1, 120, timer);
    run(front, scratch, 0, 120, timer);
    const int order[] = {1, 0, 0, 1, 0, 1, 1, 0};
    for (unsigned i = 0; i < sizeof order / sizeof order[0]; i++)
        run(front, scratch, order[i], 1200, timer);
    CGLSetCurrentContext(NULL);
    CGLDestroyContext(ctx);
    CFRelease(output);
    return 0;
}
