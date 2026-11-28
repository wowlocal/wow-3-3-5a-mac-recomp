/* Verify full rotated-quad vertex data from live i386 captures. */
#include <dispatch/dispatch.h>
#include <inttypes.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "raid-rotated-quad-kernel.h"

typedef struct {
    QuadInput input;
    QuadVertices expected;
    uint32_t expected_bounds[6];
} QuadCase;

_Static_assert(sizeof(QuadCase) == 252, "Wrong rotated quad capture layout");

typedef void (*Emit)(const QuadCase *, QuadVertices *, size_t);

__attribute__((noinline))
static void emit_scalar(const QuadCase *cases, QuadVertices *out, size_t count)
{
    for (size_t i = 0; i < count; i++) {
        const QuadInput *in = &cases[i].input;
        QuadBasis basis = quad_basis(in);
        const float x[] = {
            basis.left - basis.along_y, basis.left + basis.along_y,
            basis.right - basis.along_y, basis.right + basis.along_y,
        };
        const float y[] = {
            basis.lower + basis.along_x, basis.lower - basis.along_x,
            basis.upper + basis.along_x, basis.upper - basis.along_x,
        };
        for (unsigned v = 0; v < 4; v++) {
            out[i].position[v][0] = as_word(x[v]);
            out[i].position[v][1] = as_word(y[v]);
            out[i].position[v][2] = in->z;
            memcpy(out[i].normal[v], in->normal, sizeof in->normal);
            out[i].color[v] = in->color;
            out[i].uv[v][0] = as_word(v & 2 ? basis.u_next : basis.u);
            out[i].uv[v][1] = as_word(v & 1 ? basis.v_next : basis.v);
        }
    }
}

__attribute__((noinline))
static void emit_neon(const QuadCase *cases, QuadVertices *out, size_t count)
{
    for (size_t i = 0; i < count; i++) {
        const QuadInput *in = &cases[i].input;
        rotated_quad_vertices(in, &out[i], quad_basis(in));
    }
}

struct Work {
    const QuadCase *cases;
    QuadVertices *out;
    size_t count;
    int record_threads;
    uint64_t threads[4];
};

static void emit_chunk(void *opaque, size_t index)
{
    struct Work *work = opaque;
    size_t start = work->count * index / 4, stop = work->count * (index + 1) / 4;
    if (work->record_threads) pthread_threadid_np(NULL, &work->threads[index]);
    emit_neon(work->cases + start, work->out + start, stop - start);
}

static unsigned emit_parallel(const QuadCase *cases, QuadVertices *out, size_t count, int record_threads)
{
    struct Work work = {.cases = cases, .out = out, .count = count, .record_threads = record_threads};
    dispatch_apply_f(4, dispatch_get_global_queue(QOS_CLASS_USER_INITIATED, 0), &work, emit_chunk);
    if (!record_threads) return 0;
    unsigned unique = 0;
    for (unsigned i = 0; i < 4; i++) {
        int seen = 0;
        for (unsigned j = 0; j < i; j++) seen |= work.threads[i] == work.threads[j];
        unique += !seen;
    }
    return unique;
}

static int verify(const QuadCase *cases, const QuadVertices *out, size_t count, const char *mode)
{
    for (size_t i = 0; i < count; i++) {
        uint32_t limits[6];
        reduce_bounds(&cases[i].input, &out[i], limits);
        if (memcmp(&out[i], &cases[i].expected, sizeof out[i]) ||
            memcmp(limits, cases[i].expected_bounds, sizeof limits)) {
            fprintf(stderr, "%s mismatch in captured quad %zu\n", mode, i);
            for (unsigned vertex = 0; vertex < 4; vertex++)
                fprintf(stderr, "vertex %u position %08x %08x %08x / %08x %08x %08x\n", vertex,
                        out[i].position[vertex][0], out[i].position[vertex][1], out[i].position[vertex][2],
                        cases[i].expected.position[vertex][0], cases[i].expected.position[vertex][1],
                        cases[i].expected.position[vertex][2]);
            return 0;
        }
    }
    return 1;
}

static double timing(const QuadCase *cases, QuadVertices *out, size_t count, unsigned repeats, unsigned mode)
{
    uint64_t start = clock_gettime_nsec_np(CLOCK_UPTIME_RAW);
    for (unsigned r = 0; r < repeats; r++) {
        switch (mode) {
        case 0: emit_scalar(cases, out, count); break;
        case 1: emit_neon(cases, out, count); break;
        default: emit_parallel(cases, out, count, 0); break;
        }
    }
    return (double)(clock_gettime_nsec_np(CLOCK_UPTIME_RAW) - start) / count / repeats;
}

int main(int argc, char **argv)
{
    int verify_only = argc == 3 && strcmp(argv[2], "--verify-only") == 0;
    size_t live_batch = 0;
    if (argc == 3 && !verify_only) {
        char *end = NULL;
        live_batch = strtoul(argv[2], &end, 10);
        if (!live_batch || *end) return 2;
    }
    if (argc != 3) {
        fprintf(stderr, "Usage: %s CASE_FILE LIVE_BATCH_SIZE|--verify-only\n", argv[0]);
        return 2;
    }
    FILE *file = fopen(argv[1], "rb");
    if (!file) { perror(argv[1]); return 1; }
    if (fseek(file, 0, SEEK_END)) return 1;
    long bytes = ftell(file);
    if (bytes <= 0 || (size_t)bytes % sizeof(QuadCase) || fseek(file, 0, SEEK_SET)) return 1;
    size_t count = (size_t)bytes / sizeof(QuadCase);
    QuadCase *cases = malloc((size_t)bytes);
    QuadVertices *out = malloc(count * sizeof *out);
    if (!cases || !out || fread(cases, sizeof *cases, count, file) != count) return 1;
    fclose(file);
    emit_scalar(cases, out, count);
    if (!verify(cases, out, count, "scalar")) return 1;
    emit_neon(cases, out, count);
    if (!verify(cases, out, count, "NEON")) return 1;
    unsigned workers = emit_parallel(cases, out, count, 1);
    if (!verify(cases, out, count, "parallel")) return 1;
    printf("verified %zu quads, %u distinct threads in the parallel check\n", count, workers);
    if (!verify_only) {
        const size_t sizes[] = {256, live_batch, 4096, 32768};
        for (unsigned index = 0; index < sizeof sizes / sizeof sizes[0]; index++) {
            size_t batch = sizes[index];
            if (index == 1 && (batch == 256 || batch == 4096 || batch == 32768)) continue;
            if (batch > count) continue;
            unsigned repeats = batch < 131072 ? 131072 / batch : 1;
            for (unsigned pair = 0; pair < 7; pair++) {
                double result[3];
                for (unsigned n = 0; n < 3; n++) {
                    unsigned mode = (n + pair) % 3;
                    result[mode] = timing(cases, out, batch, repeats, mode);
                }
                printf("batch %zu run %u scalar %.4f neon %.4f parallel %.4f ns_per_quad\n",
                       batch, pair + 1, result[0], result[1], result[2]);
            }
        }
    }
    free(out);
    free(cases);
    return 0;
}
