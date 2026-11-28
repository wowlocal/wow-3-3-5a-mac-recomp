#include <dlfcn.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <sys/mman.h>
#include <unistd.h>

int main(void) {
    bool (*supported)(uint32_t) = dlsym(RTLD_DEFAULT, "os_cross_arch_is_supported");
    int (*tso)(uint32_t) = dlsym(RTLD_DEFAULT, "thread_set_x86_64_compat");
    int can_ask = !supported || supported(1);
    int result = tso && can_ask ? tso(1) : -1;
    const size_t host_page = (size_t)getpagesize();
    printf("page_size=%zu kernel_query_symbol=%d kernel_query=%d tso_symbol=%d tso_result=%d\n",
           host_page, supported != NULL, can_ask, tso != NULL, result);
    if (tso && result == 0) tso(0);
    const uintptr_t addresses[] = {0x1000, 0x4000, 0x10000, 0x100000, 0x10000000};
    for (unsigned i = 0; i < sizeof addresses / sizeof addresses[0]; ++i) {
        void *hint = (void *)addresses[i];
        void *actual = mmap(hint, host_page, PROT_NONE, MAP_PRIVATE | MAP_ANON, -1, 0);
        const char *status = actual == MAP_FAILED ? "failed" : actual == hint ? "exact" : "elsewhere";
        printf("hint=%#lx result=%s\n", (unsigned long)addresses[i], status);
        if (actual != MAP_FAILED) munmap(actual, host_page);
    }
    return 0;
}
