#include "ocerz/cpu.h"

#include <stdint.h>
#include <stdio.h>
#include <time.h>

int main(void)
{
    const unsigned repetitions = 2000000;
    ocerz_afp_enable();
    uint64_t saved = ocerz_afp_guest();
    uint64_t begin = clock_gettime_nsec_np(CLOCK_UPTIME_RAW);
    for (unsigned trial = 0; trial < repetitions; ++trial) {
        uint64_t fpcr = ocerz_afp_host();
        ocerz_afp_restore(fpcr);
    }
    uint64_t elapsed = clock_gettime_nsec_np(CLOCK_UPTIME_RAW) - begin;
    ocerz_afp_restore(saved);
    printf("afp=%d pairs=%u elapsed_ns=%llu\n", ocerz_afp(), repetitions, (unsigned long long)elapsed);
    return 0;
}
