#ifndef RAID_MATRIX_KERNEL_H
#define RAID_MATRIX_KERNEL_H

#include <arm_neon.h>
#include <string.h>

/* Restrict the trial to finite operands whose products and intermediate sums
 * stay within the binary32 normal range or are signed zero. */
static inline int raid_matrix_input_ok(const float *input)
{
    uint32x4_t bad = vdupq_n_u32(0);
    for (unsigned row = 0; row < 4; row++) {
        uint32x4_t word = vandq_u32(vreinterpretq_u32_f32(vld1q_f32(input + 4 * row)),
                                    vdupq_n_u32(0x7fffffff));
        uint32x4_t exponent = vshrq_n_u32(word, 23);
        uint32x4_t outside = vcgtq_u32(vsubq_u32(exponent, vdupq_n_u32(87)), vdupq_n_u32(80));
        bad = vorrq_u32(bad, vbicq_u32(outside, vceqzq_u32(word)));
    }
    return vmaxvq_u32(bad) == 0;
}

static inline void raid_matrix_product(float output[16], float xmm[8][4], float scratch[26],
                                       const float left[16], const float right[16])
{
#pragma clang fp contract(off)
    float32x4_t l[4], r[4], result[4];
    for (unsigned i = 0; i < 4; i++) {
        l[i] = vld1q_f32(left + 4 * i);
        r[i] = vld1q_f32(right + 4 * i);
    }
    for (unsigned i = 0; i < 3; i++) {
        float32x4_t sum = vmulq_laneq_f32(r[0], l[i], 0);
        sum = vaddq_f32(sum, vmulq_laneq_f32(r[1], l[i], 1));
        sum = vaddq_f32(sum, vmulq_laneq_f32(r[2], l[i], 2));
        result[i] = vaddq_f32(sum, vmulq_laneq_f32(r[3], l[i], 3));
    }
    const float32x4_t p1 = vmulq_laneq_f32(r[1], l[3], 1);
    const float32x4_t p2 = vmulq_laneq_f32(r[2], l[3], 2);
    const float32x4_t p3 = vmulq_laneq_f32(r[3], l[3], 3);
    const float32x4_t prefix = vaddq_f32(vaddq_f32(vmulq_laneq_f32(r[0], l[3], 0), p1), p2);
    result[3] = vaddq_f32(prefix, p3);

    const float local[26] = {
        vgetq_lane_f32(prefix, 3), vgetq_lane_f32(r[0], 3),
        vgetq_lane_f32(p1, 3), vgetq_lane_f32(r[1], 3),
        vgetq_lane_f32(p2, 3), vgetq_lane_f32(r[2], 3),
        vgetq_lane_f32(p3, 3), vgetq_lane_f32(r[3], 3),
        vgetq_lane_f32(prefix, 2), vgetq_lane_f32(p1, 2), vgetq_lane_f32(p2, 2), vgetq_lane_f32(p3, 2),
        vgetq_lane_f32(prefix, 1), vgetq_lane_f32(p1, 1), vgetq_lane_f32(p2, 1), vgetq_lane_f32(p3, 1),
        vgetq_lane_f32(prefix, 0), vgetq_lane_f32(p1, 0), vgetq_lane_f32(p2, 0), vgetq_lane_f32(p3, 0),
        vgetq_lane_f32(l[2], 1), vgetq_lane_f32(l[2], 2), vgetq_lane_f32(l[2], 3),
        vgetq_lane_f32(l[1], 1), vgetq_lane_f32(l[1], 2), vgetq_lane_f32(l[1], 3),
    };
    const float32x4_t zero = vdupq_n_f32(0.0f);
    const unsigned xmm_index[4] = {2, 3, 7, 0};
    for (unsigned i = 0; i < 4; i++) vst1q_f32(output + 4 * i, result[i]);
    for (unsigned i = 0; i < 4; i++)
        vst1q_f32(xmm[xmm_index[i]], vsetq_lane_f32(vgetq_lane_f32(result[i], 3), zero, 0));
    vst1q_f32(xmm[1], vsetq_lane_f32(vgetq_lane_f32(r[3], 2) * vgetq_lane_f32(l[2], 3), zero, 0));
    vst1q_f32(xmm[4], vsetq_lane_f32(vgetq_lane_f32(r[1], 3) * vgetq_lane_f32(l[0], 1), zero, 0));
    vst1q_f32(xmm[5], vsetq_lane_f32(vgetq_lane_f32(r[2], 3) * vgetq_lane_f32(l[0], 2), zero, 0));
    vst1q_f32(xmm[6], vsetq_lane_f32(vgetq_lane_f32(r[3], 3) * vgetq_lane_f32(l[0], 3), zero, 0));
    memcpy(scratch, local, sizeof local);
}

#endif
