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

## Native Metal shared-buffer probe

The raid diagnostic declared a median of **111,288 triangle primitives per swap**, across **826 draws**. The native Metal probe uses a similar draw and triangle count with synthetic geometry, a simple shader, and **363 vertex writes** to shared memory. It encodes each output into one command buffer and keeps eight frame slots. Each draw consumes its own updated range, and a frame-specific pixel readback verifies each GPU completion.

Across four launches, the warmed 200-output runs took **0.168–0.389 ms per output**. GPU completion intervals were **0.403–1.618 ms at p99**. The cold starts were measured separately. This isolates the direct submission and buffer-write path. The live raid measurements include WoW's shaders, effects, framebuffers, game logic, and compatibility layers. [Metal stream results](results/metal-raid-stream.json)

```sh
xcrun swiftc -O -framework Metal benchmarks/metal-raid-stream.swift -o /tmp/wrath-metal-raid-stream
/tmp/wrath-metal-raid-stream
```

## Live Metal backend prototype

The opt-in AArchX sidecar rendered the original WoW login screen at **1365 × 768**. The Carbon window retains its input role, and a borderless Metal window presents over its content rectangle. The experiment uses a fixed window size and requires the Carbon window to be foreground for the input checks.

The WineMetalGL patch implements the Apple buffer-object synchronization and mapped-write options that this client calls. The native test observed a buffer busy before finishing it, verified its completion afterward, and checked that a flushed range preserved the neighboring bytes. All **336** upstream GPU tests passed with the patch. A clean AArchX build with the experimental backend disabled passed **806 general checks** and **4,296 Carbon checks**.

Use dedicated source checkouts at AArchX `96754ec605d2cf57e2a05d0cf0f6d2e1c8177741` and WineMetalGL `fac2219f37bbbe132f7d67a530a035ae033aa437`. Apply the existing Wrath patch before the experimental AArchX patch. Build into a separate copy of the trial app. [WineMetalGL driver ABI](https://github.com/metalsharp/WineMetalGL/blob/fac2219f37bbbe132f7d67a530a035ae033aa437/include/winemetalgl/driver.h)

```sh
git -C "$METAL_TRIAL_AARCHX" apply "$TRIAL_REPO/patches/aarchx-wrath.patch"
git -C "$METAL_TRIAL_AARCHX" apply "$TRIAL_REPO/patches/aarchx-wrath-metal-trial.patch"
git -C "$METAL_TRIAL_WMGL" apply "$TRIAL_REPO/patches/winemetalgl-apple-buffer-trial.patch"
cmake -S "$METAL_TRIAL_WMGL" -B "$METAL_TRIAL_BUILD" -G Ninja \
    -DWMGL_HOST_ONLY=ON -DCMAKE_BUILD_TYPE=Release
cmake --build "$METAL_TRIAL_BUILD" --target winemetalgl_core
make -C "$METAL_TRIAL_AARCHX" WMGL_INCLUDE="$METAL_TRIAL_WMGL/include" ocerz
ditto "$TRIAL_APP" "$METAL_TRIAL_APP"
install -m 755 "$METAL_TRIAL_AARCHX/ocerz" "$METAL_TRIAL_APP/Contents/MacOS/ocerz"
codesign --force --sign - "$METAL_TRIAL_APP"
OCERZ_WMGL_LIBRARY="$METAL_TRIAL_BUILD/libwinemetalgl.dylib" \
    WINEMETALGL_SETTINGS=0 WINEMETALGL_SHADER_CACHE=0 WINEMETALGL_CRASH_HANDLER=0 \
    "$TRIAL_REPO/scripts/launch.sh" "$METAL_TRIAL_APP" "$RUNTIME" "$GAME_DIR" ruRU --copy-unpack --profile
```

The background login-screen capture verified **192 consecutive GPU-completed 1365 × 768 renders**, with 82–91 draws associated with each snapshot. This was a rendering and trace check with statistics enabled. It used the login scene while macOS held foreground focus, so the raid, combat effects, input, and acceptance runs are still pending. [Login GPU completions](results/metal-login-gpu-completions.json)

Apply the frame-completion patch to the same WineMetalGL checkout before building. For calibration, set `OCERZ_WMGL_TRACE_COMPLETIONS=1`, include Points of Interest in the Metal System Trace, and use the `--wmgl` analyzer mode. The completion marker comes from the GPU command that copies the rendered texture into the presenter's mailbox. It records the GPU completion time and draw count. Display presentation can run at a different rate.

```sh
git -C "$METAL_TRIAL_WMGL" apply "$TRIAL_REPO/patches/winemetalgl-frame-completion-trial.patch"
python3 "$TRIAL_REPO/benchmarks/trace-frames.py" LOGIN.trace PID 1365 768 --wmgl
```

The patched WineMetalGL core also ran the same offscreen **826-draw, 340-map** workload with its Apple mapping mode. Each frame obtained fresh storage and wrote disjoint ranges within it. The warmed 60-frame medians were **59.94 ms/frame** with serialized writes and **1.28 ms/frame** with application-managed writes. Both modes produced the same representative framebuffer checksum. This isolates the cost of waiting on a buffer before each map. The live raid still needs a trace of how the client configures and synchronizes those buffers. [Native mapping sweep](results/winemetalgl-apple-mapping-sweep.json)


For sustained measurements, set `OCERZ_WMGL_GPU_LOG` to a new absolute file path before launch. Keep `WINEMETALGL_STATS=0` for this mode. The recorder counts issued draws and successful GPU outputs while leaving the full GL statistics off. Pick a starting sequence from the warmed scene. The analyzer requires coverage through the requested endpoint and includes the gap that crosses it in p99.

```sh
python3 "$TRIAL_REPO/benchmarks/trace-frames.py" GPU.ns PID 1365 768 \
    --wmgl-log --start-sequence SEQUENCE --seconds 60
```

Use `--seconds 600` for the ten-minute run. If the log contains multiple mailbox generations at the requested size, select the generation that covers the run with `--stream`.

With GL statistics disabled, **193** calibration records from the direct file matched the Instruments signposts for sequence, dimensions, draw count, and GPU timestamp. A subsequent fixed **60-second login-screen window** contained **1,749** successful GPU completion records including its start marker, with zero missing sequence numbers. This verified the extended recording path at 1365 × 768. The background login screen averaged 29.13 completed FPS and had a 41.34 ms p99, with the raid workload still awaiting in-world testing. [Direct GPU recording results](results/metal-login-direct-60s.json)

## Guest-backed Metal vertex storage

The optional path places the rolling **2 MiB** allocation in the 32-bit guest address space and gives Metal the same pages. The driver supplies its usual storage for other sizes. AArchX tracks the borrowed mapping in its existing lookup table, so an explicit range flush can use the pages the guest already wrote. The driver retains and releases the backing according to the Metal resource lifetime.

The first prototype skipped the table entry and spent more time finding mapped buffers. After reusing the table, the median of three 40,000-write runs fell from **309 ns per write** with shadows to **284 ns** with guest-backed storage. The test used 320-byte flushes and one 24,064-byte flush every twenty writes. This measures the map, fill, flush, and unmap path with no concurrent drawing.

The offscreen AArchX test rendered vertices written through the guest mapping and checked partial updates, pointer round-trips, storage replacement, and release of unused pages. Both modes passed their checks. The original login screen still rendered at 1365 × 768 with the experiment enabled. The headless test exercised the rolling allocation; the login run checked startup. [Guest-backed storage results](results/winemetalgl-guest-storage.json)

Apply the following patches after the earlier Metal backend and completion patches, then rebuild both checkouts. Add `OCERZ_WMGL_GUEST_STORAGE=1` to the trial launch to select this path.

```sh
git -C "$METAL_TRIAL_AARCHX" apply "$TRIAL_REPO/patches/aarchx-wmgl-guest-storage-trial.patch"
git -C "$METAL_TRIAL_WMGL" apply "$TRIAL_REPO/patches/winemetalgl-optional-buffer-memory-trial.patch"
```

## Fixed-function shader key lookup

In a sampled offscreen indexed run, 870 of 3,349 main-thread samples included fixed-function key serialization. The trial now looks up the shader state in place and copies the key into the cache only on a miss. The mixed ARB path appends the same byte view to its stage key. All 336 GPU regression tests passed.

The AArchX offscreen test uses 826 draws at 1365 × 768 with 340 mapped writes and 23 other uploads per frame. In four interleaved 120-frame runs per arm, the median fell from **1.53 ms to 1.05 ms** with both fixed-function stages and from **1.61 ms to 1.10 ms** with an ARB vertex program and a fixed-function fragment stage. Each comparison produced the same representative framebuffer checksum.

The same fixture measured several smaller effects. One autorelease pool per synthetic frame changed the median from 1.49 ms to 1.46 ms. Reading the vertex-buffer binding directly reduced the isolated mapped-write time from 292 ns to 237 ns, with the mixed draw workload averaging about 1.50 ms per frame in both arms. Indexed drawing with three indices per draw took 1.57 ms per frame versus 1.50 ms for the equivalent array draws. These measurements use synthetic geometry. [Draw and shader key measurements](results/winemetalgl-draw-key-ab.json)

Both cores rendered the original background login screen. The login profiles sampled ARB program selection, and the baseline had one fixed-function key serializer sample. The login check covers startup and rendering. Raid performance, spell effects, and input still need an in-world run.

Apply the shader-key patch after the other WineMetalGL trial patches. The AArchX benchmark patch adds only the offscreen test. Apply it after the guest-storage patch and rebuild the test binary. Use `OCERZ_WMGL_ARB_BENCH=1` for the mixed shader path and leave it unset for the fixed-function case.

```sh
git -C "$METAL_TRIAL_WMGL" apply "$TRIAL_REPO/patches/winemetalgl-fixed-function-key-trial.patch"
git -C "$METAL_TRIAL_AARCHX" apply "$TRIAL_REPO/patches/aarchx-wmgl-draw-benchmark.patch"
cmake --build "$METAL_TRIAL_BUILD" --target winemetalgl_core
make -C "$METAL_TRIAL_AARCHX" WMGL_INCLUDE="$METAL_TRIAL_WMGL/include" tests/unit/bin/test_m32
cd "$METAL_TRIAL_AARCHX"
OCERZ_NO_ARM_EXEC=1 OCERZ_WMGL_LIBRARY="$METAL_TRIAL_BUILD/libwinemetalgl.dylib" \
    OCERZ_WMGL_GUEST_STORAGE=1 OCERZ_WMGL_INDEXED_BENCH=1 OCERZ_WMGL_BATCH_BENCH=0 \
    WINEMETALGL_SETTINGS=0 WINEMETALGL_SHADER_CACHE=0 WINEMETALGL_PIPELINE_CACHE=0 \
    WINEMETALGL_CRASH_HANDLER=0 WINEMETALGL_STATS=0 tests/unit/bin/test_m32 wmgl-storage
```

The linked ARB program lookup also now uses a fixed-size key for the current program serials and stage options. Cache entries retain an owned copy on a miss. The offscreen indexed test ran four 120-frame samples per arm. The one-stage ARB case moved from **1.12 to 1.07 ms/frame** and the two-stage case from **1.08 to 0.97 ms/frame**. Every run produced the same representative framebuffer checksum, and the 336 GPU tests passed. [ARB linked-program key results](results/winemetalgl-arb-program-key-ab.json)

Apply `patches/winemetalgl-arb-program-key-trial.patch` after the fixed-function key patch. The updated AArchX draw benchmark accepts `OCERZ_WMGL_ARB_BENCH=2` to run both programmable stages.

## Application-synchronized vertex uploads

The offscreen draw profile showed that ordinary `glBufferSubData` updates could interrupt a render pass with a staging blit. The opt-in upload patch uses direct writes on the mutable 2 MiB array buffer when the compatibility client disables Apple buffer serialization and the buffer has no GPU writes to reconcile. The application owns synchronization in this mode. [Apple buffer range specification](https://registry.khronos.org/OpenGL/extensions/APPLE/APPLE_flush_buffer_range.txt)

The two-stage ARB workload fell from **1.03 to 0.77 ms/frame** across four interleaved 120-frame runs per arm. A separate counter run recorded **2,640 upload-related pass endings with staging and zero with direct writes**. The framebuffer checksums matched. A focused readback verified both an already queued draw and a later draw from a disjoint updated range. With serialization enabled, the staging path kept the same render-pass and upload-blit counts with the trial flag off or on. All **336 GPU tests** passed with the flag enabled. [Direct upload measurements](results/winemetalgl-apple-subdata-ab.json)

Apply `patches/winemetalgl-apple-subdata-trial.patch` after the ARB program key patch and rebuild the core. The draw benchmark can compare `OCERZ_WMGL_APPLE_SUBDATA=0` and `OCERZ_WMGL_APPLE_SUBDATA=1`. Use `OCERZ_WMGL_BENCH_COUNTS=1` for a separate counter run. The background login capture produced no `glBufferParameteriAPPLE` calls in the targeted trace. The live raid still needs to establish which buffers are eligible.
