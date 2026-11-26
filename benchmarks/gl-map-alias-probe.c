#include <OpenGL/OpenGL.h>
#include <OpenGL/gl.h>
#include <OpenGL/glext.h>
#include <mach/mach.h>
#include <mach/mach_vm.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static int probe(size_t size, size_t page, int explicit_flush)
{
    enum { WIDTH = 256 };
    unsigned char *expected = malloc(size), *result = malloc(size);
    if (!expected || !result)
        return 1;
    memset(expected, 0x27, size);

    GLuint texture = 0, buffer = 0;
    glGenTextures(1, &texture);
    glBindTexture(GL_TEXTURE_2D, texture);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, WIDTH, (GLsizei)(size / (WIDTH * 4)),
                 0, GL_RGBA, GL_UNSIGNED_BYTE, NULL);
    glGenBuffers(1, &buffer);
    glBindBuffer(GL_PIXEL_UNPACK_BUFFER, buffer);
    glBufferData(GL_PIXEL_UNPACK_BUFFER, (GLsizeiptr)size, NULL, GL_STREAM_DRAW);
    glBufferSubData(GL_PIXEL_UNPACK_BUFFER, 0, (GLsizeiptr)size, expected);
    if (explicit_flush)
        glBufferParameteriAPPLE(GL_PIXEL_UNPACK_BUFFER, GL_BUFFER_FLUSHING_UNMAP_APPLE, GL_FALSE);
    void *mapped = glMapBuffer(GL_PIXEL_UNPACK_BUFFER, GL_WRITE_ONLY);
    int result_code = 0;
    if (!mapped || glGetError() != GL_NO_ERROR) {
        printf("%zu mode=%s native map failed\n", size, explicit_flush ? "explicit" : "implicit");
        result_code = 1;
    } else if ((uintptr_t)mapped % page || size % page) {
        printf("%zu mode=%s skipped unaligned offset=%zu\n", size, explicit_flush ? "explicit" : "implicit",
               (size_t)((uintptr_t)mapped % page));
        glUnmapBuffer(GL_PIXEL_UNPACK_BUFFER);
    } else {
        mach_vm_address_t alias = 0;
        vm_prot_t protection = VM_PROT_NONE, maximum = VM_PROT_NONE;
        kern_return_t rc = mach_vm_remap(mach_task_self(), &alias, size, 0, VM_FLAGS_ANYWHERE,
                                        mach_task_self(), (mach_vm_address_t)mapped, FALSE,
                                        &protection, &maximum, VM_INHERIT_NONE);
        if (rc != KERN_SUCCESS || !(protection & VM_PROT_WRITE)) {
            printf("%zu mode=%s remap unavailable (mach %d, prot %d)\n",
                   size, explicit_flush ? "explicit" : "implicit", rc, protection);
            if (rc == KERN_SUCCESS)
                mach_vm_deallocate(mach_task_self(), alias, size);
            glUnmapBuffer(GL_PIXEL_UNPACK_BUFFER);
        } else {
            const size_t offsets[2] = { 48, size - page + 64 };
            const unsigned char patterns[2][4] = {
                { 0x13, 0x42, 0x7b, 0xf0 }, { 0xfe, 0xce, 0x17, 0x21 },
            };
            for (unsigned i = 0; i < 2; i++) {
                memcpy((unsigned char *)alias + offsets[i], patterns[i], sizeof patterns[i]);
                memcpy(expected + offsets[i], patterns[i], sizeof patterns[i]);
                if (explicit_flush)
                    glFlushMappedBufferRangeAPPLE(GL_PIXEL_UNPACK_BUFFER, (GLintptr)offsets[i], sizeof patterns[i]);
            }
            rc = mach_vm_deallocate(mach_task_self(), alias, size);
            GLboolean ok = glUnmapBuffer(GL_PIXEL_UNPACK_BUFFER);
            glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, WIDTH, (GLsizei)(size / (WIDTH * 4)),
                            GL_RGBA, GL_UNSIGNED_BYTE, NULL);
            glGetTexImage(GL_TEXTURE_2D, 0, GL_RGBA, GL_UNSIGNED_BYTE, result);
            int correct = rc == KERN_SUCCESS && ok && glGetError() == GL_NO_ERROR && !memcmp(result, expected, size);
            printf("%zu mode=%s direct alias readback %s\n", size, explicit_flush ? "explicit" : "implicit",
                   correct ? "PASS" : "FAIL");
            result_code = !correct;
        }
    }
    glDeleteBuffers(1, &buffer);
    glDeleteTextures(1, &texture);
    free(expected);
    free(result);
    return result_code;
}

int main(void)
{
    const CGLPixelFormatAttribute attributes[] = { kCGLPFAAccelerated, 0 };
    CGLPixelFormatObj format = NULL;
    CGLContextObj context = NULL;
    GLint count = 0;
    if (CGLChoosePixelFormat(attributes, &format, &count) != kCGLNoError || !format ||
        CGLCreateContext(format, NULL, &context) != kCGLNoError ||
        CGLSetCurrentContext(context) != kCGLNoError)
        return 2;

    size_t page = (size_t)getpagesize();
    int failed = 0;
    printf("renderer=%s host_page=%zu\n", glGetString(GL_RENDERER), page);
    for (size_t size = 65536; size <= 8388608; size *= 2)
        for (int explicit_flush = 0; explicit_flush < 2; explicit_flush++)
            failed |= probe(size, page, explicit_flush);
    CGLSetCurrentContext(NULL);
    CGLDestroyContext(context);
    CGLDestroyPixelFormat(format);
    return failed;
}
