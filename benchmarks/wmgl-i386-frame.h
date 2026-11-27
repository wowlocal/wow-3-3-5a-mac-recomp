#ifndef WMGL_I386_FRAME_H
#define WMGL_I386_FRAME_H

#include "wmgl-indexed-shape.h"

#ifdef WMGL_I386_FRAME
#define GUEST_POINTER(type, field) type *field
#define GUEST_FUNCTION(result, field, ...) result (*field)(__VA_ARGS__)
#else
#define GUEST_POINTER(type, field) uint32_t field
#define GUEST_FUNCTION(result, field, ...) uint32_t field
#endif

typedef struct {
    GUEST_POINTER(const Draw, draws);
    GUEST_POINTER(const Upload, uploads);
    GUEST_POINTER(const unsigned char, vertices);
    uint32_t agl;
    GUEST_FUNCTION(void, clear, uint32_t);
    GUEST_FUNCTION(void, buffer_data, uint32_t, uint32_t, const void *, uint32_t);
    GUEST_FUNCTION(void, subdata, uint32_t, uint32_t, uint32_t, const void *);
    GUEST_FUNCTION(unsigned char *, map, uint32_t, uint32_t);
    GUEST_FUNCTION(void, flush, uint32_t, uint32_t, uint32_t);
    GUEST_FUNCTION(unsigned char, unmap, uint32_t);
    GUEST_FUNCTION(void, pointer, int, uint32_t, int, const void *);
    GUEST_FUNCTION(void, color, unsigned char, unsigned char, unsigned char, unsigned char);
    GUEST_FUNCTION(void, draw, uint32_t, unsigned, unsigned, int, uint32_t, const void *);
    GUEST_FUNCTION(void *, copy, void *, const void *, unsigned);
    GUEST_FUNCTION(uint32_t, fence, uint32_t, uint32_t);
    GUEST_FUNCTION(void, swap, uint32_t);
} I386Frame;

_Static_assert(sizeof(I386Frame) == 16 * sizeof(uint32_t), "i386 frame descriptor layout");

#undef GUEST_POINTER
#undef GUEST_FUNCTION

#endif
