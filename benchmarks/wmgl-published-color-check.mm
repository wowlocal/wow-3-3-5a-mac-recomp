#import <Foundation/Foundation.h>
#import <QuartzCore/CAMetalLayer.h>
#include "winemetalgl/driver.h"
#include <OpenGL/gl.h>
#include <cstdio>
#include <cstdlib>

#define LOAD(name) __typeof__(&name) p_##name = (__typeof__(&name))wmgl_get_proc_address(#name)
#define CHECK(x) do { if (!(x)) { std::fprintf(stderr, "failed: %s\n", #x); std::exit(1); } } while (0)

int main() {
    @autoreleasepool {
        CHECK(wmgl_initialize(WMGL_DRIVER_ABI_VERSION) == 0);
        struct wmgl_pixel_format formats[512];
        unsigned n = wmgl_get_pixel_formats(formats, 512, NULL), format = 0;
        for (unsigned i = 0; i < n && i < 512; ++i) {
            const auto& f = formats[i];
            if (f.draw_to_window && f.double_buffer && f.red_bits == 8 && f.alpha_bits == 8 &&
                !f.srgb_capable && f.samples <= 1 && f.depth_bits == 24 && f.stencil_bits == 8) {
                format = i + 1;
                break;
            }
        }
        CHECK(format);
        CAMetalLayer* layer = [CAMetalLayer layer];
        int error = 0;
        const int attrs[] = {0x2091, 2, 0x2092, 1, 0};
        wmgl_context* first = wmgl_context_create(format, nullptr, attrs, &error);
        wmgl_surface* surface = wmgl_surface_create_window((__bridge void*)layer, format, 64, 64);
        CHECK(first && surface && wmgl_make_current(first, surface, surface));
        wmgl_surface_flush(surface, WMGL_FLUSH_INTERVAL, 64, 64, 0);

        LOAD(glClearColor); LOAD(glClear); LOAD(glBegin); LOAD(glEnd); LOAD(glVertex2f); LOAD(glColor4ub);
        LOAD(glReadPixels); LOAD(glViewport); LOAD(glEnable); LOAD(glDisable); LOAD(glScissor);
        LOAD(glColorMask); LOAD(glGetError); LOAD(glFinish);
        const auto pixel = [&](int x, int y, unsigned red, unsigned green, unsigned blue) {
            unsigned char data[4] = {};
            p_glReadPixels(x,y,1,1,GL_RGBA,GL_UNSIGNED_BYTE,data);
            if (data[0]!=red || data[1]!=green || data[2]!=blue || data[3]!=255) {
                std::fprintf(stderr, "pixel %d,%d got %u,%u,%u,%u, expected %u,%u,%u,255\n",
                             x,y,data[0],data[1],data[2],data[3],red,green,blue);
                std::exit(2);
            }
        };
        const auto blueCorner = [&] {
            p_glColor4ub(0,0,255,255);
            p_glBegin(GL_TRIANGLES);
            p_glVertex2f(-1,-1); p_glVertex2f(0,-1); p_glVertex2f(-1,0);
            p_glEnd();
        };
        p_glViewport(0,0,64,64);
        p_glClearColor(1,0,0,1);
        p_glClear(GL_COLOR_BUFFER_BIT);
        blueCorner();
        CHECK(wmgl_surface_swap(surface));
        pixel(6,6,0,0,255);
        pixel(40,50,255,0,0);

        CHECK(wmgl_surface_swap(surface));
        pixel(6,6,0,0,255);
        pixel(40,50,255,0,0);

        p_glScissor(48,48,8,8);
        p_glEnable(GL_SCISSOR_TEST);
        p_glClearColor(0,1,0,1);
        p_glClear(GL_COLOR_BUFFER_BIT);
        p_glDisable(GL_SCISSOR_TEST);
        pixel(50,50,0,255,0);
        pixel(40,50,255,0,0);
        pixel(6,6,0,0,255);
        blueCorner();
        CHECK(wmgl_surface_swap(surface));

        p_glColorMask(GL_FALSE,GL_TRUE,GL_FALSE,GL_TRUE);
        p_glClearColor(1,1,0,1);
        p_glClear(GL_COLOR_BUFFER_BIT);
        p_glColorMask(GL_TRUE,GL_TRUE,GL_TRUE,GL_TRUE);
        pixel(6,6,0,255,255);
        pixel(40,50,255,255,0);
        blueCorner();
        CHECK(wmgl_surface_swap(surface));

        p_glClearColor(1,0,1,1);
        p_glClear(GL_COLOR_BUFFER_BIT);
        pixel(6,6,255,0,255);
        pixel(40,50,255,0,255);
        blueCorner();
        CHECK(wmgl_surface_swap(surface));

        wmgl_surface_flush(surface, WMGL_FLUSH_UPDATED, 72, 70, 0);
        pixel(6,6,0,0,255);
        pixel(40,50,255,0,255);
        p_glViewport(0,0,72,70);
        blueCorner();
        CHECK(wmgl_surface_swap(surface));
        p_glFinish();
        CHECK(p_glGetError() == GL_NO_ERROR);

        wmgl_context* second = wmgl_context_create(format, nullptr, attrs, &error);
        CHECK(second && wmgl_make_current(second, surface, surface));
        p_glColorMask(GL_FALSE,GL_TRUE,GL_FALSE,GL_TRUE);
        p_glClearColor(0,1,0,1);
        p_glClear(GL_COLOR_BUFFER_BIT);
        pixel(6,6,0,255,255);
        pixel(40,50,255,255,255);
        CHECK(p_glGetError() == GL_NO_ERROR);
        CHECK(wmgl_make_current(nullptr, nullptr, nullptr));
        wmgl_context_destroy(second);
        wmgl_context_destroy(first);
        wmgl_surface_destroy(surface);
        std::puts("Published-color checks passed for reads, repeated swaps, partial and masked clears, resize, and queue switch.");
    }
}
