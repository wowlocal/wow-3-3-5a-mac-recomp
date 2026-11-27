# Northshire performance route

Use Macsmoke in the clearing outside Northshire Abbey near Deputy Willem. Start facing Willem with the abbey on the right, and close tutorial and dialogue windows. Use the 1365 × 768 game viewport. The test runs on an Apple M4 Pro with macOS 27.2.1, sound disabled, the saved graphics settings, and vertical sync disabled. Record changes in these conditions alongside any comparison.

Start a 120-second frame capture. Repeat the following movement cycle from the same spot. Times are in milliseconds and keyboard sends include a release.

| Action | Hold |
| --- | ---: |
| Forward (W) | 700 |
| Back (S) | 1,090 |
| Strafe left (Q) | 640 |
| Strafe right (E) | 640 |
| Turn left (A) | 410 |
| Turn right (D) | 410 |

After five cycles, drag right with the left mouse button held for about one second and reverse the drag. Then hold the right mouse button on the ground for about 1.2 seconds. Repeat the five movement cycles and mouse sequence, then repeat movement cycles through the remaining time. The measurement includes pauses between input groups.

The recorder counts completed on-screen AGL buffer swaps. Keep window focus and rendering settings consistent across runs. Both runs used the same runtime and saved graphics settings. The game clock and daylight advanced between runs.

## Results

| Metric | Two-minute baseline | Two-minute repeat after relaunch | Ten-minute live session |
| --- | ---: | ---: | ---: |
| Median FPS | 57.75 | 60.21 | 57.04 |
| 95th-percentile frame time | 26.48 ms | 25.37 ms | 26.26 ms |
| Stalls over 250 ms | 0 | 0 | 0 |
| Longest frame gap | 52.91 ms | 46.52 ms | 54.32 ms |
| Average client CPU | 63.02% | 64.45% | 62.79% |
| Peak sampled physical footprint | 763.69 MiB | 747.81 MiB | 765.67 MiB |

The ten-minute run completed with movement, camera turns, working UI buttons, and Deputy Willem’s dialogue. The runtime logged both press and release, and frames continued throughout all twelve measured mouse holds. The largest gap during a hold was 33.67 ms.

## OpenGL threading experiment

Each sample covered 40 seconds in the same stationary Northshire view at 1365 × 768. The first default sample used the existing session. The single-threaded run and the restored default run used fresh launches. Only the `GLFaster` setting changed between the fresh launches.

| Mode | Median FPS | Average swaps/s | 95th-percentile frame time | Stalls over 250 ms |
| --- | ---: | ---: | ---: | ---: |
| Default | 57.15 | 64.21 | 25.55 ms | 0 |
| Single-threaded | 58.37 | 65.82 | 23.79 ms | 0 |
| Default after relaunch | 58.07 | 65.94 | 24.95 ms | 0 |

A four-second profile in single-threaded mode stopped showing the threaded `glFlush` wait. About 28% of main-thread samples landed in a WindowServer call while preparing to present a frame. The original setting was restored after the test.

## Swap timing baseline

A separate two-minute measurement added timestamps around the host AGL swap. Its median rate was **97.72 AGL swap returns per second**, with **23.39 ms p95** and **zero stalls over 250 ms**. The host swap accounted for 0.03% of aggregate frame time, while the 95th-percentile interval between swaps was 23.38 ms. Legacy OpenGL submits presentation work on a separate thread, so waits for that thread may appear between swap calls. The new baseline has yet to be paired with a controlled performance change. [Raw timing summary](results/northshire-perf-split-baseline.json)

For precise hold timing, compile the [optional mouse helper](trial-input.swift) with `xcrun swiftc -O benchmarks/trial-input.swift -o build/trial-input`. It sends input to the running `Wrath Trial.app` process and prints the monotonic time of press and release. The same holds can be performed with a physical mouse.

## Shared texture uploads

The client makes whole 8 MiB write-only mappings for texture uploads, while the sampled explicit flushes covered at most 64 KiB each. A 36-second trace accounted for 1.12 ms of incoming unpack-buffer copies per completed swap on average. The shared-upload path leases those pages to an isolated guest address for the lifetime of the native GL mapping. All other mappings fall back to copied storage. [Alignment trace summary](results/northshire-gl-map-alignment.json) · [Upload trace summary](results/northshire-unpack-payloads.json)

Each comparison used the same build and saved 1365 × 768 settings. Both arms ran the documented 120-second in-world route, including six measured mouse holds. The second pair disabled copy diagnostics in both arms.

| Mode | Median FPS | 95th-percentile frame time | Stalls over 250 ms | Average CPU | Peak footprint |
| --- | ---: | ---: | ---: | ---: | ---: |
| Copy mode with diagnostics | 106.21 | 13.40 ms | 0 | 90.52% | 752.78 MiB |
| Shared mode with diagnostics | 118.29 | 12.17 ms | 0 | 87.14% | 748.85 MiB |
| Copy mode | 105.49 | 13.36 ms | 0 | 89.35% | 751.36 MiB |
| Shared mode | 118.11 | 12.19 ms | 0 | 87.14% | 748.30 MiB |

All 24 measured mouse holds kept rendering. Counters in the first shared run recorded 48,477 acquired and released mappings and zero incoming unpack copies at the sampled point. The runs report completed AGL swaps. A contemporaneous control matters because the older controls vary. [Copy control](results/northshire-unpack-alias-control-release.json) · [Shared repeat](results/northshire-unpack-alias-release.json)

### Ten-minute play and relaunch

The diagnostics-free shared session recorded 117 median FPS, 12.21 ms p95, and no stalls over 250 ms in 600.004 seconds. Movement, camera turns, held clicks, and NPC dialogue worked. WoW released all 173,929 acquired mappings during a clean quit, kept the saved settings and license choices, and re-entered Northshire after relaunch. [Ten-minute report](results/northshire-unpack-alias-ten-minutes.json)

The native fixtures check texture readback and compare the setup and revocation cost of a fixed alias with a full copy. They use real CGL mappings in their own process. Build either with `xcrun clang -O2 -Wno-deprecated-declarations benchmarks/gl-map-alias-probe.c -framework OpenGL -o build/gl-map-alias-probe`, substituting the cost fixture name for the second build. [Readback fixture](gl-map-alias-probe.c) · [Timing fixture](gl-map-alias-cost.c)
## Bridge diagnostic lookups

The host sample from the rebuilt `f89efb0` control found 186 of 1,585 main-thread samples in the environment lookup routine. The bridge checked three diagnostic variables on every imported call. The candidate checks the imported function name first. A matching call still reads the same variable at the point of use.

The control and candidate each ran for 40 seconds with Macsmoke in the same Northshire view and the documented graphics settings. The control was measured late in its session and the candidate shortly after world entry. These are stationary measurements. Accessibility key presses did not produce the documented movement cycle.

| Metric | Control | Diagnostic lookup candidate |
| --- | ---: | ---: |
| Average AGL swaps/s | 122.23 | 134.26 |
| p50 | 8.066 ms | 7.450 ms |
| p95 | 11.415 ms | 10.347 ms |
| p99 | 13.646 ms | 12.293 ms |
| Longest interval | 22.89 ms | 24.62 ms |
| Average client CPU | 88.75% | 84.78% |
| Peak sampled physical footprint | 764.13 MiB | 744.64 MiB |

The candidate reached Northshire after a clean relaunch with the license choices retained. Two-button movement changed Macsmoke's server-recorded position by 6.61 game units. The normal in-game exit released all 67,144 shared upload mappings. The Carbon and general 32-bit unit suites passed 607 and 773 checks, respectively.

To reproduce the stationary measurement, build the control from `f89efb0` and the candidate from this patch against the same AArchX revision. Launch both with `--unpack-alias --profile`, one at a time, from the same character position. Run the recorder after entering the world and closing dialogue and menus.

```sh
python3 scripts/measure.py PID FRAME_LOG RESULT_JSON --seconds 40
```

Use the raw swap count and duration in nanoseconds for the throughput threshold. The graphics trace is needed to relate AGL swaps to completed rendering. The 500 FPS acceptance runs still require the full movement route.

### CPU and GPU profiling

Capture a separate in-world diagnostics run so the profiler's overhead stays out of the comparison.

```sh
OCERZ_M32_TRACE_SWAPS=1 scripts/launch.sh "$TRIAL_APP" "$RUNTIME" "$GAME_DIR" ruRU --unpack-alias --profile
# Enter the world before attaching. Use the PID and frame-log path printed by launch.sh.
xcrun xctrace record --template 'Game Performance' --attach PID --time-limit 7s --window 7s --output northshire.trace --no-prompt
python3 benchmarks/trace-frames.py northshire.trace PID 1365 768 --frame-log FRAME_LOG
sample PID 4 1 -file northshire-sample.txt
```

The optional swap signposts carry the same monotonic timestamp as the frame log. The trace script aligns the two clocks and reports AGL swaps, completed window writes, and p99 GPU completion gaps in their common interval. It qualifies a write when the renderer identifies a game-sized surface read by WindowServer as its presentation target and that renderer frame contains vertex work, fragment work, and a single window write that completes. Missing evidence can enlarge the gap between qualified completions. Confirm the frame correspondence in the timeline before using swaps as an acceptance count. Display refresh is tracked separately. For a longer GPU timeline, use the `Metal System Trace` template with `--instrument 'Points of Interest'`. Disable swap signposts for the timed runs.

## Metal sidecar route

Bridge-crossing diagnostics were active during this route because the launcher passed an empty `OCERZ_M32_CALLLOG`. The performance and resource figures describe that instrumented run. [Capture conditions](results/raid-winemetalgl-draw-cap-live.json)

After removing the raid fixture, the original client ran the 120-second movement route at 1365 × 768 with VSync off. The GPU log averaged **131.03 newly rendered FPS** at **14.66 ms p99** and had zero sequence gaps. The swap logger averaged **131.08 swaps/s** with zero gaps over 250 ms. [Route measurements](results/raid-winemetalgl-draw-cap-live.json)

All six mouse holds kept rendering. The largest swap gap overlapping a hold was **92.29 ms** during the first camera drag. Native movement keys changed the view and a right click opened Deputy Willem's dialogue. The client averaged 105.63% of one CPU core and reached 977.53 MiB of sampled physical footprint. [Input and resource measurements](results/raid-winemetalgl-draw-cap-live.json)

The control sample put 281 of 1,585 main-thread samples under the OpenGL state query and 200 under OpenGL flush. The query stack included renderer draw work executing while it drained the queue. An offscreen fixture reproduced the draw and upload output with and without the queries, but the fixture's queries took about 0.1 µs and did not reproduce the in-game wait.

In a separate launch with `OCERZ_GUESTPROF=2000 OCERZ_GUESTPROF_PERIOD=8 OCERZ_GUESTPROF_HOT=1`, the four warmed foreground periods each spent 0–2 ms compiling new blocks. The translated blocks accounted for 63–66% of sampled CPU time on the hottest guest thread. Precompiling the same blocks can remove the small compilation cost in that scene. Lowering the cost of executing them would require changes to the generated code.

The saved Game Performance exports contained 9 completed game window writes across 69 ms in the control and 16 across 124 ms in the candidate. Eight control writes and fifteen candidate writes had vertex and fragment work in the same renderer frame. The first output in each slice lacked that evidence. The candidate frame log also contained 16 swaps in approximately the same interval. Taking the union of vertex, fragment, and compute intervals gives about 3.4–3.7 ms of GPU activity per window write in these slices. Vertex work was the largest category, at 1.8–1.9 ms per write. The total was above the 2 ms budget before accounting for other GPU work. A longer, clock-aligned capture on the movement route is still needed.

### Direct surface experiment

The native probe renders the same scene into a 1365 × 768 IOSurface two ways. The first path draws into a separate texture and blits to the IOSurface. The second draws into the IOSurface-backed texture directly. It flushes each frame and checks the two outputs pixel for pixel.

```sh
clang -O2 -Wall -Wextra -Wno-deprecated-declarations -framework OpenGL -framework IOSurface -framework CoreFoundation benchmarks/gl-direct-surface-probe.c -o /tmp/wrath-surface-probe
/tmp/wrath-surface-probe
```

On the M4 Pro, the warmed GPU timer measured 0.053–0.060 ms per frame with the full-surface blit and 0.031–0.037 ms with direct rendering. This offscreen fixture isolates the copy at the game resolution. In-game rendering and the WindowServer presentation path still need their own measurements.

### Keyboard input and movement comparisons

Carbon delivered the correct raw key codes and characters, but the bridge returned zero for the legacy layout selector `smKCHRCache`. WoW uses that layout to populate its movement-key table. The trial enables `OCERZ_M32_LEGACY_KEY_LAYOUT=1` so the layout reaches the guest as an opaque handle. `KeyTranslate` uses the original native pointer. The default bridge behavior for other applications still gives those clients their existing fallback behavior.

The corrected movement control is the `f89efb0` runtime plus the keyboard fix. The original published runtime logged 121.97 swaps/s and a 14.81 ms p99 during a separate 45-second mouse-input check. Keyboard movement was reproduced in the corrected control before measuring rendering changes.

Build the native event helpers once. Enter Northshire before starting the recorder and keep WoW in the foreground until it exits. The script prints the start and release time for each input.

```sh
xcrun swiftc -O benchmarks/trial-motion.swift -o /tmp/wrath-trial-motion
xcrun swiftc -O benchmarks/trial-input.swift -o /tmp/wrath-trial-input
zsh benchmarks/capture-northshire.sh PID FRAME_LOG RESULT_JSON 120 /tmp/wrath-trial-motion /tmp/wrath-trial-input
```

Use 600 seconds for the soak run. The route repeats the movement cycles and camera holds so the character remains active. Keep swap signposts, Instruments, and buffer diagnostics off for the timed runs.

The corrected control, diagnostic lookup build, and vertex-shadow build each completed 25 movement cycles and six mouse holds. Each run started by Deputy Willem with VSync off and a 1365 × 768 viewport. Position and angle varied slightly between sessions, and the in-game clock advanced.

| Build | Average swaps/s | p50 | p95 | p99 | Longest gap | Average client CPU | Peak footprint |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `f89efb0` plus keyboard fix | 118.51 | 8.210 ms | 11.938 ms | 14.761 ms | 100.03 ms | 89.93% | 752.06 MiB |
| Diagnostic lookup change | 132.92 | 7.437 ms | 10.781 ms | 13.199 ms | 30.84 ms | 84.71% | 751.35 MiB |
| Diagnostic lookup and vertex-shadow changes | 137.60 | 7.154 ms | 10.423 ms | 12.554 ms | 22.16 ms | 84.39% | 753.42 MiB |

[Control JSON](results/northshire-movement-keyboard-control.json) · [Diagnostic lookup JSON](results/northshire-movement-dispatch.json) · [Vertex-shadow JSON](results/northshire-movement-shadows.json)

Swaps continued throughout every measured mouse hold. The largest gap during a hold was 22.32 ms for the control, 28.18 ms after the diagnostic lookup change, and 20.33 ms with retained vertex shadows.

To build the two controls, set `AARCHX` to the pinned AArchX repository and run these commands from this repository. The [main build command](../README.md) produces the final variant.

```sh
repo_dir="$PWD"
benchmark_dir=$(mktemp -d)
revision=96754ec605d2cf57e2a05d0cf0f6d2e1c8177741
git show f89efb0:patches/aarchx-wrath.patch > "$benchmark_dir/published.patch"
git -C "$AARCHX" worktree add --detach "$benchmark_dir/control" "$revision"
git -C "$AARCHX" worktree add --detach "$benchmark_dir/dispatch" "$revision"
git -C "$benchmark_dir/control" apply --exclude=src/m32_carbon.c "$benchmark_dir/published.patch"
git -C "$benchmark_dir/control" apply --include=src/m32_carbon.c "$repo_dir/patches/aarchx-wrath.patch"
git -C "$benchmark_dir/dispatch" apply --exclude=src/m32_gl.c "$repo_dir/patches/aarchx-wrath.patch"
git -C "$benchmark_dir/dispatch" apply --include=src/m32_gl.c "$benchmark_dir/published.patch"
for variant in control dispatch; do
    make -C "$benchmark_dir/$variant" -j 8 ocerz
    app="$benchmark_dir/$variant/Wrath Trial.app"
    mkdir -p "$app/Contents/MacOS"
    install -m 755 "$benchmark_dir/$variant/ocerz" "$app/Contents/MacOS/ocerz"
    install -m 644 "$repo_dir/support/Info.plist" "$app/Contents/Info.plist"
    codesign --force --sign - "$app"
    codesign --verify --strict "$app"
done
```

Use `scripts/launch.sh` with each built app, the same runtime data and game folder, and `--unpack-alias --profile`. Exit from the game menu between builds.

The vertex-shadow change gives a storage-free upload alias preference for an unassigned slot. In the alternating 2 MiB vertex-map and 8 MiB upload-map fixture, the first 128 successful maps used 64 evictions and 132,120,608 incoming copy bytes in the control. The candidate used one eviction and 2,097,184 copy bytes. Both paths preserved the written word and its neighbors in both native GL buffers. The normal GL map, flush, and unmap calls are used in both paths.

The final runtime passed 806 general 32-bit checks, 607 Carbon checks, and 4,296 Carbon checks with the alias and alternating-buffer fixtures enabled. The keyboard fixture compared the bridge against the installed native layout and checked shifted keys, dead-key state, and the four-byte guest output boundary.

```sh
make -j 8 tests/unit/bin/test_m32
OCERZ_NO_ARM_EXEC=1 OCERZ_TCACHE=off OCERZ_M32_UNPACK_ALIAS=1 OCERZ_M32_TEST_GL_ALIAS=1 \
    OCERZ_M32_TEST_GL_SHADOW_BATCH=1 OCERZ_M32_GL_MAP_STATS=1 tests/unit/bin/test_m32 carbon
```

### Installed validation

The three two-minute runs with both performance changes averaged 136.95–137.80 swaps/s, for a mean of 137.45 swaps/s. P99 was 12.529–12.569 ms. The ten-minute run completed 119 movement cycles and 69 mouse holds.

| Installed soak | Result |
| --- | ---: |
| Average swaps/s | 137.35 |
| p50 / p95 / p99 | 7.152 / 10.544 / 12.987 ms |
| Longest gap | 30.62 ms |
| Stalls over 250 ms | 0 |
| Average client CPU | 84.13% |
| Peak sampled footprint | 752.19 MiB |

The installed launcher re-entered Northshire after a clean exit. It preserved the license choices, windowed mode, and VSync setting. The exit log showed 141,702 shared upload aliases acquired and released, with zero live mappings.

[Repeat 2 JSON](results/northshire-movement-installed-2.json) · [Repeat 3 JSON](results/northshire-movement-installed-3.json) · [Soak JSON](results/northshire-movement-installed-soak.json)

### Rendered output and the 500 FPS target

The installed Game Performance capture retained about 0.121 seconds of usable GPU events after export. In that overlap it recorded 14 AGL swaps and 14 completions with vertex and fragment work and a unique presentation-surface write. The separate Metal System Trace retained an 11.018-second overlap with 1,375 AGL swaps and 1,376 qualifying GPU completions. Pairing the streams in time order put the median start of the GPU window write 2.48 ms after the corresponding swap returned. The display sync interval was 6.068 ms median and is recorded separately from client rendering.

The installed trace showed a median 3.12 ms of GPU activity per rendered output. Across the capture, the union of client GPU intervals came to 3.11 ms of active GPU time per confirmed output. That already exceeds the 2.0 ms total budget for 500 FPS at the measured workload. The uninstrumented two-minute runs averaged 137.45 swaps/s, up 16.0% from the keyboard-corrected control. The tested build falls short of the 500 FPS target.

[Render trace summary](results/northshire-movement-render-trace.json)

### Buffer-map order experiment

Moving the driver map ahead of its state queries passed the buffer readback and shared-upload tests. The alternating-buffer fixture took 6.50 ms median with the original order and 6.62 ms with map-first across five paired batches. On the two-minute route, the same binary averaged 129.21 swaps/s with the original order and 130.46 with map-first. P99 was 13.22 ms and 13.00 ms, respectively. These runs left the buffer order unchanged. A separate run of the installed release averaged 127.62 swaps/s while the weather was changing.

The patch now checks the imported function name before scanning the environment for the alert and upload diagnostics. The isolated host-call fixture went from about 154 to 37 ns per check, and both diagnostic settings produced matching decisions. This measurement covers the guard, while the route results include driver and guest execution.
