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
