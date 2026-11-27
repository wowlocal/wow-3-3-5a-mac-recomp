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

Joining encoder labels to the same 243 verified control outputs identifies **6,658 blit-only command buffers out of 13,705 (48.6%)**. The medians were **28 blit-only** and **29 `metal.gl`-only** buffers per output. Both full-resolution repeats had a median of 28 blit-only buffers, and the lower-resolution 24-caster run had 29. The 12-caster run fell to eight. These labels identify the work submitted by Apple's OpenGL driver. The separate GL call-order trace established when vertex uploads fed subsequent draws. Identifying each blit's source GL call would require a correlated API trace. [Encoder breakdown and replay measurements](results/raid-command-buffer-breakdown.json)

```sh
python3 benchmarks/trace-frames.py RAID.trace PID 1365 768 \
    --frame-log RAID.frames.ns --encoder-breakdown
```

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

### Metal window synchronization

Apply `patches/aarchx-wmgl-carbon-window-sync-trial.patch` after the AArchX Metal trial patches and rebuild AArchX. The bridge follows Carbon window events to keep the Metal image over the game content as the window moves. The Metal window stays out of the window cycle and hides on app switch, minimize, or close. It returns over the Carbon content when the player brings WoW back. The Carbon window receives mouse and keyboard input. [Window integration checks](results/winemetalgl-carbon-window-sync.json)

Apply `patches/aarchx-wmgl-hide-background-trial.patch` after the window synchronization patch. The bridge hides WoW when another app takes focus, so the white Carbon backing window disappears along with the Metal image. In the original client at 1365 × 768, two switches to the previous app left zero visible WoW windows. Bringing WoW forward restored the aligned title bar and image. Live title-bar dragging can still collapse the Carbon window's geometry. [Live app-switch verification](results/winemetalgl-focus-hide-live.json)

Build `benchmarks/wmgl-focus-hide-check.swift` with `xcrun swiftc -O`. Run it with the ready trial process ID and the process ID of the app that had focus before launching WoW. The check leaves the trial hidden.

The native harness passed **22 checks** with the real WineMetalGL library, including moving the window, reading a GPU pixel after the move, switching away and back, minimizing, restoring, hiding, and closing. A separate plain Carbon test aborted when it changed the window size. Use a fixed-size window for the benchmark. [Window integration results](results/winemetalgl-carbon-window-sync.json)

`benchmarks/wmgl-carbon-window-check.m` builds against the patched `src/m32_wmgl.m` with `-DOCERZ_WMGL_TRIAL` and the AArchX and WineMetalGL include paths. Link AppKit, CoreGraphics, and QuartzCore. Launch it in a temporary app bundle with `OCERZ_WMGL_LIBRARY` set to the trial library and `OVERLAY_PRIOR_PID` set to the foreground app before launch. The test prints `FOCUS` for any phase that needs the bundle brought forward again. It restores the previous foreground app on exit.

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

Use `--seconds 600 --segment-seconds 60` for the ten-minute run to see how completion rate and p99 change minute by minute. If the log contains multiple mailbox generations at the requested size, select the generation that covers the run with `--stream`.

With GL statistics disabled, **193** calibration records from the direct file matched the Instruments signposts for sequence, dimensions, draw count, and GPU timestamp. A subsequent fixed **60-second login-screen window** contained **1,749** successful GPU completion records including its start marker, with zero missing sequence numbers. This verified the extended recording path at 1365 × 768. The background login screen averaged 29.13 completed FPS and had a 41.34 ms p99, with the raid workload still awaiting in-world testing. [Direct GPU recording results](results/metal-login-direct-60s.json)

For live acceptance on the M4 Pro, use the fixed camera with all 24 casters at 1365 × 768, VSync off, and `maxFPS=0`. The stock client applies a 200 FPS foreground cap. Preserve the spell effects and cast schedule. Collect three warmed 60-second windows and one warmed 600-second window from the GPU completion log. Each window must average at least 500 newly rendered frames per second, with p99 completion gaps of 2.0 ms or less. Check sequence continuity, confirm the visible spell effects and combat timing, and run the movement and input checks separately in Northshire. [Northshire route and cap verification](northshire.md)

The first complete uncapped reference run kept the 24-caster fixture at 1365 × 768 with the saved camera and VSync off. One warmed 60-second window averaged **89.13 GPU-completed FPS** at **18.29 ms p99**, with zero gaps in the GPU completion sequence. The image showed the active casters and overlapping spell effects. [Uncapped live reference](results/raid-winemetalgl-uncapped-live.json)

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

## Client selection of Apple buffer options

The opt-in capability patch advertises `GL_APPLE_flush_buffer_range` in compatibility contexts. In the background login A/B, a shared 3,825-byte diagnostic cap kept the same 150 legacy extensions in both runs, plus the Apple extension in the candidate. With that extension visible, the client set both Apple buffer options to zero on array and unpack buffers and allocated a guest-backed 2 MiB buffer.

With the extension visible in both arms, both settings of the direct-upload trial rendered the original login at 1365 × 768 and completed a five-second GPU smoke window with consecutive sequence numbers. The guard and allocator were available at startup. Direct-upload activation remains an in-world check. The 336 GPU tests passed with both trial flags enabled. [Client capability check](results/winemetalgl-apple-buffer-capability.json)

Apply `patches/winemetalgl-apple-buffer-capability-trial.patch` after the upload patch to reproduce the capability experiment. Set `OCERZ_WMGL_ADVERTISE_APPLE_BUFFER_RANGE=1` to expose the extension. The chosen cap applies to the A/B diagnostic. Set `OCERZ_WMGL_TRACE_APPLE_SUBDATA=1` to log the first eligible direct upload.

## Guest-backed Metal texture uploads

The opt-in unpack path places 8 MiB allocations in the guest address space using the existing Metal storage callback. The unpack flag is independent of the 2 MiB vertex flag. On a matching mapping, AArchX can hand the guest the same pages that Metal reads during the upload.

Four interleaved headless runs per mode measured 512 mapped 4 KiB writes each. The median fell from **111.6 µs to 0.35 µs per mapped write**. After the timed loop, the test queued a GPU texture upload, replaced the unpack buffer storage, and queued a second upload. Both textures matched all **8,192** expected readback bytes. The timed numbers cover the map, fill, flush, and unmap path.

The original login screen used this path. Over the first 128 unpack maps, the copy control copied **1 GiB** into the guest and recorded **14.70 ms** in those copies. The candidate logged 128 direct 8 MiB mappings, rendered the login screen at 1365 × 768, and recorded a consecutive five-second GPU smoke window. This evidence covers startup and texture uploads. The live raid still needs its own performance and visual verification. [Unpack storage measurements](results/winemetalgl-unpack-storage-ab.json)

Apply `patches/aarchx-wmgl-unpack-storage-trial.patch` after the vertex storage patch and rebuild AArchX. Use `OCERZ_WMGL_GUEST_UNPACK_STORAGE=1` on launch. For the headless comparison, use the updated AArchX draw benchmark patch and run `tests/unit/bin/test_m32 wmgl-storage` with `OCERZ_WMGL_UNPACK_BENCH=1` while alternating the unpack storage flag.

## GPU completion timing at the captured triangle count

This offscreen WineMetalGL probe uses the triangle-count histogram from one recorded raid frame. It submits **826 indexed draws and 111,230 triangles** at 1365 × 768, with 340 mapped uploads and 23 ordinary uploads per frame. It builds synthetic tessellated geometry and uses simple ARB shaders. It calls the native core directly, so the test isolates renderer and scheduling cost from guest translation and game logic.

The offscreen-completion patch records the GPU timestamp at the submitted pbuffer boundary using the existing completion log. Each measured window starts after 120 warmup frames, covers five seconds of GPU completions, and checks for gaps in the sequence. Each run also reads back the same final reference frame.

Across four interleaved pairs at an in-flight limit of three frames, direct uploads raised median throughput from **1,199 to 1,635 GPU-completed frames/s** and lowered median p99 from **3.52 to 2.05 ms**. With direct uploads enabled in both arms, raising the probe's in-flight limit from three to six moved the median from **1,664 to 1,754 frames/s** and from **1.88 to 1.64 ms p99**. One six-in-flight sample still exceeded 2 ms p99. All 16 windows had consecutive sequences, and all reference frames matched. The 336 renderer tests passed. Queue depth was varied only in the probe. The live raid still needs GPU and input verification. [GPU completion measurements](results/winemetalgl-indexed-geometry-gpu-ab.json)

The staged replay recorded **23 render passes per output**, including 22 that ended at upload blits. Disabling the elapsed-time submit heuristic (`WINEMETALGL_PASS_COMMIT_US=0`) kept those splits and produced a similar median across four pairs, **1,209 to 1,218 GPU-completed frames/s**. With that heuristic disabled in both arms, also disabling the draw-count heuristic (`WINEMETALGL_COMMIT_DRAWS=0`) reduced main-stream commits from three to one while retaining 23 passes. Across four pairs, throughput fell from **1,228 to 1,174 frames/s** and p99 rose from **3.20 to 3.73 ms**. Direct uploads produced one render pass and one main-stream commit with the normal heuristics. The recorded timings used statistics disabled and all checksums matched. [Encoder breakdown and replay measurements](results/raid-command-buffer-breakdown.json)

Apply the offscreen-completion patch to the Metal trial tree with the frame-completion and application-synchronized upload patches already applied. Build the benchmark against the resulting WineMetalGL core. Use a fresh log path per run. Alternate `OCERZ_WMGL_APPLE_SUBDATA=0` and `1` for the upload comparison. Use `3` and `6` in the last argument for the depth comparison.

```sh
git -C "$METAL_TRIAL_WMGL" apply "$TRIAL_REPO/patches/winemetalgl-offscreen-completion-trial.patch"
cmake --build "$METAL_TRIAL_BUILD" --target winemetalgl_core
xcrun clang -O2 -Wall -Wextra -Werror -Wno-deprecated-declarations -I "$METAL_TRIAL_WMGL/include" \
    "$TRIAL_REPO/benchmarks/wmgl-indexed-geometry.c" -L "$METAL_TRIAL_BUILD" -lwinemetalgl \
    -Wl,-rpath,"$METAL_TRIAL_BUILD" -o /tmp/wmgl-indexed-geometry
OCERZ_WMGL_APPLE_SUBDATA=1 OCERZ_WMGL_GPU_LOG="$METAL_TRIAL_GPU_LOG" \
    WINEMETALGL_SETTINGS=0 WINEMETALGL_SHADER_CACHE=0 WINEMETALGL_PIPELINE_CACHE=0 \
    WINEMETALGL_CRASH_HANDLER=0 WINEMETALGL_STATS=0 WINEMETALGL_LOG=0 \
    /tmp/wmgl-indexed-geometry "$TRIAL_REPO/benchmarks/fixtures/raid-triangle-counts.tsv" 3 &
probe_pid=$!
wait "$probe_pid" &&
python3 "$TRIAL_REPO/benchmarks/trace-frames.py" "$METAL_TRIAL_GPU_LOG" "$probe_pid" 1365 768 \
    --wmgl-log --start-sequence 128 --seconds 5
```

## Vertex storage reuse and array binding lookup

The saved raid call trace recorded one null-data allocation of the 2 MiB vertex store in 71 of 114 swap intervals and zero in the other 43. With three frames in flight, the native indexed probe allocated three stores during warmup and then reused them across **13,437** replacements. Its `glBufferData` p99 was **584 ns**. At depths of six and eight, the probe needed **299** and **406** additional allocations, and replacement p99 rose to **137 µs** and **141 µs**, respectively. Reproduce the storage counts by adding `OCERZ_WMGL_PROBE_STORAGE=1` to the native indexed probe. It supplies aligned host allocations and times the `glBufferData` call separately. [Storage depth measurements](results/winemetalgl-array-binding-ab.json)

The opt-in array binding path lets AArchX read the current WineMetalGL context's `GL_ARRAY_BUFFER` binding directly while locating mapped guest storage. It falls back to the standard GL query when the optional entry point or current context is unavailable, during Begin/End, and during GL tracing or profiling. It uses the renderer's binding under its existing share-group lock, including an unbound result and distinct bindings in shared contexts.

In six interleaved pairs of 40,000 mapped writes, the median fell from **285 to 239 ns per write**. In four interleaved pairs of 6,000 draw-stream frames, the median fell from **0.783 to 0.764 ms/frame**, with improvement in three of the four pairs. The tests used the AArchX mapping bridge, 826 indexed draws per frame, and synthetic ARB programs. Each arm passed the mapping checks and produced the same framebuffer checksum. A focused test also verified binding changes, separate bindings on shared contexts, unbind, and the Begin/End fallback. [Array binding A/B](results/winemetalgl-array-binding-ab.json)

Apply `patches/winemetalgl-array-binding-trial.patch` after the optional buffer-memory patch. Apply `patches/aarchx-wmgl-array-binding-trial.patch` after the AArchX unpack storage patch and rebuild AArchX and WineMetalGL. Use `OCERZ_NO_ARM_EXEC=1` and set `OCERZ_WMGL_LIBRARY` to the rebuilt library. To reproduce the long draw runs via `tests/unit/bin/test_m32 wmgl-storage`, enable `OCERZ_WMGL_GUEST_STORAGE=1`, `OCERZ_WMGL_APPLE_SUBDATA=1`, and `OCERZ_WMGL_INDEXED_BENCH=1`. Set `OCERZ_WMGL_BENCH_FRAMES=6000`, `OCERZ_WMGL_ARB_BENCH=2`, and `OCERZ_WMGL_BATCH_BENCH=0` while alternating `OCERZ_WMGL_ARRAY_BINDING=0` and `1`. Set `OCERZ_WMGL_MAP_BENCH=1` to measure the separate mapped-write loop.

## GPU completion through translated i386

The offscreen replay uses the same indexed geometry generator as the native probe. Each frame runs as compiled i386 code through the AArchX JIT. The guest function calls the GL imports, inserts a fence, and calls AGL SwapBuffers. The harness caps the number of frames in flight at three. WineMetalGL records successful GPU completions for the 1365 × 768 pbuffer. Each arm must produce the same reference pixels as the native probe. The geometry, shaders, and frame loop are synthetic.

Four interleaved pairs of warmed five-second windows kept guest-backed storage and direct array binding enabled. Direct uploads moved the median from **1,012 to 1,356 GPU-completed FPS**, and p99 from **3.79 to 1.93 ms**. Every window had consecutive GPU completion sequences and 826 draws per output. The render used 111,230 triangles and 363 vertex uploads per frame. [Translated timing results](results/winemetalgl-i386-indexed-gpu-ab.json)

A separate 60-second direct-upload window contained **82,428** consecutive GPU completion records, including the starting marker. It averaged **1,374 completed FPS** with **1.96 ms p99**. The process reported 36 translated blocks, passed 59 bridge checks, and matched the native reference pixels. The live raid still needs its own sustained captures, effects checks, and input validation. [Sustained replay results](results/winemetalgl-i386-indexed-gpu-ab.json)

Apply the replay patch to the AArchX trial checkout after the draw benchmark, unpack storage, and array binding patches. Build WineMetalGL with the offscreen completion and array binding patches. Run the benchmark from the AArchX checkout so it can find `runtime/apis32`.

```sh
git -C "$METAL_TRIAL_AARCHX" apply "$TRIAL_REPO/patches/aarchx-wmgl-i386-replay.patch"
cp "$TRIAL_REPO"/benchmarks/wmgl-i386-*.h "$TRIAL_REPO/benchmarks/wmgl-indexed-shape.h" "$METAL_TRIAL_AARCHX/tests/unit/"
make -C "$METAL_TRIAL_AARCHX" WMGL_INCLUDE="$METAL_TRIAL_WMGL/include" tests/unit/bin/test_m32

replay_dir=$(mktemp -d)
xcrun clang -arch i386 -mmacosx-version-min=10.8 -O2 -ffreestanding -fno-pic \
    -fno-stack-protector -fno-unwind-tables -mno-sse -mno-sse2 -Wall -Wextra -Werror \
    -c "$TRIAL_REPO/benchmarks/wmgl-i386-frame.c" -o "$replay_dir/frame.o"
test "$(xcrun nm -p "$replay_dir/frame.o")" = '00000000 T _render_frame' || exit 1
if xcrun otool -rv "$replay_dir/frame.o" | grep -q 'Relocation information'; then exit 1; fi
xcrun llvm-objcopy --dump-section "__TEXT,__text=$replay_dir/frame.bin" "$replay_dir/frame.o"

(cd "$METAL_TRIAL_AARCHX" && exec env -u OCERZ_NO_ARM_EXEC \
    OCERZ_WMGL_LIBRARY="$METAL_TRIAL_BUILD/libwinemetalgl.dylib" \
    OCERZ_WMGL_GUEST_STORAGE=1 OCERZ_WMGL_ARRAY_BINDING=1 OCERZ_WMGL_APPLE_SUBDATA=1 \
    OCERZ_WMGL_I386_REPLAY="$replay_dir/frame.bin" \
    OCERZ_WMGL_TRIANGLE_HISTOGRAM="$TRIAL_REPO/benchmarks/fixtures/raid-triangle-counts.tsv" \
    OCERZ_WMGL_GPU_LOG="$replay_dir/gpu.ns" WINEMETALGL_SETTINGS=0 WINEMETALGL_SHADER_CACHE=0 \
    WINEMETALGL_PIPELINE_CACHE=0 WINEMETALGL_CRASH_HANDLER=0 WINEMETALGL_STATS=0 WINEMETALGL_LOG=0 \
    tests/unit/bin/test_m32 wmgl-storage) &
probe_pid=$!
wait "$probe_pid" &&
python3 "$TRIAL_REPO/benchmarks/trace-frames.py" "$replay_dir/gpu.ns" "$probe_pid" 1365 768 \
    --wmgl-log --start-sequence 128 --seconds 5
```

Alternate `OCERZ_WMGL_APPLE_SUBDATA=0` and `1` for the paired runs and use a fresh log each time. To collect a 60-second window, set `OCERZ_WMGL_I386_SECONDS=63` and analyze with `--seconds 60`.

## Reusing the validated render-target descriptor

In the translated replay's separate six-second CPU profile, **2,357 of 3,401** main-thread samples included the indexed-draw entry point. Render-target descriptor assignment accounted for **213** samples. Direct initialization from the validated descriptor avoids the release calls on empty destination texture slots while keeping the same attachment lifetime. After the change, a separate profile recorded **138** descriptor-assignment samples out of **3,409** main-thread samples. [CPU profiles and A/B measurements](results/winemetalgl-target-handoff-ab.json)

Four interleaved pairs of six-second translated replay windows moved the median from **1,358 to 1,384 GPU-completed FPS** and from **1.97 to 1.94 ms p99**. Throughput improved in three pairs. All eight runs had consecutive GPU sequences, identical reference pixels, and 826 draws per output. The change passed all 336 renderer tests. To reproduce the A/B, keep the replay, configuration, and direct uploads fixed. Build two WineMetalGL libraries and apply `patches/winemetalgl-target-handoff-trial.patch` on the candidate only. [Replay results](results/winemetalgl-target-handoff-ab.json)

Both libraries also rendered the original client’s background login screen at **1365 × 768**. The control and candidate recorded **147** and **146** consecutive GPU completions in their respective five-second windows. The login scene and compatibility dialog were visible in both readbacks, and both clients used the guest-backed texture upload path. The logged rates reflect the background login workload. [Original client smoke test](results/winemetalgl-target-handoff-ab.json)

### Default framebuffer descriptor reset

The default framebuffer initialized the render-target descriptor twice per draw. `Surface::describe` already clears it before setting the attachments. The trial lets that call handle initialization for the default framebuffer and keeps the explicit reset for named framebuffers and missing surfaces.

Across six interleaved pairs of six-second translated replay windows, median throughput rose from **1,402 to 1,437 GPU-completed FPS** and p99 fell from **1.98 to 1.84 ms**. Throughput improved in five pairs. Each run had consecutive sequences and matched the native reference pixels. All 336 renderer tests passed. A focused readback also verified sRGB behavior, disabling the draw buffer, switching to a named framebuffer and back, and drawing to a resized surface. Apply `patches/winemetalgl-default-target-reset-trial.patch` after the render-target handoff patch to reproduce this trial. [Measurements](results/winemetalgl-default-target-reset-ab.json)

A separate experiment selected the linear or sRGB color texture during default target construction. It matched the reference framebuffer and passed the focused readback, including linear rendering after a resize. Across four pairs, median throughput moved from **1,438 to 1,448 FPS** while p99 rose from **1.80 to 1.91 ms**. The trial uses the single-reset change. [Color attachment comparison](results/winemetalgl-default-target-reset-ab.json)

The single-reset build averaged **1,459 GPU-completed FPS** with **1.81 ms p99** in a separate 60-second translated replay. It recorded **87,556 consecutive GPU completions** including the starting marker, matched the reference framebuffer, and passed the bridge checks. [Sustained replay results](results/winemetalgl-default-target-reset-ab.json)

## Windowed presentation overhead

The translated replay can attach its AGL context to a Carbon window. That runs the same Metal overlay and three-slot presenter as the trial client, with VSync off. Set `OCERZ_WMGL_I386_WINDOW=1` on the replay command to use this path. The offscreen and windowed arms generated the same 826 draws, matched reference pixels, and recorded consecutive GPU completion sequences.

Four interleaved six-second pairs put the offscreen median at **1,438 GPU-completed FPS and 1.87 ms p99** and the windowed median at **1,233 FPS and 3.02 ms p99**. In a separate instrumented windowed run, the time to acquire a mailbox slot reached **1.99 ms p99** across 4,717 warmed frames. The whole swap call reached **2.04 ms p99**. [GPU timing and swap profile](results/winemetalgl-windowed-replay.json)

Apply `patches/winemetalgl-presentation-log-trial.patch` after the offscreen completion patch to record display diagnostics. Set `OCERZ_WMGL_PRESENT_LOG` to a fresh file and add `--present-log FILE` to the GPU analysis command. The reader joins presentation callbacks to verified GPU completions by process, stream, and sequence. Apple reports a zero presentation time when a drawable is still unpresented or its frame was dropped. In the background diagnostic, 721 callbacks matched the GPU sample and all reported zero time. This captures mailbox work and leaves actual display cadence unverified. [Apple Metal presentedTime documentation](https://developer.apple.com/documentation/metal/mtldrawable/presentedtime) [Presentation diagnostic](results/winemetalgl-windowed-replay.json)

An experiment changed `Surface::Mailbox::kSlots` from three to five. Across another four paired window runs it moved the median from **1,252 to 1,283 GPU-completed FPS** and from **3.01 to 2.89 ms p99**. In a separate five-slot profile the slot wait was below 0.01 ms at p99, with 21 waits of at least 0.1 ms across 5,195 warmed frames. Both arms preserved the reference pixels. Five slots require two additional frame-size textures and still need an in-world latency check. [Mailbox capacity experiment](results/winemetalgl-windowed-replay.json)

### Mailbox scheduling

The windowed replay compared three changes to when the renderer can reuse a mailbox slot. Each comparison ran four interleaved pairs of six-second windows at 1365 × 768 with VSync off and three slots. Reference pixels matched and the GPU sequences were consecutive in all runs. [Scheduling comparisons](results/winemetalgl-mailbox-scheduling-ab.json)

| Experiment | GPU-completed FPS, control → candidate | p99, control → candidate |
| --- | ---: | ---: |
| Publish the ready slot before logging GPU completion | 1,298 → 1,267 | 2.89 → 3.11 ms |
| Put rendering and presentation on one queue and release slots after submission | 1,296 → 249 | 3.06 → 10.78 ms |
| Keep two queues and use one Metal event per slot to order reuse | 1,261 → 1,250 | 3.04 → 2.99 ms |

The last experiment signaled each slot's event after the queued presentation read. A later writer could submit its render work and wait on the GPU before copying into that slot. A separate diagnostic verified all 492 queued reads signaled their events. The trial uses separate queues and waits for slot readiness on the CPU. [Apple Metal event synchronization](https://developer.apple.com/documentation/metal/synchronizing-events-within-a-single-device)

A windowed replay sweep varied the harness's fence limit through 1, 2, 3, 4, and 6 frames in flight. The lowest single-window p99 was **2.93 ms** with four frames in flight. One frame in flight averaged **846 FPS** and reached **4.16 ms p99**. [Fence-depth sweep](results/winemetalgl-mailbox-scheduling-ab.json)

### CPU and GPU time in long completion gaps

Two ten-second windowed captures matched CPU slot and flush timestamps to Metal command-buffer intervals for **25,259** consecutive GPU completion gaps. Of the **942** gaps over 2 ms, the change in GPU command-buffer execution duration was the largest component in **654** and slot acquisition was largest in **280**. GPU execution duration had a median of **0.30 ms** and a p99 of **3.21–3.25 ms**. The completion callback began about **0.10 ms at p99** after the GPU end timestamp in both captures. These intervals measure the command buffer that contains rendering and the mailbox copy in the normal windowed path. [Windowed frame timing](results/winemetalgl-windowed-frame-timing.json)

Each gap equals the interval between the two host flush entries plus the change in time from flush entry to GPU start and the change in GPU execution duration. The analyzer also splits the host interval at slot acquisition. Metal's GPU start and end timestamps and the CPU timestamps use system Mach time. [Apple GPU timestamp documentation](https://developer.apple.com/documentation/metal/mtlcommandbuffer/gpustarttime?language=objc)

Apply `patches/winemetalgl-frame-timing-trial.patch` after the presentation log patch. Set `OCERZ_WMGL_TIMING_LOG` and `OCERZ_WMGL_GPU_LOG` to separate fresh files on a windowed run, then add `--timing-log FILE` to the GPU analysis command. Collect performance windows separately with the phase log disabled.

### Early submission while waiting for a slot

When every mailbox slot is busy, the opt-in trial submits the current render commands before waiting for a slot. It then submits the copy on the same render queue after acquiring the slot. In four paired six-second windows, median p99 fell from **2.96 to 2.86 ms**, with an improvement in every pair. Median throughput moved from **1,287 to 1,279 GPU-completed FPS**. A separate warmed 60-second pair moved from **1,245 FPS and 3.09 ms p99** to **1,260 FPS and 2.96 ms p99**. The reference pixels matched and GPU sequences were consecutive across all runs. [Early submission results](results/winemetalgl-windowed-frame-timing.json)

Apply `patches/winemetalgl-flush-before-slot-wait-trial.patch` after the frame timing patch and set `OCERZ_WMGL_FLUSH_BEFORE_SLOT_WAIT=1` for this trial. Use the regular GPU log for performance comparisons. When early submission splits the draw and copy, the phase log records the copy command buffer that completes the output. Live rendering and input latency still need verification.

### Draw and copy encoder timing

Two diagnostic captures matched encoder timestamp samples to **25,196** command buffers, each containing one render pass and one mailbox blit. Among **570** gaps over 2 ms where GPU command duration was the largest component, the blit duration increased the most in **399** and the render duration increased the most in **171**. The blit took **33–40 µs** at the median and about **1.52 ms** at p99. The interval from the render pass ending to the blit starting was about **1.1 µs** at p99. [Encoder timing results](results/winemetalgl-mailbox-copy-ab.json)

Apply `patches/winemetalgl-pass-timing-trial.patch` and use both frame timing logs with `WINEMETALGL_PASS_PROFILE=1` and a fresh `OCERZ_WMGL_PASS_TIMING_LOG` file. Set `--pass-log FILE` alongside `--timing-log FILE` in the analyzer. The pass log matches the Metal command buffer ending each frame by process and GPU end timestamp. For the combined render-and-copy analysis, leave early submission disabled. [Apple GPU timestamp counters](https://developer.apple.com/documentation/metal/converting-gpu-timestamps-into-cpu-time)

A compute shader copied the source into the mailbox slot with an exact match across **4,193,280 RGBA bytes** on the sampled frame. Its copy still reached **1.61 ms at p99** in a separate encoder trace. Four timed pairs moved median p99 from **3.17 to 3.10 ms**, with improvement in two pairs. [Compute copy experiment](results/winemetalgl-mailbox-copy-ab.json)

A separate fullscreen render pass fetched the RGBA8 source by integer pixel coordinates. Its selected mailbox texture matched all **4,193,280 bytes** of the source readback. In four paired six-second windows, median throughput rose from **1,246 to 1,296 GPU-completed FPS**, and median p99 fell from **3.12 to 2.68 ms**. The p99 improved in three pairs. [Render-pass copy experiment](results/winemetalgl-mailbox-copy-ab.json)

The warmed 60-second pair moved from **1,262 FPS and 3.14 ms p99** to **1,274 FPS and 2.80 ms p99**, with matching reference pixels and consecutive completions. A separate 257 × 256 GPU readback checked all 256 byte values in each RGBA channel. The original client also rendered the background login screen and recorded 146 consecutive GPU completions in a five-second sample. [Sustained replay and validation](results/winemetalgl-mailbox-copy-ab.json)

Apply `patches/winemetalgl-raster-copy-trial.patch` after the early-submission trial patch. Set `OCERZ_WMGL_MAILBOX_RASTER_COPY=1` for this trial. It selects the render-pass copy for the linear RGBA8 surface and uses the normal blit for other formats or when the render encoder is unavailable. The measurements used early submission disabled. Live raid and input validation are still pending.

### Early submission and render-pass copy together

The windowed replay measured all four flag combinations in four interleaved blocks, then ran one warmed 60-second window per configuration. Every run matched the reference pixels and logged consecutive GPU completions. VSync was off and the mailbox used three slots. [Combined mailbox results](results/winemetalgl-combined-mailbox-ab.json)

| Configuration | Six-second median FPS | Six-second median p99 | Sixty-second FPS | Sixty-second p99 |
| --- | ---: | ---: | ---: | ---: |
| Control | 1,257 | 3.01 ms | 1,215 | 3.41 ms |
| Early submission | 1,243 | 3.00 ms | 1,209 | 3.20 ms |
| Render-pass copy | 1,262 | 3.01 ms | 1,251 | 3.07 ms |
| Both | 1,315 | 2.63 ms | 1,271 | 2.96 ms |

Both changes together lowered p99 against the control in every short block and against each individual change in three blocks. To reproduce the combined arm, set `OCERZ_WMGL_FLUSH_BEFORE_SLOT_WAIT=1` and `OCERZ_WMGL_MAILBOX_RASTER_COPY=1`. [Paired and sustained results](results/winemetalgl-combined-mailbox-ab.json)

### Publish the rendered color texture directly

The direct-publication trial hands the rendered linear RGBA8 texture to the mailbox and retains it as the readable back buffer. Before the next color write, it switches to the spare texture from that slot. A full unmasked color clear can use the spare immediately. Partial and masked clears and other writes copy the preserved pixels first. A swap with an unchanged back buffer uses the regular copy path. [Replay and pixel checks](results/winemetalgl-published-color-ab.json)

In four paired six-second windows with early submission and raster copy enabled in both arms, direct publication raised median throughput from **1,261 to 1,382 GPU-completed FPS** and lowered median p99 from **3.02 to 2.25 ms**. All four pairs improved on both metrics and matched the reference pixels. [Short paired runs](results/winemetalgl-published-color-ab.json)

Build the native preservation check against the trial library to verify reads after swap, repeated swaps, partial and masked clears, resize, and a render-queue switch.

```sh
xcrun clang++ -std=c++17 -O2 -fobjc-arc -Wno-deprecated-declarations \
    -I "$METAL_TRIAL_WMGL/include" -L "$METAL_TRIAL_BUILD" -lwinemetalgl \
    -Wl,-rpath,"$METAL_TRIAL_BUILD" -framework Foundation -framework QuartzCore \
    "$TRIAL_REPO/benchmarks/wmgl-published-color-check.mm" -o /tmp/wmgl-published-color-check
OCERZ_WMGL_MAILBOX_ROTATE_COLOR=1 OCERZ_WMGL_MAILBOX_RASTER_COPY=1 \
    OCERZ_WMGL_FLUSH_BEFORE_SLOT_WAIT=1 /tmp/wmgl-published-color-check
```

The warmed 60-second pair moved from **1,304 FPS and 2.67 ms p99** to **1,367 FPS and 2.38 ms p99**. A separate ten-second encoder capture matched **13,232** render command buffers, each without a blit interval. The render pass reached **2.43 ms p99**. The pass and command-buffer durations agreed within 42 ns in that capture. With the trial enabled, the original client rendered its background login screen and recorded 146 consecutive GPU completions. [Sustained and encoder results](results/winemetalgl-published-color-ab.json)

Apply `patches/winemetalgl-published-color-trial.patch` after the raster copy patch and set `OCERZ_WMGL_MAILBOX_ROTATE_COLOR=1`. Direct publication is eligible on the single-sample windowed linear RGBA8 path when a draw is pending on the render queue. Live raid and input validation are still pending.

### Render-pass tail after direct publication

Two windowed traces with direct publication recorded **639** completion gaps over 2 ms. The change in GPU render duration was the largest component in **446** gaps, while mailbox-slot acquisition was largest in **186**. Neither trace showed a blit in the sampled frame completions. The median GPU execution time was about **0.26 ms**, with p99 of **2.48–2.53 ms**. [Frame and encoder timing](results/winemetalgl-published-color-tail.json)

The draw-count submission sweep rotated `WINEMETALGL_COMMIT_DRAWS` through 256, 64, and 96 in three interleaved six-second blocks. A limit of 64 causes an earlier submission when the running pass reaches 512 draws. The median moved from **1,368 FPS and 2.32 ms p99** at 256 to **1,367 FPS and 2.06 ms p99** at 64. The 96 setting reached **2.42 ms p99**. All runs matched the reference pixels and recorded consecutive GPU completions. [Draw submission sweep](results/winemetalgl-published-color-tail.json)

The warmed 60-second pair moved from **1,299 FPS and 2.499 ms p99** at 256 to **1,366 FPS and 2.049 ms p99** at 64. Draw counts, reference pixels, and GPU sequence continuity matched. [Sustained draw submission results](results/winemetalgl-published-color-tail.json)

The renderer counters confirmed one pass and one submission per frame at 256, and two of each at 64 and 52. A second short sweep paired the 512-draw split at 64 with a more even 416-draw split at 52. Median p99 was **2.076 ms** at 64 and **2.119 ms** at 52, with 52 improving in one of three pairs. [Split-size and activation checks](results/winemetalgl-published-color-tail.json)

A diagnostic capture with the 512-draw split recorded **215** completion gaps over 2 ms. The change in time from host flush to GPU start was the largest component in **109** gaps, followed by GPU duration in **80** and slot acquisition in **19**. These timestamps describe the final command buffer of the split frame. The frame's earlier pass was already submitted at that point. [Final-command timing](results/winemetalgl-published-color-tail.json)

A separate experiment used early submission and a raster copy only when every mailbox slot was busy. Across four pairs, median p99 moved from **2.38 to 2.32 ms**, improving in two pairs. Median throughput fell from **1,378 to 1,362 GPU-completed FPS**. This change stayed experimental. [Busy-slot comparison](results/winemetalgl-published-color-tail.json)

### Match both submissions in a split frame

Two ten-second command-buffer traces paired **27,318** frames into a 512-draw buffer followed by a 314-draw buffer. In all **140** long completion gaps where final-buffer dispatch delay was the largest component, an increase in overlap with earlier render work was the largest part of that delay. The same frame’s first render buffer alone covered **0.97–1.10 ms** of the final buffer's dispatch wait at p99. The portion of dispatch wait outside the recorded render and presentation intervals was **0.13–0.14 ms** at p99. [Split command-buffer traces](results/winemetalgl-split-dispatch.json)

Apply `patches/winemetalgl-submission-trace-trial.patch` after the direct-publication patch. Set `OCERZ_WMGL_SUBMISSION_LOG` to a fresh file with the GPU and frame timing logs enabled, and pass that file with `--submission-log` alongside `--timing-log` in the analyzer. The trace joins the final buffer by its GPU start and end timestamps, checks each frame's producer draw count, and measures overlap with recorded GPU work during final dispatch. [Trace outputs](results/winemetalgl-split-dispatch.json)

With direct publication and the 512-draw split active, three interleaved blocks compared two, three, and four frames in flight. Median p99 was **2.116**, **2.136**, and **2.096 ms** respectively, with throughput of **1,328**, **1,361**, and **1,360 GPU-completed FPS**. All outputs matched the reference pixels and kept consecutive GPU completion sequences. [Frame-in-flight sweep](results/winemetalgl-split-dispatch.json)

The warmed 60-second pair favored three frames in flight, at **1,361 FPS and 2.077 ms p99**. Four frames in flight reached **1,340 FPS and 2.103 ms p99**. [Sustained in-flight comparison](results/winemetalgl-split-dispatch.json)

Another three paired short windows tested splitting the draw stream after each 384 draws by setting `WINEMETALGL_COMMIT_DRAWS=48`. The resulting three-pass configuration reached **1,303 FPS and 2.468 ms median p99**. The paired 512-draw split at 64 reached **1,380 FPS and 2.072 ms median p99**, improving both metrics in every pair. [Two- and three-pass results](results/winemetalgl-split-dispatch.json)

### Triangle budget for split rendering

The replay's 512-draw split assigns **71,516** triangles to its first buffer and **39,714** to its second. The triangle-budget trial counts submitted direct triangle-list work and allows it to trigger an earlier split. The existing draw limit still bounds the command buffer, including other topologies and indirect draws. The replay kept its 826 draws and original draw order. [Modeled cut points](results/winemetalgl-triangle-split-ab.json)

Three interleaved six-second blocks compared the draw limit alone with triangle budgets of 56,000 and 65,000. The 65,000 setting improved p99 in every block, moving the median from **2.085 to 1.958 ms**. Median throughput rose from **1,352 to 1,365 GPU-completed FPS**. The nearly even 56,000 split had a median p99 of **2.057 ms**. All runs preserved the reference pixels and consecutive GPU completion sequences. [Short comparisons](results/winemetalgl-triangle-split-ab.json)

The warmed 60-second control reached **1,355 FPS and 2.094 ms p99**. The two runs at 65,000 reached **1,380 FPS and 2.003 ms p99** and **1,350 FPS and 1.990 ms p99**. Both preserved reference pixels and consecutive GPU completion sequences. [Sustained comparison](results/winemetalgl-triangle-split-ab.json)

A separate submission trace verified the 65,000 setting changed the split from **512 + 314** draws to **466 + 360**. On this replay's original draw order, those two buffers contain **65,117** and **46,113** triangles. The pixel-preservation checks and all 336 renderer tests passed. [Submission and validation results](results/winemetalgl-triangle-split-ab.json)

Apply `patches/winemetalgl-triangle-budget-trial.patch` after the submission trace patch. Set `OCERZ_WMGL_COMMIT_TRIANGLES=65000` with `WINEMETALGL_COMMIT_DRAWS=64` for this trial. The triangle count is a submission heuristic. The draw budget still applies and the commands keep their original order. Live raid and input validation are still pending.

### Nearby triangle budgets

Four interleaved six-second blocks compared **62,500**, **65,000**, and **67,500** triangles with the same draw limit and three frames in flight. The 62,500 setting lowered p99 against 65,000 in every block. Median p99 was **2.037**, **2.172**, and **2.166 ms**, with median throughput of **1,364**, **1,362**, and **1,348 GPU-completed FPS**, respectively. Reference pixels and GPU sequences matched. [Nearby budget comparisons](results/winemetalgl-nearby-triangle-ab.json)

The two sustained pairs started after at least five seconds of completed GPU rendering and reversed run order. At 65,000, the runs reached **1,343 FPS and 2.148 ms p99**, then **1,359 FPS and 2.173 ms p99**. At 62,500, the corresponding results were **1,320 FPS and 2.226 ms p99**, then **1,358 FPS and 2.119 ms p99**. The pairs split the p99 wins and favored 65,000 on throughput. The 65,000 setting is the comparison baseline for the trial. [Warmed comparisons](results/winemetalgl-nearby-triangle-ab.json)

Separate submission traces verified **466 + 360** draws per frame at 65,000 and **448 + 378** at 62,500. The union of the render buffers' GPU execution intervals reached **2.111 ms p99** and **2.492 ms p99**, respectively, in those captures. All 336 renderer tests passed. [Submission traces](results/winemetalgl-nearby-triangle-ab.json)

### Ten-minute windowed replay

At 65,000 triangles, the warmed windowed i386 replay averaged **1,351.95 GPU-completed FPS** over exactly 600 seconds. It recorded **811,173** consecutive GPU completions including the start marker, with 826 draws per output. The p99 completion gap was **2.055 ms**, about 55 µs above the 2.0 ms target. Minute-level p99 ranged from **2.002 to 2.112 ms**, and minute-level throughput ranged from **1,344.07 to 1,361.70 FPS**. [GPU completion measurements](results/winemetalgl-triangle-budget-soak.json)

All ten post-start memory samples read **106.5 MiB** of process RSS. The ending framebuffer matched the reference checksum and all 71 harness checks passed. To repeat the run, set `OCERZ_WMGL_I386_SECONDS=612` with the same windowed flags, select the first GPU completion at least five seconds after the first recorded completion, and analyze with `--seconds 600 --segment-seconds 60`. The live raid acceptance runs and input checks are pending. [Replay and memory measurements](results/winemetalgl-triangle-budget-soak.json)

### Mailbox capacity after direct publication

Four interleaved six-second pairs compared three and five mailbox slots with direct color publication, the 65,000-triangle budget, and three guest frames in flight. Median throughput rose from **1,341 to 1,397 GPU-completed FPS**, and median p99 rose from **2.081 to 2.275 ms**. Five slots increased throughput in every pair. P99 improved in two pairs and worsened in two. [Short comparisons](results/winemetalgl-direct-color-mailbox-capacity-ab.json)

In the two warmed 60-second pairs, throughput moved from **1,366 to 1,414 FPS** and from **1,342 to 1,410 FPS**. P99 moved from **2.065 to 2.056 ms** and from **2.108 to 2.050 ms**. Every window kept consecutive GPU sequences and matched the reference pixels. The trial keeps three slots for live validation. [Warmed comparisons](results/winemetalgl-direct-color-mailbox-capacity-ab.json)

Separate ten-second traces put slot acquisition p99 at **1.241 ms** with three slots and **0.5 µs** with five. Both traces kept the **466 + 360** draw split. Changes in GPU execution time and final-buffer dispatch delay were the largest contributors to **169 of 201** gaps over 2 ms at three slots and **137 of 154** at five. The two render buffers' combined GPU execution intervals reached **2.77 ms p99** and **2.75 ms p99**, respectively. [Timing traces](results/winemetalgl-direct-color-mailbox-capacity-ab.json)

Both libraries passed all 336 renderer tests and the published-color preservation checks. To reproduce the comparison, build a second library with `Surface::Mailbox::kSlots = 5` and value-initialize the `state` array to `Free`. Alternate the library passed to `OCERZ_WMGL_LIBRARY` while keeping the replay flags fixed. Use a fresh GPU log and start each measured window after at least five seconds of GPU completions.

### Captured raid draw order

The raid frame that supplied the triangle histogram contains **826 draws** and **111,230 triangles**. The ordered fixture retains each draw's triangle count in the captured call order. The generator still uses synthetic indexed triangle lists and shaders, with the same vertex upload schedule and window size. The translated replay checks each order against its own native probe framebuffer checksum.

At a 512-draw cut, the first buffer contains **94,081 triangles** in the captured order and **71,516** in the shuffled replay. Both triangle budgets trigger an earlier split and leave enough draws for the 512-draw cap to trigger a third buffer. Separate ten-second GPU traces verified each split. [Captured triangle order](fixtures/raid-triangle-order.tsv) · [Submission traces](results/winemetalgl-captured-draw-order-ab.json)

| Triangle budget | Draws per command buffer | Triangles per command buffer | GPU execution p99 per frame |
| --- | ---: | ---: | ---: |
| Draw limit alone | 512 + 314 | 94,081 + 17,149 | 0.487 ms |
| 56,000 | 202 + 512 + 112 | 56,261 + 50,810 + 4,159 | 0.629 ms |
| 65,000 | 225 + 512 + 89 | 65,195 + 44,809 + 1,226 | 0.630 ms |

To reproduce the ordered replay, rebuild the native probe and the AArchX test binary with the updated headers and pass `benchmarks/fixtures/raid-triangle-order.tsv` through `OCERZ_WMGL_TRIANGLE_HISTOGRAM` or as the native probe's first argument. Its `# ordered` header selects captured order. The two columns give a triangle count and its number of consecutive occurrences.

The unchanged histogram control reached only **321 FPS** at the outset of the first sweep. The repeated sweep used a histogram control at both ends, which reached **1,356** and **1,335 FPS**. In three interleaved six-second blocks of the repeat, the draw limit alone, 56,000 triangles, and 65,000 triangles reached median p99 values of **2.058**, **2.363**, and **2.352 ms**. [Sweep and controls](results/winemetalgl-captured-draw-order-ab.json)

The warmed 60-second pairs split the wins. The draw limit alone reached **1,443 FPS and 0.766 ms p99**, then **1,486 FPS and 0.740 ms p99**. At 65,000 triangles the paired runs reached **1,463 FPS and 0.751 ms p99**, then **1,455 FPS and 0.754 ms p99**. Every run matched its native reference pixels and logged consecutive GPU completions. The trial retains 65,000 for live validation. [Warmed comparisons](results/winemetalgl-captured-draw-order-ab.json)

The three diagnostic traces matched 3,601 display callbacks with zero confirmed presentations. The desktop session was unlocked before the benchmark and locked when checked after capture. These measurements describe GPU completions in the replay. [Presentation diagnostics](results/winemetalgl-captured-draw-order-ab.json)

### Draw cap across the captured raid intervals

With the triangle budget fixed at 65,000, the continuing-pass model checked `WINEMETALGL_COMMIT_DRAWS` settings from 64 through 81 across **114** complete raid swap intervals. Setting 81 raises the draw cap from 512 to 648 and is the first tested setting that fits every interval into two command buffers. Counting triangle strips toward the budget and counting only direct triangle lists gave the same result at 64, 80, and 81. [Draw cap model](results/winemetalgl-draw-cap-model.json)

| Draw setting | Draw cap | Intervals with two buffers | Intervals with three buffers |
| --- | ---: | ---: | ---: |
| 64 | 512 | 0 | 114 |
| 72 | 576 | 25 | 89 |
| 80 | 640 | 111 | 3 |
| 81 | 648 | 114 | 0 |

The model reproduces the ordered replay's measured **225 + 512 + 89** split at 64 and predicts **225 + 601** at 81. It treats each swap interval as a continuing pass with one backend draw per logged call. Live passes can end on framebuffer changes, synchronization, or other state transitions. Setting 81 is a candidate for a paired capture at the same triangle budget. [Captured replay traces](results/winemetalgl-captured-draw-order-ab.json)

```sh
python3 benchmarks/model-raid-submissions.py CALL_LOG --example-frame 100 \
    --draw-settings $(seq 64 81)
```

### Foreground raid draw-cap comparison

These runs and their profiles had bridge-crossing diagnostics active. The launcher passed an empty `OCERZ_M32_CALLLOG`, which matched every native crossing. The captures still verify live input and GPU sequence continuity. The timing comparison describes the instrumented client. [Capture conditions](results/raid-winemetalgl-draw-cap-live.json)

The original client ran the full 24-caster fixture at 1365 × 768 with VSync off and the 65,000-triangle budget. Both runs used Macsmoke's saved Northshire camera. The boss, casters, overlapping ground effects, and particles were visible, and the world server kept the same cast schedule between runs. Each warmed 60-second window logged consecutive GPU completions. [Live measurements](results/raid-winemetalgl-draw-cap-live.json)

| Draw setting | GPU-completed FPS | p99 completion gap |
| --- | ---: | ---: |
| 64 | 64.55 | 21.67 ms |
| 81 | 65.72 | 21.79 ms |

A separate Metal System Trace at 64 tagged 4,200 active GPU intervals to the client. Their union occupied **2.53 of 7.57 seconds**, or **33.4%** of the span. In a separate CPU sample at 64, WineMetalGL was on **747 of 3,317** main-thread stacks. Fixed-function uniform packing accounted for **181** of those stacks. The trial keeps setting 64 pending further profiling. [CPU and GPU profiles](results/raid-winemetalgl-draw-cap-live.json)

### Reuse the fixed-function uniform buffer

The clean control omits `OCERZ_M32_CALLLOG` entirely. At draw setting 64 its two warmed live raid windows averaged **86.92** and **86.93 GPU-completed FPS**, with p99 gaps of **15.74** and **15.92 ms**. A separate clean Metal trace attributed **37.8%** of the sampled span to client GPU work. CPU sampling put fixed-function uniform packing on **288 of 2,959** main-thread stacks. [Clean control and profiles](results/raid-winemetalgl-fixed-uniform-tail-ab.json)

The trial writes the fixed-function values directly into the existing per-context buffer after the aligned matrix prefix. It reuses the buffer's capacity on subsequent updates and leaves the shader layout intact. The two warmed windows with reuse, collected in one launch between the two control launches, averaged **88.00** and **88.77 GPU-completed FPS** at **15.45** and **15.21 ms p99**. Median throughput rose **1.7%** and median p99 fell **0.50 ms**. All four windows had consecutive GPU completions. [Live comparison](results/raid-winemetalgl-fixed-uniform-tail-ab.json)

The sampled share of uniform-packing stacks fell from **9.7% to 7.6%**. Both libraries passed all 336 renderer tests, including compatibility lighting, texture generation, and matrix-only updates. The fixed camera, casters, overlapping ground effects, and particles were visible in both versions. Apply `patches/winemetalgl-fixed-uniform-tail-trial.patch` after the existing WineMetalGL trial patches and rebuild the library. Keep draw setting 64 and the triangle budget at 65,000 for the comparison. [Validation and samples](results/raid-winemetalgl-fixed-uniform-tail-ab.json)

### Warmed guest CPU profile on Metal

With uniform storage reuse and bridge logging disabled, five warmed eight-second intervals on the busiest guest thread attributed **59.5–60.6%** of sampled CPU time to executing translated code. The profiler sampled **7.86–7.89 seconds** of CPU time per interval. Each interval spent less than one millisecond compiling new blocks and reported zero retirements. Precompiling the same blocks can save only the observed translation cost. [Guest CPU samples](results/raid-winemetalgl-guest-profile.json)

Use `OCERZ_GUESTPROF=2000`, `OCERZ_GUESTPROF_PERIOD=8`, and `OCERZ_GUESTPROF_HOT=1` in a separate profiling launch. In the earlier instrumented sessions, `glTestObjectAPPLE` accounted for **48.7–51.8%** of crossing messages. Those counts include startup and describe call frequency in the instrumented sessions. [Profiling conditions and crossing counts](results/raid-winemetalgl-guest-profile.json)

#### i386 scalar memory offsets

The most sampled translated instruction block in the five warmed intervals accounted for **1.6–2.1%** of the thread's sampled CPU time. It starts with repeated scalar SSE loads and stores. The compiler trial formed the wrapped 32-bit guest offset and used it directly in an Arm SIMD load or store. All 34 differential cases matched the interpreter in both trial arms under default and ordered vector memory settings. [Scalar address experiment](results/raid-i386-scalar-offset.json)

In the calibrated headless benchmark, folding both addresses emitted **192 fewer bytes** and increased median wall time from **3.323 to 3.390 ms** for six million copies. Folding loads alone reached **3.330 ms**, and folding stores alone reached **3.315 ms**. These measurements cover a synthetic i386 copy loop. [Interleaved timings](results/raid-i386-scalar-offset.json)

### Direct calls for byte-valued GL queries

The i386 fast path can zero-extend an unsigned byte return into EAX and call the existing renderer entry point. `glTestObjectAPPLE` still checks buffer readiness in WineMetalGL. Separate CPU samples found **17** `glTestObjectAPPLE` stacks through the generic crossing in the control and **21** through the fast path in the trial. Generic crossing stacks below the guest callback fell from **78 of 2,630** main-thread samples to **33 of 2,464**. [CPU samples](results/raid-winemetalgl-byte-leaf-ab.json)

Two warmed 60-second windows per launch used the same executable and uniform-reuse library in the fixed 24-caster raid. The control averaged **90.32** and **90.58 GPU-completed FPS**. The fast path reached **92.42** and **91.82 FPS**. Median p99 fell from **15.53 to 15.10 ms**, and all four windows kept consecutive GPU completions. The actual client showed the same camera, casters, and overlapping effects in both launches. [Live measurements](results/raid-winemetalgl-byte-leaf-ab.json)

Apply `patches/aarchx-wmgl-byte-leaf-trial.patch` after the existing AArchX trial patches and rebuild AArchX. Set `OCERZ_M32_GL_BYTE_LEAF=1` for the trial and `0` for the control. With the same flag, run the AArchX `test_m32 wmgl-storage` target using the trial library and `OCERZ_WMGL_TEST_BYTE_RETURN=1`. Both variants passed all 109 checks, including true and false results and the invalid-buffer GL error. For the focused test, `OCERZ_M32_CALLLOG=glTestObjectAPPLE` recorded two generic crossings in the control and zero in the trial. Omit the filter for live measurements. [ABI verification](results/raid-winemetalgl-byte-leaf-ab.json)

### Direct call for bounded case-insensitive string comparisons

The earlier instrumented client session logged **4.53 million** generic crossings for `strncasecmp`. The opt-in leaf calls the same macOS libc function after resolving both guest pointers and widening the unsigned length. The focused test ran six comparisons through a translated i386 import stub. Both arms preserved the signed results and length bounds. The trace showed six generic crossings in the control and zero with the leaf enabled. Each arm also passed all **806** general m32 checks. [ABI checks and previous crossing count](results/aarchx-i386-strncasecmp-leaf.json)

In a headless benchmark with four interleaved launches per arm and 200,000 translated calls per timed sample, the median moved from **78.31 to 25.59 ns per comparison**. Apply `patches/aarchx-m32-strncasecmp-leaf-trial.patch` after the byte-leaf trial. Run `test_m32 strncasecmp-leaf` with `OCERZ_M32_STRNCASECMP_LEAF=1` and `OCERZ_BENCH_M32_STRNCASECMP=1`, then repeat with the leaf set to `0`. Use `OCERZ_TCACHE=off` and omit the call log while timing. The live A/B is pending. [Headless timings](results/aarchx-i386-strncasecmp-leaf.json)

### GPU work per completed raid render

The clean live control trace from the uniform-storage A/B included **604** outputs that matched the WineMetalGL GPU completion log within **42 ns** after aligning the clocks. The full 1365 × 768 fixture used the same 24 casters and draw setting 64. Five outputs in the matched sequence range lacked the required final fragment interval in the trace. The per-output statistics cover the matched set. [Matched Metal intervals](results/raid-winemetalgl-gpu-work.json)

| Metal work | Mean per output | Median per output | p99 per output |
| --- | ---: | ---: | ---: |
| Vertex | 3.15 ms | 2.67 ms | 8.05 ms |
| Fragment | 1.48 ms | 1.27 ms | 4.59 ms |
| Union through render completion | 4.23 ms | 3.89 ms | 9.47 ms |

The first two render buffers account for **97.4%** of the vertex activity. Each stage uses the union of its active intervals within the matched output. The union through render completion stops at the publication marker, and stages can overlap. At this observed GPU cost, removing CPU stalls alone would still leave the vertex stage averaging more than the full **2 ms** frame budget. Shader and draw attribution requires a separate capture. [Per-stage and per-buffer results](results/raid-winemetalgl-gpu-work.json)

To reproduce the join, use a Metal System Trace plus the GPU completion log from the same process. Identify the final render buffer and one output shared by both records. In this trace, the final fragment interval in `Command Buffer 2` for trace frame 2 corresponds to GPU completion 16901. The analyzer checks the remaining render timestamps against that offset.

```sh
python3 benchmarks/trace-frames.py TRACE PID 1365 768 \
    --wmgl-gpu-work GPU_LOG --output-buffer 'Command Buffer 2' \
    --gpu-clock-anchor 2 16901
```

### Label ARB stages in Metal pass profiles

Apply `patches/winemetalgl-arb-pass-labels-trial.patch` to the WineMetalGL trial and enable `WINEMETALGL_PASS_PROFILE=1` with `WINEMETALGL_LOG=0,perf:info` for a diagnostic capture. The pass report includes the bound ARB program name and load serial for each active stage, and marks a fixed-function stage as `fixed`. Each pass lists up to four distinct shader combinations, as in the existing pass profiler. Assess timed throughput separately with pass profiling off.

The 1365 × 768 indexed stand-alone test exercised 4,500 measured frames per mode and passed all **187**, **202**, and **217** bridge checks for the fixed-function, mixed, and two-stage ARB modes. The representative framebuffer checksum was `4c7788a9d3ba3e94` in each mode. The corresponding main pass labels were `fixed function`, `vertex=1@1 fragment=fixed`, and `vertex=1@1 fragment=2@2`. Live shader attribution is pending.

### GPU work with the raid in the background

The hidden client continued rendering the full 1365 × 768 scene at **29.60 GPU-completed FPS** under the background cap. The local realm reported the saved raid position, all 25 actors, and the exact 26 fixture scripts. A Metal trace matched **309 consecutive outputs** within **42 ns**. In this capture three game render buffers appeared before the matching completion. The vertex channel averaged **5.29 ms per output** and reached **11.08 ms p99**. The union through render completion averaged **6.90 ms**. [Background trace summary](results/raid-winemetalgl-background-gpu-work.json)

In a separate six-second CPU profile, **151 of 1,673** main-thread stacks included fixed-function uniform packing, and **465** included the indexed draw entry point. The background capture used a later runtime and a capped client. An A/B for window focus would use the same runtime and cap in both arms. [Background CPU sample summary](results/raid-winemetalgl-background-gpu-work.json)

### Keep packed uniforms when selecting a matrix stack

The background CPU sample also included `glMatrixMode`. The setter used to advance the fixed-function uniform version each time it selected a stack. The trial updates the selector directly. Commands that change matrix contents still advance the matrix version and refresh the packed values.

The focused offscreen replay uses the captured draw order at 1365 × 768. It selects a stack for each of the **826** draws and updates the modelview and projection once per output. Across four interleaved eight-second launches per arm, the median rose from **453.06 to 857.25 outputs per second**. Every launch produced the same framebuffer checksum, and both libraries passed all **336** GPU tests. The benchmark deliberately selects a stack on every draw. The frequency of selector calls and the throughput effect in the live client are pending. [Replay A/B and checks](results/winemetalgl-matrix-mode-uniforms-ab.json)

Apply `patches/winemetalgl-matrix-mode-uniforms-trial.patch` after the uniform-storage trial and rebuild the renderer. Build `benchmarks/wmgl-indexed-geometry.c` against it and set `OCERZ_WMGL_PROBE_MATRIX_MODE=1` with `benchmarks/fixtures/raid-triangle-order.tsv` as the first argument. Use draw setting 64 and a triangle budget of 65,000 in both arms. The probe includes 12 warmup frames in its output count.

### Reuse directional light half vectors in the vertex shader

A hidden live trace with Metal GPU Counters and Metal Performance Overview listed **49 vertex** and **37 fragment** shader records for the client. It contained zero shader execution samples, so the records give an inventory of registered shaders. [Trace coverage](results/winemetalgl-directional-half-vector-ab.json)

The fixed-function uniform block already supplies a directional light's half vector for the infinite-viewer model. The trial reads that value in the generated vertex shader. The offscreen A/B used four directional lights, variable per-draw normals, the captured 826-draw triangle order, and a 1365 × 768 surface. Four interleaved five-second windows per arm moved the median from **909.5 to 934.0 GPU-completed FPS** and p99 from **5.06 to 4.86 ms**. The sequence stayed consecutive in every window, all framebuffer checksums matched, and both arms passed **336** GPU tests. [Offscreen timings and checks](results/winemetalgl-directional-half-vector-ab.json)

Separate instrumented runs moved the median of the mean render-encoder durations from **54.78 to 50.69 µs**. These timings span vertex and fragment work. The synthetic scene isolates a directional-light workload. [Encoder timing](results/winemetalgl-directional-half-vector-ab.json)

Apply `patches/winemetalgl-directional-half-vector-trial.patch` to the WineMetalGL trial and rebuild. Run the indexed probe with `OCERZ_WMGL_PROBE_DIRECTIONAL_LIGHTS=1`, `WINEMETALGL_COMMIT_DRAWS=64`, and `OCERZ_WMGL_COMMIT_TRIANGLES=65000`. Supply `benchmarks/fixtures/raid-triangle-order.tsv` and a fresh `OCERZ_WMGL_GPU_LOG` for each launch.

### Launch the original client with an offscreen drawable

The headless trial gives the client's Carbon drawable a Metal pbuffer at the requested content dimensions. It suppresses trial window display and foreground activation. The normal launcher stays on its regular window path unless `OCERZ_M32_HEADLESS_TRIAL=1` is set.

An isolated copy of the original client started with VSync and both FPS limits set to zero. The 22-second workspace monitor saw the trial process with zero activation notifications and zero visible trial windows. The readback showed the login screen at **1365 × 768**. A warmed ten-second window at that size averaged **808.5 GPU-completed FPS** at **4.68 ms p99** with consecutive completion sequences. The bridged storage and general runtime checks passed. Background sign-in and the 24-caster scene remain to be verified on this path. [Headless startup and GPU checks](results/raid-winemetalgl-headless-login.json)

Apply `patches/aarchx-wmgl-headless-drawable-trial.patch` after the Carbon window sync and hide trials. Use a copy of the client and a separate signed launcher bundle with `CFBundleIdentifier=org.local.wrath-trial.headless` and `LSUIElement=true`. Set `maxFPS`, `maxFPSBk`, and `gxVSync` to zero in the copied client's `WTF/Config.wtf`. Start the monitor before launching the trial with `open -g -j -n`, the regular trial paths, `OCERZ_M32_HEADLESS_TRIAL=1`, and fresh `OCERZ_WMGL_GPU_LOG` and `OCERZ_WMGL_READBACK` paths. The monitor observes only the test bundle. The GPU completion log is compatible with the existing `trace-frames.py --wmgl-log` analyzer.

```sh
swift benchmarks/wmgl-headless-window-check.swift
```

### Report the emulated x86 feature bits to the client

A startup trace found that the original client probes `hw.optional.mmx`, `hw.optional.sse`, and `hw.optional.sse2` through the i386 libc bridge. The native ARM system call returned an error for all three. The opt-in trial answers supported 32-bit x86 feature queries from AArchX's existing emulated feature table. Other names use the standard bridge call.

The same headless executable was launched with the feature switch set to `0` and `1`. The control readback showed the SSE startup warning over the login fields. The trial readback showed the login fields directly. Both captures kept 1365 × 768, consecutive GPU completions, and zero observed trial windows or activations. The ten-second GPU windows documented startup rendering while the two captures showed different UI. All 806 general runtime checks passed. [Readback checks and CPU query evidence](results/winemetalgl-headless-x86-features.json)

Build with `patches/aarchx-m32-x86-cpu-sysctl-trial.patch` and set `OCERZ_M32_X86_CPU_FEATURES=1` for the trial. Run `test_m32 x86-cpu-features` with the flag set to zero and one to verify ordinary lookup, the reported feature values, and output-buffer sizing.

### Enter the local realm in the headless client

The opt-in realm trial posts Carbon events inside the isolated process. The text event carries the original key event as `typeEventRef` and sends the complete field as Unicode. App-local Tab transfers input to the password field. A synthetic-credential run confirmed the account field and password mask. The headless runtime exits before opening a native Carbon alert.

Apply `patches/aarchx-wmgl-headless-realm-trial.patch` after the headless drawable and emulated CPU feature patches. Place the test launcher bundle beside `Data` in the copied client. Check both realm addresses point to `127.0.0.1`, and launch with `open -g -j -n --stdin` pointing at the existing private fixture login file. The file must be owned by the launching user with no group or other permissions. It supplies `Username:` and `Password:` fields for the local fixture. Set `OCERZ_HEADLESS_REALM_LOGIN` to enable the login sequence.

Set `OCERZ_HEADLESS_CHARACTER_READBACK` to a fresh capture path. Create the file at `OCERZ_HEADLESS_CHARACTER_REQUEST` after the local realm confirms authentication or the trial connects to its world socket. Verify the fixture character in that readback before creating the file at `OCERZ_HEADLESS_ENTER_REQUEST`. Use `OCERZ_HEADLESS_WORLD_READBACK` for the in-world frame. Pass the expected total run time in seconds to `wmgl-headless-window-check.swift` to watch the entire capture.

### First full-raid A/B with the headless client

One warmed 60-second run per arm rendered the full 24-caster fixture at 1365 × 768. The two launches used the same client and fixed camera, both FPS limits set to zero, VSync off, and separate WineMetalGL libraries that differed by the matrix-stack change. The realm confirmed local authentication, all 25 actors, and the exact 26 fixture scripts in both launches. The two world readbacks showed the same camera, actors, and overlapping spell effects.

| Arm | GPU-completed FPS | p99 completion gap |
| --- | ---: | ---: |
| Control | 94.37 | 16.31 ms |
| Matrix-stack trial | 93.80 | 15.75 ms |

Both GPU sequences were consecutive. The desktop monitor saw zero visible trial windows and zero activations in both runs. This pair measured a smaller p99 gap with the patch and slightly lower average throughput. [Full-raid pair and verification](results/raid-winemetalgl-headless-matrix-mode-ab.json)

### Directional half vectors in the live headless raid

Four warmed 60-second runs used control, trial, trial, and control order in the original client. The full 24-caster workload rendered at 1365 × 768 with VSync and both frame caps disabled. The renderer mapping and all 26 fixture timing rows were checked for every launch. The four captures showed the same camera and overlapping spell effects. GPU sequences were consecutive, and the monitor recorded zero visible trial windows or activations.

| Arm | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| Control | 94.87 | 15.69 ms |
| Directional half-vector trial | 94.89 | 15.95 ms |

Throughput differed by 0.03%, and p99 rose by 0.26 ms. The live benchmark retains the control renderer. [Full-raid A/B](results/raid-winemetalgl-headless-directional-half-vector-ab.json)

### Vertex path and guest CPU profile in the full raid

A separate instrumented headless run counted **2.98 million** direct draws in ten complete four-second intervals. Programmable vertex stages accounted for **91.26% of draws** and **92.08% of submitted vertex references**. The remaining classified draws used unlit fixed-function vertices. The directional-light path recorded zero draws. Vertex references count submitted indices or array vertices times instance count. Hardware invocations also depend on reuse and the vertex cache. [Vertex path profile](results/raid-winemetalgl-headless-vertex-path-profile.json)

Across six warmed eight-second CPU reports, translated code accounted for **55.2–58.6%** on the sampled guest thread. Each report covered about 7.88 seconds of that thread’s CPU time. Import tracing resolved the largest runtime bucket to `_RunApplicationEventLoop`. Its callback stack can include game, bridge, and renderer work. [Guest CPU attribution](results/raid-winemetalgl-headless-vertex-path-profile.json)

Apply `patches/winemetalgl-raid-lighting-profile-trial.patch` after the ARB pass-label patch. Set `WINEMETALGL_PROFILE_RAID_LIGHTING=1` and `WINEMETALGL_LOG=0,perf:info` for the diagnostic capture. For the guest CPU report, set `OCERZ_GUESTPROF=2000`, `OCERZ_GUESTPROF_PERIOD=8`, `OCERZ_GUESTPROF_HOT=1`, and `OCERZ_M32LOG=imports`. Collect throughput separately with these diagnostics disabled.

### ARB vertex programs and Metal activity in the headless raid

A separate 36-second draw sample ranked **24** loaded ARB vertex programs by submitted vertex references. The largest program accounted for **34.72%** of ARB vertex references with **65 assembly instructions** and **223 declared parameters**. The next largest accounted for **11.43%**, with **40 instructions** and **223 parameters**. Program resource counts describe the ARB assembly before Metal compilation. [Program metadata](results/raid-winemetalgl-headless-arb-program-profile.json)

In the Metal trace from the same diagnostic launch, all **735** in-range GPU completions matched Metal fragment endpoints within **43 ns** after clock alignment. Headless trace frame tags describe individual command buffers. The analysis clips active intervals to consecutive output completion boundaries and measures their union per GPU channel. Across **734** consecutive gaps, vertex activity averaged **2.98 ms**, fragment activity **1.37 ms**, and the combined union **4.30 ms**. GPU work can overlap output boundaries. [Headless Metal trace summary](results/raid-winemetalgl-headless-arb-program-profile.json)

Apply `patches/winemetalgl-raid-arb-program-profile-trial.patch` after the vertex-path profile patch and use the same diagnostics switch. The report records program identifiers and resource counts.

### Read contiguous uniforms directly in mixed ARB parameter arrays

The most heavily used ARB vertex shapes declared mixed arrays of **223** values with contiguous uniform ranges of **221 or 222** entries. The original translation creates a local GLSL array for each mixed declaration. Relative addressing in the trial clamps the index as before, reads the contiguous segment from the uniform array, and selects the remaining expressions individually. Constant indices reference their original expressions directly.

The new GPU regression draws from a literal, both ends of the uniform range, and the final literal. It also checks the lower and upper clamp boundaries. Both settings passed **337** GPU tests. Apply `patches/winemetalgl-arb-uniform-segments-trial.patch` and set `WINEMETALGL_ARB_UNIFORM_SEGMENTS=1` to use the trial. For live A/B, use the same renderer build, set the flag to zero for control, and disable shader and pipeline caches in both arms.

In four warmed 60-second headless launches, ordered control, trial, trial, control, median throughput rose from **92.15 to 96.09 GPU-completed FPS**. Median p99 fell from **17.17 to 15.88 ms**. Both pairs improved on both metrics. Each run kept consecutive GPU completions, the same camera and fixture schedule, and zero visible trial windows or activations. We inspected all four in-world readbacks for the 1365 × 768 view and overlapping spell effects. [Full-raid A/B and GPU tests](results/raid-winemetalgl-headless-arb-uniform-segments-ab.json)

Two separate Metal System Traces measured the GPU work behind that change. The control matched **765** completions and the trial matched **811**, with maximum alignment error below **43 ns**. Mean vertex activity per completion interval fell from **3.59 to 0.75 ms**. Mean fragment activity went from **1.70 to 1.50 ms**, and the combined active union fell from **5.27 to 2.27 ms**. These are active GPU intervals clipped to consecutive output completion boundaries. GPU execution can overlap submissions. [Metal trace comparison](results/raid-winemetalgl-headless-arb-uniform-segments-gpu-trace.json)

### Instruction-level guest profile with direct ARB uniform reads

A separate live profiling launch used direct ARB uniform reads. Across seven warmed eight-second CPU periods, generated code mapped to `MOV` accounted for **24.23% of the mapped translated samples**, and code mapped to `Jcc` accounted for **19.06%**. Their shares of all samples were **14.17%** and **11.15%**. The profiler uses the JIT’s host-to-guest fault map. A guest instruction’s assigned weight includes generated address work and exits associated with that instruction. [Instruction samples](results/raid-aarchx-headless-guest-instruction-profile.json)

Apply `patches/aarchx-m32-guest-instruction-profile-trial.patch` and add `OCERZ_GUESTPROF_INSTRUCTIONS=1` to the existing guest profiling flags. The report switches the translated address list to instruction PCs and adds opcode weights. All 806 general runtime checks passed.

### Use CCMP for fused scalar floating-point equality branches

One hot guest site compares floats and branches first on parity, then on equality. A trial that tried to carry the host flags from that specific parity branch emitted zero matching sites in the live client. The broader trial normalizes the `FCMP` result with `CSET` and `CCMP` before branching. It preserves x86 equality on the unordered path. An instrumented launch recorded **147 distinct emitted sites**.

Both settings passed eight focused i386 interpreter comparisons in separate processes. A native AArch64 check passed **81** combinations covering signed zeros, finite values, infinities, and NaNs. The existing full `test_jit32` suite still encounters failures after reusing guest code addresses on this host. The same failure pattern reproduced in the archived earlier build.

Four warmed 60-second headless launches with direct ARB uniform reads enabled in both arms had the same median throughput of **94.57 FPS**. Median p99 moved from **14.90 to 14.43 ms**. All four sequences were consecutive, the camera and cast schedule matched, and no trial window appeared or acquired focus. The change remains opt-in. [Live A/B and checks](results/raid-aarchx-headless-comis-ccmp-ab.json)

Apply `patches/aarchx-m32-comis-equality-ccmp-trial.patch` and set `OCERZ_M32_COMIS_EQ_CCMP=1` to test the trial. Leave the switch unset for control. Run `test_jit32` with each case number from `0` through `7` in its own process. `OCERZ_M32_COMIS_EQ_CCMP_LOG` enables the compile witness in a separate run.

### MOV operand forms in the full raid

Seven warmed eight-second periods mapped all sampled `MOV` weight to an operand form. The largest forms were 32-bit loads through `EBP` and 32-bit loads through another base register with a displacement. They accounted for **32.93%** and **22.30%** of mapped `MOV` time and **7.53%** together across all CPU samples. [Operand profile](results/raid-aarchx-headless-mov-form-profile.json)

Apply `patches/aarchx-m32-mov-form-profile-trial.patch` after the guest instruction profile patch. Use the same profiling flags and disable the translation cache so the compact instruction records contain operand metadata. All **806** general runtime checks passed.

### Fold the host mapping base into 32-bit MOV loads

The trial forms the wrapped guest offset in a W register and adds the host mapping base in the AArch64 word load’s register-offset addressing mode. It applies after the existing fast paths, to plain-order loads with a pinned base and destination, no index or segment override, and a displacement that fits an immediate. A diagnostic raid launch recorded **27,675 distinct emitted sites**.

Six isolated interpreter comparisons passed per arm. The negative-displacement and wraparound cases emitted the trial load. Apply `patches/aarchx-m32-mov-base-uxtw-trial.patch` and set `OCERZ_M32_MOV_BASE_UXTW=1` for the trial. Leave the switch unset for control. Run `test_jit32` with each case number from `0` through `5` in a separate process. The compile witness uses `OCERZ_M32_MOV_BASE_UXTW_LOG` in a separate diagnostic launch.

Four warmed 60-second headless runs moved median throughput from **94.47 to 97.93 GPU-completed FPS**. Median p99 rose from **14.21 to 15.33 ms**. Both pairs showed the same throughput and tail-latency tradeoff. The camera, spell effects, and fixture timing were checked in each launch. GPU sequences were consecutive and the trials recorded zero visible windows or activations. The change stays opt-in. [Live raid A/B and input cases](results/raid-aarchx-headless-mov-base-uxtw-ab.json)

### Native ARM64 instructions behind the JIT samples

Seven warmed eight-second periods recorded sampled host instruction words for translated `MOV`, `MOVSS`, and `Jcc` in the headless raid. For samples attributed to guest `MOV`, the largest native groups were `ADD` at **51.38%** and `SUB` at **22.38%**. For guest `Jcc`, the largest groups were `STP` at **34.55%** and `LDR` at **28.30%**, followed by unconditional `B` at **12.90%**. The sampled host instructions include the address work and exit code associated with the guest instruction. The GPU sequence was continuous, and all 806 general runtime checks passed. [Native instruction profile](results/raid-aarchx-headless-arm64-instruction-profile.json)

Apply `patches/aarchx-m32-arm64-instruction-profile-trial.patch` after the MOV form profile patch and add `OCERZ_GUESTPROF_HOST_WORDS=1` to the profiling flags. Run `python3 benchmarks/summarize-aarchx-host-profile.py RUN_DIR` against the diagnostic directory on macOS with Xcode Command Line Tools. It assembles the sampled words into a temporary object and uses the local disassembler for the instruction names.

All sampled `STP` instructions attributed to guest `Jcc` wrote at byte offset 152 from the CPU state pointer. That pair stores `cc_src` and `cc_dst`, the operands of the deferred x86 flags. Of the `LDR` samples attributed to `Jcc`, **57.13%** read the saved floating-point compare operand and **31.83%** read `cc_op`, the descriptor of the deferred flags. The CPU field offsets were checked with `offsetof` in the same source. [Decoded CPU offsets](results/raid-aarchx-headless-arm64-instruction-profile.json)

### Triangle submission budget with direct ARB uniform reads

Four warmed 60-second headless runs alternated the triangle budget in 65,000, 0, 0, and 65,000 order. All runs used direct ARB uniform reads, the same 1365 × 768 viewport, VSync off, and draw setting 64. Setting `OCERZ_WMGL_COMMIT_TRIANGLES=0` disables the triangle threshold while retaining the 512-draw threshold.

| Triangle budget | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| 65,000 | 97.94 | 14.52 ms |
| Disabled | 99.23 | 13.14 ms |

Both pairs had higher throughput and a shorter p99 gap with the triangle threshold disabled. The second pair differed by 0.27 FPS. The realm verified all 25 actors and 26 timed script rows in every launch. All four GPU sequences were consecutive, and all four readbacks showed the saved camera and overlapping spell effects. The desktop monitor recorded zero activations or visible trial windows. [Full-raid A/B](results/raid-winemetalgl-headless-triangle-budget-ab.json)

Two separate 16-second diagnostic runs joined each completed output to its final render command buffer by GPU end timestamp. With the 65,000-triangle threshold, **1,556 of 1,563** consecutive output intervals used three draw-bearing command buffers. With the threshold disabled, **all 1,559** used two, starting with a 512-draw submission. Set `OCERZ_WMGL_SUBMISSION_LOG` to a fresh path for this capture. Count draw-bearing buffers in serial order on the matched rendering stream. The trace describes how work was submitted. The four minute-long measurements above give the throughput comparison. [Metal submission counts](results/raid-winemetalgl-headless-triangle-submissions.json)

### Repeated loads through one i386 register

A separate run classified plain 32-bit `MOV` loads with one base register and displacements from −256 to 255. The profiler counts matching instructions in each decoded block through the first instruction that could change the base. It then weights each group by the CPU samples attributed to those loads. Across seven warmed eight-second periods, groups of at least two loads accounted for **20.70%** of mapped `MOV` time and **2.85%** of all sampled CPU time. Groups of at least three accounted for **14.91%** and **2.05%**, respectively.

These percentages measure coverage for an address-reuse trial. A working optimization would save only a portion of this time and would need to preserve the i386 address wraparound. The diagnostic launch recorded consecutive GPU completions and zero visible trial windows or activations. All **806** general runtime checks passed. [Static group counts and CPU weights](results/raid-aarchx-headless-same-base-load-profile.json)

Apply `patches/aarchx-m32-same-base-load-profile-trial.patch` after the MOV form profile patch. Use the same profiling flags and add `OCERZ_GUESTPROF_BASE_GROUPS=1`. The log adds `same_base_loads` rows with sample weights in microseconds.

### Draw submission thresholds of 512 and 1,024

Four warmed 60-second headless launches compared `WINEMETALGL_COMMIT_DRAWS` settings 64 and 128 in 64, 128, 128, 64 order. Direct ARB uniform reads stayed enabled and the triangle threshold stayed disabled. Setting 128 raises the draw threshold to 1,024. The viewport, camera, VSync, and caster schedule matched across runs.

| Draw threshold | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| 512 | 97.58 | 14.49 ms |
| 1,024 | 97.48 | 15.52 ms |

The higher threshold improved both metrics in the second pair and regressed both in the first. The smaller median p99 supports keeping 512 for further trials. The four GPU sequences were consecutive, the four readbacks showed the same camera and overlapping spell effects, and the desktop monitor recorded zero visible trial windows or activations. [Full-raid A/B](results/raid-winemetalgl-headless-draw-threshold-ab.json)

### Cross-block x86 flag liveness

The JIT can analyze successor blocks and keep only the x86 flags they may read. The headless throughput trial compared the launcher setting `OCERZ_NO_XLIVE=1` with that variable unset and the normal analysis depth of three. The runtime and renderer binaries were shared by both arms. Direct ARB uniform reads were on, the triangle threshold was disabled, and the draw threshold was 512.

| Cross-block liveness | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| Disabled | 98.03 | 13.29 ms |
| Enabled | 102.14 | 12.73 ms |

Both pairs improved with liveness analysis enabled. Four warmed 60-second launches matched the camera, checked all 26 cast timing rows, and captured the same scene with overlapping spell effects. Every GPU sequence was consecutive. The window monitor recorded zero visible trial windows or activations. Both settings passed all 806 general runtime checks. This comparison covers the isolated headless client. [Full-raid A/B](results/raid-aarchx-headless-cross-block-flag-liveness-ab.json)

Separate native instruction profiles sampled seven warmed eight-second intervals per arm. Of all CPU samples on the hottest guest thread, the share attributed to `Jcc` writing the deferred flag operands fell from **3.35% to 0.62%** with liveness analysis enabled. The share attributed to reloading the saved floating-point operand fell from **2.05% to 1.43%**. The deferred descriptor load stayed close to **1.02%** in both arms. These percentages describe sampled CPU time. [Native flag traffic profile](results/raid-aarchx-headless-cross-block-liveness-profile.json)

### Flag liveness analysis depth

With cross-block liveness enabled, four warmed 60-second headless launches compared depths 3 and 6 in 3, 6, 6, 3 order. Both arms kept direct ARB uniform reads, a disabled triangle threshold, and a 512-draw threshold.

| Analysis depth | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| 3 | 101.83 | 12.84 ms |
| 6 | 101.30 | 14.08 ms |

Depth 6 increased the p99 gap in both pairs by about 1.2 ms. Depth 3 is the baseline for further trials. Each run checked the saved camera and cast schedule, produced consecutive GPU completions, and recorded zero visible trial windows or activations. The readbacks showed overlapping spell effects at both depths. [Full-raid A/B](results/raid-aarchx-headless-flag-liveness-depth-ab.json)

### Native renderer stacks after enabling flag liveness

A separate 18-second `sample PID 18 1 -file CPU_SAMPLE` capture collected **7,921** main-thread stacks at liveness depth 3. The indexed draw path appeared in **2,324** stacks, uniform binding in **906**, and fixed-function uniform packing in **697**. In **326** of the fixed-function packing stacks, the sampled leaf was byte vector insertion. These are wall-clock stack counts. The capture perturbed frame pacing, so the throughput comparisons use the uninstrumented A/B launches. GPU completions stayed consecutive and the monitor observed zero activations or visible windows. [Native stack counts](results/raid-winemetalgl-headless-main-thread-stacks.json)

### Write the fixed-function uniform tail directly

The first pack sizes the existing per-context vector and initializes the std140 padding. Later packs overwrite the same fields at their aligned offsets. This removes byte vector insertion and resize calls from the hot path. The shader layout, matrix prefix, and generation tracking stay intact. Apply `patches/winemetalgl-fixed-uniform-direct-trial.patch` after the fixed uniform tail trial and set `WINEMETALGL_FIXED_UNIFORM_DIRECT=1` to select the direct writer.

Four warmed 60-second headless launches used the same runtime and renderer in control, trial, trial, and control order. Direct ARB uniforms were enabled, cross-block flag liveness used depth 3, and the triangle and draw thresholds were 0 and 512. All four launches kept the fixed camera and verified the 25 raid actors and exact cast schedule.

| Tail writer | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| Vector insertions | 103.23 | 16.06 ms |
| Direct writes | 112.66 | 14.27 ms |

Throughput rose **9.14%** and the median p99 fell **1.80 ms**, with both metrics improving in both pairs. Each readback showed the scene and overlapping effects. GPU completion sequences stayed consecutive and the monitor observed zero trial windows or activations. Both arms passed **337** GPU tests. [Full-raid A/B and GPU checks](results/raid-winemetalgl-headless-direct-uniform-tail-ab.json)

A separate diagnostic packed both ways before checking each complete 6,384-byte block. It logged at least **2,834,432 byte-identical repacks**. Of the **2,429,952** in-world repacks between logged checkpoints, **2,135,640 (87.89%)** left every tail byte unchanged. Across that interval, a repack changed an average of **0.14 aligned 16-byte slots**. The parity probe ran separately from the four timed launches. [Buffer parity and changed-slot counts](results/raid-winemetalgl-headless-direct-uniform-tail-ab.json)

### Follow forward i386 branches within a guest page

The original timing series had other trial instances running, rising from one to four across its launches. The isolated recheck improved throughput and p99 in both pairs. [Isolated recheck](results/raid-aarchx-headless-i386-forward-superblock-isolated-recheck.json)

The trial translates through the fall-through of up to four forward conditional branches when both destinations lie in the entry guest page. It uses the existing side exits for taken branches. The trial runs with the translation cache off. A separate compile witness found **26,848** distinct translated entry sites with side exits, including **4,481** with four.

Apply `patches/aarchx-m32-forward-superblock-trial.patch` to the AArchX source and set `OCERZ_M32_FORWARD_SUPERBLOCK=1` to select the trial. Run the comparison with that variable unset in control. Set `OCERZ_TCACHE=off` in both arms. Enabling `OCERZ_M32_FORWARD_SUPERBLOCK_LOG` in a separate launch records the emitted sites. Run `test_jit32` in separate processes with `OCERZ_TEST_JIT32_FORWARD_BRANCH` set to each number from 0 through 5. The cases cover taken and fall-through paths, integer and floating-point flags, a guest-page boundary, and a backward branch. Both arms passed all six interpreter comparisons and all **806** general runtime checks.

| Forward fall-through merging | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| Disabled | 108.30 | 14.40 ms |
| Enabled | 105.53 | 14.39 ms |

The first pair lost throughput and the second gained 1 FPS. The median throughput fell **2.55%**. The p99 changes across pairs had opposite signs. Four warmed 60-second launches used direct fixed-function uniform writes, direct ARB uniforms, liveness depth 3, and the same renderer. GPU completions were consecutive. The readbacks showed the fixed camera and overlapping effects, the realm checked all cast timing rows, and the desktop monitor recorded zero visible trial windows or activations. [Full-raid A/B, compile coverage and checks](results/raid-aarchx-headless-i386-forward-superblock-ab.json)

### Fixed-function uniform invalidations and changed fields

This diagnostic ran with five other trial instances present.

A separate headless run compared old and new packed tails in 16-byte units and recorded the last OpenGL fixed-state mutation before each repack. Of **2,097,152** in-world repacks between checkpoints, **1,841,620 (87.82%)** left the tail byte-identical. The last tracked call was `setLightModel` for **1,893,492** repacks and `MatrixMode` for **203,660**. Every interval between repacks included more than one version increment, so the recorded call identifies the latest mutation in each interval.

Only five aligned slots changed in the capture. They contained the modelview normal scale, alpha-test parameters, two fog slots, and line parameters. The separately captured throughput comparisons omit these diagnostics. [Invalidation and uniform slot counts](results/raid-winemetalgl-headless-uniform-invalidation-profile.json)

### Invalidate packed uniform data only when its fields change

The original timing series ran with five other trial instances present. [Isolated recheck](results/raid-winemetalgl-headless-precise-uniform-invalidation-isolated-recheck.json)

The trial updates shader selectors independently of the packed block. Its grouped setters invalidate the packed block when the stored field changes at the byte level. Paths that update materials, individual lights, point parameters, or saved attributes still use their existing invalidation. Apply `patches/winemetalgl-fixed-uniform-invalidation-trial.patch` and set `WINEMETALGL_FIXED_UNIFORM_INVALIDATION=1`. The headless comparison used direct fixed-function writes, direct ARB uniform reads, and liveness depth 3 in both arms.

| Uniform invalidation | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| Existing | 104.53 | 17.22 ms |
| Based on packed fields | 106.88 | 17.14 ms |

The median rose **2.25%**. Throughput improved in both pairs, and p99 moved in opposite directions across the pairs. Four warmed 60-second launches kept the fixed camera, 25 raid actors, exact cast timing, and consecutive GPU completions. Readbacks showed overlapping effects in both arms. Both arms passed **337** GPU tests. The monitor recorded zero visible trial windows or activations.

In separate instrumented launches, a reference writer independently rebuilt and compared the full 6,384-byte block on every uniform binding. All compared blocks matched. Between in-world checkpoints, the share of bindings that fully repacked the block fell from **91.70% to 5.92%**. The share that advanced the packed block generation fell from **91.70% to 37.40%**. The throughput launches omit the reference writer. [Full-raid A/B and byte checks](results/raid-winemetalgl-headless-precise-uniform-invalidation-ab.json)

### Reuse the packed block generation when matrix bytes match

The original timing series ran with five other trial instances present. [Isolated recheck](results/raid-winemetalgl-headless-retain-matrix-generation-isolated-recheck.json)

The trial advances the packed block generation after matrix-only invalidation when a packed matrix changes. It uses the matrix and texture comparisons already performed by the packer. Apply `patches/winemetalgl-fixed-retain-matrix-generation-trial.patch` after the direct uniform writer and set `WINEMETALGL_FIXED_KEEP_MATRIX_GENERATION=1`. Both arms enabled precise uniform invalidation.

A diagnostic rebuilt and compared the 6,384-byte block at every binding. Of **908,838** matrix-only invalidations between in-world checkpoints, **275,222 (30.28%)** had byte-identical output and kept the existing generation. The diagnostic compared at least **3,276,800** complete blocks and found byte parity throughout.

| Matrix-only invalidation | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| Existing generation updates | 106.83 | 17.10 ms |
| Reuse generation on equal matrices | 106.63 | 17.08 ms |

Both median differences were within **0.2%**, and the individual pairs moved in opposite directions. Each of the four warmed 60-second runs kept the camera, cast schedule, and overlapping effects. All four GPU completion sequences were consecutive. Both arms passed **337** GPU tests, and the desktop monitor observed zero trial activations or visible windows. [Full-raid A/B and byte checks](results/raid-winemetalgl-headless-retain-matrix-generation-ab.json)

### Native render stacks after precise uniform invalidation

This diagnostic ran with five other trial instances present.

A live 18-second main-thread sample with direct packing and precise invalidation collected **7,842** stacks. The fixed-function block appeared in **26** stacks and uniform binding in **161**. The indexed draw path appeared in **1,682** stacks, including **1,253** in `DrawState::issue`. Direct children of the latter included **271** in state binding, **186** in draw preparation, and **186** in the Metal indexed draw call.

Of the sampled leaves, **4,774** resolved to an unknown binary and **1,044** to WineMetalGL. The unknown bucket records symbol resolution. The instrumented launch kept consecutive GPU completions, with zero visible windows or activations. The throughput comparisons use uninstrumented launches. [Native stack counts](results/raid-winemetalgl-headless-precise-main-thread-stacks.json)

### Translated CPU hotspots after reducing uniform work

This diagnostic ran with five other trial instances present.

A separate CPU-time-weighted capture covered seven warmed eight-second periods on the hottest guest thread with precise uniform invalidation enabled. Already translated instructions accounted for **63.2–65.9%** of sampled CPU time across those periods. Guest `MOV` accounted for **18.59%** of all sampled CPU time, conditional branches for **9.70%**, and `MOVSS` for **3.87%**.

Of the sampled ARM64 instruction weight attributed to guest `MOV`, **52.5%** was on `ADD` and **23.9%** on `SUB`. The largest operand shapes added or subtracted an immediate from `w26` into `w15`. With full pinning, `w26` holds the guest frame pointer EBP. The profiles use the same headless scene, camera, renderer options, cast schedule, and verified GPU completions. [CPU-time and ARM64 instruction distributions](results/raid-aarchx-headless-precise-guest-profile.json)

### Verify process isolation across launches

The forward-branch runner launched a dedicated test bundle and its cleanup checked the standard bundle's executable path. The smoke instance and four timed instances stayed alive. Observation after the later profiles found all five running. Their aggregate CPU reading at that check was **263% of one core**. The number of other instances rose across the forward-branch trial and stayed at five through the precise-invalidation and matrix-generation trials. Those timing records carry a process-isolation note. The byte comparisons verified the packed data produced by each instrumented launch.

The runner now checks the executable path of the launched bundle. The isolated run uses the process checker before the first launch and after every launch to confirm that the entire isolated client directory is free of running trial instances. The checker scopes its process scan to that directory.

```sh
python3 benchmarks/check-isolated-raid-processes.py ISOLATED_CLIENT_DIR
```

#### Isolated recheck of precise uniform invalidation

Four warmed 60-second launches reused the same precise-invalidation build and checked process isolation through the final cleanup. The control median was **115.32 FPS** at **13.00 ms p99** and precise invalidation reached **119.26 FPS** at **13.05 ms p99**. Throughput rose **3.42%** and improved in both pairs. Median p99 rose **0.05 ms**. GPU sequences stayed consecutive, the fixed camera and overlapping spell effects were visible, and the server verified the full cast schedule. [Isolated throughput recheck](results/raid-winemetalgl-headless-precise-uniform-invalidation-isolated-recheck.json)

#### Isolated recheck of i386 forward branches

Four warmed 60-second launches repeated the forward-branch comparison on the direct uniform writer. The control median was **111.68 FPS** at **14.78 ms p99**. Forward-branch merging reached **116.79 FPS** at **11.92 ms p99**, a **4.57%** median throughput gain and a **2.86 ms** shorter median p99 gap. Both pairs improved on both metrics. All launches checked the scene, exact cast timing, continuous GPU completion sequences, and zero trial windows or activations. The process checker verified cleanup between every launch. [Isolated throughput recheck](results/raid-aarchx-headless-i386-forward-superblock-isolated-recheck.json)

#### Isolated recheck of matrix generation reuse

Four warmed 60-second launches repeated the matrix-generation comparison with precise uniform invalidation on in both arms. The control median was **117.41 FPS** at **13.40 ms p99**. Reusing the generation reached **115.28 FPS** at **13.94 ms p99**. The two pairs disagreed in direction. Every launch verified the camera, spell effects, cast schedule, consecutive GPU completions, and zero visible windows or activations. The process checker verified cleanup between launches. [Isolated throughput recheck](results/raid-winemetalgl-headless-retain-matrix-generation-isolated-recheck.json)

### Same-base load groups with isolated forward-branch profiles

Two isolated diagnostic launches used precise uniform invalidation in both arms and varied forward-branch merging. The same-base classifier weights samples for plain 32-bit guest `MOV` loads using the number of statically matching loads before the first write to that base in the compiled block. A side exit can make the executed group shorter. Both launches supplied seven warmed eight-second periods.

Guest conditional branches accounted for **9.95%** of sampled CPU time with merging off and **9.17%** with merging on. Loads in static groups of at least three accounted for **3.10%** and **3.21%**, respectively. The translated share ranged from **62.6–65.0%** with merging off and **62.8–64.0%** with merging on. Each launch kept consecutive GPU completions and the full scene with zero visible windows or activations. The process checker passed between launches. [Isolated CPU profiles](results/raid-aarchx-headless-isolated-forward-base-profile.json)

### i386 forward branches with precise uniform invalidation

Four isolated 60-second launches kept precise uniform invalidation enabled and varied the forward-branch switch in control, trial, trial, and control order. All launches used the same runtime, renderer, camera, draw threshold, and cast schedule.

| Forward-branch merging | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| Disabled | 120.31 | 11.16 ms |
| Enabled | 114.52 | 13.73 ms |

Throughput fell **4.81%** and median p99 rose **2.57 ms**, with both pairs moving in the same direction. The previous isolated comparison with precise invalidation disabled improved both metrics with forward merging. Test the switches together on the intended baseline. The capture verified consecutive GPU frames, overlapping effects, all cast timing rows, zero visible trial windows or activations, and cleanup between launches. [Combined-baseline A/B](results/raid-aarchx-headless-forward-superblock-precise-uniform-ab.json)

### Headless render thread scheduling

The core probe sampled every 64 swaps. All **72** in-world samples were on the main thread and reported `User Interactive` QoS with relative priority zero. Sampled CPU indices were 4–8 and 10. The machine exposes four efficiency and eight performance logical CPUs, and the device tree identifies the efficiency CPUs as 0–3. Median main-thread CPU time was **8.22 ms per swap**. The trial used the system's original scheduling policy. The probe observed consecutive GPU completions and zero windows or activations. [Render thread profile](results/raid-aarchx-headless-render-thread-core-profile.json)

Apply `patches/aarchx-m32-render-core-profile-trial.patch` after the headless drawable and realm patches and set `OCERZ_M32_HEADLESS_CORE_SAMPLE=1` for this diagnostic.

### Fold 32-bit load addresses with precise uniform invalidation

Four isolated 60-second launches varied only `OCERZ_M32_MOV_BASE_UXTW` with precise uniform invalidation and the direct uniform writer enabled. Forward-branch merging and explicit QoS requests were off in both arms. The core sampler was off for the timed launches.

| Wrapped load addressing | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| Existing | 119.58 | 11.05 ms |
| Folded into the load | 123.91 | 10.45 ms |

Throughput rose **3.62%** and median p99 fell **0.60 ms**. Both pairs improved on both metrics. All launches verified the cast schedule and GPU completion sequences. The readbacks showed the saved camera and overlapping spells. Both settings passed **806** general checks and six focused interpreter comparisons. The trial emitted the folded load on the negative-displacement and wraparound cases. The process checker and desktop monitor verified isolation. [Full-raid A/B and checks](results/raid-aarchx-headless-mov-base-uxtw-precise-uniform-ab.json)

### GPU activity with precise uniform invalidation and folded loads

An eight-second Metal System Trace matched **988 of 988** in-range GPU completion timestamps within **43 ns**. In 987 consecutive intervals, GPU activity occupied **24.38%** of the joined span. The mean active union was **2.10 ms** per interval. Mean vertex and fragment activity were **0.70 ms** and **1.39 ms**, respectively. The p99 active union was **4.36 ms**.

The ten longest completion gaps had a median duration of **11.96 ms** and **2.53 ms** median total active GPU time. The active union counts time on the captured render and compute channels. The gaps can include idle time and scheduling, and work can overlap output boundaries. This launch used the folded-load and precise-invalidation baseline with zero other trial processes and no window activation. [Aligned GPU activity](results/raid-winemetalgl-headless-precise-uxtw-gpu-work.json)

### Translated CPU hotspot coverage on the folded-load baseline

Seven warmed eight-second periods attributed **60.7–63.4%** of CPU time to translated instructions. The full histogram covered **9,125** sampled guest PCs. The hottest single address accounted for **0.30%** of all sampled CPU time. The hottest four aligned 4 KiB regions accounted for **10.43%**, while the hottest four aligned 64 KiB regions accounted for **22.88%**. The 1 MiB range beginning at guest address `0x800000` accounted for **27.16%**. These groups describe address ranges, each of which can contain multiple functions.

Apply `patches/aarchx-m32-full-guest-pc-profile-trial.patch` after the guest instruction profile patch and enable `OCERZ_GUESTPROF_FULL_GUEST_PC=1` and `OCERZ_GUESTPROF_INSTRUCTIONS=1` for a separate diagnostic. The recorder emits weighted guest addresses and leaves the uninstrumented throughput measurements separate. The live capture verified the full cast schedule, consecutive GPU completions, cleanup, and zero window activations. [Translated address coverage](results/raid-aarchx-headless-precise-uxtw-guest-regions.json)

### Use the later SSE batching pass for inline i386 reads and registers

The i386 trial selects the later FP batching pass for inline legacy SSE arithmetic and moves. An eligible group can read through a wrapped 32-bit address. Groups end at stores and conditional branches. When the pass finds no batch, the translator uses its earlier pass. Apply `patches/aarchx-m32-fpb-v2-trial.patch` after the folded-load patch and enable `OCERZ_M32_FPB_V2=1` with the translation cache off.

Six interpreter cases passed with the existing pass, the trial, and forced replay. They cover finite arithmetic, signed zero, infinities, NaN payloads, wrapped finite and NaN memory reads, and a store followed by a compare. The wrapped reads were emitted inside the trial batch. Both normal settings passed 806 general checks. For the focused cases, run `test_jit32` in separate processes with `OCERZ_TEST_JIT32_FPB` set to each number from 0 to 5. In a separate diagnostic, `OCERZ_FPB_DBGPRINT=1` logged **995** distinct emitted groups with 32-bit reads admitted. A probe preceding the read support recorded **483** groups.

| i386 FP batching | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| Earlier pass | 122.19 | 11.83 ms |
| Later pass | 125.00 | 10.47 ms |

Throughput rose **2.30%** and median p99 fell **1.36 ms**. Both pairs improved on both metrics. All four isolated 60-second launches used precise uniform invalidation and folded load addresses. The completion sequences were consecutive, and the launches verified the camera, full cast schedule, overlapping effects, zero visible windows or activations, and cleanup. [Full-raid A/B and checks](results/raid-aarchx-headless-i386-fpb-v2-ab.json)

### Attribute sampled guest addresses to memory forms

A streaming disassembly of the local i386 executable's `__text` matched **98.30%** of the saved translated CPU weight. Instructions with a memory operand based on EBP without an index accounted for **14.38%** of all sampled CPU time. The corresponding ESP forms accounted for **1.12%**. EBP-relative `MOV` loads without an index accounted for **3.70%** with negative displacements and **2.49%** with nonnegative displacements. The matched weight includes the ARM64 work assigned to each guest instruction. The underlying capture predates the later FP batching trial. [Weighted address forms](results/raid-aarchx-headless-i386-weighted-address-forms.json)

```sh
python3 benchmarks/summarize-i386-address-forms.py CAPTURE_DIR LOCAL_I386_EXECUTABLE
```

### Guard and reuse the mapped EBP base inside a stable i386 prefix

The trial selects a block with at least six compatible EBP-relative MOV or SSE memory operands before the first operation that could write EBP. One entry guard checks that every small displacement stays inside the 32-bit address space. The selected code uses the mapped base with an immediate displacement and preserves the original memory ordering. Near either address-space boundary, the existing retirement path recompiles the block with ordinary address generation.

Apply `patches/aarchx-m32-ebp-hoist-trial.patch` after the i386 FP batching patch. Set `OCERZ_M32_EBP_HOIST=1` and turn the translation cache off. Eight focused cases passed in each setting, including indexed operands, EBP reassignment, both guard exits and the wrapped load. A separate forced SSE replay check with the mapped base also passed. Both settings passed all **806** general runtime checks.

| EBP address generation | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| Existing | 116.73 | 12.61 ms |
| Guarded mapped base | 118.32 | 10.97 ms |

The four isolated minute-long launches improved both metrics in both pairs. The throughput median rose **1.36%** and the p99 median fell **1.64 ms**. Camera, effects, the full cast schedule, and consecutive GPU completions were verified. The monitor recorded zero visible trial windows or activations.

A separate compile witness reported **1,448** selected prefixes with a median of **eight** matching operands. Matching those prefixes to the earlier folded-load CPU profile covered candidate operand shapes accounting for **2.46%** of total sampled CPU time. This is static coverage of the selected prefixes. [Full-raid A/B and checks](results/raid-aarchx-headless-i386-ebp-hoist-ab.json)

### Measure the native callback scope around swaps

The headless diagnostic sampled every 64 swaps on the render thread and counted outstanding native-to-guest callbacks. All **78** in-world samples were at depth one in the **same** outer callback. By the last sampled swap, **9,856** swaps had occurred since that callback began. The monitor observed zero windows or activations, and GPU completions were consecutive.

The renderer's existing batch API keeps one autorelease pool until its matching end call. Here a pool scoped to the outer game callback would retain autoreleased objects across thousands of swaps. The probe makes frame-specific lifetime boundaries the relevant unit for a future batching trial.

Apply `patches/aarchx-m32-render-callback-scope-profile-trial.patch` after the headless drawable and realm patches and set `OCERZ_M32_HEADLESS_CALLBACK_SAMPLE=1` in a diagnostic build. [Callback-scope measurements](results/raid-aarchx-headless-render-callback-scopes.json)

### Skip autorelease pools around selected CPU-only OpenGL state calls

A diagnostic entrypoint log contained **111,260** calls after the in-world readback, including **8,094** draw calls. The selected state setters accounted for **48,661** of those calls. The trace changes the array-buffer binding dispatch. The throughput comparison used the trace off.

The trial skips the call-local pool for the audited CPU-only setters. It keeps sharing and validation under the original lock. Debug output, display-list recording and immediate-mode calls retain a pool that drains after releasing the lock. Buffer bindings and draw calls use their ordinary pools. Apply `patches/winemetalgl-pure-state-pool-trial.patch` and set `WINEMETALGL_PURE_STATE_NO_POOL=1`. A separate software counter confirmed **18,890,752** eligible omissions between in-world log rows.

| CPU-only state calls | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| Existing pools | 118.28 | 12.71 ms |
| Pool omission | 117.43 | 13.53 ms |

The pairs disagreed in direction. The result includes one RSS reading just after each measured window. Both modes passed **337** GPU tests, including the debug callback and display lists. Four isolated 60-second launches verified the camera, full cast schedule, overlapping effects, consecutive GPU completions and cleanup. [Full-raid A/B, counts and checks](results/raid-winemetalgl-headless-pure-state-pool-ab.json)

### Reuse unchanged ARB environment parameters

The trial versions each context's vertex and fragment environment arrays. A linked ARB program can skip the default uniform block's byte comparison when its last checked version matches. Changed arrays receive process-wide unique versions so shared programs can distinguish the state of two contexts. Apply `patches/winemetalgl-arb-env-versions-trial.patch` after the segmented ARB uniform patch and set `WINEMETALGL_ARB_ENV_VERSIONS=1`.

A separate in-world diagnostic skipped **994,162 of 3,096,576** comparisons between recorded counter rows (**32.11%**). The counter and the state pool omission trial were off in every timed run. Both modes passed **338** GPU tests, including alternating environment values between two contexts that share one fragment program.

| ARB environment comparisons | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| Compare bytes at binding | 122.58 | 12.22 ms |
| Reuse by version | 121.10 | 12.23 ms |

Throughput fell **1.21%** and declined in both pairs. P99 moved in opposite directions across the pairs. The trial stays off. Four isolated 60-second launches verified the camera, overlapping effects, the full cast schedule, consecutive GPU completions, zero visible trial windows or activations, and process cleanup. [Full-raid A/B and reuse counts](results/raid-winemetalgl-headless-arb-env-versions-ab.json)

### Attribute guest CPU samples to i386 unwind ranges

The original client exposes two text symbols and 24,397 non-overlapping unwind ranges. Grouping the seven warmed periods of the isolated folded-load capture by those ranges attributed **65.32% of translated CPU time**. The 823 ranges with samples covered **40.69% of all sampled CPU time**.

The largest matched range, starting at `0x880c70`, accounted for **4.85%** of all sampled CPU time. The four largest accounted for **9.42%**, and the largest 16 for **17.23%**. These are exclusive instruction samples. About a third of the translated CPU time fell outside the unwind ranges. The coverage and the spread of samples favor optimizing instruction patterns shared across routines. [Unwind attribution](results/raid-aarchx-headless-i386-unwind-hotspots.json)

Run the analysis on the original 32-bit Mach-O file and the full guest-PC capture, with Xcode's `llvm-dwarfdump` available.

```sh
python3 benchmarks/summarize-i386-unwind-hotspots.py CAPTURE_DIR LOCAL_I386_EXECUTABLE
```

### Choose the i386 forward path from observed branches

The earlier forward-superblock trial always extends a trace along the fall-through path. This trial allows the existing branch profiler to sample eligible i386 forward edges. When the profiler selects the target, the next translation follows that target and preserves the original fall-through as a side exit. Both successors must be in the same guest page as the block entry. A branch with an open floating-point batch is ineligible for a probe.

Apply `patches/aarchx-m32-profiled-forward-branches-trial.patch` after the forward-superblock and EBP hoist patches. Set `OCERZ_M32_FORWARD_SUPERBLOCK=1` and `OCERZ_M32_BRANCH_PROFILE=1` for this variant with the translation cache off. Both switches use presence, so unset them to use the baseline. Run `test_jit32` with `OCERZ_TEST_JIT32_BRANCH_TRACE` set to 0 and 1 in separate processes and `OCERZ_FLIP_JCC=0x400008` to force the known test edge. Both branch outcomes matched the interpreter's registers, flags and memory with and without the trial. Each of those modes passed **806** general checks.

A separate run used `OCERZ_FLIPLOG=1` to verify branch choices. After in-world readback it logged **14,707** verdict rows and emitted inverted traces at **91** distinct branch addresses. Four of the addresses were in the hottest matched unwind range. A branch can appear in more than one trace or verdict row.

| i386 forward path | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| Baseline block chaining | 120.68 | 12.52 ms |
| Merge along fall-through | 121.42 | 10.89 ms |
| Merge with observed branch direction | 118.25 | 10.92 ms |

Each mode ran twice for 60 seconds in mirrored order. The profiled path reduced throughput against both controls in both pairs. Fall-through merging improved throughput by **0.61%** and moved p99 in opposite directions across the pairs. The baseline keeps both switches off. Every run verified the camera, cast schedule, overlapping effects, consecutive GPU completions, zero visible trial windows or activations, and cleanup. [Six live-raid windows and branch witness](results/raid-aarchx-headless-i386-profiled-forward-branches.json)

### Measure the CPU time available to move off the game thread

A separate 24-caster launch enabled WineMetalGL's per-frame timers. After the in-world readback, **4,697** consecutive renderer frames averaged **8.60 ms** between frame boundaries with about **818** draws per frame. GL calls occupied **2.46 ms** of elapsed time per frame on the calling thread. Preparing draw state took **0.22 ms**, binding it **0.31 ms**, and issuing draw commands **0.49 ms**. The renderer's explicit GPU wait counter stayed at zero.

Draw encoding accounted for **5.7%** of the observed mean frame interval, and binding plus encoding accounted for **9.3%**. In a serial frame-time estimate, completely overlapping encoding with other work would raise throughput by about **6%** before the cost of a queue. A larger scheduling gain would need to overlap more of the GL work. The phase totals are elapsed times on the calling thread. They sit within GL and frame time.

The diagnostic used `WINEMETALGL_STATS=1` and `WINEMETALGL_LOG=0,perf:info` with the baseline branch mode. The readback verified the 1365 × 768 raid view with overlapping effects. The realm verified all 26 timed rows and the separate 24-second GPU window had consecutive completions. The monitor observed zero visible trial windows or activations. [Renderer phase distributions](results/raid-winemetalgl-headless-render-cpu-phases.json)

### Fold the mapping base into wrapped i386 SSE accesses

The trial keeps the existing i386 effective-address calculation in a 32-bit register and folds the guest mapping base into the ARM64 load or store. It applies to eligible 4-, 8-, and 16-byte SSE memory operations. The ordinary address path still handles segments, guarded layouts and cases that require ordered memory primitives. Apply `patches/aarchx-m32-sse-wrapped-offset-trial.patch` after the EBP hoist patch and set `OCERZ_M32_SSE_UXTW=1`. Unset the variable for control.

Both modes passed **806** general checks. Three address cases matched the interpreter under both default and strict vector ordering. They cover indexed scalar addresses, a wrapped double address, and unaligned packed data. Six forced FP-batch replay checks also passed in each mode. In a separate diagnostic, `OCERZ_M32_SSE_UXTW_LOG=1` recorded **12,968** distinct emitted guest addresses from startup through the raid. Of those, **834** matched sampled PCs in an earlier raid CPU profile and represented **6.89%** of that capture's total CPU weight. These counts measure emission and address coverage.

| SSE memory addressing | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| Existing addressing | 120.58 | 14.43 ms |
| Wrapped offset in the memory instruction | 120.18 | 17.57 ms |

The four warmed 60-second launches ran with emission logging off. The pairs moved in opposite directions on throughput, and p99 increased in both. The trial stays off. Each launch verified the camera, overlapping effects, all cast timing rows, consecutive GPU completions, no visible trial window or activation, and process cleanup. [Full-raid A/B and instruction coverage](results/raid-aarchx-headless-i386-sse-wrapped-offsets.json)

### Attribute slow frames to CPU work and Metal submissions

Two warmed 60-second diagnostic launches at the 512-draw threshold matched **7,099** and **7,420** completed outputs to the final command buffer's exact GPU end timestamp. Every interval contained two render submissions whose draws summed to the output's recorded draw count. A CPU timing row per output in the second launch identified one render thread across the whole window.

In the first capture, the largest increase above the corresponding component's median came from the final submission's GPU dispatch wait or GPU span in **69 of 71** intervals at or above p99. The second capture attributed **71 of 75** to those two GPU components. Median calling-thread CPU time in the second run was **7.99 ms** across the window and **8.26 ms** in its slowest intervals. The median wall time minus thread CPU time was **0.017 ms** across the window and **0.020 ms** in those intervals.

The median GPU span of the first submission increased from **0.94 to 13.71 ms** in the first capture's tail and from **1.07 to 7.97 ms** in the second. These spans use Metal's GPU start and end timestamps and include any interruption within the span. The final command buffer's dispatch wait was largely covered by the first submission of that output. Both launches verified the saved camera, effects, 26 timing rows, consecutive GPU completions, and zero visible trial windows or activations. [Frame-tail decomposition](results/raid-winemetalgl-headless-frame-tail-attribution.json)

Set `OCERZ_WMGL_SUBMISSION_LOG` to the capture's `submissions.ns`. For thread CPU timing, also apply `patches/winemetalgl-headless-swap-cpu-timing-trial.patch` and set `OCERZ_WMGL_HEADLESS_SWAP_LOG` to `cpu-swaps.ns`. With all diagnostic switches off, the patched renderer passed **338** GPU tests. Use the same verified `window.json` and `gpu.ns` as the performance recorder.

```sh
python3 benchmarks/summarize-raid-frame-tail.py CAPTURE_DIR
```

### Submit earlier in the live raid at 448 draws

Four warmed 60-second runs alternated the continuing-pass draw threshold in 512, 448, 448, and 512 order. The two settings correspond to `WINEMETALGL_COMMIT_DRAWS=64` and `56`. The triangle threshold stayed disabled. The timing runs used the same runtime and renderer with submission and CPU timing logs off.

| Continuing-pass draw threshold | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| 512 | 115.39 | 16.05 ms |
| 448 | 116.48 | 14.50 ms |

P99 improved by **3.03 ms** in the first pair and **0.08 ms** in the second. Throughput gained **8.12 FPS** in the first pair and fell **5.93 FPS** in the second. The baseline remains 512. All runs kept the saved camera, overlapping effects, exact cast schedule, consecutive GPU completion sequences, and zero visible trial windows or activations. [Live 448-draw comparison](results/raid-winemetalgl-headless-448-draw-split-ab.json)

### Identify active GPU stages in completion gaps

A separate 20-second Metal System Trace at setting 56 matched all **2,217** in-range output completions to fragment endpoints within **41 ns**. The 23 intervals at or above this trace's p99 had a median completion gap of **18.64 ms**. Median recorded client GPU activity rose from **3.65 ms** across the joined intervals to **12.87 ms** in the tail. Vertex activity increased from **1.99 to 9.18 ms**, and fragment activity from **1.06 to 4.60 ms**. These values are unions of active intervals clipped to each output gap.

Other processes also recorded activity. Their intervals can overlap the client's on another channel. The median time covered by other or unattributed activity outside the client's intervals was **2.98 ms** in the tail. [Metal System Trace stage distributions](results/raid-winemetalgl-headless-hardware-tail.json)

In the longer submission log from this diagnostic launch, every matched output interval's first buffer had **448 draws**. The 6,394 intervals used two, three, and four render submissions in **5,705**, **688**, and **one** cases, respectively. The launcher verified all 26 timing rows and no visible window or activation. The standalone 60-second comparisons use the hardware and CPU timing instrumentation disabled.

The frame-tail summarizer includes the completion gap that crosses the measurement cutoff in p99, matching the performance recorder's definition. This adds one joined command buffer beyond the in-window frame count when the last output falls before the cutoff.

### Join vertex work to its submitted Metal buffer

A separate 20-second Metal System Trace at the 512-draw threshold matched **2,332** outputs within **42 ns**. All **2,331** consecutive intervals matched the corresponding render submissions and direct draw counts. The median output contained **330,882 submitted vertex references**, compared with **336,311** in the slowest one percent. Median vertex activity summed over the output's matching command buffers increased from **0.62 to 2.20 ms**.

Two ARB programs, with 65 and 40 assembly instructions and 223 parameters each, accounted for **42.66%** of all submitted vertex references and **41.71%** in the slowest intervals. The counts use the submitted native vertex or index count and multiplicity. They measure references before any reuse in the GPU vertex cache. Activity is attributed to whole command buffers. The per-program totals describe their contents.

Apply `patches/winemetalgl-submission-vertex-work-trial.patch` on the diagnostic renderer and set `OCERZ_WMGL_VERTEX_WORK_LOG` alongside `OCERZ_WMGL_SUBMISSION_LOG`. The recorder counts issued direct draws after primitive expansion. The renderer passed **338** GPU tests with the diagnostic disabled. The live trace verified the camera, overlapping effects, all 26 cast timing rows, consecutive output completions and zero visible trial windows or activations. [Matched draw counts and GPU timestamps](results/raid-winemetalgl-headless-vertex-work-by-submission.json)

### Select mixed ARB parameters without a shader branch

The trial reads the contiguous uniform segment through a clamped index and uses GLSL's boolean `mix` to select either of up to two remaining elements. The existing index clamp still handles values outside the complete parameter array. The larger mixed arrays use the original selector.

Apply `patches/winemetalgl-arb-branchless-segments-trial.patch` after the direct uniform-segment patch and enable `WINEMETALGL_ARB_BRANCHLESS_SEGMENTS=1` with `WINEMETALGL_ARB_UNIFORM_SEGMENTS=1`. A separate compilation diagnostic confirmed the emitted path on the 40- and 65-instruction shader shapes with 223 parameters. Both modes passed **338** GPU tests after a clean rebuild. The focused test checks both ends of the array and literal selection when the unselected uniform contains NaNs.

| Mixed ARB parameter selection | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| Branch and switch | 116.29 | 13.87 ms |
| Boolean selection | 120.58 | 13.87 ms |

The throughput median rose **3.69%**, with gains of **6.08** and **2.50 FPS** in the two pairs. P99 moved by about **0.13 ms** in opposite directions. Keep this trial enabled for follow-up throughput comparisons. The four warmed 60-second launches kept the same runtime, renderer, camera, spell timing, draw threshold and overlapping effects. All GPU sequences were consecutive, and the monitor recorded zero trial windows or activations. [Live A/B and shape witness](results/raid-winemetalgl-headless-arb-branchless-segments-ab.json)

### Store mixed ARB literals in private uniform slots

The trial appends up to two finite vector literals to the linked shader's existing environment or local uniform array. It writes them once when the internal program links. Guest state updates still write only their original range. A relative shader read selects the integer slot before loading one vector. A contiguous suffix can use the existing clamped index directly.

Apply `patches/winemetalgl-arb-uniform-padding-trial.patch` after the boolean selection patch. Enable `WINEMETALGL_ARB_UNIFORM_PADDING=1` with both earlier uniform segment switches on. Both modes passed **339** GPU tests, including prefix and suffix literals, NaN in an unselected uniform, and a guest write to the numerical index that coincides with a private slot. The compilation diagnostic recorded both direct and remapped indexing in the test suite. The live scene emitted **24** remapped helpers across **14** shapes, including the hot 40- and 65-instruction forms with 223 parameters.

| Mixed ARB parameter selection | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| Boolean vector selection | 120.65 | 13.27 ms |
| Private literal slots and integer index selection | 119.84 | 12.54 ms |

The first pair dropped **3.02 FPS** and added **2.48 ms** to p99. The reverse pair gained **1.40 FPS** and cut p99 by **3.94 ms**. The comparison baseline uses boolean vector selection. All four warmed 60-second launches verified the same camera, 26 cast timings, overlapping effects, consecutive GPU completions, and no trial windows or activations. [Full-raid A/B and compile witness](results/raid-winemetalgl-headless-arb-uniform-padding-ab.json)

### Read the hottest mixed ARB array as one contiguous uniform

The trial picks the longest eligible dynamically indexed array in each vertex program. It allocates a private uniform array in the original logical order. The linker fills the literal entries once and the draw path copies only the live segment. The shader uses one clamped index for the complete array. Any original environment or local values used elsewhere keep their original layout. If the specialized program fails to link or the reflected array is incomplete, the linker retries with the ordinary selector.

Apply `patches/winemetalgl-arb-contiguous-segments-trial.patch` after the private literal slot patch. Enable `WINEMETALGL_ARB_CONTIGUOUS_SEGMENTS=1` with direct segments and boolean selection on. Both modes passed **339** GPU tests, including guest reads of independent parameters and guest writes to a numerical index that overlaps a private slot. The live compilation witness recorded leading literals in both hot 223-parameter shader shapes.

In separate 20-second diagnostic launches, median time in OpenGL calls on the calling thread was **2.45 ms** in control and **2.43 ms** with the contiguous layout. The median GPU span of the first submission was **1.14 and 1.17 ms** respectively. These spans use command-buffer timestamps. The main timing runs had both sets of timers off.

| Mixed ARB array reads | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| Boolean vector selection | 119.79 | 13.34 ms |
| Contiguous uniform | 121.98 | 10.92 ms |

Throughput rose **1.83%** in the median and increased in both pairs. P99 shortened by **0.15 ms** in the first pair and **4.68 ms** in the reverse pair. Keep this option enabled for follow-up comparisons. Every warmed 60-second launch verified the camera, overlapping effects, 26 cast timing rows, consecutive GPU completions, and zero visible windows or activations. [Live A/B and phase diagnostics](results/raid-winemetalgl-headless-arb-contiguous-segments-ab.json)

### Limit ARB vertex varyings to the linked fragment stage

The trial emits only the ARB vertex outputs declared by the generated fragment stage. It skips writes and default initialization for the omitted outputs. Position, clipping and point size retain their ordinary generation. Unwritten back colors can still take their front-color values. The linked-program cache key includes the paired fragment stage.

Apply `patches/winemetalgl-arb-trim-vertex-varyings-trial.patch` after the contiguous segment trial and set `WINEMETALGL_ARB_TRIM_VARYINGS=1`. Both modes passed **340** GPU tests. A focused test switches one vertex program between ARB fragment programs that read color or texture coordinates, switches back to its cached pair, and exercises the fixed-function fragment path.

A separate compilation log showed **5–11** omitted interface variables over **39** live emissions. All explicit output writes in those programs were required by their paired stages. The optimization removed declarations and default writes in that capture.

| ARB vertex outputs | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| Full interface | 111.50 | 13.54 ms |
| Fragment-consumed interface | 110.21 | 15.49 ms |

Throughput fell in both pairs, and p99 moved in opposite directions. The baseline keeps the contiguous uniform layout with the full interface. All four warmed 60-second launches verified the camera, spell schedule, overlapping effects, consecutive GPU completions and zero visible windows or activations. [Live A/B and compile witness](results/raid-winemetalgl-headless-arb-trim-varyings-ab.json)

### Optimize the internal ARB vertex SPIR-V

The trial optionally links SPIRV-Tools and runs its performance passes for the internal ARB vertex stage. It preserves the entry-point interface and bindings, validates the optimized module, and retains the original if optimization or validation fails. The internal program linker retries with the original vertex module if the optimized link fails. Cache keys include the optimizer version when enabled.

Apply `patches/winemetalgl-arb-vertex-spirv-opt-trial.patch` after the varying trial. Configure with `-DWMGL_ARB_SPIRV_TOOLS_SOURCE=/path/to/SPIRV-Tools` and enable `WINEMETALGL_ARB_SPIRV_OPT=1`. The test used the SPIRV-Tools and SPIRV-Headers revisions pinned by glslang's `known_good.json`, listed with archive hashes in the result. Without that build option, the ordinary link path is available.

A separate live diagnostic validated **38** optimized vertex modules, all smaller than their originals. The median reduction was **50.34%** by SPIR-V word count. All **40** optimized link attempts succeeded on the first attempt, including links that reused in-process compilation results.

| ARB vertex SPIR-V | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| Original | 117.03 | 14.55 ms |
| Optimized | 119.21 | 14.57 ms |

The first pair gained **6.07 FPS** and shortened p99 by **0.46 ms**. The reverse pair lost **1.70 FPS** and added **0.49 ms** to p99. The option remains experimental. The four warmed 60-second timings used the same contiguous array implementation and disabled file-backed shader and pipeline caches. Each launch verified the camera, effects, all cast timing rows, consecutive GPU completions and zero visible windows or activations.

With these cache settings, each mode passed two full suites of **340** GPU tests and four focused fragment-switch checks. One full run with default caches reported a pixel mismatch on a fragment switch. The repeated full suite with default caches passed. [Live A/B and validation](results/raid-winemetalgl-headless-arb-vertex-spirv-opt-ab.json)

Two separate 20-second Metal System Traces matched all **2,355** control completions and **2,293** optimized completions within **42 ns**. Median client vertex activity was **0.612 ms** in control and **0.622 ms** with optimization. Its p99 was **5.27 and 5.82 ms**. Median total client GPU activity was **1.90 and 2.10 ms**. Both traces kept the same camera, effects and cast timings. The shorter intermediate modules did not produce shorter measured GPU stage activity in these captures. [Hardware stage comparison](results/raid-winemetalgl-headless-arb-spirv-opt-metal-trace.json)

### Measure matching geometry and vertex inputs

Two separate 30-second diagnostic launches matched the reuse counters to **3,487** and **3,431** verified GPU outputs. The covered direct ARB calls accounted for about **91.2%** of all recorded draws. The probe examines native indexed commands, CPU-rewritten indices and sequential vertex ranges. It compares buffer addresses and formats, then adds versions, then the ARB vertex stage's parameters and fixed state, then the complete linked-program and pipeline signature.

| Matching submitted vertex references | First capture | Second capture |
| --- | ---: | ---: |
| Geometry and buffer versions within the frame | 42.02% | 41.31% |
| Vertex inputs within the frame | 0.21% | 0.15% |
| Complete linked state within the frame | 0.00% | 0.00% |
| Geometry and buffer versions from the previous frame | 87.72% | 87.63% |
| Vertex inputs from the previous frame | 9.49% | 9.25% |
| Complete linked state from the previous frame | 9.49% | 9.25% |

These are signature matches weighted by submitted vertex references. The measurement covers the captured draw paths, and buffer-wide versions can miss an unchanged range after an unrelated write. The measured vertex-state match rate limits the opportunity for a cache of complete transformed vertices. Both runs verified the camera, overlapping effects, cast schedule and consecutive GPU completions with no visible trial windows or activations. [Matched geometry and vertex inputs](results/raid-winemetalgl-headless-vertex-input-reuse.json)

Apply `patches/winemetalgl-vertex-reuse-profile-trial.patch` with the contiguous segment trial. Set `OCERZ_WMGL_VERTEX_REUSE_LOG` to `vertex-reuse.ns` and keep the usual GPU completion log. Run `python3 benchmarks/summarize-raid-vertex-reuse.py CAPTURE_DIR` after the monitor and client finish. A focused four-frame test verifies repeated geometry, a vertex parameter change and a fragment-only switch. The renderer passed **340** GPU tests with the probe off.

### Measure reuse of position inputs separately

The diagnostic works backward from the ARB writes to `result.position`, following temporary-register and address dependencies. It compares the retained instruction slice and the parameters, matrices and fixed state it needs. Dynamically indexed arrays contribute their full accessible range. Tracking whole source registers and the full fixed block where required makes this a conservative signature.

| Matching submitted vertex references | First capture | Second capture |
| --- | ---: | ---: |
| Position inputs within the frame | 0.20% | 0.23% |
| Position inputs from the previous frame | 20.77% | 20.31% |
| Full vertex inputs from the previous frame | 9.19% | 9.12% |

The two 30-second windows covered **6,789** consecutive verified outputs. The position probe covered every submitted reference in the observed direct ARB paths. Both runs verified the camera, overlapping spell effects, all 26 timing rows, and zero visible trial windows or activations. The reported fractions count matching input signatures weighted by submitted references. [Live position-input measurements](results/raid-winemetalgl-headless-arb-position-input-reuse.json)

Apply `patches/winemetalgl-arb-position-reuse-profile-trial.patch` after the vertex reuse probe. The same log variables and summarizer accept both record formats. A six-frame GPU test verified that changing color preserves the position match, changing the position uniform breaks it, and a different program with the same position calculation preserves it. The renderer passed **340** GPU tests with the probe off.

### Submit the first default-framebuffer batch when the queue is empty

The trial can submit the first continuing default-framebuffer pass early when the renderer's queue has no commands in flight. It checks at the selected draw threshold and every 32 draws until the normal threshold. The unused draw budget carries over to the next submission. A focused test verified two 100-draw frames with batches of 64 + 36 in control and 32 + 68 with the trial. Both modes passed **340** GPU tests.

Apply `patches/winemetalgl-idle-first-submission-trial.patch` to the renderer. Set `WINEMETALGL_IDLE_FIRST_DRAWS=256` for the trial and keep the ordinary 512-draw continuing-pass threshold. The separate 20-second diagnostic recorded first submissions at **256 draws in 2,389 of 2,494** intervals. All 2,494 used two command buffers. The median interval between the first and final submissions increased from **2.58 to 3.60 ms**.

| First submission scheduling | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| Ordinary threshold | 122.90 | 14.39 ms |
| Early threshold when the queue is empty | 123.03 | 13.88 ms |

P99 shortened by **0.56 and 0.46 ms** in the two pairs. The median throughput difference was **0.13 FPS**. Keep this as an optional baseline for tail-latency comparisons. All four warmed 60-second timings had submission logging off and verified the camera, cast schedule, overlapping effects, consecutive GPU completions and zero trial windows or activations. [Live A/B and command-buffer diagnostics](results/raid-winemetalgl-headless-idle-first-submission-ab.json)

### Pair SIMD saves across i386 bridge calls

The trial uses ARM64 paired loads and stores for adjacent pinned XMM slots around an i386 native bridge call. It applies to typed leaves and the generic fast-call path. With eight pinned registers, the generated code uses four save instructions and four restore instructions. Individual operations handle unpaired slots.

Apply `patches/aarchx-m32-bridge-xmm-pairs-trial.patch` after the EBP hoist trial and set `OCERZ_M32_BRIDGE_XMM_PAIRS=1`. Unset it for control. Both modes passed **806** general checks. The focused `test_m32 bridge-xmm` case passes four arguments to a native mock leaf, deliberately overwrites the host vector registers, and checks all **128 bytes** of guest XMM0–XMM7 after returning. The compile witness confirmed four pairs in the test. A separate live diagnostic recorded paired emissions for **68 distinct OpenGL leaf names** and the generic bridge path.

| i386 bridge XMM operations | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| Separate loads and stores | 122.49 | 13.77 ms |
| Paired loads and stores | 123.23 | 13.48 ms |

Throughput rose by **2.00 FPS** in the first pair and fell by **0.52 FPS** in the reverse pair. P99 fell by **0.54 and 0.04 ms**, respectively. The option stays experimental. All four warmed 60-second runs used the contiguous shader layout and early first submission with compilation logging off. They verified the camera, 26 cast timing rows, overlapping effects, consecutive GPU completions, and zero visible trial windows or activations. [Live A/B and native-call checks](results/raid-aarchx-headless-bridge-xmm-pairs-ab.json)

### Resolve completed Metal serials before acquiring the queue lock

The trial publishes the prefix before the first reserved or in-flight command buffer. A readiness lookup inside that prefix succeeds via an atomic load. Other lookups use the existing per-serial check so a completed later submission cannot hide an earlier reservation. Completion and abandonment advance the prefix under the queue lock.

Apply `patches/winemetalgl-completed-serial-prefix-trial.patch` and set `WINEMETALGL_COMPLETED_SERIAL_FAST_PATH=1`. The focused GPU test reserves a draw in one shared context, completes a later draw in another, and checks that both buffer readiness results remain correct. Both modes passed that test with early first submission enabled. Both also passed **341** GPU tests with the ordinary submission policy.

The separate in-world counter diagnostic recorded **3,225,925** lock-free hits among **4,390,912** readiness checks between logged checkpoints (**73.47%**). Counter logging was off during the main timing runs.

| Queue readiness lookup | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| Per-serial lookup under the lock | 122.64 | 13.59 ms |
| Completed-prefix fast path | 123.30 | 13.57 ms |

Throughput rose **0.72 and 0.60 FPS** in the two pairs. P99 changed by **+0.04 and −0.09 ms**. Keep the fast path as an option for follow-up throughput comparisons. All four warmed 60-second runs used the same early first-batch policy and verified the camera, cast schedule, visible effects, consecutive GPU completions, and zero trial windows or activations. [Raid A/B and readiness checks](results/raid-winemetalgl-headless-completed-serial-prefix-ab.json)

### Measure the i386 routines together with their callees

Two isolated 60-second launches sampled the hottest guest thread with the original client and the full raid fixture. At each translated sample, the profiler reads up to 16 return addresses through the aligned i386 frame-pointer chain. The analysis checks each address against the end of a decoded call in the client image and ends the chain at the first unrecognized return. It credits an unwind entry once per sample, including recursive calls. CPU percentages use the entire weighted sample, including untranslated and host work. The validated calls cover **62.09% and 62.69%** of that CPU time.

| i386 unwind entry | Inclusive CPU time, first capture | Inclusive CPU time, second capture |
| --- | ---: | ---: |
| `0x73f8e0` | 21.58% | 21.70% |
| `0x97dbc0` | 19.40% | 19.35% |
| Either entry | 40.97% | 41.06% |

The two entries appeared in separate sampled stacks. In both captures, `0x73f8e0` spent most of its measured time through `0x97e390` and `0x97dbc0` spent most through `0x97cef0`. These callee families give two targets for profiling code generation across functions. Stack separation establishes measured coverage. Data dependencies and safe parallelism require additional evidence.

Both captures verified the saved camera, overlapping effects, all 26 timings, and consecutive GPU sequences with zero visible trial windows or activations. The profiler adds suspension and stack-read work. The diagnostic FPS figures belong to these instrumented runs. The runtime also passed **806** general i386 checks with the profiler off. [Stack distributions, call weights, and GPU windows](results/raid-aarchx-headless-i386-inclusive-stack-profile.json)

Apply `patches/aarchx-m32-guest-stack-profile-trial.patch` and set `OCERZ_GUESTPROF=2500`, `OCERZ_GUESTPROF_PERIOD=4`, and `OCERZ_GUESTPROF_HOT=1`. Set `OCERZ_GUESTPROF_I386_STACK_LOG` to a fresh `i386-stacks.ns` in the capture directory. Analyze it after the monitored launch and GPU window verification.

```sh
python3 benchmarks/summarize-i386-unwind-hotspots.py CAPTURE_DIR LOCAL_I386_EXECUTABLE \
    --stacks --compare-entries 0x73f8e0 0x97dbc0
```

### Reuse a guarded i386 address base from more registers

The hot stack samples put **11.05%** of total CPU time in `MOVL` instructions beneath the two selected entries. The existing translator reuses a guarded mapped EBP base in eligible prefixes. This trial allows any pinned i386 integer register except ESP. It picks the base with the most compatible MOV or SSE operands before the first instruction that can change it, with a minimum of six operands. EBP wins ties. Each selected block guards the same −256 to 255 displacement range. A failed guard retires the specialized block and falls back to wrapped 32-bit address generation.

Apply `patches/aarchx-m32-general-base-hoist-trial.patch` after the EBP hoist trial. Set `OCERZ_M32_GENERAL_BASE_HOIST=1` for the trial and leave it unset for control. Both arms enabled `OCERZ_M32_EBP_HOIST=1` and disabled the translation cache. Both passed **806** general runtime checks, eight existing EBP comparisons, and six new interpreter comparisons. The new cases cover a dirty upper register half, base reassignment, both address bounds, mixed scalar and SSE operations, and indexed operands.

A separate compile witness selected **400** distinct blocks with a base other than EBP. Disassembly matched **385** of those blocks to the original client. Matching compatible operands in those prefixes to the earlier stack profiles covered **0.89% and 0.62%** of sampled CPU time. This is address coverage. Use `OCERZ_M32_GENERAL_BASE_HOIST_LOG=1` in a separate diagnostic launch to record selections.

| Guarded base selection | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| EBP | 122.25 | 13.85 ms |
| Best eligible base | 123.24 | 13.64 ms |

The four primary warmed 60-second windows used trial, control, control, and trial order with logging off. Throughput rose **1.77 and 0.22 FPS** in the two pairs. P99 fell **0.77 ms** in the first pair and rose **0.35 ms** in the reverse pair. Keep the option for follow-up throughput comparisons. A local disassembly job overlapped the first control, so the first pair remains outside the primary comparison. All six launches verified the saved camera, effects, exact cast timings, consecutive GPU completions, and zero visible trial windows or activations. [Full-raid timing and coverage](results/raid-aarchx-headless-i386-general-base-hoist-ab.json)

### Carry a guarded i386 address across a direct branch

The experiment adds an alternate entry after the address setup and guard. A direct linked branch can use that entry when both blocks use the same base and the source holds it unchanged through the branch. A block with a base write can still receive the address for its eligible prefix. Call continuations and synthesized branches outside the decoded block retain the ordinary entry. An eligible transition skips one mapped-address calculation and four guard instructions.

Apply `patches/aarchx-m32-base-handoff-trial.patch` after the general base trial and set `OCERZ_M32_BASE_HANDOFF=1`. Both arms used general base selection with its six-operand minimum. Both passed **806** general checks and four focused interpreter comparisons. The focused cases cover matching guards, a base reassignment, retirement near 4 GiB, and a change of base register. All four passed with the compiler's forced branch veneers as well.

A separate 25-second in-world diagnostic enabled `OCERZ_M32_BASE_HANDOFF_LOG=1` and `OCERZ_M32_BASE_HANDOFF_VERIFY=1`. The linker logged **81 distinct destination and base pairs** from startup and **8** after the final in-world readback. The alternate entry checked the mapped address and range without changing condition flags. The diagnostic completed with consecutive GPU sequences. The linker counts describe selected entries.

| Linked entry | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| Guard each block | 123.28 | 13.81 ms |
| Reuse the guarded address | 123.63 | 13.88 ms |

The trial lost **1.03 FPS** in the first pair and gained **1.72 FPS** in the reverse pair. P99 rose **0.38 ms** and fell **0.23 ms**, respectively. The setting stays off for follow-up comparisons. All four warmed 60-second launches ran with the diagnostic off and verified the camera, spell effects, 26 timing rows, consecutive GPU completions, and zero visible trial windows or activations. [Full-raid A/B and link diagnostics](results/raid-aarchx-headless-i386-base-handoff-ab.json)

### Capture a complete live raid frame from the offscreen drawable

The trial starts Metal capture after one offscreen flush and stops after the next flush. It writes the render program label into the capture as a debug signpost for each of the original client's direct draws. The log records both GPU output sequence numbers. The trigger can wait for a previous output with a specified draw count. This selects the start boundary before the captured frame begins. [Apple programmatic capture documentation](https://developer.apple.com/documentation/xcode/capturing-a-metal-workload-programmatically)

Two isolated launches recorded one-frame archives with **905 and 922 draws** at 1365 × 768. The archive command streams contained every logged shader label, covering **33 and 34 distinct combinations**. Both runs verified the saved camera and 26 timing rows, consecutive GPU completions, and zero visible trial windows or activations. The renderer passed **341** GPU tests with capture disabled. [Archive verification and shader counts](results/raid-winemetalgl-offscreen-metal-frame-capture.json)

The second launch also recorded Metal System Trace. Clock alignment matched **2,462** fragment endpoints to the submission log within **41 ns**. Both command buffers in the captured output matched exactly. Their draw counts and stage time are shown below. Each command buffer contains several shader combinations.

| Captured Metal command buffer | Draws | Vertex active time | Fragment active time |
| --- | ---: | ---: | ---: |
| First | 256 | 1.13 ms | 1.13 ms |
| Final | 666 | 0.35 ms | 1.00 ms |

The completion gaps containing the captures were **174.53 and 196.05 ms**. The stage times describe the instrumented frame. The archive format and shader labels were checked directly. The raw archives contain local GPU resources and are stored outside the tracked results. [Submission timing and shader counts](results/raid-winemetalgl-offscreen-metal-frame-capture.json)

Apply `patches/winemetalgl-offscreen-frame-capture-trial.patch`. Launch with `MTL_CAPTURE_ENABLED=1`, the ordinary GPU completion log, a fresh `OCERZ_WMGL_CAPTURE_PATH` ending in `.gputrace`, and a fresh path for `OCERZ_WMGL_CAPTURE_REQUEST`. Set `OCERZ_WMGL_CAPTURE_MIN_DRAWS` to the minimum preceding draw count. Create the request file after the fixture has warmed.

For the joined report, also set `OCERZ_WMGL_SUBMISSION_LOG`, record Metal System Trace for the same process, and create the request after Instruments starts recording. Export the `metal-gpu-intervals` table, then run the analyzer. Omit `--intervals-xml` for the archive and shader checks alone.

```sh
xcrun xctrace export --input CAPTURE_DIR/metal.trace --output CAPTURE_DIR/intervals.xml \
    --xpath '/trace-toc/run/data/table[@schema="metal-gpu-intervals"]'
python3 benchmarks/summarize-metal-frame-capture.py CAPTURE_DIR \
    --intervals-xml CAPTURE_DIR/intervals.xml
```

### Choose the first submission using submitted vertex references

The trial can submit the first batch of a continuing default-framebuffer pass when the queue is idle and the recorded vertex references reach a budget. It checks in 32-draw steps beginning at the configured ordinary draw minimum. The 256-draw trigger and transfer of unused draw credit remain active. The estimate counts vertices or indices after expansion, multiplied by the instance count. GPU-provided indirect counts use the existing draw heuristic.

Apply `patches/winemetalgl-idle-first-vertex-budget-trial.patch` after the idle-first-submission trial. Keep `WINEMETALGL_IDLE_FIRST_DRAWS=256` and `WINEMETALGL_COMMIT_DRAWS=64` in both arms. Set `WINEMETALGL_IDLE_FIRST_VERTICES=65000` for the trial. The chosen value is 65 percent of the control diagnostic's **100,719** median first-batch references, rounded to the nearest thousand. The reference counter describes the submitted workload before hardware vertex reuse.

A separate diagnostic with submission and vertex logging enabled verified a first batch of **96 draws in 2,192 of 2,428** frames with the trial. The control submitted at **256 draws in 2,335 of 2,463** frames. All those frames completed in two draw-bearing buffers. The median first GPU span moved from **0.62 to 0.53 ms**, and the final span moved from **1.17 to 1.32 ms**. Use `OCERZ_WMGL_FIRST_VERTEX_LOG` for the process, serial, draw count and reference count. Join the serial to `OCERZ_WMGL_SUBMISSION_LOG` to identify each output's first buffer.

| First batch policy | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| Existing draw threshold | 122.75 | 13.80 ms |
| Additional vertex budget | 122.53 | 13.80 ms |

The first pair gained **0.67 FPS** and shortened p99 by **0.45 ms**. The reverse pair lost **1.10 FPS** and lengthened p99 by **0.43 ms**. The vertex budget stays off for the next performance baseline. All four warmed 60-second launches verified the camera, overlapping effects, all 26 timing rows, consecutive GPU completions, and zero visible trial windows or activations.

Both settings passed **341** GPU tests with ordinary draw submission settings. In the focused test, the heavier instanced frame changed from **32 + 68** draws to **8 + 92** draws and matched the reference pixels. The other frame used **32 + 68** draws in both arms. [Live A/B, submission diagnostics, and test counts](results/raid-winemetalgl-idle-first-vertex-budget-ab.json)

### Use the guest's autorelease pool during a frame

An initial pilot rotated a renderer-owned pool at swap. The client had opened its own `NSAutoreleasePool` inside that scope and released it after the swap. Rotating the outer pool prematurely removed the inner pool, and the later guest release aborted. Restarting the trial under a fresh launcher bundle identifier separated it from the pilot's AppKit restored state.

The measured implementation tracks the native `NSAutoreleasePool` objects opened by the guest through Objective-C messages. WineMetalGL skips its call-local pool while an eligible guest pool is present. The client owns the pool and performs its drain. The bridge ends borrowing at each swap, on release of the active pool, or when its callback scope ends. Borrowing resumes when a new pool opens for the current frame. Tracking runs on the main thread of the isolated headless trial.

Apply `patches/aarchx-wmgl-guest-autorelease-pool-trial.patch` after the headless realm patches. Apply `patches/winemetalgl-guest-autorelease-pool-trial.patch` to the renderer. Set `OCERZ_M32_HEADLESS_FRAME_BATCH=1` and keep the same runtime and library in both arms. In a separate diagnostic with `OCERZ_M32_HEADLESS_FRAME_BATCH_LOG=1`, an eligible pool was present at **3,456 of 3,456** swaps between sampled counters. The guest opened and closed **6,912** pools in that interval.

| Autorelease pool policy | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| Renderer call-local pools | 122.45 | 13.49 ms |
| Eligible guest pool | 124.13 | 13.46 ms |

Throughput rose **1.50 and 1.87 FPS** across the two pairs. P99 rose **0.19 ms** and fell **0.24 ms**, respectively. Use the option for follow-up throughput comparisons. Each warmed 60-second run verified the camera, overlapping effects, all 26 timing rows, consecutive GPU completions, and zero visible trial windows or activations.

Separate 20-second diagnostics sampled physical footprint every two seconds. Sampled peaks were **764.42 MiB** in control and **761.78 MiB** with borrowing. The borrowed run changed by **+0.19 MiB** from its first sample to its last. The renderer passed **341** GPU tests with borrowing off, and the patched runtime passed **806** general i386 checks. [Full-raid A/B, scope counts, and memory samples](results/raid-winemetalgl-guest-autorelease-pool-ab.json)

### Trace GL call dependencies on the live game thread

The diagnostic records entry and return times for GL commands on the calling thread, plus offscreen swap boundaries. It also records the direct array-buffer binding query and context changes. It classifies draw inputs using the bound index buffer and enabled vertex arrays. Return values, unclassified arguments, and calls that can enter a callback or display-list recording stay synchronous in the model. Selected calls with known pointer inputs have a separate copy category.

Three 64-frame windows in different phases of the saved raid recorded **2,226,956 calls**. All **192** outputs matched consecutive GPU completions and draw counts at 1365 × 768. Median measured GL call time ranged from **2.34 to 2.39 ms** per frame. The median elapsed time outside the recorded GL calls ranged from **6.13 to 6.42 ms**. The first diagnostic attempt exceeded its buffer at a 256-frame limit. The accepted windows use the shorter frame limit.

| Queue model | Ideal median savings per frame | Median calls per packet |
| --- | ---: | ---: |
| Scalar commands and draws with buffer-backed inputs | 0.19–0.20 ms | 1–2 |
| Also copy selected pointer inputs | 0.28–0.29 ms | 5 |

The model keeps the observed call and gap durations. One FIFO worker overlaps eligible calls with the game and drains before each synchronous call and swap. It initially gives the queue and copies zero cost. At **200 ns per enqueued call**, both models lengthened the median frame in all three windows. These are scheduling estimates from the recorded workload.

A fourth 64-frame recording identified **52,671** draws blocked by client-memory indices. The model with ready snapshots of those inputs saved **1.14 ms** per frame by the median paired comparison at zero enqueue cost. Adding **200 ns per enqueue** left about **0.03 ms** of difference between the model and observed median frame intervals. The snapshot model also gives input copying and validation zero cost. [Command counts, timing windows, and models](results/raid-winemetalgl-command-timeline.json)

Apply `patches/winemetalgl-gl-command-timeline-trial.patch` after the guest autorelease pool trial. Set fresh paths for `OCERZ_WMGL_GL_TIMELINE_PATH` and `OCERZ_WMGL_GL_TIMELINE_REQUEST`, and keep the diagnostic GPU completion log. Create the request file after the fixture warms. The recorder writes one buffered sequence of 64 frames to `gl-timeline.tsv`. The analyzer verifies that each recorded swap matches a GPU output by sequence, dimensions, and draw count.

```sh
python3 benchmarks/summarize-gl-command-timeline.py CAPTURE_DIR
```

All four launches checked the camera, spell effects, and 26 cast timing rows. The desktop monitor recorded zero trial windows or activations. The renderer passed **341** GPU tests with the timeline disabled. [Scope and verification](results/raid-winemetalgl-command-timeline.json)

### Upload client indices at their native width

The existing client-index path scans the used range, widens the indices into a 32-bit vector, then builds a second vector before uploading it. The trial keeps the range scan and snapshots eligible 16- and 32-bit lists directly into a transient Metal buffer. It allocates that storage after draw preparation, using the serial of the command buffer that will read it. Client addresses can be unaligned. The renderer's destination offset meets Metal's native index alignment.

Topology conversion, provoking-vertex rotation, primitive restart handling, point polygons and restricted base offsets retain a CPU rewrite path. A valid maximum index such as `0xffff` stays on the wider path so Metal's implicit restart cannot consume it. Draws that may invoke debug callbacks keep the earlier snapshot path.

Apply `patches/winemetalgl-direct-client-index-upload-trial.patch` to the renderer. Set `WINEMETALGL_DIRECT_CLIENT_INDICES=1` for the trial. Both modes passed **342** GPU tests. The focused case checks unaligned 16- and 32-bit indices, reusing caller storage between draws, and a drawable vertex at `0xffff` with restart disabled.

In a separate in-world diagnostic, `WINEMETALGL_DIRECT_CLIENT_INDICES_LOG=1` recorded **3,149,824** direct uploads between counter rows and **2.54 GB** copied. Every upload in that interval used 16-bit indices. The 20-second diagnostic GPU window was verified separately.

| Client index upload | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| Expand and upload | 124.07 | 13.43 ms |
| Snapshot at native width | 131.83 | 12.96 ms |

Throughput increased **6.30 and 9.23 FPS** across the two pairs. P99 rose **0.02 ms** in the first and fell **0.97 ms** in the reverse pair. Use the native-width option for follow-up comparisons. The four warmed 60-second launches ran without copy logging and verified the camera, spell effects, all 26 timing rows, consecutive GPU completions, and zero visible trial windows or activations. [Full-raid A/B and copy counts](results/raid-winemetalgl-direct-client-index-upload-ab.json)

### Reuse byte-identical client index snapshots

The experiment stores snapshots and their scanned vertex ranges in a shared Metal buffer. A candidate matches the source address, byte count, and native index width. The renderer compares every byte before reusing the snapshot or its range. Each draw context can allocate **2,048 slots** of **4,096 bytes**. A miss on a slot still in use by the GPU falls back to the native-width transient upload. Larger lists and draws with primitive restart enabled use the ordinary path.

Apply `patches/winemetalgl-client-index-snapshot-cache-trial.patch` after the direct client-index upload trial. Keep `WINEMETALGL_DIRECT_CLIENT_INDICES=1` in both arms and set `WINEMETALGL_CLIENT_INDEX_CACHE=1` for the trial. Both modes passed **343** GPU tests. The focused test verified a hit on unchanged bytes, a safe upload when the caller changed the same array during an outstanding draw, and replacement after GPU completion. It checked output pixels in both modes.

The separate logged launch recorded **2,312,356 hits** across **3,145,728** tracked direct draws after the world readback. The **73.51%** hit rate reused **1.27 GB** of indices. Busy slots caused **598,982** transient uploads, or **19.04%** of those draws. Set `WINEMETALGL_CLIENT_INDEX_CACHE_LOG=4096` to log cumulative counts every 4,096 direct draws. The verified 20-second GPU window and the interval between complete logged counters have separate bounds.

| Client index policy | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| Native-width transient uploads | 131.63 | 13.13 ms |
| Byte-checked snapshot cache | 130.31 | 13.11 ms |

The cache lost **1.57 and 1.07 FPS** in the two pairs. P99 rose **0.13 ms** in the first pair and fell **0.17 ms** in the reverse pair. Keep native-width transient uploads as the performance baseline. The four warmed 60-second launches had cache logging off and verified the saved camera, spell effects, all 26 timing rows, consecutive GPU completions, and zero visible trial windows or activations. [Live A/B, correctness tests, and cache counts](results/raid-winemetalgl-client-index-snapshot-cache-ab.json)

### Combine the client index scan and upload

The trial computes the minimum and maximum while copying eligible client indices into transient Metal storage. It covers desktop triangle lists with 16- or 32-bit indices and retains the existing paths for restart, provoking-vertex rotation, geometry emulation, transform feedback, and debug callbacks. The early allocation pins its chunk against recycling during preparation. Once the draw has its command buffer, the reservation attaches that serial to the chunk's lifetime.

Apply `patches/winemetalgl-fused-client-index-upload-trial.patch` after the direct client-index upload trial. Keep `WINEMETALGL_DIRECT_CLIENT_INDICES=1` in both arms and set `WINEMETALGL_FUSED_CLIENT_INDICES=1` for the fused loop. The focused early-submission test produced **32 + 68** draws in each frame and matched the reference pixels in both modes. That test's early submission occurs in validation before the reservation. A separate allocator probe uses real Metal storage with simulated queue completion to verify that a held chunk survives pressure, binds to a later serial, and recycles after that serial completes.

```sh
python3 benchmarks/test-transient-reservation.py PATCHED_WINEMETALGL_CHECKOUT
```

The separate 20-second diagnostic verified consecutive GPU completions. Counters enabled with `WINEMETALGL_FUSED_CLIENT_INDICES_LOG=4096` recorded **3,076,096** fused draws and **2.56 GB** of copied indices between post-readback log rows. Zero draws in that logged interval crossed a commit between reservation and use.

| Client index staging | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| Scan, then copy | 132.13 | 12.63 ms |
| Fused scan and copy | 132.43 | 12.96 ms |

The first pair gained **1.53 FPS** and the reverse pair lost **0.93 FPS**. P99 rose **0.39 and 0.27 ms** in the two pairs. Keep the separate scan and native-width copy as the performance baseline. The four warmed 60-second launches had fused logging off and verified the saved camera, overlapping spells, all 26 timing rows, consecutive GPU completions, and zero visible trial windows or activations. Both modes passed **342** GPU tests with the ordinary draw policy. [Full-raid A/B and staged allocation checks](results/raid-winemetalgl-fused-client-index-upload-ab.json)

### Use one native GL share-group lock per thread scope

The trial uses `os_unfair_lock` for physical ownership of a share group. A thread-local scope chain lets nested calls on the same group borrow a held lock. A GPU wait suspends that ownership before unlocking and restores it after reacquisition. Calls on a shared group from another thread take the same native lock.

Apply `patches/winemetalgl-fast-share-lock-trial.patch` after the guest autorelease pool trial and set `WINEMETALGL_FAST_SHARE_LOCK=1`. The control uses the original recursive mutex. Both modes passed **343** GPU tests. The focused tests read GL state from a reentrant debug callback and draw through two shared contexts on different threads. With the diagnostic on, they logged both a borrowed acquisition and a GPU wait that released the lock.

In the separate in-world diagnostic, the logger recorded **43,540,584** physical acquisitions and **4,693,912** borrowed acquisitions between counter rows after the world readback. The thread borrowed an outer lock for **9.73%** of recorded scopes. That interval logged zero temporary releases for GPU waits. Use `WINEMETALGL_FAST_SHARE_LOCK_LOG=262144` to log cumulative counts. The separately verified GPU window lasts 20 seconds.

| GL share-group lock | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| Recursive mutex | 132.85 | 12.80 ms |
| Unfair lock with thread-local recursion | 136.00 | 12.80 ms |

Throughput rose **3.35 and 2.95 FPS** in the two pairs. P99 rose **0.10 ms** and fell **0.10 ms**, respectively. Use the option for follow-up throughput comparisons. The four warmed 60-second runs verified the saved camera, overlapping effects, all 26 timing rows, consecutive GPU completions, and zero visible trial windows or activations. [Full-raid A/B and lock validation](results/raid-winemetalgl-fast-share-lock-ab.json)

### Measure CPU pipeline limits on the optimized raid baseline

The live client ran the full 24-caster raid with the native-width index upload, guest autorelease pool, and fast share lock enabled. Instruments recorded a separate CPU Counters trace after the world readback and fixture checks. The main thread ran for **20.57 of 20.87 seconds** (**98.55%**), including **20.20 seconds** attributed to performance cores. The blocked state accounted for **106 ms**. The headless client kept the saved camera and all 26 cast timings, delivered consecutive GPU completions, and recorded zero window activations.

| Main-thread CPU Counters category | Cycle-weighted share of sustainable instruction bandwidth |
| --- | ---: |
| Useful | 33.10% |
| Instruction processing bottleneck | 30.80% |
| Instruction delivery bottleneck | 28.48% |
| Discarded | 7.05% |

The estimate weights each complete interval by its recorded core cycles. The **44,497** nonoverlapping intervals cover **20.38 seconds** of main-thread execution. Categories sum to **99.44%** in the export. They describe instruction bandwidth. This mode leaves instruction-cache misses, data-cache misses, dependency stalls, and branch behavior unresolved within their respective categories. The concurrent GPU window includes profiling overhead and serves as a workload check. [Raid trace summary](results/raid-winemetalgl-fast-share-lock-cpu-trace.json) · [Apple's CPU Counters guide](https://developer.apple.com/documentation/xcode/addressing-cpu-bottlenecks)

Previous guest-stack captures attributed **41%** of sampled CPU time to the two call trees rooted at `0x73f8e0` and `0x97dbc0`. A serial model starting at 136 FPS estimates about **171 FPS** with a twofold speedup across both trees and **196 FPS** with a fourfold speedup, assuming the same CPU share and all other frame costs fixed. The model predicts average throughput only. It gives a scale for testing function-level ARM64 optimizations across call boundaries. [Guest stack captures](results/raid-aarchx-headless-i386-inclusive-stack-profile.json) · [Uninstrumented throughput baseline](results/raid-winemetalgl-fast-share-lock-ab.json)

Attach to the isolated client PID after the fixture check and export the two tables separately. The published summary carries timings and counts from the target process. Trace bundles and full exports stay local.

```sh
xcrun xctrace record --no-prompt --template 'CPU Counters' --attach "$pid" \
    --time-limit 20s --output cpu.trace
xcrun xctrace export --input cpu.trace \
    --xpath '/trace-toc/run[@number="1"]/data/table[@schema="thread-state"]' \
    --output thread-state.xml
xcrun xctrace export --input cpu.trace \
    --xpath '/trace-toc/run[@number="1"]/data/table[@schema="MetricTableForThread"]' \
    --output thread-metrics.xml
python3 benchmarks/summarize-raid-cpu-trace.py thread-state.xml thread-metrics.xml "$pid"
```

### Separate instruction-fetch and data-cache limits

Two more isolated launches of the same optimized 24-caster raid used the detailed processing and delivery modes of CPU Counters. Each mode captured over **20 seconds** of complete main-thread intervals on performance cores. Both launches verified the saved camera, visible spell effects, all 26 timing rows, and consecutive 1365 × 768 GPU completions with no visible windows or activations.

| Instruction Processing Bottlenecks category | Share of counted cycles |
| --- | ---: |
| Executing | 63.32% |
| Critical L1D miss with no micro-ops executing | 18.14% |
| Critical L1D miss while micro-ops execute | 10.09% |
| Execution latency | 7.33% |
| Non-critical L1D miss | 1.13% |

| Instruction Delivery Bottlenecks category | Share of sustainable instruction bandwidth |
| --- | ---: |
| L1 instruction-cache miss | 15.48% |
| Instruction delivery bandwidth | 7.56% |
| Other instruction delivery latency | 5.26% |
| Instruction TLB miss | 0.34% |

Critical data misses took place in **28.23%** of the processing-mode cycles. That includes cycles in which other micro-ops progressed. Instruction-cache misses made up the largest measured delivery loss. The two modes use different denominators, and the main thread runs both translated game code and native renderer code. These traces measure aggregate costs. They leave the responsible instruction addresses and the effect of any proposed code change to separate measurements. [Detailed main-thread counters](results/raid-winemetalgl-fast-share-lock-counter-breakdown.json) · [Apple's CPU Counters guide](https://developer.apple.com/documentation/xcode/addressing-cpu-bottlenecks)

To reproduce either mode, save the options returned by `xcrun xctrace record --template 'CPU Counters' --show-recording-options`. In the `CPU Counters` object, set `selectedCountingMode.countingMode` to `processing` or `delivery` and set the corresponding `selectedCountingModeDisplayName` from the tables. Keep `selectedCountingMode.analysisMode` set to `bottleneck` and `countingLevel` set to `EL0`. Attach before the diagnostic GPU window ends and export `MetricTableForThread` as above.

```sh
xcrun xctrace record --no-prompt --template 'CPU Counters' --attach "$pid" \
    --recording-options recording-options.json --time-limit 20s --output cpu.trace
python3 benchmarks/summarize-raid-cpu-trace.py thread-metrics.xml "$pid" --mode processing
```

### Locate data-cache miss samples and copy callers

An isolated **12-second** L1D Cache Miss Sampling recording used the same live raid. Of **113,620** sampled main-thread store misses, **78,185 (68.81%)** had their sampled PC in `_platform_memmove`. It also accounted for **25,654 of 130,336** load-miss samples. These counts describe sampled PCs. The exported miss samples at that symbol carry no caller frame.

The separate Time Profiler table in the same trace assigned **464 ms of 12,417 ms** of main-thread running-sample weight to `_platform_memmove`. Its two largest direct callers were AArchX's `sp_glUnmapBuffer` (**114 ms**) and `map_common` (**110 ms**). The next three were WineMetalGL's `bindUniforms`, `uniformBlockBuffer`, and `issue` (**64, 62, and 50 ms**). The two AArchX copy sites together represent **1.80%** of running-sample weight. The time profile identifies recurring copies. The miss samples leave the per-caller cache cost unresolved. The client verified the saved camera, 26 timing rows, and consecutive 1365 × 768 GPU completions with zero visible windows or activations. [Miss samples and copy callers](results/raid-winemetalgl-fast-share-lock-l1d-samples.json)

Use the same CPU Counters options with `selectedCountingMode.countingMode` set to `l1d_miss_sampling` and `selectedCountingModeDisplayName` set to `L1D Cache Miss Sampling`. Export the sampling and time-profile tables from that recording.

```sh
xcrun xctrace export --input cpu.trace \
    --xpath '/trace-toc/run[@number="1"]/data/table[@schema="SamplingModeSamples"]' \
    --output l1d-samples.xml
xcrun xctrace export --input cpu.trace \
    --xpath '/trace-toc/run[@number="1"]/data/table[@schema="time-profile"]' \
    --output time-profile.xml
python3 benchmarks/summarize-raid-cpu-trace.py l1d-samples.xml "$pid" \
    --mode l1d_miss_sampling --time-profile time-profile.xml
```

### Reuse evicted mapped-buffer shadow allocations

The existing AArchX option lets an unmapped shadow slot donate its guest allocation when a different buffer takes the slot. The new buffer starts with no valid mirror, so the fallback path still copies its own bytes. The allocation must fit the request with at most 64 KiB of excess capacity. The renderer-owned guest storage for the large array and unpack buffers remained enabled in both modes.

In the control diagnostic, **1,671,168** post-readback fallback maps made one allocation each. The reuse diagnostic made **11,945** allocations across **1,724,416** maps, about **6.9 allocations per 1,000 maps**. Those intervals span the first and last complete counter rows after the world readback. The incoming copies were all write-only array-buffer maps. They averaged **5.41 KB per fallback map** in control and **5.30 KB** with reuse. The logged runs bound the remaining copy cost and were collected separately from the comparisons.

| Shadow allocation policy | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| Allocate on eviction | 134.75 | 12.71 ms |
| Reuse a fitting allocation | 137.81 | 12.50 ms |

Throughput rose **2.18 and 3.93 FPS** in the two pairs. P99 fell **0.18 and 0.22 ms**. All four 60-second windows verified the saved camera, effects, 26 timing rows, consecutive GPU completions, and zero test windows or activations. The process guard covered the copied fixture. An older client from another bundle was observed running and its GPU load was unmeasured. Both modes passed **806** general i386 checks and **110** offscreen WineMetalGL storage checks. [Timed runs and allocation counters](results/raid-aarchx-map-shadow-reuse-ab.json)

Set `OCERZ_M32_REUSE_GL_SHADOW_STORAGE=1` for follow-up comparisons and leave it unset for a control. Enable `OCERZ_M32_GL_MAP_STATS=1` only for the separate diagnostic captures. Both switches are enabled by presence. The analyzer expects logged control and reuse captures, followed by the directory containing the timed captures `1-0`, `2-1`, `3-1`, and `4-0`.

```sh
python3 benchmarks/summarize-raid-shadow-reuse.py LOGGED_CONTROL LOGGED_REUSE TIMED_RUNS
```

### Allocate one-page Metal buffer storage in guest memory

The trial extends the existing buffer allocator to requests for one host page, **16 KiB** on this Mac. WineMetalGL still determines which allocations reach the provider and rounds each eligible request before calling it. The new selection applies to all one-page requests through that provider. Both arms enable the earlier 2 MiB and 8 MiB policies and copied-shadow allocation reuse. The renderer still owns the storage lifetime.

The offscreen tests drew from a 4 KiB mapped vertex buffer, checked its last mapped word, and verified that a buffer four bytes larger than a page uses the copied path. Both modes passed **806** general i386 checks and **128** WineMetalGL storage checks. With only the one-page policy enabled, the storage tests passed **113** checks.

In separate logged runs, the fallback map count after world readback fell from **1.69–1.70 million** to **147,457–155,647**. The ranges account for the counter reports every 4,096 fallback maps. Incoming copies between complete reports fell from **9.10 GB** to **6.87 GB**. The resident-memory sample after the 25-second GPU window rose from **482 to 595 MiB**. These are separate diagnostic launches with their own world readback and counter bounds.

| Small-buffer storage | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| Copied shadow | 135.99 | 12.84 ms |
| One-page guest storage | 131.28 | 13.20 ms |

Throughput fell **3.05 and 6.37 FPS** across the two pairs. P99 rose **0.40 and 0.33 ms**. The benchmark retains copied shadows for these buffers. Each of the four warmed 60-second runs verified the saved camera, spell effects, 26 timing rows, and consecutive GPU completions at 1365 × 768. The trial monitor recorded zero windows or activations. The copied client folder was isolated. A legacy instance from another bundle was observed running and its GPU load was unmeasured. [Timed A/B and fallback counters](results/raid-aarchx-one-page-guest-storage-ab.json)

Separate ten-second Time Profiler recordings located the added CPU work. The one-page arm spent **4.62%** of main-thread running-sample weight directly in `m32_wmgl_guest_buffer`. All **477 ms** of its leaf weight resolved to the buffer-list loop. The control recorded zero leaf weight there. Across the same two samples, the share directly in `_platform_memmove` fell from **3.10%** to **2.65%**. The profile supports testing address-indexed lookup while keeping the allocation policy fixed. CPU sample shares describe the profiled launches. [Native CPU profiles](results/raid-aarchx-one-page-guest-storage-cpu-profile.json)

Apply `patches/aarchx-wmgl-small-guest-storage-trial.patch` after the AArchX guest autorelease pool trial. Use `OCERZ_WMGL_GUEST_SMALL_STORAGE=1` for the candidate on the same rebuilt binary. The comparison keeps `OCERZ_M32_REUSE_GL_SHADOW_STORAGE=1` in both arms, and captures map counters only in the two diagnostic launches.

```sh
python3 benchmarks/summarize-raid-shadow-reuse.py LOGGED_CONTROL LOGGED_ONE_PAGE TIMED_RUNS \
    --small-storage
```

For the CPU profile, attach with the Time Profiler template and export its `time-profile` table. Count leaf and inclusive sample weight for each requested symbol on the main thread.

```sh
python3 benchmarks/summarize-raid-cpu-trace.py time-profile.xml "$pid" --mode functions \
    --symbol m32_wmgl_guest_buffer --symbol _platform_memmove
```

### Index one-page guest buffer storage by address

The trial keeps one registration byte for each possible 64 KiB-aligned guest address below the i386 handle area. An eligible map checks the requested extent within the host page and reads its registration byte. The release callback clears that byte before freeing the storage. The existing list still covers larger allocations and allocations outside the address index's alignment requirements.

Apply `patches/aarchx-wmgl-small-page-index-trial.patch` after the one-page guest-storage trial. Set `OCERZ_WMGL_GUEST_SMALL_PAGE_INDEX=1` with `OCERZ_WMGL_GUEST_SMALL_STORAGE=1` to select the index. The list arm keeps the same storage policy and sets the index flag to zero. The copied-shadow reference sets the storage flag to zero and keeps the index flag at one. The two large-storage options and shadow-allocation reuse are enabled in every arm.

All three modes passed **806** general i386 checks. The offscreen storage suite passed **128** checks in the reference and **143** each with the list and index. The latter cases check the last byte, a request crossing the page boundary, and removal after the renderer releases a buffer with no pending GPU references. Running the index with the two larger-storage flags cleared passed **128** storage checks.

| Buffer policy | Median GPU-completed FPS | Median p99 completion gap | Median sampled RSS |
| --- | ---: | ---: | ---: |
| Copied shadow | 137.57 | 12.56 ms | 408.20 MiB |
| One-page guest storage, list lookup | 131.48 | 12.94 ms | 436.09 MiB |
| One-page guest storage, address index | 136.63 | 12.78 ms | 422.20 MiB |

The index gained **4.88 and 5.40 FPS** against the list and reduced p99 by **0.11 and 0.21 ms**. Against the copied-shadow reference it lost **0.77 and 1.12 FPS** and increased p99 by **0.12 and 0.33 ms**. Keep the copied-shadow reference for follow-up comparisons. All six warmed 60-second windows verified the saved camera, spell effects, 26 timing rows, and consecutive GPU completions at 1365 × 768. The trial recorded zero visible windows or activations. The copied client folder was isolated. A separate legacy client was observed running during the comparisons and its GPU load was unmeasured. [Six timed windows and paired differences](results/raid-aarchx-one-page-address-index-ab.json)

Separate ten-second Time Profiler captures kept one-page storage enabled in both modes. The list capture assigned **305 ms of 10,418 ms** of main-thread running-sample weight to `m32_wmgl_guest_buffer` itself (**2.93%**). The indexed capture assigned zero leaf samples to that function in **10,323 ms** of main-thread running-sample weight. [Lookup profiles](results/raid-aarchx-one-page-address-index-cpu-profile.json)

```sh
python3 benchmarks/summarize-raid-shadow-reuse.py TIMED_RUNS --indexed-storage
```

### Recheck the CPU and GPU tail on the copied-shadow baseline

The current baseline combines native-width client indices, fast share locking, guest autorelease-pool borrowing, and shadow allocation reuse. A Metal System Trace with Thread Activity matched **150** final GPU endpoints within **42 ns**. Those sparse endpoints align the clocks. The separate application submission log and the recorded scheduler states cover **2,745 consecutive completion intervals** over **21.28 seconds**. The analysis uses this capture for CPU scheduling and command-buffer timing.

The main thread ran a median **7.63 ms** between final-buffer submissions. Running time accounted for **98.94%** of the total interval duration. In the slowest **28** completion intervals, the largest component increase over its typical median came from the final GPU span **17** times, main-thread running time **nine** times, and time when the main thread was off CPU **twice**.

A separate Metal System Trace without Thread Activity matched **1,845 consecutive outputs** over **14.54 seconds**. The accelerator rows are clipped to consecutive completion gaps. Stage and process activity can overlap.

| Median in traced completion gaps | All 1,844 gaps | Slowest 19 gaps |
| --- | ---: | ---: |
| Completion gap | 7.83 ms | 14.40 ms |
| Client GPU active union | 1.99 ms | 7.93 ms |
| Client vertex activity | 0.55 ms | 5.96 ms |
| Client fragment activity | 1.28 ms | 3.83 ms |
| Client activity overlapping external GPU activity | 0.57 ms | 7.21 ms |
| Draws in the completed output | 825 | 881 |

The older client recorded activity in **668 of 1,844** intervals, including **15 of 19** in the tail. Background activity and client vertex activity both rose in the tail. Measuring them together leaves their causal contributions open. Both launches used the accepted runtime and renderer, checked the saved camera, effects, and 26 timing rows, and recorded zero trial windows or activations. This instrumentation was separate from the throughput comparisons. [Scheduler and accelerator results](results/raid-winemetalgl-current-cpu-gpu-tail.json)

Enable `OCERZ_WMGL_SUBMISSION_LOG` alongside the existing verified GPU log in each launch. Attach with the Metal System Trace template and add Thread Activity for the scheduler capture. Export `metal-gpu-intervals` and, for the scheduler capture, `thread-state`. Pass the separately observed legacy PID to track its GPU activity.

```sh
python3 benchmarks/summarize-raid-system-tail.py SCHEDULER_CAPTURE gpu-intervals.xml \
    --scheduler-window --thread-states thread-states.xml --legacy-pid "$legacy_pid"
python3 benchmarks/summarize-raid-system-tail.py ACCELERATOR_CAPTURE gpu-intervals.xml \
    --legacy-pid "$legacy_pid"
```

### Isolate the older test client's background GPU load

The background process belonged to the earlier uncapped raid benchmark. Its executable, redirected output, and completion stream identified the same temporary test instance. It was offscreen and had no established TCP connection at the preflight check. For the paused arms, the runner suspended only that process, allowed two seconds for queued work to drain, and resumed it after each launch. The current copied client, runtime, renderer, shadow reuse, and raid settings were shared across all arms. [Earlier uncapped reference](results/raid-winemetalgl-uncapped-live.json)

The timed run order is `1-running`, `2-paused`, `3-paused`, and `4-running`. Each directory carries a legacy completion delta from the full monitored launch and a verified 60-second throughput window for the current client. The analyzer also checks the fixture marker, consecutive completions, window monitor result, and restored state.

| Earlier test process | Median current-client FPS | Median current-client p99 completion gap |
| --- | ---: | ---: |
| Running | 134.28 | 12.94 ms |
| Paused | 134.88 | 12.70 ms |

The older client completed **2,675 outputs** during each running launch and **zero** during each paused launch. Pausing reduced p99 by **0.20 and 0.26 ms** in the two pairs. Throughput changed by **−1.42 and +2.62 FPS**. The saved camera and all 26 timing rows verified in all four runs. The readbacks showed overlapping effects, the GPU completion sequences were consecutive, and the test monitor recorded zero windows or activations. The earlier instance resumed with the same process identity. [Timed A/B and legacy completion witnesses](results/raid-winemetalgl-legacy-load-ab.json)

Two separate 14-second Metal System Traces matched **1,771** consecutive outputs with the old test running and **1,841** with it paused. The trace with the older test paused contained zero GPU activity attributed to it. Client vertex activity in the slowest one percent still had a **5.93 ms** median, compared with **0.56 ms** across the capture. The median overlap with other GPU activity in that tail was **6.94 ms**. The measurements describe the tail in each trace. The paired minute windows quantify the effect on live throughput. [Matched GPU traces](results/raid-winemetalgl-legacy-load-gpu-traces.json)

Use the Metal System Trace template and the same submission log for each state. After exporting `metal-gpu-intervals`, run `summarize-raid-system-tail.py` on each capture with the older test's PID.

```sh
python3 benchmarks/summarize-raid-legacy-load.py TIMED_CAPTURES
```

### Reuse the mapped address of deep i386 frame slots

The trial can keep a second mapped EBP address, biased by **−512 bytes**, through a stable block prefix. It brings guest displacements **−768 to −257** into the same signed-immediate range as near accesses. Near accesses keep the original address. A selected block needs at least two deep operands and six eligible operands in total. The widened guard checks that all covered starting addresses stay within 32 bits and uses the existing retirement path on failure.

Apply `patches/aarchx-m32-deep-ebp-hoist-trial.patch` after the i386 general base hoist patch. Set `OCERZ_M32_DEEP_EBP_HOIST=1` for the candidate, with the existing general base selection enabled and the translation cache disabled. Add `OCERZ_M32_DEEP_EBP_HOIST_LOG=1` for the separate compile witness. In Wine's register layout, the register reserved for the Wine address-limit check keeps that role.

Both modes passed **806** general i386 checks, **14** earlier address cases, and five new focused interpreter comparisons. The new comparisons cover the deepest and nearest signed offsets, a dirty upper EBP half, both 32-bit address bounds, writes to EBP, and SSE batching. Both boundary cases recorded one guard exit in the candidate and reached the same final state as the interpreter. The candidate passed all five cases with forced branch veneers. The forced replay check passed with a recorded eight-instruction FP batch.

A separate in-world launch logged **412** distinct selected prefixes, including **five** emitted after the last world readback. All prefix entries matched the original i386 text. The deep displacement shape represented **1.83%** of the CPU weight in the two earlier stack captures. The shape inside the newly selected static prefixes represented **1.00%**. These numbers describe sampled instruction coverage across different launches.

| Guarded address policy | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| Existing guard and mapped base | 134.85 | 12.87 ms |
| Additional biased address for deep EBP slots | 133.88 | 12.74 ms |

The trial lost **1.62 and 0.32 FPS**. P99 fell **0.26 ms** in the first pair and rose **0.01 ms** in the reverse pair. The benchmark keeps the existing guard and mapped base. The older trial was suspended during all four 60-second launches and the logging run and resumed between launches. All runs verified the saved camera, spell effects, 26 timing rows, consecutive GPU completions, and zero visible trial windows or focus activations. [Full-raid measurements and coverage](results/raid-aarchx-headless-i386-deep-ebp-hoist-ab.json)

```sh
python3 benchmarks/summarize-raid-deep-ebp.py CAPTURE_ROOT I386_CLIENT EARLIER_STACK_RUN_1 EARLIER_STACK_RUN_2
```
