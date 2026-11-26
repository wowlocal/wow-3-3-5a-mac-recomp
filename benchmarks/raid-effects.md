# Raid spell-effects benchmark

The local AzerothCore fixture places one boss actor and 24 scripted casters in front of Macsmoke's saved Northshire position. The boss repeats an attack animation. The actors cast Blast Wave, Arcane Explosion, Holy Nova, Consecration, and Thunder Clap on staggered timers. It exercises the client’s animation, particles, and overlapping ground effects with repeatable timings.

Use the single-player test realm. Log Macsmoke out in the clearing and set `TRIAL_REPO` to this repository. From the Realm checkout, run the preflight before preparing the scene. The installed database uses `creature.id`. Use `--spawn-entry id1` consistently on a schema with that column.

```sh
db() {
    docker --context orbstack compose exec -T ac-database sh -c 'export MYSQL_PWD="$MYSQL_ROOT_PASSWORD"; exec mysql --batch --skip-column-names -uroot acore_world'
}
python3 "$TRIAL_REPO/benchmarks/raid-effects.py" check | db
```

The expected result is `0,0,0,0,1,2`. It verifies that the fixture IDs are free, nobody is logged in, Macsmoke is parked in Northshire, and both source models exist.

```sh
docker --context orbstack stop -t 15 wrath-trial-world
python3 "$TRIAL_REPO/benchmarks/raid-effects.py" create | db
docker --context orbstack start wrath-trial-world
```

Sign in and verify the casters and overlapping effects are visible. Launch with `--profile` and use the movement sequence and recorder from the [Northshire protocol](northshire.md). Record the GPU trace in a separate window after the timed run.

Log out before removing the fixture.

```sh
docker --context orbstack stop -t 15 wrath-trial-world
python3 "$TRIAL_REPO/benchmarks/raid-effects.py" remove | db
docker --context orbstack start wrath-trial-world
```

## First effects run

At 1365 × 768 with VSync off, the 120-second movement run averaged **52.14 swaps/s**. P50, p95, and p99 frame gaps were **18.12, 28.28, and 33.57 ms**. The longest gap was **45.59 ms**. The client used **87.05% of one CPU core** on average and peaked at **762.80 MiB** of sampled physical footprint. [Frame and resource measurements](results/raid-effects.json)

The separate profiled run had **341 AGL swaps** and **342 qualifying GPU completions** in the 9.02-second overlap. Client GPU work occupied **41.3%** of that interval, or **10.89 ms per rendered output** after combining overlapping GPU intervals. Vertex, fragment, and compute activity were 5.34, 4.28, and 1.74 ms per output. Those channels overlap. In a separate stationary sample of this scene, 48.4% of main-thread stacks were in OpenGL, including binding queries and flushes. [GPU trace summary](results/raid-effects-gpu.json)

## Controlled resolution and caster comparisons

Use a fixed camera and the same saved entry position. Keep VSync off and use the same graphics options for all runs. Start with 24 casters at 1365 × 768, then use 1066 × 600 with the same cast schedule. Restore the original resolution and repeat the full fixture to check drift. For the caster-count condition, use `create --casters 12` at the original resolution. That version retains three casters per row and two per column, with all five spell types and the original positions, delays, and intervals for the selected actors.

With the classic client closed, set the smaller size with `python3 benchmarks/set-window-bounds.py 1066 600`. The Mac client reads this saved content rectangle at startup. The helper edits the rectangle's dimensions while retaining its origin. Restore the original rectangle before the next full-resolution run. Verify the full scene and UI fit the window and confirm the actual surface dimensions in the GPU trace. The resolution shown in the graphics menu can differ from the windowed rendering surface.

After warmup, record each stationary condition for 60 seconds with `scripts/measure.py PID FRAME_LOG OUTPUT --seconds 60`. Capture Metal System Trace separately with Points of Interest and swap signposts enabled. Pass the pixel dimensions to `trace-frames.py` to verify the rendering surface dimensions and count completed frames. Restore the original preferences and remove the fixture when the comparison is over.

The first two fixed-camera 24-caster controls averaged **55.65** and **54.72 swaps/s**, with p99 frame intervals of **30.23** and **31.56 ms**. Weather effects were temporarily disabled for both controls. In the separate Metal traces, client GPU work occupied 37.1% of the first capture and took **9.95** and **8.77 ms per rendered output**, respectively. [Fixed-camera control measurements](results/raid-fixed-camera-controls.json)

The main-thread samples put **31–34%** of stacks in `sp_glBufferSubData`. In the largest branch, the bridge queries `CGLGetShareGroup` immediately after the upload. Apple's OpenGL driver processes the queued upload while answering that query. This is a candidate for a focused A/B of the share-group lookup.

At 1365 × 768, the 12-caster run averaged **78.71 swaps/s** with a **24.75 ms p99** and **5.12 ms of active GPU work per rendered output**. The saved camera position matched the controls exactly. Throughput rose 42.6% over the mean of the 24-caster controls, while GPU work per output fell 45.3%. The strongest GPU changes were in vertex and fragment work. [12-caster measurements](results/raid-fixed-camera-12-casters.json)

At 1066 × 600 with all 24 casters, the fixed-camera run averaged **57.88 swaps/s** with a **28.33 ms p99**. The trace confirmed a 1066 × 600 surface and 394 completed renders for 394 swaps in the common interval. Reducing the pixel count 39.0% cut measured GPU work per output 13.3%, to **8.12 ms**. Throughput rose 4.9% over the full-size mean. This smaller response than the caster-count change points toward effect-dependent work and CPU submission for the next test. [Reduced-resolution measurements](results/raid-fixed-camera-1066x600.json)

## Share-group lookup A/B

With all 24 casters at 1365 × 768, the same test binary averaged **54.33 swaps/s** with the cache off and **55.43** with it on. P99 was **31.10** and **30.88 ms**, respectively. The cache-on frame rate was within the prior control range.

With the cache off, 636 of 638 main-thread samples inside `sp_glBufferSubData` contained the share-group query. With it on, 509 of 514 samples inside that function contained the following binding query. Both paths triggered queued driver work. The cache stays experimental. The next isolated change moves both state queries before the upload. [Share-group A/B measurements](results/raid-share-group-cache-ab.json)

## Buffer upload query order A/B

With all 24 casters at 1365 × 768, the same test binary averaged **55.33 swaps/s** when the bridge queried state after each upload and **54.35** when it queried state before each upload. P99 rose from **31.51** to **32.50 ms**. The GPU trace matched 244 completions to 244 swaps in the control and 224 to 224 in the variant.

The share-group query still accounted for 595 of the 599 main-thread samples inside `sp_glBufferSubData` with the new ordering. The driver stack showed queued uploads waiting for buffer access when the next query ran. This experiment stays out of the runtime. [Upload-order A/B measurements](results/raid-upload-order-ab.json)

## Vertex-buffer mirror A/B

The bridge keeps a guest shadow of mapped vertex and index buffers. The control reuses the shadow's contents and size between maps and updates it when the guest submits a write. The variant reads the size and contents from the native buffer when mapping it and omits the mirror updates and their state queries.

The control averaged **55.41 swaps/s** at **30.91 ms p99** and the variant averaged **31.11 swaps/s** at **48.48 ms p99**. The short GPU traces confirmed 233 and 151 qualifying completions, respectively, with one new output per swap in each trace. The variant put 647 of 1,668 main-thread samples in the mapped-buffer copy. Another 369 samples fell under the mapping's binding query, which also drained pending uploads. The runtime keeps the mirror. [Buffer-mirror A/B measurements](results/raid-buffer-mirrors-ab.json)

## Raid vertex uploads

A separate 25-second diagnostic sampled one in 127 mirrored-target `glBufferSubData` calls per thread at 1365 × 768 with all 24 casters. All **246 sampled calls** came from one guest call site and wrote a subrange of the same **2 MiB `GL_ARRAY_BUFFER`**. Payloads in the sample measured **1,056–1,584 bytes**. The game already made 858 null-data `GL_DYNAMIC_DRAW` allocations on that buffer during the interval.

The native upload submission took **0 μs** at the timer's 1 μs resolution for the median call and **1 μs** at p95. The following state queries took **171 μs** at the median and **971 μs** at p95, including time spent draining previously queued work. The CPU traces put the driver inside resource-access preparation during these drains. No sampled upload replaced the entire buffer, and every sample matched a live guest-memory mirror. This diagnostic added query timing and logging and was recorded separately from the throughput comparisons. [Vertex upload profile](results/raid-buffer-upload-profile.json)

## OpenGL threading

With the same build and vertex-buffer mirrors enabled, setting `GLFaster` to zero yielded **54.00 swaps/s** and **30.87 ms p99**. Default threading had yielded **55.41 swaps/s** and **30.91 ms p99**. The profiler confirmed that the driver stopped using the threaded query path. Time inside AGL swap rose to **5.92 ms p95** from **0.01 ms**. The short trace had 209 completed outputs and 208 swaps. The benchmark uses the default threading mode for subsequent runs. [Single-threaded measurements](results/raid-gl-single-threaded.json)

## Tracked vertex-buffer state A/B

The test reused the bridge's tracked array binding and the registered AGL share group for uploads matching the 2 MiB rolling buffer. That kept guest mirror updates intact while skipping both native state queries on most matching uploads. The log recorded at least 147,457 matching calls and 145 spot checks, with zero reported disagreements.

The same binary averaged **55.42 swaps/s** in the control and **55.78 swaps/s** with tracked state. P99 was **30.61** and **31.10 ms**. The separate traces each matched swaps to new outputs and measured approximately **7.2 ms** of GPU work per output in both arms. Main-thread samples inside `sp_glBufferSubData` fell from 470 to 2. The next buffer map's binding query accounted for 712 of 1,713 samples with tracking enabled and showed the same uploads draining there. The prototype stays out of the runtime. [Tracked-state A/B measurements](results/raid-tracked-array-ab.json)

## Guest CPU profile

The installed runtime recorded five successive eight-second profiling intervals after the 24-caster scene warmed at 1365 × 768. Executing already translated guest code accounted for **60.9–65.8%** of sampled CPU time on the hottest guest thread. Compiling new blocks took **0–1 ms** per interval at the log's millisecond resolution. This measures the compilation cost that precompiling the same blocks could remove. Improving warmed throughput would require faster generated code or less rendering work. [Guest CPU measurements](results/raid-guest-cpu-profile.json)

## Native mapped-upload probe

The standalone OpenGL probe draws 48 batches per frame using the raid's three measured payload sizes and a rolling 2 MiB vertex buffer. The accelerated legacy OpenGL profile exposed `GL_APPLE_flush_buffer_range`. The probe compared mapping, explicit flushing, and unmapping each write with `glBufferSubData`. It kept driver serialization enabled and verified identical framebuffer pixels.

The 200-frame runs took **2.73–3.04 ms/frame** with `glBufferSubData` and **6.70–8.64 ms/frame** with mapped writes. This is a native offscreen measurement. The mapped variant stays out of the trial runtime. [Native probe results](results/vertex-upload-explicit-flush.json)

```sh
xcrun clang -O2 -Wall -Wextra -Wno-deprecated-declarations -framework OpenGL \
    benchmarks/gl-map-upload-probe.c -o /tmp/wrath-map-upload-probe
/tmp/wrath-map-upload-probe
```

The native probe also tested rolling between four buffer objects with `glBufferSubData`, reinitializing the buffer store every two frames in both arms. The warmed median of three 200-frame runs was **2.93 ms/frame** with one buffer and **3.00 ms/frame** with four. A further comparison used a single buffer, assigned nonoverlapping upload ranges within each allocation, and waited for GPU work before reusing offsets. Its warmed medians were **2.98 ms/frame** with driver serialization enabled and **3.14 ms/frame** with it disabled. Each variant matched every framebuffer pixel. Neither produced a repeatable gain. [Native buffer-storage measurements](results/vertex-upload-buffer-storage.json)

## Metal command submissions

The Instruments frame assignments show a median of **59 Metal command buffers per verified game output**, with **73 at p95**, in the 24-caster control. The medians and p95s matched at reduced resolution, with tracked array state, and with single-threaded OpenGL. At 12 casters the counts dropped to **17** and **45**. The samples include only outputs with verified rendering and surfaces. [Raid submission counts](results/raid-metal-submissions.json)

A native Metal probe drew the same 48 triangles at 1365 × 768 with one command buffer and pass, or with one command buffer and pass per draw. Each arm copied a per-frame color marker to a readback buffer and verified every marker. Full framebuffer readbacks also matched pixel for pixel. The 200-frame runs averaged **0.021–0.049 ms/frame** combined and **0.985–1.103 ms/frame** split. The probe uses synthetic geometry and preloaded vertices, so the measurements isolate submission and render-pass overhead in that scene. [Native Metal probe results](results/metal-submission-probe.json)

```sh
xcrun swiftc -O -framework Metal benchmarks/metal-submit-probe.swift -o /tmp/wrath-metal-submit-probe
/tmp/wrath-metal-submit-probe
```

## Vertex write and draw order

Grouping uploads ahead of the same 48 draws in the standalone OpenGL probe preserved every framebuffer pixel. The three 200-frame runs took **1.35–1.64 ms/frame** with grouped writes and **2.80–3.20 ms/frame** with writes interleaved with draws.

An in-game diagnostic logged draw calls, buffer calls, and complete vertex attribute pointers. Across 114 full swap intervals it recorded a median of **826 draws** and **680 array-buffer map or unmap calls** per interval. Every one of the **2,598** measured small writes to the rolling buffer was followed by a draw before the next write or swap. The write covered the entire declared vertex range for at least one enabled attribute on a following draw in each case. The trace was for call order and dependency analysis. Its logging and disabled direct GL call path add overhead. [Command order measurements](results/raid-vertex-command-order.json)

## Alternative renderer and mapped writes

The WineMetalGL arm64 core built from source and passed 336 upstream GPU tests and six selected ARB program tests. The complete host suite stopped at an intentional crash test that failed to terminate. The native core opened an offscreen compatibility context and resolved the tested ARB buffer and draw entry points. Its Apple-specific buffer option returned `GL_INVALID_OPERATION`. An explicit range map with the resolved flush function preserved the written and surrounding bytes. [WineMetalGL driver ABI](https://github.com/metalsharp/WineMetalGL/blob/fac2219f37bbbe132f7d67a530a035ae033aa437/include/winemetalgl/driver.h)

The raid trace recorded **39,225 write-only maps** of the rolling buffer, each followed by one range flush and an unmap among the logged calls. A flush covered **320 bytes** at the median and **24,064 bytes** at p95.

An offscreen workload used **826 draws, 340 maps and 23 ordinary uploads** per frame. The two backends produced identical representative RGBA output. With the mapped-write path, the median of three runs was **49.28 ms/frame** on Apple OpenGL and **81.50 ms/frame** on WineMetalGL. Sending mapped writes as direct subrange uploads yielded **22.10** and **9.51 ms/frame**, respectively. The synthetic geometry isolates draw and upload costs with the raid's operation counts. [Backend and mapped-write measurements](results/raid-shaped-write-probe.json)

## In-game mapped-write substitution A/B

The same binary compared the existing native write-only mapping with an experimental path for the 2 MiB rolling buffer. The candidate used a guest-memory shadow and passed each explicit flush range to the native upload call, preserving the order of draws. More than one million maps used this path during the session. Each arm passed 4,351 Carbon checks, including range readback of neighboring bytes and the mapped pointer round trip.

Throughput fell from **54.93 swaps/s to 16.02**, and p99 rose from **31.86 ms to 72.47 ms**. The short candidate trace matched 45 qualifying GPU completions to 45 swaps. GPU active work per output increased from **10.27 ms to 43.36 ms** across the short traces. The candidate put 1,069 of 1,495 main-thread samples in the unmap's state query, with 995 under the driver's queued buffer upload. The runtime keeps the existing native mapping. [Mapped-write A/B measurements](results/raid-array-map-upload-ab.json)
