#include <arm_neon.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

enum { AXES = 3, VERTICES = 4, PAD = 4 };

typedef struct {
    uint32_t old_lower[AXES], old_upper[AXES];
    uint32_t points[VERTICES][AXES];
    uint32_t expected_lower[AXES], expected_upper[AXES];
} QuadCase;

typedef struct {
    uint32_t lower[AXES], upper[AXES];
    uint32_t vertices[VERTICES][PAD];
} QuadOutput;

_Static_assert(sizeof(QuadCase) == 96, "Unexpected case file layout");

static float as_float(uint32_t bits)
{
    float value;
    memcpy(&value, &bits, sizeof value);
    return value;
}

static uint32x4_t load_three(const uint32_t words[AXES])
{
    uint32x4_t values = vcombine_u32(vld1_u32(words), vdup_n_u32(0));
    return vld1q_lane_u32(words + 2, values, 2);
}

static void store_three(uint32_t words[AXES], uint32x4_t values)
{
    vst1_u32(words, vget_low_u32(values));
    vst1q_lane_u32(words + 2, values, 2);
}

typedef void (*Batch)(const QuadCase *, QuadOutput *, size_t);

__attribute__((noinline))
static void emit_scalar(const QuadCase *cases, QuadOutput *outputs, size_t count)
{
    for (size_t i = 0; i < count; i++) {
        const QuadCase *input = &cases[i];
        QuadOutput *output = &outputs[i];
        memcpy(output->lower, input->old_lower, sizeof output->lower);
        memcpy(output->upper, input->old_upper, sizeof output->upper);
        for (size_t vertex = 0; vertex < VERTICES; vertex++) {
            for (size_t axis = 0; axis < AXES; axis++) {
                uint32_t bits = input->points[vertex][axis];
                float position = as_float(bits);
                output->vertices[vertex][axis] = bits;
                if (position < as_float(output->lower[axis])) output->lower[axis] = bits;
                if (position > as_float(output->upper[axis])) output->upper[axis] = bits;
            }
        }
    }
}

__attribute__((noinline))
static void emit_neon(const QuadCase *cases, QuadOutput *outputs, size_t count)
{
    for (size_t i = 0; i < count; i++) {
        const QuadCase *input = &cases[i];
        QuadOutput *output = &outputs[i];
        uint32x4_t lower = load_three(input->old_lower);
        uint32x4_t upper = load_three(input->old_upper);
        for (size_t vertex = 0; vertex < VERTICES; vertex++) {
            uint32x4_t point = load_three(input->points[vertex]);
            store_three(output->vertices[vertex], point);
            float32x4_t distance = vreinterpretq_f32_u32(point);
            uint32x4_t less = vcltq_f32(distance, vreinterpretq_f32_u32(lower));
            uint32x4_t greater = vcgtq_f32(distance, vreinterpretq_f32_u32(upper));
            lower = vbslq_u32(less, point, lower);
            upper = vbslq_u32(greater, point, upper);
        }
        store_three(output->lower, lower);
        store_three(output->upper, upper);
    }
}

static int verify_cases(const QuadCase *cases, QuadOutput *scalar, QuadOutput *neon,
                        size_t count, int has_observation)
{
    memset(scalar, 0xa5, count * sizeof *scalar);
    memset(neon, 0xa5, count * sizeof *neon);
    emit_scalar(cases, scalar, count);
    emit_neon(cases, neon, count);
    for (size_t i = 0; i < count; i++) {
        if (memcmp(&scalar[i], &neon[i], sizeof scalar[i])) {
            fprintf(stderr, "Output mismatch in case %zu\n", i);
            return 0;
        }
        if (has_observation && (
            memcmp(cases[i].expected_lower, neon[i].lower, sizeof neon[i].lower) ||
            memcmp(cases[i].expected_upper, neon[i].upper, sizeof neon[i].upper)
        )) {
            fprintf(stderr, "Original bound mismatch in case %zu\n", i);
            return 0;
        }
        for (size_t vertex = 0; vertex < VERTICES; vertex++) {
            if (memcmp(cases[i].points[vertex], neon[i].vertices[vertex], sizeof cases[i].points[vertex]) ||
                neon[i].vertices[vertex][AXES] != UINT32_C(0xa5a5a5a5)) {
                fprintf(stderr, "Position or padding mismatch in case %zu\n", i);
                return 0;
            }
        }
    }
    return 1;
}

static uint32_t random_word(uint32_t *state)
{
    *state = *state * UINT32_C(1664525) + UINT32_C(1013904223);
    return *state;
}

static void fill_edge_cases(QuadCase *cases, size_t count)
{
    static const uint32_t values[] = {
        0x00000000u, 0x80000000u, 0x00000001u, 0x80000001u,
        0x007fffffu, 0x807fffffu, 0x00800000u, 0x80800000u,
        0x3f800000u, 0xbf800000u, 0x7f7fffffu, 0xff7fffffu,
        0x7f800000u, 0xff800000u, 0x7fc12a03u, 0xffc43175u,
        0x7f800001u, 0xff800002u,
    };
    uint32_t state = 72357;
    const size_t limit = sizeof values / sizeof values[0];
    for (size_t i = 0; i < count; i++) {
        for (size_t axis = 0; axis < AXES; axis++) {
            cases[i].old_lower[axis] = values[(random_word(&state) >> 16) % limit];
            cases[i].old_upper[axis] = values[(random_word(&state) >> 16) % limit];
            for (size_t vertex = 0; vertex < VERTICES; vertex++)
                cases[i].points[vertex][axis] = values[(random_word(&state) >> 16) % limit];
        }
    }
}

static double time_batch(Batch emit, const QuadCase *cases, QuadOutput *outputs,
                         size_t count, size_t repetitions)
{
    emit(cases, outputs, count);
    uint64_t start = clock_gettime_nsec_np(CLOCK_UPTIME_RAW);
    for (size_t i = 0; i < repetitions; i++) emit(cases, outputs, count);
    uint64_t elapsed = clock_gettime_nsec_np(CLOCK_UPTIME_RAW) - start;
    return (double)elapsed / (double)count / (double)repetitions;
}

static void compare_modes(const char *name, const QuadCase *input, QuadOutput *scratch,
                          size_t count, size_t repetitions)
{
    for (size_t pair = 0; pair < 8; pair++) {
        double scalar, neon;
        if (pair % 2) {
            neon = time_batch(emit_neon, input, scratch, count, repetitions);
            scalar = time_batch(emit_scalar, input, scratch, count, repetitions);
        } else {
            scalar = time_batch(emit_scalar, input, scratch, count, repetitions);
            neon = time_batch(emit_neon, input, scratch, count, repetitions);
        }
        printf("%s %zu %zu %zu %.5f %.5f\n", name, pair + 1, count, repetitions, scalar, neon);
    }
}

int main(int argc, char **argv)
{
    enum { EDGE_COUNT = 32768, CACHE_CASES = 2048 };
    int verify_only = argc == 3 && strcmp(argv[2], "--verify-only") == 0;
    if (argc != 2 && !verify_only) {
        fprintf(stderr, "Usage: %s CASE_FILE [--verify-only]\n", argv[0]);
        return 2;
    }
    FILE *file = fopen(argv[1], "rb");
    if (!file) { perror(argv[1]); return 1; }
    if (fseek(file, 0, SEEK_END)) { perror("seek"); fclose(file); return 1; }
    long bytes = ftell(file);
    if (bytes < (long)sizeof(QuadCase) * CACHE_CASES || bytes % sizeof(QuadCase) ||
        fseek(file, 0, SEEK_SET)) {
        fprintf(stderr, "Invalid replay case file\n");
        fclose(file);
        return 1;
    }
    size_t count = (size_t)bytes / sizeof(QuadCase);
    size_t capacity = count > EDGE_COUNT ? count : EDGE_COUNT;
    QuadCase *cases = calloc(capacity, sizeof *cases);
    QuadOutput *scalar = calloc(capacity, sizeof *scalar);
    QuadOutput *neon = calloc(capacity, sizeof *neon);
    if (!cases || !scalar || !neon || fread(cases, sizeof *cases, count, file) != count) {
        fclose(file);
        free(cases);
        free(scalar);
        free(neon);
        fprintf(stderr, "Cannot load replay input\n");
        return 1;
    }
    fclose(file);
    int ok = verify_cases(cases, scalar, neon, count, 1);
    if (ok) {
        if (!verify_only) {
            compare_modes("all", cases, neon, count, 64);
            compare_modes("hot", cases + (count - CACHE_CASES) / 2, neon, CACHE_CASES, 4096);
        }
        fill_edge_cases(cases, EDGE_COUNT);
        ok = verify_cases(cases, scalar, neon, EDGE_COUNT, 0);
    }
    if (ok) printf("verified %zu %d\n", count, EDGE_COUNT);
    free(cases);
    free(scalar);
    free(neon);
    return ok ? 0 : 1;
}
