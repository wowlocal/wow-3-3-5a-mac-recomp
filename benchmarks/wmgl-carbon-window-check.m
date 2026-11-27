#import <AppKit/AppKit.h>
#include <dlfcn.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include "ocerz/m32_wmgl.h"

uint64_t ocerz_guest_base;
FILE *ocerz_stderr;
uint64_t ocerz_map_anywhere_aligned(uint64_t n, int prot, uint64_t align) {
    (void)n; (void)prot; (void)align; return 0;
}
int ocerz_unmap(uint64_t address, uint64_t bytes) {
    (void)address; (void)bytes; return -1;
}

typedef struct { int16_t top, left, bottom, right; } CarbonRect;
static NSRunningApplication *previous;
static void return_focus(void) {
    if (previous && !previous.terminated) [previous activateWithOptions:0];
}
static void require(int condition, const char *check) {
    printf("%s %s\n",condition ? "PASS" : "FAIL",check);
    fflush(stdout);
    if (!condition) { return_focus(); exit(1); }
}

static void events(void) {
    NSDate *deadline=[NSDate dateWithTimeIntervalSinceNow:0.14];
    while (deadline.timeIntervalSinceNow>0) {
        NSEvent *event=[NSApp nextEventMatchingMask:NSEventMaskAny untilDate:deadline
                                              inMode:NSDefaultRunLoopMode dequeue:YES];
        if (event) [NSApp sendEvent:event];
    }
}

static void wait_for_activation(NSApplication *app, const char *step) {
    if (!app.active) {
        printf("FOCUS %s %d\n",step,getpid());
        fflush(stdout);
        for (int attempt=0;attempt<35 && !app.active;++attempt) events();
    }
}

static NSWindow *metal_window(NSApplication *app) {
    for (NSWindow *window in app.windows)
        if ([window.title isEqualToString:@"WoW Metal Trial Output"])
            return window;
    return nil;
}

static int matches(NSWindow *window, CarbonRect rect) {
    NSRect frame=window.frame;
    CGFloat top=NSMaxY(NSScreen.screens[0].frame);
    return frame.origin.x == rect.left && frame.origin.y == top-rect.bottom &&
           frame.size.width == rect.right-rect.left && frame.size.height == rect.bottom-rect.top;
}

static int stack_index(CGWindowID wanted) {
    NSArray *windows = (NSArray *)CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly,kCGNullWindowID);
    int result=-1;
    for (NSUInteger i=0;i<windows.count;++i) {
        if ([windows[i][(NSString *)kCGWindowNumber] unsignedIntValue] == wanted) {
            result=(int)i;
            break;
        }
    }
    [windows release];
    return result;
}

int main(void) {
  @autoreleasepool {
    const char *prior_pid=getenv("OVERLAY_PRIOR_PID");
    if (prior_pid) previous=[[NSRunningApplication runningApplicationWithProcessIdentifier:(pid_t)atoi(prior_pid)] retain];
    NSApplication *app=[NSApplication sharedApplication];
    [app setActivationPolicy:NSApplicationActivationPolicyRegular];
    [app finishLaunching];
    void *carbon=dlopen("/System/Library/Frameworks/Carbon.framework/Frameworks/HIToolbox.framework/HIToolbox",
                       RTLD_NOW|RTLD_LOCAL);
    int32_t (*create)(uint32_t,uint32_t,const CarbonRect*,void**)=dlsym(carbon,"CreateNewWindow");
    uint32_t (*number)(void*)=dlsym(carbon,"HIWindowGetCGWindowID");
    void (*show)(void*)=dlsym(carbon,"ShowWindow");
    void (*hide)(void*)=dlsym(carbon,"HideWindow");
    void (*dispose)(void*)=dlsym(carbon,"DisposeWindow");
    int32_t (*place)(void*,uint16_t,const CarbonRect*)=dlsym(carbon,"SetWindowBounds");
    int32_t (*collapse)(void*,unsigned char)=dlsym(carbon,"CollapseWindow");
    require(create && number && show && hide && dispose && place && collapse,"host Carbon window API");
    CarbonRect original={90,86,310,446};
    void *parent=NULL;
    require(create(6,0x0008001f,&original,&parent)==0 && parent,"create a compositing Carbon window");
    const uint32_t wid=number(parent);
    require(wid && [app windowWithWindowNumber:wid] == nil,"Carbon window has no AppKit window object");
    int agl;
    require(m32_wmgl_create(&agl,NULL) && m32_wmgl_set_current(&agl) &&
            m32_wmgl_drawable(&agl,parent),"bind Metal to the Carbon window");
    NSWindow *overlay=[metal_window(app) retain];
    require(overlay && !overlay.visible && overlay.ignoresMouseEvents &&
            overlay.excludedFromWindowsMenu && overlay.level == NSNormalWindowLevel + 1,
            "initial overlay stays hidden and leaves input with Carbon");
    show(parent);
    events();
    wait_for_activation(app,"initial");
    require(app.active,"activate the disposable test window");
    events();
    require(overlay.visible && matches(overlay,original),"show overlays exactly the parent content");
    int pi=stack_index(wid), oi=stack_index((CGWindowID)overlay.windowNumber);
    printf("parent rank %d Metal rank %d, IDs %u/%ld\n",pi,oi,wid,(long)overlay.windowNumber);
    require(pi>=0 && oi>=0 && oi<pi,"Metal appears just above its Carbon backing window");

    CarbonRect moved={137,139,357,499};
    require(place(parent,33,&moved)==0,"move Carbon content");
    events();
    require(matches(overlay,moved),"parent movement repositions Metal output");
    void *color_addr=NULL,*clear_addr=NULL,*read_addr=NULL;
    require(m32_wmgl_symbol("glClearColor",&color_addr) && m32_wmgl_symbol("glClear",&clear_addr) &&
            m32_wmgl_symbol("glReadPixels",&read_addr) && color_addr && clear_addr && read_addr,
            "load real renderer entry points");
    void (*color)(float,float,float,float)=color_addr;
    void (*clear)(unsigned)=clear_addr;
    void (*read)(int,int,int,int,unsigned,unsigned,void*)=read_addr;
    unsigned char pixel[4]={0};
    color(0.25,0.5,0.75,1.0);
    clear(0x4000);
    read(310,180,1,1,0x1908,0x1401,pixel);
    require(abs(pixel[0]-64)<=1 && abs(pixel[1]-128)<=1 && abs(pixel[2]-191)<=1 && pixel[3]==255,
            "render and read inside the moved GPU surface");

    return_focus();
    for (int attempt = 0; attempt < 14 && app.active; ++attempt) events();
    require(!app.active && !overlay.visible && stack_index((CGWindowID)overlay.windowNumber)<0,
            "switching away removes Metal from the visible window stack");
    wait_for_activation(app,"return");
    events();
    void *(*front)(void)=dlsym(carbon,"FrontWindow");
    printf("return state active %d, front matches %d, Metal visible %d, geometry matches %d\n",
           app.active,front && front()==parent,overlay.visible,matches(overlay,moved));
    require(app.active && overlay.visible && matches(overlay,moved),"returning to the app restores aligned Metal output");
    hide(parent);
    events();
    require(!overlay.visible,"hiding Carbon removes the Metal window");
    show(parent);
    wait_for_activation(app,"show");
    events();
    require(overlay.visible && matches(overlay,moved),"show restores aligned Metal output");
    require(collapse(parent,1)==0,"request Carbon minimization");
    events();
    require(!overlay.visible,"minimization hides the Metal output");
    require(collapse(parent,0)==0,"restore the Carbon window");
    wait_for_activation(app,"restore");
    events();
    require(overlay.visible && matches(overlay,moved),"restore puts Metal output back in the window");
    dispose(parent);
    events();
    require(!overlay.visible,"closing Carbon leaves no orphan overlay");
    require(m32_wmgl_drawable(&agl,NULL),"detach the closed window to a pbuffer");
    m32_wmgl_destroy(&agl);
    [overlay release];
    return_focus();
    [previous release];
  }
}
