#include <stdint.h>
#include <math.h>
#include <string.h>
#include <stdio.h>
#include "culling-math.h"

static float single(uint32_t bits)
{
    float value;
    memcpy(&value, &bits, sizeof value);
    return value;
}
static uint32_t reference(uint32_t a, uint32_t b)
{
    volatile float product = single(a) * single(b);
    double integer = nearbyint(product);
    return !(integer >= -2147483648.0 && integer <= 2147483647.0)
             ? 0x80000000u : (uint32_t)(int32_t)integer;
}
static uint32_t scramble(uint32_t *seed)
{
    *seed ^= *seed << 13;
    *seed ^= *seed >> 17;
    *seed ^= *seed << 5;
    uint32_t value = *seed;
    return finite_float(value) ? value : value ^ 0x00800000;
}
int main(void)
{
    uint32_t seed = 0x18783529;
    for (unsigned i = 0; i < 1048576; i++) {
        uint32_t a = scramble(&seed), b = scramble(&seed);
        if (i & 1) {
            a = (a & 0x807fffff) | (uint32_t)(120 + (i % 9)) << 23;
            b = (b & 0x807fffff) | (uint32_t)(124 + (i % 5)) << 23;
        }
        uint32_t expected = reference(a, b), actual = phase_index(a, b);
        if (expected != actual || ((float_order(a) <= float_order(b)) != (single(a) <= single(b)))) {
            fprintf(stderr, "%#x %#x expected %#x actual %#x\n", a, b, expected, actual);
            return 1;
        }
    }
    puts("Particle culling arithmetic: 1048576 comparisons, 0 failed");
    return 0;
}
