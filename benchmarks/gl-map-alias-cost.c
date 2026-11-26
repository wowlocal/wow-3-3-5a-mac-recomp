#include <OpenGL/OpenGL.h>
#include <OpenGL/gl.h>
#include <mach/mach.h>
#include <mach/mach_vm.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

enum { SAMPLES = 129 };
static volatile unsigned sink;
static void *(*volatile copy_buffer)(void *, const void *, size_t) = memcpy;

static void touch_pages(const void *pointer, size_t size, size_t page)
{
    const unsigned char *bytes = pointer;
    unsigned value = 0;
    for (size_t offset = 0; offset < size; offset += page)
        value += bytes[offset];
    sink = value;
}

static int compare_times(const void *left, const void *right)
{
    const uint64_t a = *(const uint64_t *)left, b = *(const uint64_t *)right;
    return (a > b) - (a < b);
}

static int fixed_alias(void *destination, void *source, size_t size, size_t touched, size_t page)
{
    mach_vm_address_t address = (uintptr_t)destination;
    vm_prot_t current = VM_PROT_NONE, maximum = VM_PROT_NONE;
    kern_return_t code = mach_vm_remap(mach_task_self(), &address, size, 0,
                                       VM_FLAGS_FIXED | VM_FLAGS_OVERWRITE, mach_task_self(),
                                       (uintptr_t)source, FALSE, &current, &maximum, VM_INHERIT_NONE);
    if (code != KERN_SUCCESS || address != (uintptr_t)destination ||
        !(current & VM_PROT_READ) || !(current & VM_PROT_WRITE) || (current & VM_PROT_EXECUTE))
        return 0;
    touch_pages(destination, touched, page);
    return mmap(destination, size, PROT_NONE, MAP_PRIVATE | MAP_ANON | MAP_FIXED, -1, 0) == destination;
}

static int measure(size_t size, size_t touched, size_t page)
{
    GLuint buffer = 0;
    void *copy = malloc(size);
    void *guarded = mmap(NULL, size + 2 * page, PROT_NONE, MAP_PRIVATE | MAP_ANON, -1, 0);
    if (!copy || guarded == MAP_FAILED)
        return 1;
    memset(copy, 0x5a, size);
    glGenBuffers(1, &buffer);
    glBindBuffer(GL_PIXEL_UNPACK_BUFFER, buffer);
    glBufferData(GL_PIXEL_UNPACK_BUFFER, (GLsizeiptr)size, copy, GL_STREAM_DRAW);
    void *mapped = glMapBuffer(GL_PIXEL_UNPACK_BUFFER, GL_READ_WRITE);
    if (!mapped || (uintptr_t)mapped % page || size % page) {
        printf("%zu skipped (native map unavailable or unaligned)\n", size);
        if (mapped)
            glUnmapBuffer(GL_PIXEL_UNPACK_BUFFER);
        glDeleteBuffers(1, &buffer);
        munmap(guarded, size + 2 * page);
        free(copy);
        return 0;
    }
    unsigned char *alias = (unsigned char *)guarded + page;
    uint64_t copied[SAMPLES], shared[SAMPLES];
    int valid = 1;
    for (int i = -16; i < SAMPLES; i++) {
        uint64_t start = clock_gettime_nsec_np(CLOCK_MONOTONIC);
        copy_buffer(copy, mapped, size);
        touch_pages(copy, touched, page);
        uint64_t middle = clock_gettime_nsec_np(CLOCK_MONOTONIC);
        if (!fixed_alias(alias, mapped, size, touched, page)) {
            valid = 0;
            break;
        }
        uint64_t end = clock_gettime_nsec_np(CLOCK_MONOTONIC);
        if (i >= 0) {
            copied[i] = middle - start;
            shared[i] = end - middle;
        }
    }
    munmap(guarded, size + 2 * page);
    valid &= glUnmapBuffer(GL_PIXEL_UNPACK_BUFFER);
    glDeleteBuffers(1, &buffer);
    free(copy);
    if (!valid) {
        printf("%zu fixed alias failed\n", size);
        return 1;
    }
    qsort(copied, SAMPLES, sizeof *copied, compare_times);
    qsort(shared, SAMPLES, sizeof *shared, compare_times);
    printf("%zu touched=%zu copy_us=%.2f alias_and_revoke_us=%.2f\n", size, touched, copied[SAMPLES / 2] / 1000.0,
           shared[SAMPLES / 2] / 1000.0);
    return 0;
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
    const size_t page = (size_t)getpagesize();
    printf("renderer=%s page=%zu\n", glGetString(GL_RENDERER), page);
    int failed = 0;
    for (size_t size = 65536; size <= 8388608; size *= 2)
        failed |= measure(size, size, page);
    for (size_t touched = page; touched <= 1048576; touched *= 4)
        failed |= measure(8388608, touched, page);
    CGLSetCurrentContext(NULL);
    CGLDestroyContext(context);
    CGLDestroyPixelFormat(format);
    return failed;
}
