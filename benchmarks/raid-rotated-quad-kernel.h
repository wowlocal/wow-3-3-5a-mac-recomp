/* The audited rotated quad calculation, shared by replay and the live trial. */
#ifndef RAID_ROTATED_QUAD_KERNEL_H
#define RAID_ROTATED_QUAD_KERNEL_H

#include <arm_neon.h>
#include <stdint.h>
#include <string.h>

#pragma clang fp contract(off)

typedef struct {
    uint32_t axis[2], z, extent[2], center[2], uv[2], uv_delta[2], normal[3], color;
    uint32_t bounds[6];
} QuadInput;

typedef struct {
    uint32_t position[4][3], normal[4][3], color[4], uv[4][2];
} QuadVertices;

static inline float as_float(uint32_t bits)
{
    float value;
    memcpy(&value, &bits, sizeof value);
    return value;
}

static inline uint32_t as_word(float value)
{
    uint32_t bits;
    memcpy(&bits, &value, sizeof bits);
    return bits;
}

typedef struct {
    float left, right, lower, upper, along_x, along_y, across_x, across_y;
    float u, u_next, v, v_next;
} QuadBasis;

static inline QuadBasis quad_basis(const QuadInput *in)
{
    float cosine = as_float(in->axis[0]), sine = as_float(in->axis[1]);
    float extent_u = as_float(in->extent[0]), extent_v = as_float(in->extent[1]);
    float x = as_float(in->center[0]), y = as_float(in->center[1]);
    float across_x = cosine * extent_v, across_y = sine * extent_v;
    float u = as_float(in->uv[0]), v = as_float(in->uv[1]);
    return (QuadBasis){
        .left = x - across_x, .right = x + across_x,
        .lower = y - across_y, .upper = across_y + y,
        .along_x = cosine * extent_u, .along_y = extent_u * sine,
        .across_x = across_x, .across_y = across_y,
        .u = u, .u_next = u + as_float(in->uv_delta[0]),
        .v = v, .v_next = v + as_float(in->uv_delta[1]),
    };
}

static inline void rotated_quad_vertices(const QuadInput *in, QuadVertices *out, QuadBasis b)
{
    float32x4_t xb = {b.left, b.left, b.right, b.right};
    float32x4_t yb = {b.lower, b.lower, b.upper, b.upper};
    float32x4_t dx = {-b.along_y, b.along_y, -b.along_y, b.along_y};
    float32x4_t dy = {b.along_x, -b.along_x, b.along_x, -b.along_x};
    uint32x4x3_t position = {{
        vreinterpretq_u32_f32(vaddq_f32(xb, dx)),
        vreinterpretq_u32_f32(vaddq_f32(yb, dy)),
        vdupq_n_u32(in->z),
    }};
    uint32x4x3_t normal = {{
        vdupq_n_u32(in->normal[0]), vdupq_n_u32(in->normal[1]), vdupq_n_u32(in->normal[2]),
    }};
    float32x4_t u = {b.u, b.u, b.u_next, b.u_next};
    float32x4_t v = {b.v, b.v_next, b.v, b.v_next};
    uint32x4x2_t uv = {{vreinterpretq_u32_f32(u), vreinterpretq_u32_f32(v)}};
    vst3q_u32(&out->position[0][0], position);
    vst3q_u32(&out->normal[0][0], normal);
    vst1q_u32(out->color, vdupq_n_u32(in->color));
    vst2q_u32(&out->uv[0][0], uv);
}

static inline void reduce_bounds(const QuadInput *in, const QuadVertices *vertices, uint32_t result[6])
{
    memcpy(result, in->bounds, sizeof in->bounds);
    for (unsigned vertex = 0; vertex < 4; vertex++) {
        for (unsigned axis = 0; axis < 3; axis++) {
            uint32_t word = vertices->position[vertex][axis];
            float value = as_float(word);
            if (value < as_float(result[axis])) result[axis] = word;
            if (value > as_float(result[3 + axis])) result[3 + axis] = word;
        }
    }
}

#endif
