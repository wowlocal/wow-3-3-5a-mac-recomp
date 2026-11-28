#include "raid-matrix-kernel.h"
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

typedef struct {
    float left[16], right[16], result[16], xmm[8][4];
    uint32_t mxcsr, alias;
} MatrixCase;

_Static_assert(sizeof(MatrixCase) == 328, "Capture record layout changed");

static int eligible(const MatrixCase *input)
{
    if ((input->mxcsr & ~UINT32_C(63)) != UINT32_C(0x1f80)) return 0;
    return raid_matrix_input_ok(input->left) && raid_matrix_input_ok(input->right);
}

typedef void (*Product)(float *, float [8][4], const float *, const float *);

__attribute__((noinline))
static void scalar(float *output, float xmm[8][4], const float *left, const float *right)
{
    float l[16], r[16];
    memcpy(l, left, sizeof l);
    memcpy(r, right, sizeof r);
    for (unsigned row = 0; row < 4; row++) {
        for (unsigned col = 0; col < 4; col++) {
            const float *a = l + 4 * row;
            float sum = a[0] * r[col] + a[1] * r[4 + col];
            sum = sum + a[2] * r[8 + col];
            output[4 * row + col] = sum + a[3] * r[12 + col];
        }
    }
    memset(xmm, 0, 8 * 4 * sizeof(float));
    xmm[0][0] = output[15];
    xmm[1][0] = r[14] * l[11];
    xmm[2][0] = output[3];
    xmm[3][0] = output[7];
    xmm[4][0] = l[1] * r[7];
    xmm[5][0] = l[2] * r[11];
    xmm[6][0] = l[3] * r[15];
    xmm[7][0] = output[11];
}

__attribute__((noinline))
static void neon(float *output, float xmm[8][4], const float *left, const float *right)
{
    float scratch[26];
    raid_matrix_product(output, xmm, scratch, left, right);
}

static void scalar_scratch(float result[26], const float left[16], const float right[16])
{
    for (unsigned col = 0; col < 4; col++) {
        const float p0 = left[12] * right[col];
        const float p1 = left[13] * right[col + 4];
        const float p2 = left[14] * right[col + 8];
        const float p3 = left[15] * right[col + 12];
        if (col == 3) {
            const float last[8] = {(p0 + p1) + p2, right[3], p1, right[7], p2, right[11], p3, right[15]};
            memcpy(result, last, sizeof last);
        } else {
            const float column[4] = {(p0 + p1) + p2, p1, p2, p3};
            memcpy(result + 8 + 4 * (2 - col), column, sizeof column);
        }
    }
    memcpy(result + 20, left + 9, 3 * sizeof(float));
    memcpy(result + 23, left + 5, 3 * sizeof(float));
}

static double measure(Product product, const MatrixCase *cases, size_t count, int repeats)
{
    float output[16], xmm[8][4];
    uint64_t start = clock_gettime_nsec_np(CLOCK_UPTIME_RAW);
    for (int repeat = 0; repeat < repeats; repeat++)
        for (size_t i = 0; i < count; i++) {
            product(output, xmm, cases[i].left, cases[i].right);
            __asm__ volatile("" : : "m"(output), "m"(xmm) : "memory");
        }
    return (double)(clock_gettime_nsec_np(CLOCK_UPTIME_RAW) - start) / (count * (double)repeats);
}

static int verify(const MatrixCase *input, size_t index)
{
    float scalar_out[16], vector_out[16], scalar_xmm[8][4], vector_xmm[8][4];
    float reference_scratch[26], vector_scratch[26];
    scalar(scalar_out, scalar_xmm, input->left, input->right);
    scalar_scratch(reference_scratch, input->left, input->right);
    raid_matrix_product(vector_out, vector_xmm, vector_scratch, input->left, input->right);
    if (memcmp(vector_out, input->result, sizeof vector_out) ||
        memcmp(scalar_out, input->result, sizeof scalar_out) ||
        memcmp(vector_xmm, input->xmm, sizeof vector_xmm) ||
        memcmp(scalar_xmm, input->xmm, sizeof scalar_xmm) ||
        memcmp(vector_scratch, reference_scratch, sizeof vector_scratch)) {
        fprintf(stderr, "Matrix result, XMM or scratch mismatch in case %zu\n", index);
        return 0;
    }
    float alias[20];
    memcpy(alias, input->left, 16 * sizeof(float));
    neon(alias + 4, vector_xmm, alias, input->right);
    if (memcmp(alias + 4, input->result, sizeof vector_out) ||
        memcmp(vector_xmm, input->xmm, sizeof vector_xmm)) return 0;
    memcpy(alias + 4, input->right, 16 * sizeof(float));
    neon(alias, vector_xmm, input->left, alias + 4);
    return !memcmp(alias, input->result, sizeof vector_out) &&
           !memcmp(vector_xmm, input->xmm, sizeof vector_xmm);
}

static int verify_guard(void)
{
    MatrixCase input = {.mxcsr = 0x1f80};
    const uint32_t bad[] = {1, 0x80000001u, 0x2b7fffffu, 0x54000000u, 0x7f800000u, 0x7fc10000u};
    for (unsigned i = 0; i < sizeof bad / sizeof bad[0]; i++) {
        memcpy(input.left, &bad[i], 4);
        if (eligible(&input)) return 0;
    }
    const uint32_t allowed[] = {0, 0x80000000u, 0x2b800000u, 0x53ffffffu};
    for (unsigned i = 0; i < sizeof allowed / sizeof allowed[0]; i++)
        memcpy(input.left + i, &allowed[i], 4);
    if (!eligible(&input)) return 0;
    input.mxcsr |= 0x2000;
    return !eligible(&input);
}

int main(int argc, char **argv)
{
    if (argc != 2 && !(argc == 3 && !strcmp(argv[2], "--verify-only"))) {
        fprintf(stderr, "Usage: %s CASE_FILE [--verify-only]\n", argv[0]);
        return 2;
    }
    if (!verify_guard()) return 1;
    FILE *input = fopen(argv[1], "rb");
    if (!input) { perror(argv[1]); return 1; }
    if (fseek(input, 0, SEEK_END)) { perror("fseek"); return 1; }
    long bytes = ftell(input);
    if (bytes <= 0 || bytes % sizeof(MatrixCase)) return 1;
    const size_t count = (size_t)bytes / sizeof(MatrixCase);
    MatrixCase *cases = malloc((size_t)bytes);
    if (!cases) return 1;
    rewind(input);
    if (fread(cases, sizeof *cases, count, input) != count || fclose(input)) return 1;
    size_t verified = 0, alias = 0;
    for (size_t i = 0; i < count; i++) {
        if (!eligible(&cases[i])) continue;
        if (!verify(&cases[i], i)) return 1;
        alias += cases[i].alias != 0;
        cases[verified++] = cases[i];
    }
    if (!verified) return 1;
    printf("verified %zu %zu %zu\n", verified, count - verified, alias);
    if (argc == 2) {
        for (unsigned pair = 0; pair < 8; pair++) {
            double scalar_ns, neon_ns;
            if (pair % 2) {
                neon_ns = measure(neon, cases, verified, 12);
                scalar_ns = measure(scalar, cases, verified, 12);
            } else {
                scalar_ns = measure(scalar, cases, verified, 12);
                neon_ns = measure(neon, cases, verified, 12);
            }
            printf("pair %u %.6f %.6f\n", pair + 1, scalar_ns, neon_ns);
        }
    }
    free(cases);
    return 0;
}
