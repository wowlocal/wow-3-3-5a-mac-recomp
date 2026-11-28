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

### Match sampled ARM64 instructions to the hot i386 call trees

The stack profiler can record the ARM64 PC and instruction word during the same suspension that captures the i386 leaf and saved return addresses. This keeps the host instruction and call-tree attribution on the same CPU-time-weighted sample. The analyzer validates the unwind chain against decoded i386 call sites. It decodes the sampled host words locally and counts 16 KiB host-code pages. The page counts describe sampled code placement.

Apply `patches/aarchx-m32-stack-arm64-profile-trial.patch` after the i386 guest-stack profiler. Use `OCERZ_GUESTPROF=2500`, `OCERZ_GUESTPROF_PERIOD=4`, `OCERZ_GUESTPROF_HOT=1`, and `OCERZ_GUESTPROF_I386_ARM64_LOG` pointing to a fresh `i386-arm64.ns`. The original guest-stack log format remains supported. Both recordings use the accepted optimization settings and suspend the older test during capture. The patched runtime passed **806** general i386 checks with the profiler off.

The two verified 60-second windows contained **7,790** and **7,837** consecutive GPU completions. They kept the saved camera, overlapping spell effects, all 26 timing rows, and zero trial windows or activations. The profiler selected one distinct guest thread after world readback in each run. Instruction and page shares describe profiling samples.

| Weighted sample measure | First capture | Second capture |
| --- | ---: | ---: |
| Translated i386 code, share of all sampled CPU | 66.32% | 66.67% |
| Either call tree rooted at `0x73f8e0` or `0x97dbc0`, share of all sampled CPU | 43.59% | 43.20% |
| Immediate 32-bit ARM64 ADD/SUB mapped to guest memory MOVs, share of all sampled CPU | 10.15% | 10.60% |
| Same ADD/SUB forms within either selected call tree, share of all sampled CPU | 6.82% | 7.14% |
| Host code pages covering 80% of translated CPU weight | 148 | 150 |

The largest address band was base-only EBP with near displacements of **−256 to 255 bytes**. In the full sample, its immediate ADD/SUB forms accounted for **3.78% and 4.16%** of CPU weight. The deep negative EBP band accounted for **0.67% and 0.73%**. EAX, EBX, and ECX also appeared among the leading near-displacement forms. The same recordings assigned **1.83% and 1.72%** to conditional branches loading `jit_fcmp_mem`, verified at offset 5,176 from the CPU state pointer. The data supports targeting common address calculations across the two call trees and measuring their generated-code placement. [Combined guest stacks and ARM64 attribution](results/raid-aarchx-native-instructions-in-guest-call-trees.json)

```sh
python3 benchmarks/summarize-raid-guest-native-stacks.py I386_CLIENT CAPTURE_1 CAPTURE_2 \
    --roots 0x73f8e0 0x97dbc0
```

### Carry the guarded EBP address into short blocks

The earlier handoff requires both blocks to qualify for standalone mapped-base reuse. This trial also admits a target with **two to five** eligible EBP operands. Admission requires an already pending direct link from an i386 block that preserves the same guarded EBP address and uses the same pinned register layout. That link enters after the target's setup and guard. Other entry routes use the ordinary guard. An EBP write ends the eligible prefix and prevents the block from forwarding the old address.

Apply `patches/aarchx-m32-short-ebp-handoff-trial.patch` after the original mapped-base handoff patch. Keep general base selection enabled and the translation cache off. The reference leaves handoff unset. The long mode sets `OCERZ_M32_BASE_HANDOFF=1`. The short mode also sets `OCERZ_M32_SHORT_EBP_HANDOFF=1`. Logging and the address witness are enabled only in the diagnostic launches.

All modes passed **806** general i386 checks and seven interpreter comparisons. The focused short-path cases witness an actual linked entry, stop the optimized prefix at an EBP write, and reject short admission after a predecessor changes EBP. All three passed with forced branch veneers.

The diagnostics counted **81** distinct linked target and base pairs in long mode and **340** in short mode. Short mode admitted **245** short blocks and every one had a witnessed linked entry. After the last world readback, the counts were **six** and **17** linked target and base pairs. All 245 short entries matched the original client text. The earlier paired guest and host profiles put **0.37% and 0.34%** of sampled CPU weight in matching immediate ARM64 address arithmetic inside those linked prefixes. Link counts describe compilation and linking. The CPU shares describe coverage in the earlier profiles.

| Mapped address handoff | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| Disabled | 133.82 | 12.71 ms |
| Long blocks | 134.70 | 12.54 ms |
| Long and eligible short blocks | 134.42 | 12.44 ms |

Short mode lost **0.28 FPS** against long mode in both pairs while p99 fell **0.03 and 0.18 ms**. Against the reference, short mode lost **0.37 FPS** in the first pair and gained **1.57 FPS** in the reverse pair. Its p99 rose **0.06 ms** and fell **0.61 ms**, respectively. Keep handoff disabled in the benchmark baseline. All six 60-second windows verified the saved camera, effects, 26 timing rows, consecutive GPU completions and zero visible trial windows or focus activations. The earlier test was paused during each launch and resumed afterward. [Live comparisons and CPU coverage](results/raid-aarchx-i386-short-ebp-handoff-ab.json)

```sh
python3 benchmarks/summarize-raid-short-ebp.py CAPTURE_ROOT I386_CLIENT PRIOR_STACK_CAPTURE_1 PRIOR_STACK_CAPTURE_2
```

### Pack the profiled i386 hot blocks before lazy translation

The trial records original guest block entries and ARM64 PCs on CPU-time-weighted samples. A training launch ranks entries within the original i386 executable's text by sampled CPU time. A subsequent launch pretranslates up to 1,024 ordered entries in one arena span after its first dispatcher miss inside that text. It accepts readable, executable guest addresses. The rest of the program still translates on demand.

Apply `patches/aarchx-m32-hot-code-order-trial.patch` after the combined i386 stack and ARM64 profiler patch. On the training launch, enable the guest profiler and set `OCERZ_GUESTPROF_I386_BLOCK_LOG` to a fresh `i386-blocks.ns`. Generate the ordered list from that capture. On the candidate launch, set `OCERZ_M32_HOT_ORDER` to the list with the translation cache disabled. Pretranslation uses generic guest-state metadata. The comparison measures that complete pretranslation policy.

The runtime passed **806** general i386 checks. The focused i386 fixture matched the interpreter with the order absent and present. The ordered run prepared the two executable blocks and skipped a listed data address. A malformed list produced a diagnostic and the fixture still matched the interpreter.

The training capture sampled **3,882** client blocks. The selected **1,024** accounted for **74.43%** of sampled CPU time within translated client code. Each ordered launch translated all 1,024 blocks before lazy dispatch, taking **6.51–6.98 ms** after reading the list and spanning about **704 KiB** of the arena. The paired placement profiles shared **885** sampled seed blocks. The number of 16 KiB pages covering 80% of their weight fell from **64 to 18**. Over all sampled client code it fell from **142 to 110**. These are sampled page counts.

| Translation policy | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| Lazy | 133.88 | 12.77 ms |
| Hot order | 133.88 | 12.55 ms |

Throughput changed by **+0.80 and −0.78 FPS** in the two pairs. P99 improved by **0.26 and 0.17 ms**. Keep lazy translation as the baseline and retain the ordered launcher as an experiment. All four 60-second windows and the two block profiles checked the saved camera, overlapping effects, 26 timing rows and consecutive GPU completions. The captures stayed offscreen while the older trial was suspended. [Live measurements and sampled-page counts](results/raid-aarchx-i386-hot-code-order-ab.json)

Separate 12-second CPU Counters recordings used the Instruction Delivery Bottlenecks mode with one capture per translation policy. Instruction-cache misses accounted for **15.78%** of sustainable instruction bandwidth in the lazy capture and **16.53%** with hot ordering. The denser pages and these measured delivery costs can vary independently. These two captures describe the sampled intervals. The four minute windows provide the throughput comparison. [Instruction delivery counters](results/raid-aarchx-i386-hot-code-order-delivery.json)

```sh
python3 benchmarks/summarize-raid-hot-layout.py CAPTURE_ROOT I386_CLIENT --prepare
python3 benchmarks/summarize-raid-hot-layout.py CAPTURE_ROOT I386_CLIENT
```

### Group contemporaneous blocks within the hot-code order

Both modes pretranslate the same **1,024** i386 blocks on the same runtime. The trial starts with the frequency order and rearranges blocks within each band of **64** ranks. An edge gains weight when two blocks have samples within **12 ms**, with more weight for closer samples. Two greedy swap passes reduce the weighted distances, capped at 64 ranks, while preserving the ordering between bands.

The planning score is in list positions. In the training capture, the share of proximity weight within 32 ranks rose from **15.42% to 20.85%**. In the earlier packed-profile capture it rose from **16.76% to 19.21%**. The earlier packed-profile capture helped select the restriction to bands after unrestricted chaining fared worse in that capture. The individual swaps use the training capture.

| Hot-block order | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| Frequency | 134.08 | 12.51 ms |
| Temporal affinity | 132.78 | 12.98 ms |

The reordered variant moved **798** blocks to another rank and pretranslated all **1,024**. It lost **1.20 and 1.42 FPS**, and p99 rose about **0.47 ms** in each pair. The four 60-second windows verified the same saved camera, overlapping effects, 26 timing rows, consecutive GPU completions and zero trial windows or activations. The earlier test process remained paused during the measurements and resumed between launches.

One 12-second Instruction Delivery Bottlenecks trace per mode covered the main thread on performance cores. Instruction-cache misses accounted for **16.66%** of sustainable instruction bandwidth in the frequency capture and **15.96%** in the affinity capture. These are diagnostic samples. The minute-long pairs favor the previous frequency order for pretranslation experiments. Lazy translation remains the baseline. [Timed A/B, order scores, and instruction delivery counters](results/raid-aarchx-i386-hot-affinity-ab.json)

```sh
python3 benchmarks/summarize-raid-hot-affinity.py CAPTURE_ROOT --prepare-from EARLIER_HOT_ORDER_ROOT
python3 benchmarks/summarize-raid-hot-affinity.py CAPTURE_ROOT
```

### Compare a loaded memory operand before the adjacent branch

The ordinary fusion path loads the memory operand at the i386 COMIS instruction and saves it to the CPU state. The conditional branch reloads that operand and emits `FCMP`. The trial recognizes a COMIS immediately followed by the terminal branch of the same block. When the old flags are dead outside that pairing and the emitter has inserted no host instructions between the operand save and the new compare site, it replaces the save with `FCMP` and consumes those flags at the branch. The memory read stays at the compare's guest instruction. Floating-point batches and unrelated branch paths use their existing handling.

Apply `patches/aarchx-m32-adjacent-memory-comis-trial.patch` to the accepted runtime. Set `OCERZ_M32_COMIS_ADJACENT=1` for the trial and `OCERZ_M32_COMIS_ADJACENT_LOG=1` only for the compile witness. Both modes passed **806** general i386 checks and seven focused interpreter comparisons. The five eligible cases exercised float and double compares, ordered inputs, and NaN with equality and inequality branches. Intervening code and live flags prevented the rewrite in the other two cases. The enabled mode also passed all seven with forced branch veneers and passed six earlier FP-batch replay checks.

The logging launch found **425** distinct rewritten producers, including **ten** emitted after the world readback. All matched adjacent branch pairs in the original client text. Matching operand stores and loads at offset **5,176** in the two earlier ARM64 profiles covered **0.50% and 0.47%** of CPU sample weight. This maps the actual emitted pairs to earlier samples.

| Memory compare policy | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| Save and reload the operand | 134.59 | 12.55 ms |
| Compare at the producer for an adjacent branch | 134.76 | 12.76 ms |

The trial changed throughput by **+0.52 and −0.18 FPS**. P99 increased by **0.19 and 0.23 ms**. Keep the ordinary operand path as the baseline. All four minute windows retained the saved camera, overlapping effects, 26 timing rows, consecutive GPU completions, and zero visible trial windows or activations. The older test was paused during all launches and resumed between them. [Live comparison and CPU coverage](results/raid-aarchx-i386-adjacent-memory-comis-ab.json)

```sh
python3 benchmarks/summarize-raid-adjacent-comis.py CAPTURE_ROOT I386_CLIENT PRIOR_STACK_CAPTURE_1 PRIOR_STACK_CAPTURE_2
```

### Defer tracked Metal commits to a worker

The experiment reserves each tracked command buffer's position with `enqueue()` on the calling thread and runs `commit()` on a serial dispatch queue. Buffer encoding, prelude commits, serial tracking, callbacks, and waits remain in their original paths. Both modes use the same renderer build. Metal executes buffers in their reserved queue order. [Metal enqueue documentation](https://developer.apple.com/documentation/metal/mtlcommandbuffer/enqueue%28%29)

Apply `patches/winemetalgl-deferred-metal-commit-trial.patch` after the completed-serial prefix patch. Set `WINEMETALGL_DEFER_METAL_COMMIT=1` for the worker path. Both modes passed **343** GPU tests with the fast share lock, completed-prefix query, and native-width client indices enabled. For separate diagnostics, set `OCERZ_WMGL_COMMIT_LOG` to a fresh `commits.ns`. The analyzer joins main and worker calls by queue, serial, and start timestamp. The submission-time total covers calls inside the verified output time window and is divided by that window's verified output count.

Separate 25-second diagnostics measured **6,704** inline submissions and **6,691** deferred submissions. Caller-side submission wall time was **11.86 µs per verified output** inline and **5.36 µs** with the worker. In deferred mode, worker start followed main submission start by **12.21 µs** at the median and **34.96 µs** at p99. The worker's median `commit()` call took **5.67 µs**.

| Tracked Metal submission | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| Inline commit | 135.10 | 12.44 ms |
| Deferred commit | 134.18 | 12.83 ms |

Throughput fell **1.32 and 0.52 FPS**. P99 rose **0.56 and 0.22 ms** in the two pairs. Keep inline commits as the baseline. The four 60-second windows and separate diagnostic launches verified the camera, overlapping effects, all 26 timing rows, consecutive GPU completions and zero visible trial windows or activations. The older test was suspended during each launch and resumed afterward. [Live A/B and submission timing distributions](results/raid-winemetalgl-deferred-metal-commit-ab.json)

```sh
python3 benchmarks/summarize-raid-deferred-commit.py CAPTURE_ROOT
```

### Cache the checked i386 EBP address across linked blocks

The trial keeps the last guarded mapped EBP address in the callee-saved register x29 in the eligible i386 register layout. Full entries clear the register. Linked blocks can carry the value through code with no eligible EBP accesses. A block that selects the existing guarded EBP prefix compares its newly computed mapped address against the cached address. A match skips the range check. A mismatch branches to an out-of-line copy of the original check, then caches the address if the guard succeeds.

Apply `patches/aarchx-m32-ebp-guard-cache-trial.patch` after the general base hoist. Use `OCERZ_M32_EBP_GUARD_CACHE=1` with general base selection enabled and the translation cache off. Only the separate diagnostic uses `OCERZ_M32_EBP_GUARD_CACHE_COUNTS` to record two 64-bit counters on the local LSE-capable host.

Both modes passed **806** general i386 checks, eight earlier EBP cases and two new interpreter comparisons. With counters enabled, the plain-intervening-block test recorded **four hits and two misses**. Changing EBP in the intervening block produced **six misses** and the same end state as the interpreter.

Both new cases also matched the interpreter with forced branch veneers. In the separate raid diagnostic, the observer read **79,603,267 hits and 41,696,378 misses** (**65.63%** hits) between its world-ready and GPU-window snapshots. Those snapshots were **9.82 s** apart. The four timed runs used the counter off.

| Guard policy | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| Check on block entry | 135.86 | 12.46 ms |
| Cache the checked address | 133.17 | 12.88 ms |

The cache lost **3.38 and 2.00 FPS** in the two pairs. P99 lengthened by **0.59 and 0.26 ms**. Keep the cache disabled in the benchmark baseline. Each 60-second comparison verified the saved camera, overlapping effects, all 26 timing rows, consecutive GPU completions, and zero visible windows or activations. The older test was paused during the launches and resumed afterward. [Live comparison and guard counts](results/raid-aarchx-i386-ebp-guard-cache-ab.json)

```sh
python3 benchmarks/summarize-raid-ebp-guard-cache.py CAPTURE_ROOT
```

### Estimate the gains available from the largest CPU work

The two hot i386 call trees together covered **43.20–43.59%** of sampled CPU time. Their root instructions had little exclusive weight. Summing samples by original instruction address put two conspicuous scalar matrix-copy sequences, at `0x97cfba–0x97d06c` and `0x97e3e0–0x97e44d`, below **0.02%** in each capture. The largest confirmed exclusive callee, `0x880c70`, covered **4.55–4.84%**. Recompiling the call trees as regions would only be valuable if it changes how much work their callees perform or significantly reduces their per-item cost. At 136 FPS, a serial model with fixed other costs yields about **174 FPS** for a twofold speedup across both trees. [CPU profiles](results/raid-aarchx-native-instructions-in-guest-call-trees.json)

Prioritize attributing the hot trees' CPU work to repeated inputs and outputs within each frame. Use that evidence to select one self-contained per-item loop for native batching, or to identify equivalent work that can safely be reused. Compare complete output buffers and frame readbacks with the original path using the same inputs. For the GPU tail, inspect the draw groups active during slow frames while keeping the original ordering, camera, and effects. With the older client paused, median vertex activity rose from **0.56 ms** across all completion gaps to **5.93 ms** across the slowest one percent. This capture makes GPU tail work a separate requirement for the 2 ms goal. [Isolated GPU trace](results/raid-winemetalgl-legacy-load-gpu-traces.json)

### Check bounds reduction and position reuse in the hot quad emitter

The diagnostic records three incoming coordinate values and the six bounds words before and after each of four ordered compare-and-store groups inside `0x880c70`. The surrounding code writes the coordinates for four vertices. Each group can expand an axis's lower or upper bound. The analyzer replays the ordered comparisons on the recorded values and compares all six words bit for bit, including the old word when the comparison is equal or unordered. A separate edge-case check covers NaNs and signed zero.

Across three monitored raids, **690,896** before-and-after pairs matched the scalar replay. Between **71.37% and 80.13%** had no bounds changes. On each launch, every observed adjacent group for the same bounds address and frame saw the previous group's final words. That continuity establishes the observed data dependency. Delaying stores would also require checking for intermediate readers and aliasing.

| Probe recording | Complete attributed frames | Median bounds updates per frame | Quad positions aligned to the preceding frame | Identical positions |
| --- | ---: | ---: | ---: | ---: |
| First | 13 | 20,748 | 51,803 | 0 |
| Second | 18 | 10,742 | 33,643 | 0 |
| Delayed | 22 | 8,584 | 41,439 | 0 |

Position comparisons align by guest CPU, bounds address and quad order within that address. The twelve recorded coordinate words describe position only. Two pairs in the second launch straddled a swap timestamp and remained in the replay counts. Their neighboring frame intervals were excluded from the frame comparison. The first trace wrapped its bounded ring and the analyzer verified the final 524,288 published rows. The delayed trace starts 3.38 seconds after its verified GPU-window anchor.

The same instruction addresses in two earlier unmodified CPU profiles account for **0.51–0.52%** of weighted CPU samples across all four reductions. The larger vertex-emission region accounts for **1.89–1.98%**. Keep the original path as the benchmark baseline. The three diagnostic GPU windows verified the full caster fixture, saved camera, overlapping effects, 26 timing rows and consecutive GPU completions with zero visible windows or activations. The runtime passed **806** general i386 checks with the probe disabled. [Replay, reuse counts and CPU coverage](results/raid-aarchx-i386-vertex-bounds-profile.json)

Apply `patches/aarchx-m32-raid-vertex-bounds-probe.patch` to the copied-shadow baseline with the general base hoist. Use the profiled i386 image identified by SHA-256 in the result. Set `OCERZ_TCACHE=off` and `OCERZ_M32_RAID_BOUNDS_LOG` to a fresh `vertex-bounds.bin` in the capture directory. The probe uses the host's LSE instructions and its 1 GHz virtual counter. After the in-world marker, enable capture for a short interval inside the verified 25-second GPU window.

```sh
python3 - "$CAPTURE_DIR" <<'PY'
import json
import mmap
from pathlib import Path
import struct
import sys
import time

capture = Path(sys.argv[1])
with (capture / "vertex-bounds.bin").open("r+b") as file, mmap.mmap(file.fileno(), 128) as header:
    struct.pack_into("<Q", header, 8, 1)
    try:
        time.sleep(0.18)
    finally:
        struct.pack_into("<Q", header, 8, 0)
    time.sleep(0.25)
    rows, = struct.unpack_from("<Q", header)
(capture / "bounds-gate.json").write_text(json.dumps({"captured_rows": rows}) + "\n")
PY
python3 benchmarks/summarize-raid-vertex-bounds.py "$CAPTURE_DIR" \
    --cpu-profile EARLIER_STACK_RUN_1 --cpu-profile EARLIER_STACK_RUN_2
```

### Join the vertex-stage tail to each Metal render encoder

The analyzer matches fragment endpoints from Metal System Trace to application command-buffer submissions, then joins active vertex intervals and the application encoder list by command-buffer and encoder ID. Each joined output has two render submissions and their draws add up to the verified draw count. The archive uses the original copied-shadow renderer. In two separate recordings, the diagnostic renderer labels each render encoder and records its target, clear flag, draws and submitted vertex references by program. The labels join that work to the measured encoder.

| Capture with the earlier client paused | Joined output gaps | First encoder vertex activity, all / tail | Final encoder vertex activity, all / tail | Tail gaps with longer final vertex activity |
| --- | ---: | ---: | ---: | ---: |
| Archived unlabelled | 1,840 | 0.26 / 0.26 ms | 0.28 / 3.81 ms | 15 / 19 |
| Labelled 1 | 2,195 | 0.30 / 0.30 ms | 0.32 / 4.40 ms | 18 / 22 |
| Labelled 2 | 2,188 | 0.26 / 0.26 ms | 0.28 / 2.57 ms | 16 / 22 |

The figures are medians of active vertex intervals by render submission. A submission has one measured vertex encoder at the median in every capture. Three endpoint-matched gaps per capture lacked active vertex rows for one of the buffers. They still enter the completion-gap p99 calculation. Every gap at or above p99 had both vertex records. Median vertex activity in those gaps outside the current output's two command buffers was **0.56–0.89 ms**.

The labelled final pass had one color attachment and depth at 1365 × 768 and recorded no pending clear. The dominant ARB vertex-program shape in that pass shifted from **65 instructions and 223 parameters** to **40 instructions and 223 parameters** across the recordings. Match each tail buffer to a non-tail buffer from the same capture and submission position with the same draw count, choosing the closest number of submitted vertex references per program. For the final pass, **20 of 22** and **14 of 22** tail buffers matched within **1%** summed reference distance. Their median vertex-active durations were **12.28× and 10.16×** those of their matches. Counts can hide changes in index reuse, shader inputs and primitive data.

In the fixed **500 µs** before the final vertex stage, median WindowServer GPU activity was **499.1–499.9 µs** for the slowest gaps and **0 µs** across all attributed gaps. This is a timing association with another GPU workload. The result supports testing how the client splits its render pass while keeping draw order and measuring WindowServer activity in both arms. [Encoder attribution and matched work](results/raid-winemetalgl-encoder-vertex-tail.json)

Apply `patches/winemetalgl-encoder-vertex-work-trial.patch` to the copied-shadow baseline. Set `OCERZ_WMGL_SUBMISSION_LOG` to `submissions.ns` and `OCERZ_WMGL_ENCODER_WORK_LOG` to `encoder-work.tsv`. Attach the Metal System Trace template to the isolated in-world client and export `metal-gpu-intervals` and `metal-application-encoders-list` as `gpu-intervals.xml` and `encoders.xml`. Keep the same verified GPU window and desktop monitor. The M4 Pro reported stage-boundary timestamp sampling. Its draw-boundary sampling query returned false. [Apple Metal counter sampling](https://developer.apple.com/documentation/metal/sampling-gpu-data-into-counter-sample-buffers)

```sh
python3 benchmarks/summarize-raid-encoder-tail.py CAPTURE_DIR
```

The two labelled captures were 17-second Metal System Traces within 35-second verified GPU windows. Both checked the saved camera, overlapping effects, all 26 timing rows, consecutive GPU completions and zero visible windows or activations. The earlier client resumed after every capture. The renderer passed **343** GPU tests with diagnostic logging disabled.

### Disable automatic render-pass splitting in the full raid

Both arms use the accepted copied-shadow runtime and renderer. Set `WINEMETALGL_COMMIT_DRAWS=64` for the normal policy or `0` to bypass the draw-time submission heuristic. The same draw order reaches the renderer. Swap, explicit synchronization and resource-dependent submissions retain their normal handling.

The two diagnostics used `OCERZ_WMGL_SUBMISSION_LOG` and 17-second Metal System Traces within 35-second verified GPU windows. All **4,360** control intervals contained two draw-bearing submissions. All **4,425** trial intervals contained one draw-bearing submission. One trial interval also ended with a zero-draw buffer after the buffer holding all 855 draws. The timing launches use four 60-second windows in 64, 0, 0, 64 order with submission and encoder logging disabled.

| Draw-time submission policy | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| Automatic split | 134.92 | 12.79 ms |
| Automatic split disabled | 135.08 | 12.95 ms |

Disabling the heuristic changed throughput by **−0.23 and +0.55 FPS** in the two pairs. P99 changed by **+0.38 and −0.06 ms**. Keep automatic splitting as the benchmark baseline. Six live launches verified the saved camera, overlapping effects, all 26 timing rows, consecutive GPU completions and zero visible windows or activations. The earlier client was paused during each launch and resumed afterward. On the ordered offscreen replay, both policies had **161,327** colored pixels and the same framebuffer hash. [Paired results, draw counts and replay](results/raid-winemetalgl-single-render-pass-ab.json)

The Metal traces bracketed **2,180 and 2,240** consecutive output intervals with clock alignment within **42 ns**. Two control and three trial intervals lacked complete encoder attribution. They enter the completion-gap percentile and drop out of the per-encoder summary. In the slowest one percent, median client vertex activity within the frame gap was **2.90 ms** with the split and **3.94 ms** with one draw-bearing buffer. Median WindowServer activity in those gaps was **7.14 and 8.89 ms**, respectively. Each instrumented arm samples one period of desktop activity. The four 60-second runs supply the policy comparison. [Aligned hardware intervals](results/raid-winemetalgl-single-render-pass-ab.json)

```sh
python3 benchmarks/summarize-raid-single-render-pass.py CAPTURE_ROOT
```

### Forward the register value from an adjacent x87 float load

The i386 trial recognizes a memory `FLDS` followed by `FISTPL` in the same emitted x87 run. It records the exact host-code boundary at which the load leaves the checked double in a scratch register. If conversion starts at that boundary, it reuses the register and the earlier NaN check. The load still writes the x87 stack slot and its metadata. The conversion uses the existing rounding, range checks, status updates and store path. If the load has to interpret the x87 run, that path also executes the conversion.

Apply `patches/aarchx-m32-x87-forward-trial.patch` after the general base-hoist trial. Set `OCERZ_TCACHE=off` and `OCERZ_M32_X87_FORWARD=1`. Set `OCERZ_M32_X87_FORWARD_LOG=1` only for a separate compiler diagnostic.

Both modes passed **806** general i386 checks and **44** focused interpreter comparisons. The focused fixture uses all four x87 rounding modes with positive and negative numbers, signed zero, the 32-bit integer boundary, an overflow, infinity and a quiet NaN. It compares the result, status and control words, and an existing x87 stack value. The trial also passed all 44 cases with forced branch veneers. Run one fresh process for each `OCERZ_TEST_JIT32_FLD_FIST` value from 0 through 43.

The compiler diagnostic recorded **61** distinct forwarded pairs, including one emitted after the last world readback. All 61 matched adjacent instructions in the original i386 executable. The emitted `FISTPL` addresses accounted for **1.03% and 1.01%** of all weighted CPU samples in the two earlier raid profiles. The attribution covers the instructions' native code in those captures. The trial removes the stack reload and duplicate NaN check within that code.

| x87 float operand | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| Reload the x87 stack slot | 134.87 | 12.64 ms |
| Forward the ARM64 register | 134.63 | 12.66 ms |

The trial changed throughput by **−0.65 and +0.17 FPS** in the two pairs. P99 changed by **+0.13 and −0.07 ms**. Keep the ordinary reload as the benchmark baseline. The four 60-second launches and the separate diagnostic verified the saved camera, overlapping effects, all 26 timing rows, consecutive GPU completions and zero visible windows or activations. The earlier trial was paused during each launch and resumed afterward. [Live A/B, emitted pairs and CPU coverage](results/raid-aarchx-i386-x87-forward-ab.json)

```sh
python3 benchmarks/summarize-raid-x87-forward.py CAPTURE_ROOT I386_CLIENT \
    --cpu-profile EARLIER_STACK_RUN_1 --cpu-profile EARLIER_STACK_RUN_2
```

### Check short i386 calls and dependent pointer loads

A static scan followed immediate calls in the original i386 text. It first admitted straight-line callees of at most 24 instructions ending in a plain return. It then admitted up to 64 instructions with local forward branches and one level of separately checked calls. In the two earlier CPU profiles, the first scan's callee bodies accounted for **0.82% and 0.76%** of weighted CPU samples. The expanded scan covered **1.79% and 1.64%** in the outer bodies. The direct call instructions themselves accounted for **0.20% and 0.24%** in the expanded scan. These counts describe static candidates. [Earlier CPU captures](results/raid-aarchx-native-instructions-in-guest-call-trees.json)

The next trial uses the existing dependency analysis for a limited set of ordered i386 MOV loads. It checks whether the next memory operation uses the value returned by the first load as a base or index. Full-width register copies can carry the dependency between the loads. The rule stops at other operations with effects and leaves an acquire at the end of the chain. Address dependence orders the earlier load before the later one. The last acquire orders it before subsequent accesses. [Armv8-A memory model guide](https://developer.arm.com/-/media/Arm%20Developer%20Community/PDF/Learn%20the%20Architecture/Armv8-A%20memory%20model%20guide.pdf?revision=58b1dd0a-3800-4218-b21a-f95a0332034c)

Apply `patches/aarchx-m32-dependent-loads-trial.patch` after the general base hoist. Set `OCERZ_TCACHE=off` and `OCERZ_M32_DEP_PLAIN=1`. The separate compiler diagnostic sets `OCERZ_M32_DEP_PLAIN_LOG=1` and `OCERZ_DEP_PLAIN_CHECK=1` to record the selected source and consumer and check for the terminal acquire.

Both modes passed **806** general i386 checks and eight interpreter comparisons. The focused cases cover adjacent and copied dependencies, a chain of three loads, wraparound at 4 GiB, a scaled index, an independent intervening load, an immediate overwrite, and an ordinary frame-local load. Each ran in a fresh process with `OCERZ_TEST_JIT32_DEPENDENCIES` set to a value from 0 through 7.

The focused machine-code dump showed that the first load changed from an address add plus `LDAPUR` to one register-offset `LDR`. The terminal load's address calculation and `LDAPUR` matched in both modes. In the live diagnostic, the compiler reported **6,013** distinct dependencies. The original client disassembly confirmed **5,951**, including **90** emitted after the last world readback. The matched source addresses represented **0.96% and 0.89%** of weighted CPU time in the two earlier raid profiles. The terminal-acquire diagnostic reported zero omissions.

| i386 load ordering | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| Existing acquire sequence | 135.15 | 12.67 ms |
| Dependent load plus terminal acquire | 134.53 | 12.80 ms |

The first pair changed throughput by **+1.23 FPS** and p99 by **−0.07 ms**. The reverse pair changed them by **−2.47 FPS** and **+0.33 ms**. Keep the existing ordering as the benchmark baseline. The four 60-second launches and compiler diagnostic checked the saved camera, overlapping effects, all 26 timing rows, consecutive GPU completions and zero visible windows or activations. The earlier trial was paused during each launch and resumed afterward. [Live A/B, CPU coverage and focused ARM64 output](results/raid-aarchx-i386-dependent-loads-ab.json)

```sh
python3 benchmarks/summarize-raid-i386-dependent-loads.py CAPTURE_ROOT I386_CLIENT \
    --cpu-profile EARLIER_STACK_RUN_1 --cpu-profile EARLIER_STACK_RUN_2
```

### Recover wrapped i386 loads through guard pages

The trial reserves one inaccessible host page on each side of the offset-mapped 4 GiB i386 arena. It uses the ordinary host load for selected 32-bit MOV operands based on one unchanged register with displacements from −256 to 255. A load whose starting address crosses an arena boundary faults on a guard page. Recovery matches the emitted instruction and effective address, restores the guest registers and flags, and interprets just that load using the wrapped 32-bit address. A read that starts inside the arena keeps ordinary fault handling.

The existing six-operand heuristic keeps its explicit guard when the selected prefix includes stores or SSE operands. A load-only prefix can use the guard pages. When the original selector would decline a prefix, the trial can admit it with two nonzero-displacement loads. Apply `patches/aarchx-m32-wrap-guard-loads-trial.patch` after the general base hoist with the translation cache off. Set `OCERZ_M32_WRAP_FAULT_LOADS=1`. Use `OCERZ_M32_WRAP_FAULT_LOADS_LOG=1` only for the separate compiler and fault diagnostic.

Both modes passed **806** general i386 checks. Three separate `OCERZ_TEST_JIT32_WRAP_FAULT` launches use `upper`, `lower`, or `inside`. The two boundary cases verified one recovery each and correct wrapped reads. The inside case recorded no recovery. The return value also checks that an instruction preceding the first load ran only once.

The three cases also passed with forced branch veneers. The live diagnostic recorded **7,716** load-only prefixes and zero recoveries. **7,694** prefixes matched the original i386 text. Of those, **7,566** contain fewer than six loads and **131** were emitted after the last world readback. Matching the selected loads to the previous two CPU profiles covered **2.58% and 2.56%** of all weighted samples. The immediate address arithmetic within those loads accounted for **1.71% and 1.73%**. All of that sampled address arithmetic was in the newly admitted short prefixes.

| i386 load address policy | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| Explicit bounds checks | 134.78 | 12.50 ms |
| Guard pages and added short prefixes | 133.97 | 12.74 ms |

Throughput changed by **−2.18 and +0.57 FPS** across the two pairs. P99 increased by **0.04 and 0.46 ms**. Keep the explicit bounds checks as the benchmark baseline. Each of the four warmed 60-second runs used logging off and checked the saved camera, overlapping effects, all 26 timing rows, consecutive GPU completions, and zero visible windows or activations. The earlier client was paused during each launch and resumed afterward. [Live A/B, earlier CPU coverage and boundary tests](results/raid-aarchx-i386-wrap-guard-loads-ab.json)

```sh
python3 benchmarks/summarize-raid-i386-wrap-guards.py CAPTURE_ROOT I386_CLIENT \
    --cpu-profile EARLIER_STACK_RUN_1 --cpu-profile EARLIER_STACK_RUN_2
```

### Profile-guided optimization of the native renderer

The accepted renderer already builds with `-O3` and ThinLTO. This trial takes one copy of the same patched renderer sources and applies an LLVM instrumentation profile from the live raid. The game runtime remains the same executable. Profile generation, the control build, and profile use share the same CMake build tree and renderer configuration.

Apply `patches/winemetalgl-raid-pgo-trial.patch` to the copied renderer sources. Keep `WMGL_HOST_ONLY=ON`, `WMGL_LTO=ON`, `CMAKE_BUILD_TYPE=Release` and arm64 for the three builds. Select `WMGL_PGO_GENERATE=ON` to generate profiles and pass a fresh `LLVM_PROFILE_FILE` pattern such as `wmgl-%p%c.profraw` to each training launch. Continuous mode writes counters even when the benchmark terminates the client. Merge the two recorded profiles with `llvm-profdata merge`. Set `WMGL_PGO_GENERATE=OFF` for timing. The control leaves `WMGL_PGO_PROFILE` empty and the candidate points it at the indexed profile. [Clang PGO instructions](https://clang.llvm.org/docs/UsersManual.html#profile-guided-optimization)

The two 60-second training windows verified **12,562** GPU completions. The merged IR profile contains **10,988** function records. The measured builds used Apple Clang 21 and matched all **78** renderer compile commands apart from the profile-use flag and its stale-profile check. Neither measured Mach-O includes LLVM instrumentation counters. The executable text section shrank from **9,932,836 to 6,982,872 bytes**.

| Native renderer build | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| Release and ThinLTO | 135.05 | 12.74 ms |
| Release, ThinLTO and the raid profile | 138.58 | 12.30 ms |

The two pairs gained **2.20 and 4.85 FPS** and shortened p99 by **0.12 and 0.76 ms**. Use the locally profiled library in follow-up comparisons that keep this renderer source unchanged. Each of the four 60-second timing windows verified the saved camera, overlapping effects, all 26 timing rows, consecutive GPU completions and zero visible windows or activations. Both builds passed **343** GPU tests. The deterministic indexed replay produced the same framebuffer hash in both modes. [Timed results, build flags and replay](results/raid-winemetalgl-renderer-pgo-ab.json)

```sh
python3 benchmarks/summarize-raid-renderer-pgo.py CAPTURE_ROOT
```

### Profile-guided optimization of the native AArchX runtime

The comparison uses one AArchX source snapshot and the same profiled WineMetalGL library from the preceding experiment in every launch. The compiler profile covers the C and Objective-C runtime code. Its options leave the settings for dynamically generated guest code in place. Training and timing use the same dedicated headless app path and bundle identifier. The runner stages each runtime before launch and verifies its signature and hash.

Apply `patches/aarchx-runtime-pgo-trial.patch` to the accepted AArchX source layers. Keep the existing `WMGL_INCLUDE`, arm64 and `-O2` flags. Rebuild with `make -B` after switching modes. Use `OCERZ_PGO_GENERATE=1` and a fresh `LLVM_PROFILE_FILE=runtime-%p%c.profraw` pattern in the training process. Merge the profiles with `llvm-profdata merge`. For timing, turn generation off and set `OCERZ_PGO_PROFILE` only for the candidate. [Clang PGO instructions](https://clang.llvm.org/docs/UsersManual.html#profile-guided-optimization)

The two 60-second training windows contained **14,949** verified GPU completions. The merged profile contains **3,843** function records. All **72** native compilation commands matched once the profile-use flag and its stale-profile check were removed. These flags apply to 69 C and Objective-C units. Neither timed executable includes the profiling counters. The native machine-code section shrank from **2,540,520 to 2,312,408 bytes**.

| Native AArchX build with the fixed PGO renderer | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| Existing `-O2` build | 139.60 | 12.64 ms |
| `-O2` with the raid profile | 140.78 | 12.28 ms |

The paired gains were **1.18 and 1.17 FPS**, with p99 shortened by **0.20 and 0.52 ms**. Use the locally profiled runtime with the profiled renderer for follow-up comparisons on the same source snapshots. The two runtime builds passed **805** standalone i386 checks and **40,044** JIT-versus-interpreter cases in each of the offset and low-shadow layouts. Each of the four 60-second timings verified the saved camera, overlapping effects, 26 timing rows, consecutive GPU completions and zero visible test windows or activations. [Live A/B and build validation](results/raid-aarchx-runtime-pgo-ab.json)

A separate Time Profiler capture on the selected build accounted for **16.38 seconds** of running main-thread sample weight. The i386 bridge's map and unmap routines appeared in **4.44%** of that weight. Buffer-shadow lookup or allocation appeared in **1.25%**, and guest leaf memory helpers appeared in **0.26%**. The groups use inclusive weights and can overlap. The CPU trace used the same verified fixture. It was recorded separately from the four timing windows. [Native call-group weights](results/raid-aarchx-runtime-pgo-ab.json)

```sh
python3 benchmarks/summarize-raid-runtime-pgo.py CAPTURE_ROOT
```

### Index OpenGL buffer shadows by share group and name

The trial adds a fixed index alongside the existing 256 shadow slots. The fast one-slot hint stays in front of it. Index links are read and updated under the existing shadow mutex. Deletion and eviction unlink the old key before a slot is reused. On a key miss, allocation can stop at the first spare that satisfies the existing eviction policy. The same native buffer mappings and shadow allocations back both modes.

Apply `patches/aarchx-m32-gl-shadow-index-trial.patch` to the accepted runtime source layers. Keep the profile-guided WineMetalGL library fixed. Set `OCERZ_M32_GL_SHADOW_INDEX=1` for the index arm. Train the same AArchX source snapshot for one 60-second raid window in each mode and merge the profiles before building the runtime that will be timed. Both timing arms use that one runtime with profile generation disabled. Use `OCERZ_M32_GL_SHADOW_INDEX_LOG=1` only for a separate diagnostic.

The index diagnostic logged **3,670,016 key misses and zero key hits** between the first and last complete counter rows after the world readback. These calls reached the index after the one-slot hint. The counters describe key presence and include probes from both the lookup and allocation paths.

| Search after the one-slot hint | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| Linear scan | 141.98 | 12.40 ms |
| Hash index | 142.78 | 12.44 ms |

The paired gains were **0.97 and 0.63 FPS**. P99 rose by **0.16 ms** in the first pair and fell by **0.08 ms** in the reverse pair. Keep the previous runtime as the raid baseline. All four 60-second timing windows had logging disabled and verified the saved camera, overlapping effects, 26 timing rows, consecutive GPU completions and zero visible windows or activations.

Both modes passed **806** general i386 checks and **2,728** offscreen buffer checks. The focused cases fill all 256 shadow slots, reject a further mapping while all slots are occupied, and verify remapping after eviction. Two unshared contexts keep independent contents under the same numeric buffer name. Enable the focused cases with `OCERZ_M32_GL_SHADOW_INDEX_TEST=1` in the existing `test_m32 wmgl-storage` test. [Live timing, index counters and checks](results/raid-aarchx-gl-shadow-index-ab.json)

```sh
python3 benchmarks/summarize-raid-shadow-index.py CAPTURE_ROOT
```

### Reject lookups in empty OpenGL shadow buckets

The trial publishes a small array of atomic population counts for the keys in the existing shadow slots. A lookup that misses the one-slot hint reads the relevant count. A zero count establishes that the key is absent before any scan or lock acquisition. Occupied buckets use the existing linear lookup, so hash collisions preserve normal behavior. During reassignment, the writer increments the new bucket before changing the slot and decrements the old bucket afterward. The existing mutex serializes key changes.

Apply `patches/aarchx-m32-gl-shadow-empty-bucket-trial.patch` to the accepted profiled-runtime source layers. The fixed WineMetalGL library is the accepted profiled renderer. Set `OCERZ_M32_GL_SHADOW_EMPTY=1` to try the shortcut. Generate one 60-second raid profile per mode and merge them before building the common timed runtime. Set `OCERZ_M32_GL_SHADOW_EMPTY_LOG=1` only for the separate bucket diagnostic.

Between the first and last complete counter rows after the last world readback, **3,670,016** checks reported an empty bucket and **4,498** reported an occupied one. That is **99.88%** empty among checks that reached the shortcut. The one-slot hint still runs first.

| Shadow lookup policy | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| Existing scan | 140.51 | 12.30 ms |
| Empty-bucket shortcut | 141.96 | 12.17 ms |

Throughput rose by **0.37 and 2.53 FPS** in the two pairs. P99 rose by **0.13 ms** and fell by **0.40 ms**, respectively. Keep the previous runtime as the benchmark baseline. The four 60-second timings had diagnostic logging disabled and verified the saved camera, effects, 26 timing rows, consecutive GPU completions and zero visible windows or activations.

Each mode passed **806** general i386 checks and **2,728** offscreen buffer checks. The buffer suite reuses the test hunk from `patches/aarchx-m32-gl-shadow-index-trial.patch` with `OCERZ_M32_GL_SHADOW_INDEX_TEST=1`. It keeps the full-table, eviction and share-group cases from the earlier index trial. [Live timing, bucket counters and checks](results/raid-aarchx-gl-shadow-empty-bucket-ab.json)

```sh
python3 benchmarks/summarize-raid-shadow-bucket.py CAPTURE_ROOT
```

### Classify mapped-buffer shadow copies in the full raid

Apply `patches/aarchx-m32-gl-map-policy-probe.patch` to the accepted AArchX source layers and set `OCERZ_M32_GL_MAP_POLICY_LOG=1`. Keep the accepted profile-guided WineMetalGL library. After each successful fallback map that copies incoming bytes, the probe queries the actual native map access flags. It counts bytes eligible by the write-only, nonpersistent explicit-flush or invalidation predicates. It also counts bytes copied back at unmap and at explicit flush calls.

| Capture | Whole-buffer copies | Native to guest | Guest to native | Incoming bytes with an explicit-flush or invalidation flag | Incoming bytes after matching null allocation |
| --- | ---: | ---: | ---: | ---: | ---: |
| First | 2,342,912 | 12.39 GB | 12.39 GB | 0 | 0.81% |
| Second | 2,408,448 | 12.62 GB | 12.62 GB | 0 | 0.80% |

The copy totals span the first and last complete counter rows after the world readback. The separate 35-second GPU windows verified **4,817** and **4,801** consecutive completions, the saved camera and effects, all 26 timing rows, and zero visible trial windows or activations. The probe records a same-thread, same-share-group, same-buffer, same-size match for a preceding `glBufferData` with a null pointer. This is a call correlation. Allocation success and intervening writes through other paths would require additional tracking. The map flags give zero coverage for either copy-skip rule in both captures. [Copy counters and verified GPU windows](results/raid-aarchx-gl-map-copy-policy.json)

```sh
python3 benchmarks/summarize-raid-map-copy-policy.py CAPTURE_ROOT
```

### Frame-time budget for a larger improvement

The selected runtime's two timed runs give a median **140.78 FPS**, or **7.10 ms** between completed renders, and **12.28 ms** at p99. Reaching **500 FPS** with **2.0 ms** p99 requires about **5.10 ms** less per average completion and **10.28 ms** less at p99. Reaching **900 FPS** requires about **5.99 ms** less per average completion. These differences describe throughput and pacing budgets. CPU and GPU work overlap. [Uninstrumented full-raid baseline](results/raid-aarchx-runtime-pgo-ab.json)

The main thread spent **98.55%** of a prior trace running. A serial estimate that applies the two hot guest call trees' measured **43.20–43.59%** share to the current baseline gives **180 FPS** at twice their speed, **208–209 FPS** at four times their speed, and **248–250 FPS** if their cost were eliminated. Eliminating the whole map-and-unmap call group at its sampled **4.44%** share gives about **147 FPS**. These estimates use profiles recorded separately from the timing baseline and hold other CPU costs fixed. [Scheduler profile](results/raid-winemetalgl-fast-share-lock-cpu-trace.json) · [Guest call trees](results/raid-aarchx-native-instructions-in-guest-call-trees.json) · [Runtime call groups](results/raid-aarchx-runtime-pgo-ab.json)

Start with the preparation work under `0x97cef0` and `0x97e390`. Measure time and memory dependencies at the per-item boundary, including where each result is first consumed. Look for a batch of independent work that can feed the existing draw order, with a target of at least **1 ms per full raid frame** after staging and merging. A replay of identical inputs should compare all output bytes before the live A/B. The earlier quad probe measured changing positions and a bounds dependency between consecutive writes. The OpenGL command trace also showed that queueing individual calls consumed the modeled overlap savings at 200 ns per call. [Guest stack attribution](results/raid-aarchx-headless-i386-inclusive-stack-profile.json) · [Quad probe](results/raid-aarchx-i386-vertex-bounds-profile.json) · [GL timeline](results/raid-winemetalgl-command-timeline.json)

Measure the GPU budget in parallel with the CPU investigation. The final render encoder took a median **2.57–4.40 ms** of vertex activity in the slowest one percent of the two labeled captures. Closely matched draw and vertex-reference counts still saw **10–12×** longer median vertex intervals, with concurrent WindowServer activity. An ordered replay of several captured full raid frames can test throughput and p99 at higher submission rates. Compare steady command queue depth and actual stage intervals against the live trace. The existing captures provide full frames with more than 900 draws at the target size. [GPU encoder attribution](results/raid-winemetalgl-encoder-vertex-tail.json) · [Full-frame captures](results/raid-winemetalgl-offscreen-metal-frame-capture.json)

### Time the item preparation loops and locate ordered state

The i386 probe records registers and host virtual-counter timestamps at the item dispatch loop in `0x97cef0` and at two linked-list calls in `0x97e390`. It pairs starts and ends by guest CPU and stack frame, then sums elapsed intervals between complete consecutive swaps. The dispatch interval includes setup, callee work and copying the resulting state. Timings include the probe and the JIT block boundaries it introduces.

At `0x97d31d` the dispatcher starts writing the current item into a work context. At `0x97d3b0` it begins copying the current values into the previous-item fields of that same context. Several subpaths change the loop index, influencing the next item selected. Any parallel preparation needs a boundary inside the callees and an ordered merge of state and draw output.

Across six probe windows, the analyzer paired **214,231** dispatch publications and found **211,362** consecutive iterations preparing different items through the same context. **22,737 (10.6%)** of the paired publications changed the loop index inside the body. The dispatch body took **2.44–3.14 ms** per complete frame across the six window medians. Items arriving with tags 0, 1 and 4 had the highest median costs. Tag 4 alone accounted for **0.66–1.11 ms**. The tag is read at item entry. The dispatcher reads it again after a helper call before choosing a callee.

Two earlier sampled guest profiles attribute **9.16–9.59%** of main-thread CPU weight to the call from `0x97d552` into `0x97caa0`. The largest sampled leaf under that call was the quad emitter at `0x880c70`, with **4.36–4.61%** of the whole thread. Separate nonoverlapping draw preparation from context publication and draw submission inside that call family first. The replay needs to retain the index-changing paths and compare the output bytes against the original serial path. The other major input tags give follow-up targets for savings across the dispatch loop. [Probe timings and validated call returns](results/raid-aarchx-i386-item-preparation.json)

The second loop walks linked items and conditionally calls `0x964ca0` at `0x9806ae`. The subsequent list pass calls `0x967f20` at `0x9801a7`. Their median times were **0.38–0.45 ms** and **0.34–0.52 ms** per frame, respectively. The probe observed **22,075** consumer calls after a completed update of the same item and stack-frame address in the same swap interval. Those pairs establish execution order and item identity. Identifying data read by the consumer requires tracing memory effects inside the two callees.

Two 35-second diagnostic runs verified **4,853** and **4,863** consecutive GPU completions, the saved camera, overlapping effects, all 26 cast timing rows and zero visible trial windows or activations. The six probes covered **428** complete swap intervals. The independent GPU log brackets each trial's first and last completed output with earlier-client renders before and after. It records zero completions from the earlier client during either trial's output interval. [Verified run details](results/raid-aarchx-i386-item-preparation.json)

Use the original client image identified by SHA-256 in the result. Apply `patches/aarchx-m32-raid-preparation-probe.patch` to the accepted runtime source layers and keep the accepted PGO renderer. Set `OCERZ_TCACHE=off` and `OCERZ_M32_RAID_PREP_LOG` to a fresh path `raid-prep.bin` for each run. Use the usual monitored headless launch with a verified 35-second GPU window. Once the window opens, enable the ring for about 0.52 seconds each at offsets of 2, 12 and 24 seconds. In the header, the first two little-endian uint64 words are the row count and enabled flag. Record a pair of row counts before enabling and after disabling each gate in `prep-gates.json`. Keep both captures under `measured-1` and `measured-2` with the existing artifact checksum witness and previous guest-stack captures for corroboration.

```sh
python3 benchmarks/summarize-raid-preparation.py CAPTURE_ROOT LOCAL_I386_EXECUTABLE \
    --stack-captures EARLIER_STACK_RUN_1 EARLIER_STACK_RUN_2 \
    --legacy-gpu EARLIER_CLIENT_GPU_LOG --legacy-pid EARLIER_CLIENT_PID
```

### Replay the quad position and bounds kernel on ARM64

The replay exports complete four-vertex cases from the three archived raid captures. The exporter requires all four ordered before-and-after bounds pairs, verifies each transition, and carries the original float words into the native test. The scalar and NEON paths write the same 12 coordinate words and six final bounds words. Their comparison also includes a sentinel next to each vertex position. The NEON path compares three axes together and selects each original word on an equal or unordered comparison.

Both builds matched **172,720** recorded quads and **32,768** cases containing signed zeros, subnormals, infinities and NaN payloads. The address and undefined behavior sanitizer build passed the same byte checks. The timed test begins with calculated positions and measures a native kernel outside the game. It leaves guest register transitions, intermediate bounds visibility, floating-point exception status and JIT memory checks to an in-client implementation.

| Native replay window | Scalar with compiler vectorization | NEON | Speedup |
| --- | ---: | ---: | ---: |
| All recorded cases | 5.54 ns/quad | 2.73 ns/quad | 2.03× |
| 2,048 cached cases | 4.41 ns/quad | 2.65 ns/quad | 1.66× |

Each median comes from eight pairs in alternating order. With compiler vectorization disabled, the ratios were **2.01×** and **1.68×**. The earlier sampled profile put only **1.89–1.98%** of main-thread CPU weight in the entire original vertex-emission region. Applying the native kernel speedup to that whole region would be an optimistic bound. The standalone replay is a correctness and implementation comparison for this part of the larger dispatch path. [Replay data](results/raid-aarchx-quad-native-replay.json) · [Earlier instruction coverage](results/raid-aarchx-i386-vertex-bounds-profile.json)

The live layout probe checks the four stream cursors and their strides at both ends of the same quad. It reads the coordinate registers at each vertex and re-reads the resulting 12-byte position from guest memory after the quad. It checks address arithmetic and overlap with the cursor, bounds, stack, color source, and normal and texture-coordinate inputs. The 4 KiB measurement identifies how often each stream fits into a single guest page. This is an eligibility measurement for an in-client fast path.

Across **293,466** complete quads, every observed cursor advanced as expected. All **1,171,709** sampled position triplets matched the register words byte for byte. The diagnostic skipped **2,155** position loads that would cross a 4 KiB guest-page boundary. The position, normal, color and texture-coordinate strides were **24, 0, 24, 24 bytes** in **172,380** quads and **36 bytes for all four streams** in the other **121,086**. The zero normal stride sends four writes to the same address. The source normal words matched at both ends of each quad.

After treating repeated writes to that one normal address as one range, all sampled output ranges were disjoint from one another and from the checked inputs. In **286,252** quads (**97.5%**), each stream fit in a single guest page. Use both layouts when testing a guarded fast path. Check cross-thread observations and any intermediate reads of bounds before moving the stores. Two 35-second launches verified **4,796** and **4,699** consecutive GPU completions, the saved camera, overlapping effects, all 26 timing rows and zero trial windows or activations. [Live layout and byte checks](results/raid-aarchx-quad-native-replay.json)

To reproduce the replay and collect inputs for the summary, use the three raw vertex-bounds recordings described earlier. Run each binary with the same case file and save its output under the indicated filename in the capture root.

```sh
python3 benchmarks/export-raid-quad-replay.py CAPTURE_ROOT/quads.bin \
    VERTEX_BOUNDS_RUN_1 VERTEX_BOUNDS_RUN_2 VERTEX_BOUNDS_RUN_3 > CAPTURE_ROOT/export.json
xcrun clang -arch arm64 -O3 -fno-vectorize -fno-slp-vectorize \
    benchmarks/raid-quad-native-replay.c -o CAPTURE_ROOT/replay
xcrun clang -arch arm64 -O3 benchmarks/raid-quad-native-replay.c -o CAPTURE_ROOT/replay-autovec
xcrun clang -arch arm64 -O1 -g -fsanitize=address,undefined \
    benchmarks/raid-quad-native-replay.c -o CAPTURE_ROOT/replay-sanitized
CAPTURE_ROOT/replay CAPTURE_ROOT/quads.bin > CAPTURE_ROOT/replay.txt
CAPTURE_ROOT/replay-autovec CAPTURE_ROOT/quads.bin > CAPTURE_ROOT/replay-autovec.txt
CAPTURE_ROOT/replay-sanitized CAPTURE_ROOT/quads.bin --verify-only > CAPTURE_ROOT/replay-sanitized.txt
```

Apply `patches/aarchx-m32-raid-quad-layout-probe.patch` to the accepted runtime source layers and keep the accepted PGO renderer. Use the guest image identified in the result. Set `OCERZ_TCACHE=off` and `OCERZ_M32_QUAD_LAYOUT_LOG` to a fresh `quad-layout.bin` per run. Use a verified 35-second full-raid headless launch for `layout-1` and `layout-2`. At about 2, 12 and 24 seconds into each window, enable logging for 0.14 seconds. The first two little-endian uint64 words of the ring are the row count and enabled flag. Save the before-and-after row counts for each window in `prep-gates.json`, along with the artifact checksum witness at the capture root.

```sh
python3 benchmarks/summarize-raid-quad-layout.py CAPTURE_ROOT LOCAL_I386_EXECUTABLE
```

### Join conditional bounds stores inside the translated block

The JIT recognizes a single `MOVSS` store skipped by `JBE` immediately after `UCOMISS`. At the four measured quad bounds groups, it decodes through the store and emits the join as a local ARM64 branch. Each original comparison, load, conditional write, and guest flag use stays in order. The address cache resets at each join, and the floating-point batching pass stays off for the joined region. The surrounding guest blocks use the standard direct chaining path. The four timed runs used one `-O2` runtime and the accepted PGO renderer.

Apply `patches/aarchx-m32-raid-quad-local-trial.patch` to the accepted runtime source layers. Use the client image identified by SHA-256 in the result. Set `OCERZ_TCACHE=off` for both arms and `OCERZ_M32_QUAD_LOCAL=1` for the candidate. Use `OCERZ_M32_QUAD_LOCAL_LOG=1` to log code generation in the separate diagnostic. With `OCERZ_M32_QUAD_LOCAL_TEST=1`, the focused JIT test can exercise the same pattern in the test guest rather than at the four client addresses. Run a fresh `test_jit32` process with `OCERZ_TEST_JIT32_QUAD_JOIN` set to each value from 0 through 5. The six comparisons in each mode cover both store paths, unaligned addresses, signed zeros, unordered compares, infinities, and a load that reads a conditional write. Both modes passed **805** general i386 checks.

For the separate in-world validation, apply `patches/aarchx-m32-raid-quad-local-validation.patch` on top of the trial. Set `OCERZ_M32_RAID_BOUNDS_LOG` to a fresh `vertex-bounds.bin` and arm the ring for approximately **90 ms** after the verified scene starts. Use the bounds-ring gate and metadata from the earlier quad probe and a 25-second GPU window. Record this launch under `in-world-bounds`. Code generation confirmed all six joins at each of the four client addresses. The scalar replay matched **201,960** observed bounds updates bit for bit, covering **10** whole frames inside the probe window.

| Conditional-store strategy | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| Existing block chaining | 139.89 | 12.39 ms |
| Local branches | 140.01 | 12.60 ms |

Throughput changed by **−0.07** and **+0.30 FPS**. P99 changed by **+0.007** and **+0.410 ms**. Keep the prior benchmark baseline. The four warmed 60-second runs and the bounds diagnostic verified the saved camera, overlapping spell effects, all 26 cast timing rows, consecutive GPU completions, and zero visible windows or activations. The earlier test client resumed after every run. [Full-raid A/B and bitwise validation](results/raid-aarchx-quad-local-ab.json)

```sh
python3 benchmarks/summarize-raid-quad-local.py CAPTURE_ROOT LOCAL_I386_EXECUTABLE
```

### Replay four consecutive raid outputs on the same GPU

The capture manager starts immediately after an offscreen flush and counts four consecutive GPU output sequences. The live log, GPU completions, draw signposts and `gpudebug` command tree agree on **3,772 draws** at **1365 × 768**. Each frame has one archived blit buffer and two render buffers. The archive metadata reports one frame, so the verification uses the four renderer output markers and their command-buffer boundaries. Replaying each final attachment produced four distinct images. Repeating the final fetch after rewinding produced the identical PNG. [Full-burst evidence](results/raid-metal-burst-replay.json) · [Apple’s gpudebug documentation](https://developer.apple.com/documentation/xcode/debugging-with-interactive-command-line-tools)

The two final-attachment requests bracket a rewind to the first draw. Metal System Trace shows two groups of eight render buffers and one render buffer for the rewind. The application encoding rows separate the requests and match seven render buffers in the first full request and all eight in the second. The GPU trace supplies both vertex and fragment intervals for every render buffer. The numbers below count the union of vertex and fragment activity in the two render buffers for each output.

| Draws by render buffer | First replay GPU active | Second replay GPU active | WindowServer overlap, first / second |
| --- | ---: | ---: | ---: |
| 256 + 685 | 1.96 ms | 4.65 ms | 0.67 / 4.03 ms |
| 512 + 427 | 1.45 ms | 7.53 ms | 0.00 / 7.41 ms |
| 512 + 435 | 3.35 ms | 1.10 ms | 2.72 / 0.00 ms |
| 512 + 433 | 2.05 ms | 4.50 ms | 0.00 / 3.85 ms |

Five of the eight sampled render intervals exceeded **2 ms**. The identical 939-draw frame varied by more than fivefold. The slower replay coincided with active WindowServer intervals for nearly its entire render time, establishing a timing association. The earlier test client had zero overlapping GPU intervals in the measured render spans. Debugger requests also incur replay setup, resource copies and readback. Sustainable gameplay throughput and p99 use the live output windows. [Hardware intervals and replay hashes](results/raid-metal-burst-replay.json)

Apply `patches/winemetalgl-offscreen-frame-capture-trial.patch`, then `patches/winemetalgl-offscreen-burst-capture.patch`. Set `MTL_CAPTURE_ENABLED=1` and `OCERZ_WMGL_CAPTURE_FRAMES=4` alongside the existing capture request and output variables. Save the full-raid launch under `live-burst-quiet` with a 35-second verification window. Set `OCERZ_WMGL_CAPTURE_PATH` to `raid-burst.gputrace` inside that directory. The diagnostic checked the saved camera, overlapping effects, all 26 cast timing rows and consecutive GPU completions with no trial windows or activations. The renderer passed **343** GPU tests.

Open the resulting archive with `gpudebug -t` and retain its session ID. Record Metal System Trace while attaching to that session's `GPUToolsReplayService` process. In order, fetch `color0` from `/commands/cb11/re0`, `/commands/cb1/re0/draw0`, and `/commands/cb11/re0` into `quiet-replay/burst-final-first.png`, `burst-rewind.png`, and `burst-final-second.png`. Record the replay service PID, request paths, fetch durations and earlier-client GPU completion count in `quiet-replay/fetch-witness.json`. Fetch `/commands/cb2/re0`, `/commands/cb5/re0`, and `/commands/cb8/re0` afterward as `frame-1.png` through `frame-3.png`. Export the `metal-gpu-intervals` and `metal-application-encoders-list` tables as `quiet-replay/burst-fetch-gpu.xml` and `quiet-replay/burst-fetch-encoders.xml`. Run these analysis commands with the GPU test log and `burst-artifacts.json` checksum witness at the capture root.

```sh
python3 benchmarks/summarize-raid-metal-burst.py commands SESSION_ID > CAPTURE_ROOT/quiet-replay/commands.json
python3 benchmarks/summarize-raid-metal-burst.py summary CAPTURE_ROOT --legacy-pid EARLIER_CLIENT_PID
```

### Add a separate Metal workload during the four-frame replay

The experiment reuses the verified 3,772-draw archive at 1365 × 768. It rewinds to the first draw before each full replay and checks the final PNG against the archived reference. Two sets of eight requests use the order control, loaded, loaded, control, control, loaded, loaded, control. The loaded arm runs a roughly 300 ms pulse on a separate process's Metal queue. Its renderer draws blended fullscreen triangles into a private 512 × 512 texture, with one command buffer in flight. The two sets use eight and 64 draws per test buffer.

Metal System Trace matched all **16** named begin and end markers in each set. The offsets between the marker completion timestamps and the hardware trace varied by at most **43 ns**. Each request contained eight raid render buffers with vertex and fragment activity. The earlier client had zero overlapping GPU work. All **16** final PNGs matched the reference byte for byte. [Timing, work counts and verification](results/raid-metal-replay-contention.json)

| Test queue draws per buffer | Median test buffer GPU time | Raid median GPU active, control / loaded | Median time across four raid renders, control / loaded |
| --- | ---: | ---: | ---: |
| 8 | 0.23 ms | 4.04 / 4.00 ms | 39.03 / 42.94 ms |
| 64 | 1.02 ms | 1.39 / 3.30 ms | 38.37 / 49.33 ms |

With 64 draws per test buffer, the paired median GPU active time increased by **0.68–4.32 ms** in all four pairs. Frames above 2 ms increased from **6 to 11 of 16** in that set. During each loaded request the test queue was active for a median **235 ms**, compared with **180 ms** in the eight-draw set. These workloads differed in both buffer duration and total activity. Median WindowServer overlap also varied, from **0.21 to 3.26 ms** in the 64-draw set and from **3.10 to 3.53 ms** in the eight-draw set. The experiment measures the replay response under injected workload and concurrent desktop activity. The live client provides the gameplay FPS and p99 measurements. [Paired observations](results/raid-metal-replay-contention.json)

Build the helper with `xcrun swiftc -O benchmarks/metal-replay-contention.swift -o raid-contention`. Start it with `8` or `64` as its argument and keep its stdin and stdout open. Start a 15-second Metal System Trace attached to the archive's `GPUToolsReplayService` process. Warm up by fetching the final attachment before timing. Allow the recorder to begin collecting hardware work, then submit an unmeasured `mark 100`. For trial `i`, fetch the first draw's attachment, send `mark 2*i` with the computed integer, wait for the marker, and fetch the final attachment. In the loaded arm, send `pulse i` before the final fetch and wait for its `armed` event. Collect the `pulse` record after the fetch. End each trial with `mark 2*i+1` after at least 300 ms and after the pulse ends. Store the full-size final images as `sample-0.png` through `sample-7.png`.

Export the trace's `metal-gpu-intervals` table to `gpu.xml`. Record `archive`, `replayer_pid`, `load_pid`, `load_draws_per_buffer` and `legacy_outputs_during_experiment` in `witness.json`. Each row of `samples` carries `name`, `mode`, `begin`, `end`, `fetch_wall_ns` and `load_buffers`. Use the helper's marker records for both edges and its `gpu_buffers_ns` list for the loaded arm. The control uses an empty list. GPU activity in the intervening render buffers supplies the measurements.

```sh
python3 benchmarks/summarize-raid-replay-contention.py SHORT_CAPTURE LONG_CAPTURE --legacy-pid EARLIER_CLIENT_PID
```

### Halve the continuing-pass draw budget in the live raid

Use the accepted PGO AArchX runtime and PGO WineMetalGL library in both arms. `WINEMETALGL_COMMIT_DRAWS=64` sets the ordinary continuing-pass limit to 512 draws. Set it to `32` to reach the ordinary limit at 256 draws. Keep `WINEMETALGL_IDLE_FIRST_DRAWS=256` in both arms. At the smaller budget the ordinary limit meets the idle-first threshold, so the early path has no draw credit to carry. The setting also lowers the base draw-count gate for other pass transitions. The live submission logs show the actual split for each output.

Use four warmed 60-second captures in `64, 32, 32, 64` order, with submission logging off. Make separate 35-second diagnostic captures for each setting with the submission log enabled. Record 16 seconds of Metal System Trace within those windows and export `metal-gpu-intervals` and `metal-application-encoders-list`. Run the eight-second ordered indexed replay with three outputs in flight at each setting and verify the final framebuffer hashes. Use the same linked renderer in the replay and in the live launch.

| Continuing-pass limit | Median GPU-completed FPS | Median p99 GPU completion gap |
| --- | ---: | ---: |
| 512 draws | 143.13 | 12.22 ms |
| 256 draws | 143.15 | 12.31 ms |

The pairs changed by **−0.27 and +0.32 FPS**. P99 increased by **0.135 and 0.034 ms**. Keep the 512-draw baseline. All six runs verified the saved camera, overlapping effects, 26 timing rows, consecutive GPU outputs and zero trial windows or activations. The ordered indexed replay produced the same RGBA hash and **161,327** colored pixels at both settings. [Full raid timings and replay comparison](results/raid-winemetalgl-256-draw-split-ab.json)

Every control interval used two draw-bearing submissions. At 256 draws, **4,092 of 4,601** intervals used four and the other **509** used three. The median GPU span of the final submission fell from **1.21 to 0.53 ms**. The median combined span of earlier submissions rose from **0.63 to 2.05 ms**. Within the two Metal recordings, median vertex-and-fragment activity between successive output completions rose from **2.01 to 2.21 ms**, and fragment activity rose from **1.26 to 1.42 ms**.

In the slowest one percent of the instrumented completion gaps, median final-buffer vertex activity fell from **5.38 to 0.067 ms**. Across the complete gap, median vertex activity was **6.56 and 6.51 ms**, and the union of vertex and fragment activity was **9.23 and 9.30 ms**. Median WindowServer activity in those gaps was **9.35 and 8.69 ms**. The traces cover separate 16-second periods with clock alignment within **42 ns**. The four uninstrumented runs supply the throughput and p99 comparison. [Joined GPU stages and submission timings](results/raid-winemetalgl-256-draw-split-ab.json)

```sh
python3 benchmarks/summarize-raid-256-draw-split.py CAPTURE_ROOT PGO_LIBWINEMETALGL
```

### Use hardware floating-point behavior for native i386 translation

An ordinary ad-hoc signed ARM64 process on this Mac reports cross-architecture support and resolves the thread compatibility API. Its request returned **5** (`KERN_FAILURE`). This records hardware store-order availability for the local test process. The AFP trial uses the separate `FEAT_AFP` hardware feature reported by the M4 Pro.

Apply `patches/aarchx-m32-native-afp-trial.patch` to the accepted AArchX source layers. Set `OCERZ_M32_AFP=1` to turn on `FPCR.AH` for translated i386 floating-point operations. The existing i386 trap, leaf and callback boundaries handle transitions between guest FPCR and host code. Use a common `-O2` native runtime and the accepted PGO WineMetalGL renderer. Keep `OCERZ_TCACHE=off` and leave the AFP switch unset in control. Enable `OCERZ_M32_AFP_LOG=1` only in the separate diagnostics.

With `OCERZ_DIFF32_AFP=1`, the differential harness enables the hardware arithmetic path for its translated code. Both modes passed **40,044** interpreter comparisons in each of the offset and low-shadow layouts and **805** general i386 checks passed. With `OCERZ_DIFF32_HAND_STATE_HASH=1`, the warm JIT outputs for the **44** hand cases had identical state hashes between modes in both layouts. These hashes use the defined flags and valid x87 images checked by the existing comparator.

For both 25-second diagnostics, set `OCERZ_JITDIS` to a fresh `jit` prefix and use `OCERZ_JITDIS_BRIEF=1`, `OCERZ_JITDIS_LO=0x880c70` and `OCERZ_JITDIS_HI=0x882c7f`. The same **197** guest block shapes matched across the two logs. The **242** selected floating-point operations occupied **1,216** reported ARM64 words in control and **906** with AFP. These are static code lengths for matching guest instructions in the hot quad routine.

Write the results of the ordinary host probe to `layout-standard.log`. Build and run `benchmarks/m32-memory-layout-probe.c` with `xcrun clang -arch arm64`. Keep the two diagnostic capture directories and the four warmed 60-second runs in control, AFP, AFP, control order. Save SHA-256 witnesses with the timing data.

| Floating-point handling | Median GPU-completed FPS | Median p99 GPU completion gap |
| --- | ---: | ---: |
| Software checks | 140.43 | 12.43 ms |
| Hardware AFP | 135.54 | 12.48 ms |

AFP lost **5.18 and 4.58 FPS** in the two pairs. P99 changed by **−0.27 and +0.36 ms**. Keep software checks as the baseline. The four timing runs and both diagnostics verified the saved camera, overlapping spell effects, all 26 cast timing rows, consecutive GPU completions, and zero trial windows or activations. The earlier test client resumed after every launch. [Live comparisons and instruction counts](results/raid-aarchx-i386-afp-ab.json)

`benchmarks/m32-afp-transition-probe.c` times the existing `ocerz_afp_host` and `ocerz_afp_restore` helpers in an isolated loop. Link it against the trial runtime objects, omitting `main.o` and `fex.o`, using `-lcompression -lobjc`. Write four repetitions per arm to `transitions.log` in control, AFP, AFP, control, control, AFP, AFP, control order. Set `OCERZ_NO_AFP=1` for control and unset it for AFP. The pair took **1.78 ns** with software checks and **21.78 ns** with AFP in the isolated measurement. The live timing includes the remaining bridge work and all guest execution. [Transition measurements](results/raid-aarchx-i386-afp-ab.json)

```sh
python3 benchmarks/summarize-raid-i386-afp.py CAPTURE_ROOT PGO_LIBWINEMETALGL
```

### Count FPCR transitions and preclassify i386 diagnostic dispatch

Apply `patches/aarchx-m32-afp-crossings-trial.patch` after the native AFP trial for the crossing diagnostic. Enable `OCERZ_M32_AFP=1`, `OCERZ_M32_AFP_LOG=1` and `OCERZ_M32_AFP_CROSS_LOG=1`. The probe records the trap or direct leaf only when entering the host clears `FPCR.AH`. It also counts host-to-guest callback pairs that set the bit. The WineMetalGL swap thread reports each complete block of 256 swaps. Use two verified 35-second captures under `crossing-1` and `crossing-2`, and save the runtime and renderer hashes in each capture and at the root.

The analyzer retained **4,608** and **4,352** swaps in complete report intervals inside the verified GPU windows. The two captures recorded **13,785** and **13,961** FPCR pairs per counted swap. OpenGL-named imports accounted for about **9,810–9,936** pairs per swap, mainly direct leaves. Special traps accounted for **4,529–4,594** pairs per swap. The most frequent special traps were `_sinf` and `_floorf`, at roughly a thousand each. The callback contribution was about seven per swap.

Multiplying the measured crossing rate by the isolated **20 ns** incremental transition cost estimates **0.276–0.279 ms** per swap. The earlier live AFP comparison added **0.257 ms** per average GPU completion. The estimate uses swap-thread counts from separate instrumented launches and the isolated cost of a pair of transitions. [Measured crossing counts and links to the prior timing](results/raid-aarchx-trap-dispatch-ab.json)

Run the diagnostic dispatch experiment on a separate copy of the accepted runtime with software floating-point checks. Apply `patches/aarchx-m32-preclassify-diagnostics-trial.patch` to that copy. Set `OCERZ_M32_PRECLASSIFY_DIAGNOSTICS=1` to store the diagnostic kind with each export when it is published. The trap uses that value to select the existing exception-port, QuickTime-selector, and event-handler checks. Each override still checks its environment variable at dispatch, and trap handlers retain their ordinary behavior. Run `test_m32` and `test_m32 trap-diagnostics` under both settings. The focused checks cover activating and removing overrides, the QuickTime selector and another selector, an unrelated export name, and event-handler fallthrough.

Place four warmed 60-second windows in `dispatch/timed-1-0`, `timed-2-1`, `timed-3-1`, and `timed-4-0`. Keep the same signed `-O2` runtime and accepted PGO renderer across these windows. Use software floating-point checks and leave the crossing log off. Verify the mode with `trap-dispatch-mode.txt` and write each run's signed runtime and renderer hashes. Store the two focused and two general test outputs in `dispatch`.

| Diagnostic dispatch | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| Classify the name at dispatch | 142.38 | 12.06 ms |
| Classify the name at export | 141.41 | 12.25 ms |

Throughput changed by **−1.37 and −0.58 FPS**. P99 increased by **0.21 and 0.17 ms**. Keep the dispatch baseline. Both modes passed **32** focused tests and **805** general i386 checks. All six live captures verified the saved camera, overlapping spell effects, 26 timing rows, consecutive GPU outputs and zero trial windows or activations. [Live timing, tests and crossing counts](results/raid-aarchx-trap-dispatch-ab.json)

```sh
python3 benchmarks/summarize-raid-trap-dispatch.py CAPTURE_ROOT PGO_LIBWINEMETALGL
```

### Measure libm reuse and call i386 math functions directly

Apply `patches/aarchx-m32-raid-math-cache-profile.patch` to the accepted source layers. Set `OCERZ_M32_MATH_PROFILE=1` and `OCERZ_M32_MATH_CACHE=0` in two separate 25-second live captures under `profile-1` and `profile-2`. The profiler models a 256-entry direct-mapped cache per function. The key carries both argument words, the input FPSR and errno. It admits ordinary binary32 values and signed zeros in the default FPCR mode. Each observed hit still executes the original function and checks the exact result bits, final FPSR and errno against its cached output. The report uses complete 256-swap intervals inside the verified GPU windows.

| Operation | Calls per counted swap, range | Simulated hit rate, range | Sampled host calculation per call |
| --- | ---: | ---: | ---: |
| `sinf` | 991–1,227 | 38.5–39.5% | 17.0–17.3 ns |
| `cosf` | 417–608 | 62.5–71.8% | 18.7–20.0 ns |
| `floorf` | 1,019–1,588 | 8.8–14.2% | 15.2–15.3 ns |
| `atan2f` | 338–356 | 40.9–42.0% | 17.6–18.2 ns |

The observed hits preserved all checked output state. Host samples include the calculation and probe clock overhead. Multiplying the hit counts by the sampled mean calculation times gives **17.2–20.2 µs per swap** before charging for cache lookups. Run `test_m32 math-cache` with `OCERZ_M32_MATH_CACHE=0` and `15` to exercise both code paths. The **2,304** focused checks per mode compare math result bits, FPSR and errno across four rounding modes, preexisting sticky flags, ordinary inputs, signed zeros, subnormals, infinities and NaNs. The direct-leaf comparison measures bypassing the general i386 trap dispatch. [Math profile and tests](results/raid-aarchx-math-leaf-ab.json)

Apply `patches/aarchx-m32-direct-math-leaf-trial.patch` on top of the profiling patch on a separate copy. The candidate selects the four known libSystem math handlers by their registered function identities. The translated leaf keeps the guest stack return path and uses the shared x87 result setter. It retains errno loading and host FPCR handling. A pending signal or interrupt takes the ordinary dispatcher. Keep `OCERZ_M32_MATH_CACHE=0`, `OCERZ_M32_MATH_PROFILE=0`, and `OCERZ_TCACHE=off` in both arms. Set `OCERZ_M32_DIRECT_MATH_LEAF=1` for the candidate.

The **167** focused checks in each arm run i386 guest calls through the interpreter and JIT. They compare the stored x87 result bytes and guest errno and verify that the older x87 stack entry survives each call. Set `OCERZ_NO_CACHE_HOLDER=1` for the focused `test_m32 math-leaf` runs. Use `OCERZ_M32_MATH_LEAF_LOG=1` in the focused candidate and the separate 25-second in-world diagnostic. The guest fixtures and the live diagnostic compiled all four direct leaves. The timing experiment uses four warmed 60-second runs in control, candidate, candidate, control order. Keep the same signed `-O2` runtime and accepted PGO renderer with logging off in all four windows.

| i386 libm dispatch | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| General trap | 135.46 | 12.64 ms |
| Direct math leaves | 136.67 | 12.72 ms |

Throughput gained **1.03 and 1.38 FPS**. P99 moved by **+0.29 and −0.12 ms**. Keep the direct leaves as a throughput option. Both modes passed **805** general i386 checks. The four timing runs, two profiling launches and direct-leaf diagnostic verified the saved camera, overlapping spell effects, all 26 cast timing rows, consecutive GPU outputs and zero trial windows or activations. [Live diagnostic and paired timing](results/raid-aarchx-math-leaf-ab.json)

```sh
python3 benchmarks/summarize-raid-math-leaf.py CAPTURE_ROOT PGO_LIBWINEMETALGL
```

### Compare the budget for GL state calls and parallel item preparation

The four archived GL timelines matched **256** GPU outputs at full resolution, with consecutive completions in each capture. Median traced time per frame in `glVertexAttribPointer`, `glLightModelfv`, `glDisable`, `glActiveTexture`, and `glClientActiveTexture` together ranged from **0.123 to 0.135 ms**. `glVertexAttribPointer` alone accounted for **0.055–0.062 ms**. The times cover the renderer entry-to-return intervals in these instrumented recordings. Guest and bridge work outside those intervals has separate costs. [GL timeline captures](results/raid-winemetalgl-command-timeline.json)

The preparation model uses **428** swap intervals from the six previously recorded probe gates. It leaves all cross-swap iterations and index-changing iterations serial. Within each run between index changes, it bounds the eligible worker time by the larger of total eligible time divided by worker count and the largest individual task. It preserves the measured cost of the rest of each iteration and the gaps between iterations. All proposed worker paths still need a dependency check and must pay for staging, synchronization, and ordered publication. [Original preparation traces](results/raid-aarchx-i386-item-preparation.json)

| Total workers including the calling thread | Eligible work in each index-preserving iteration | Ideal median savings | Lowest gate median |
| --- | --- | --- | --- |
| 2 | Half | 0.46 ms | 0.40 ms |
| 2 | All | 0.91 ms | 0.80 ms |
| 4 | Half | 0.68 ms | 0.58 ms |
| 4 | All | 1.35 ms | 1.17 ms |

If all the modeled savings reached the critical path, **0.68–1.35 ms** at the selected **140.78 FPS** baseline would correspond to about **156–174 FPS**. Verify a candidate by isolating a preparation unit with measured disjoint outputs, replaying the original input, and comparing the emitted buffer bytes and draw order before timing the full fixture. GPU completion p99 remains a separate measurement. [Recomputed bounds](results/raid-preparation-budget.json) · [Baseline](results/raid-aarchx-runtime-pgo-ab.json) · [GPU stage timing](results/raid-metal-burst-replay.json)

```sh
python3 benchmarks/summarize-raid-preparation-budget.py PREPARATION_CAPTURE_ROOT \
    GL_CAPTURE_1 GL_CAPTURE_2 GL_CAPTURE_3 GL_CAPTURE_WITH_DRAW_REASONS
```

### Trace all emitter calls in a quad batch

The diagnostic records the arguments and elapsed ticks for the original wrapper at `0x882c80` and each call to the emitter at `0x880c70`. It retains the four stream starts, strides and cursor counts at both ends of the tracked write path. For each whole wrapper, the analyzer checks the original call count, the output byte ranges, and their overlap with the tracked particle arguments, color words, normal source, shared context and cursor. It treats repeated zero-stride normal writes to one address as one range when every recorded normal word agrees. Cursor publication, bounds updates and other effects still require an ordering and dependency check.

Six recording gates contain **15,758** complete nonempty batches and **202,768** emitter calls. The gate medians range from **3 to 7** calls per batch. The largest contains **374** calls. **10,776** batches recorded exactly one tracked write interval per call, with continuously advancing cursors and no overlap in the tracked byte ranges. Another **4,982** wrappers had one or more calls outside that recorded write shape or missing a probe boundary. **553** qualifying batches contain at least 32 calls and account for **55.3%** of all recorded emitter call time. [Batch counts and timing](results/raid-aarchx-quad-batch-layout.json)

Time before the tracked writes in qualifying batches accounts for **13.54 ms** of the recorded **76.23 ms** in emitter calls. It includes any work done by callees before the first recorded write boundary. The four-worker model bounds each eligible batch by the larger of one fourth of that elapsed time and its longest call prefix. It gives **8.90 ms** of ideal savings across the six gates, or **5.30 ms** with a hypothetical **2 μs** overhead for each batch selected by the model. A separate pair of weighted CPU profiles places emitter call returns in **6.93% and 7.22%** of all samples by weight. Applying the ideal prefix savings fraction to those profiles puts this prefix alone at roughly **0.8% of whole-thread time**. Actual speedup requires independent inputs and measured staging costs. [Worker model](results/raid-aarchx-quad-batch-layout.json) · [Earlier CPU captures](results/raid-aarchx-native-instructions-in-guest-call-trees.json)

Apply `patches/aarchx-m32-raid-quad-batch-probe.patch` after the quad layout probe. Keep the profiled renderer and use the same headless launch and registry witness as the layout captures. Arm the ring for **70 ms** at 2, 12 and 24 seconds into each verified GPU window. An initial 140 ms configuration filled the bounded ring and was discarded. The two accepted 35-second diagnostics verified **4,654** and **4,693** consecutive GPU outputs, the saved camera, overlapping effects, all 26 timing rows, and zero visible windows or activations. The runtime passed **805** general i386 checks with the probe disabled.

```sh
python3 benchmarks/summarize-raid-quad-batch.py CAPTURE_ROOT I386_CLIENT \
    --cpu-profiles EARLIER_PROFILE_1 EARLIER_PROFILE_2
```

### Trace dependencies between raid preparation items

The diagnostic records explicit flat-address memory operands in six i386 code ranges, together with the existing dispatcher and linked-list item markers. It skips unindexed stack-register operands, excludes observed addresses in the 4 KiB preceding the caller's frame pointer through its return-address word, and counts calls into other ranges separately. The analyzer matches scopes on the same guest CPU within one complete swap interval. It compares each prior item's last observed write with the next item's first read of the byte before that item has written it. The memory trace identifies specific overlap and leaves omitted accesses open.

| Original order examined | Pairs | Observed read-after-write overlap |
| --- | ---: | ---: |
| Consecutive dispatch items with the same work context | 2,311 | 2,121 |
| Consecutive list updates of different items | 271 | 0 |
| Consecutive list consumers of different items | 696 | 688 |
| List update followed by the consumer of the same item | 75 | 75 |

The dispatcher had **607** adjacent pairs with overlap outside offsets `0x50–0x9f` of the work context. The most frequent context dependency was the write at `0x97d3b9` and the comparison at `0x9791bf`, both using offset `0x5c`. The consumer pass reads and replaces a linked-list head at `0x968501` and `0x968507`. Keep that publication ordered when testing independent computation. For a consumer whose own update was complete, **718** comparisons with later updates of other items had zero observed read-write or write-write conflicts. All recorded list updates called paths outside the selected ranges. These counts establish candidate work to audit before reordering. [Per-gate counts and instruction witnesses](results/raid-aarchx-i386-item-memory.json)

The two most frequent calls omitted from the update pass went to `0x8c17c0` and `0x8c1e90`, **9,058** times each. The latter writes sixteen 32-bit results through one argument and performs ordered scalar SSE multiply and add operations. The decoded entry-to-return range represented **2.25% and 2.04%** of independently sampled CPU time. It is a candidate for extracting local math while preserving the shared publication order. A replay should verify all sixteen results and input-output aliasing before using a native path. [Call sites and earlier CPU coverage](results/raid-aarchx-i386-item-memory.json)

Apply `patches/aarchx-m32-raid-item-memory-probe.patch` after the preparation probe. Set `OCERZ_M32_RAID_PREP_MEMORY=1`, use a fresh ring of `64 × (2²³ + 1)` bytes, and arm it for **70 ms** at offsets **2, 12 and 24 seconds** after the verified full-raid window starts. The first launch requested the timing window before the draw count reached the raid range. The next attempt filled the smaller ring. The accepted build splits at retained memory instructions and excludes direct stack-relative operands. Its two diagnostic windows verified **1,689** and **1,677** consecutive GPU completions, the saved camera, overlapping effects, and all 26 timed rows. They averaged about **48 FPS** with the diagnostic block boundaries and logging in place. The runtime passed **805** general i386 checks with logging disabled.

```sh
python3 benchmarks/summarize-raid-item-memory.py CAPTURE_ROOT I386_CLIENT \
    --cpu-profiles EARLIER_PROFILE_1 EARLIER_PROFILE_2
```

### Run the verified i386 matrix product on ARM64

The original function at `0x8c1e90` loads both 4 × 4 matrices before the first output store. Disassembly verifies **280** scalar instructions in the arithmetic body and the original addition order for all **16** results. The replay checks the final bits of **eight XMM registers** and the formulas for **26 scratch words**. Two 35-second original-client diagnostics record the input and output words at `0x8c1e9f` and `0x8c23de`.

Apply `patches/aarchx-m32-raid-matrix-capture.patch` after the accepted AArchX source layers. Keep the profiled WineMetalGL renderer and translation cache off. Set `OCERZ_M32_RAID_MATRIX_LOG` to a new `matrix.bin` file and follow the quad-layout capture procedure. Arm each ring for **55 ms** at offsets **2, 12 and 24 seconds** in two verified 35-second launches. The probe skips snapshots that would cross a guest page and reports unpaired boundaries separately.

For native execution, apply `patches/aarchx-m32-raid-matrix-native-trial.patch` on top and copy `benchmarks/raid-matrix-kernel.h` to `include/ocerz/raid-matrix-kernel.h` before rebuilding. Set `OCERZ_M32_RAID_MATRIX=1` for the native path. The JIT checks the original machine-code fingerprint. The helper checks guest permissions under the mapping lock, preserves the original rounding order without FMA, and writes the original result, scratch and XMM state. A pending signal, unexpected floating-point controls, unsuitable memory or input exponents outside the supported range use the normal translation. The original guest epilogue still runs.

Use the same signed runtime in all four 60-second timings. Run control, native, native, control with both matrix logging flags unset. In a separate 25-second native diagnostic, set `OCERZ_M32_RAID_MATRIX_WITNESS=1` and capture the input and output ring at offsets **2, 10 and 18 seconds**. Export that capture with `--native` from `CAPTURE_ROOT/timing`.

```sh
python3 benchmarks/export-raid-matrix-replay.py CAPTURE_ROOT I386_CLIENT \
    CAPTURE_ROOT/matrix-cases.bin > CAPTURE_ROOT/matrix-export.json
python3 benchmarks/export-raid-matrix-replay.py CAPTURE_ROOT/timing I386_CLIENT \
    CAPTURE_ROOT/timing/matrix-cases.bin --native > CAPTURE_ROOT/timing/matrix-export.json
xcrun clang -arch arm64 -O3 -ffp-contract=off -fno-vectorize -fno-slp-vectorize \
    benchmarks/raid-matrix-native-replay.c -o CAPTURE_ROOT/replay
xcrun clang -arch arm64 -O1 -g -ffp-contract=off -fsanitize=address,undefined \
    benchmarks/raid-matrix-native-replay.c -o CAPTURE_ROOT/replay-sanitized
CAPTURE_ROOT/replay CAPTURE_ROOT/matrix-cases.bin > CAPTURE_ROOT/matrix-replay.txt
CAPTURE_ROOT/replay CAPTURE_ROOT/timing/matrix-cases.bin --verify-only > CAPTURE_ROOT/timing/matrix-replay.txt
CAPTURE_ROOT/replay-sanitized CAPTURE_ROOT/matrix-cases.bin --verify-only > CAPTURE_ROOT/matrix-sanitized.txt
CAPTURE_ROOT/replay-sanitized CAPTURE_ROOT/timing/matrix-cases.bin --verify-only > CAPTURE_ROOT/timing/matrix-sanitized.txt
python3 benchmarks/summarize-raid-matrix.py CAPTURE_ROOT
```

The replay matched all 16 outputs and 128 XMM bytes in **241,908** original calls and **131,499** calls from the native-enabled diagnostic. The scalar and NEON versions agreed on the scratch words, and the sanitizer build passed both datasets. All paired calls used the default masked MXCSR controls and passed the exponent guard. The corpus had zero actual output aliases, so the replay separately tested both directions of partial aliasing for every case. The standalone host timings were **17.12 ns** for scalar and **20.52 ns** for NEON per call over the captured corpus. Both benchmarked functions write outputs and XMM state.

| Matrix path | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| Original translation | 140.72 | 12.43 ms |
| Guarded ARM64 helper | 124.09 | 13.54 ms |

The native helper lost **15.97 and 17.28 FPS** in the two pairs. P99 increased by **1.02 and 1.20 ms**. The timed comparison covers the complete path through the helper, including register transfers, mapping checks, the lock and the return to translated execution. The diagnostic logged over **25.9 million** successful native calls on one thread. All four timed launches, the native diagnostic and the two original recordings verified the saved camera, overlapping effects, all 26 timing rows, consecutive GPU completions and zero visible windows or activations. The original translation remains the benchmark baseline. The trial runtime passed **805** general i386 checks. [Replays, per-run timings and artifact hashes](results/raid-aarchx-matrix-native-ab.json)

In the original recordings, **7.64%** of inputs matched the immediately preceding complete call on the same CPU. **21.06%** matched one of its preceding 64 distinct pairs within a recording gate. The comparison uses exact input bytes and MXCSR controls. These rates bound potential reuse for this helper before staging or cache costs. [Observed input recurrence](results/raid-aarchx-matrix-native-ab.json)

### Split the matrix block, decline the callout and chain the native return

Apply `patches/aarchx-m32-raid-matrix-boundary-trial.patch` after the matrix capture and native helper patches. Build one runtime and use the same signed executable and profiled WineMetalGL library for each mode. Keep `OCERZ_TCACHE=off`.

| Timing mode | Launch switches |
| --- | --- |
| Control | Default translation |
| Split | `OCERZ_M32_RAID_MATRIX_SPLIT=1` |
| Decline | `OCERZ_M32_RAID_MATRIX=1 OCERZ_M32_RAID_MATRIX_DECLINE=1` |
| Native | `OCERZ_M32_RAID_MATRIX=1` |
| Chain | `OCERZ_M32_RAID_MATRIX=1 OCERZ_M32_RAID_MATRIX_CHAIN=1` |

Split changes the generated block boundary at `0x8c1e9f`. Decline also performs the native ABI transition and returns immediately to the original translated body. Native executes the guarded matrix helper and resumes at `0x8c23de` through the dispatcher. Chain uses the existing interrupt-aware body edge to enter the same epilogue. Retirement and an unavailable target still use the dispatcher. The decline-to-native comparison includes both the removed scalar body and the new guards, calculation and return path.

Time each mode for **60 seconds** in the table order and then the reverse order. Store launches under `timed-1-control` through `timed-10-control`. Record the mode and logging bit in `matrix-boundary-mode.txt`. Leave both logging switches unset during timing. In a separate 25-second chain launch under `diagnostic`, enable `OCERZ_M32_RAID_MATRIX_WITNESS=1` and the existing matrix capture ring. Record `chain 1` in `matrix-boundary-mode.txt` and `1 1` in `matrix-mode.txt` for the shared exporter. Export its data with `--native`, check scalar and NEON replay in the ordinary and sanitizer builds, and retain the replay files at the same paths used by the matrix experiment.

```sh
python3 benchmarks/export-raid-matrix-replay.py CAPTURE_ROOT I386_CLIENT \
    CAPTURE_ROOT/matrix-cases.bin --native > CAPTURE_ROOT/matrix-export.json
REPLAY_BINARY CAPTURE_ROOT/matrix-cases.bin --verify-only > CAPTURE_ROOT/matrix-replay.txt
SANITIZER_REPLAY_BINARY CAPTURE_ROOT/matrix-cases.bin --verify-only > CAPTURE_ROOT/matrix-sanitized.txt
python3 benchmarks/summarize-raid-matrix-boundary.py CAPTURE_ROOT
```

| Timing mode | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| Control | 139.17 | 12.48 ms |
| Split | 137.98 | 12.38 ms |
| Decline | 135.48 | 12.81 ms |
| Native | 131.33 | 12.93 ms |
| Chain | 132.16 | 12.94 ms |

Splitting lost **0.93 and 1.43 FPS**. Adding the empty callout lost another **2.32 and 2.70 FPS**. Running the guarded product in place of the original body lost **3.23 and 5.05 FPS** relative to decline. Chaining to the epilogue recovered **0.25 and 1.40 FPS**. The two chain runs stayed **6.23 and 7.78 FPS** below their paired controls. Keep the original translation as the benchmark baseline. [Per-run and paired timing](results/raid-aarchx-matrix-boundary-ab.json)

The chain-enabled diagnostic logged a successful link to the original guest epilogue and supplied **137,710** paired calls. Both replay builds, including the sanitizer build, matched the recorded outputs and XMM contents. All ten timing windows and the diagnostic verified the saved camera, overlapping effects, 26 timing rows, consecutive GPU completions and zero visible windows or activations. The runtime passed **805** general i386 checks. [Chain witness and replay](results/raid-aarchx-matrix-boundary-ab.json)

### Expand both preparation calls inside the translated caller

Apply `patches/aarchx-m32-raid-prep-inline-trial.patch` to the accepted AArchX source layers. Use `OCERZ_M32_PREP_INLINE=1` and `OCERZ_TCACHE=off` for the expanded path. The compiled caller contains the original instructions at the two direct calls from `0x965473` and `0x96548f` through their returns, followed by the caller's writes to the scene item. The supported path keeps the original i386 stack slots, checks the saved return target, and applies both the four-byte return and the immediate stack adjustment. A changed target resumes through the dispatcher with the actual return address.

The normal decode budget is **256** instructions. The selected preparation caller can use up to **448**. The runtime checks the direct targets and accepts the two helpers through their straight-line i386 instruction sequences. The emitted blocks use the existing invalidation tracking for the caller and the helper addresses.

Use the same signed runtime and profiled WineMetalGL library for four warmed 60-second captures under `timed-1-0`, `timed-2-1`, `timed-3-1`, and `timed-4-0`. Unset the expansion switch for control. Record its setting and the logging bit in `prep-inline-mode.txt`. In the separate 25-second `diagnostic` launch, set `OCERZ_M32_PREP_INLINE_LOG=1` to record both callee lengths and the compiled block.

Run `test_jit32` with `OCERZ_TEST_JIT32_PREP_INLINE=1` in both modes. Enable `OCERZ_M32_PREP_INLINE` and `OCERZ_M32_PREP_INLINE_LOG` for the candidate. Save the logs as `focused-control.log` and `focused-candidate.log`, and save the `test_m32` output as `general.log`. The focused cases compare two consecutive calls, flags, the original return slots, the immediate return adjustment, and a changed return address.

```sh
python3 benchmarks/summarize-raid-prep-inline.py CAPTURE_ROOT I386_CLIENT \
    --cpu-profiles EARLIER_PROFILE_1 EARLIER_PROFILE_2
```

The diagnostic produced three versions of the caller with **422–431 original i386 instructions** and both expanded calls in each block. The original disassembly gives **36** instructions in the first helper and **288** in the second. The caller and those specific invocations account for **0.97–1.03%** of weighted CPU samples in the two earlier profiles. That attribution uses the innermost return address to distinguish other callers of the same helpers. [Compiler witness and CPU coverage](results/raid-aarchx-prep-inline-ab.json)

| Preparation path | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| Existing translated calls | 139.88 | 12.29 ms |
| Both calls expanded | 140.34 | 12.33 ms |

The two pairs changed by **−0.40 and +1.33 FPS**. P99 changed by **−0.08 and +0.17 ms**. Keep the original preparation path as the benchmark baseline. All four 60-second launches and the compiler diagnostic verified the saved camera, overlapping effects, all 26 timing rows, consecutive GPU completions and zero visible trial windows or activations. The expanded path passed both focused differential cases and **805** general i386 checks. [Full-raid timing and validation](results/raid-aarchx-prep-inline-ab.json)

### Encode the live Metal render pass on a serial worker

Apply `patches/winemetalgl-queued-render-encoder-trial.patch` to the accepted WineMetalGL source layers. Build one arm64 Release renderer with ThinLTO and PGO disabled. Both modes use that dylib and the same accepted profiled AArchX runtime. Set `WINEMETALGL_QUEUED_RENDER=1` for the candidate and `0` for control.

The OpenGL thread still validates the draw and resolves its resources. Common Metal state, binding and draw calls become typed records in batches of up to **96** commands. Batches retain object arguments and copy immediate data and viewport/scissor arrays before the caller returns. A serial worker issues each record to the original Metal encoder in submission order. Other methods drain preceding records and forward synchronously. Store actions and pass closure finish before the calling thread can commit the buffer.

Use four warmed 60-second windows in control, queued, queued, control order. Store the mode and logging bit in `queued-render-mode.txt`. In a separate 25-second queued launch under `diagnostic`, set `OCERZ_WMGL_QUEUED_RENDER_LOG` to `queued-encoder.tsv` and attach Time Profiler to that launch after the fixture loads. The TSV counts the whole diagnostic launch, including startup. Export the 9-second profile to `encoder-cpu.xml` with the `time-profile` table.

Save the two GPU suite results under `gpu-tests-0.log` and `gpu-tests-1.log`. Record the indexed replay of `benchmarks/fixtures/raid-triangle-order.tsv` with three in-flight outputs in `replay-0.log` and `replay-1.log`.

```sh
python3 benchmarks/summarize-raid-queued-render.py CAPTURE_ROOT
```

| Render encoder | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| Calling thread | 133.80 | 12.27 ms |
| Serial worker | 133.13 | 12.31 ms |

The queued mode lost **1.10 and 0.25 FPS** in the two pairs. P99 changed by **−0.03 and +0.11 ms**. The standalone indexed replay returned the same RGBA hash and **161,327** colored pixels in both modes. Both modes passed **343** GPU tests. The full-raid diagnostic and all four timing runs verified the saved camera, overlapping effects, all 26 timed rows, consecutive GPU completions and zero visible trial windows or activations. [Live timings and pixel checks](results/raid-winemetalgl-queued-render-ab.json)

The diagnostic logged **21,881,427** queued commands in **235,241** batches, with zero synchronous forwards. The Time Profiler export covered **9.51 seconds** of samples. It attributed **355 ms** of running-sample weight to the worker consuming batches and **271 ms** to the proxy on the game thread. Within that main-thread total, recording commands accounted for **116 ms** and closing encoders for **79 ms**. These are observed running-sample weights from the queued mode. The uninstrumented A/B supplies the throughput comparison. The calling-thread encoder remains the benchmark baseline. [Worker and caller samples](results/raid-winemetalgl-queued-render-ab.json)

### Reuse storage for queued Metal command batches

Apply `patches/winemetalgl-queued-render-reuse-trial.patch` after the queued encoder trial. Enable `WINEMETALGL_QUEUED_RENDER_REUSE=1` with the queued path selected. A serial encoder queue owns **16** reusable slots. The worker clears retained Metal objects and publishes a free slot with release ordering. The game thread acquires it before reusing its vectors. When its next slot is busy, the game thread can allocate an ordinary batch. Batches still contain up to **96** records, and the same drain at pass end precedes command-buffer submission.

Use one signed runtime and one arm64 Release ThinLTO renderer with PGO off. Time control, original queue, and reused queue for **60 seconds** each and then repeat in reverse order. Store the mode and logging bit in `render-reuse-mode.txt`. Unset both capturing and profiling in the six timing launches. Keep the same indexed replay and GPU tests as the queued encoder trial, with log names ending in `-control.log`, `-queued.log`, or `-reuse.log`.

For the separate 25-second launches under `diagnostic-queued` and `diagnostic-reuse`, set `OCERZ_WMGL_QUEUED_RENDER_LOG` to `queued-encoder.tsv`. It records commands, submitted batches, payload bytes, synchronous forwards, reused batches and unpooled batches over the diagnostic launch. Attach Time Profiler for **9 seconds** after the verified fixture loads and export its `time-profile` table to `encoder-cpu.xml` in each directory.

```sh
python3 benchmarks/summarize-raid-render-reuse.py CAPTURE_ROOT
```

| Render encoder | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| Calling thread | 134.09 | 12.41 ms |
| Original queue | 134.49 | 12.34 ms |
| Queue with reused batches | 133.18 | 12.26 ms |

Compared with the original queue, the reuse pairs changed by **+0.47 and −3.08 FPS**, with p99 changes of **+0.10 and −0.26 ms**. Keep the original calling-thread renderer as the benchmark baseline. All six timing launches and both diagnostics verified the saved camera, overlapping effects, all 26 timing rows, consecutive GPU completions and zero visible trial windows or activations. Each mode passed **343** GPU tests. The indexed replay returned the same RGBA hash and **161,327** colored pixels in all three modes. [Timing and verified replay](results/raid-winemetalgl-render-reuse-ab.json)

The original queue created a batch object for each of its **231,384** diagnostic submissions. The reuse launch created **16** batch objects and reused them for the remaining **237,386** of **237,402** submissions. These counts cover whole diagnostic launches. Time Profiler attributed **269 ms** of running-sample weight to the game-thread proxy in the original queue and **238 ms** with reuse. The corresponding worker-consumer weights were **371 and 341 ms**. The profiles covered **9.48 and 9.40 seconds** of samples. These measurements track CPU execution. [Batch counts and CPU samples](results/raid-winemetalgl-render-reuse-ab.json)

### Close the final Metal pass and commit on the encoder worker

Apply `patches/winemetalgl-defer-render-end-trial.patch` after the queued encoder and batch reuse trials. Use one signed AArchX runtime and one arm64 Release ThinLTO renderer with PGO off. Use the existing calling-thread path, the queued path with `WINEMETALGL_QUEUED_RENDER_REUSE=1`, and that same queued path with `WINEMETALGL_DEFER_RENDER_END=1`.

At a flush, the game thread records final store actions and commits any preludes. It reserves a position for the render buffer with `enqueue()` and marks its serial in flight. The existing serial worker issues the last partial batch, ends the encoder and commits the command buffer. The worker inherits ordering with earlier batches, while Metal's reservation orders the buffer against subsequent work on the shared command queue. Transitions to another encoder in the same buffer keep synchronous closure. [Metal's enqueue contract](https://developer.apple.com/documentation/metal/mtlcommandbuffer/enqueue%28%29)

Use six warmed 60-second launches in control, reuse, deferred, deferred, reuse, control order. Record the mode and logging bit in `render-end-mode.txt`. Collect the GPU suites and indexed replay with log suffixes `-control.log`, `-reuse.log`, and `-deferred.log`.

In separate 25-second launches under `diagnostic-reuse` and `diagnostic-deferred`, set `OCERZ_WMGL_RENDER_FINALIZE_LOG` to `render-finalize.tsv`. For the deferred case also set `OCERZ_WMGL_DEFER_RENDER_END_LOG` to `render-worker.tsv`. The analyzer bounds producer timestamps by the first and last completed GPU outputs in the verified interval and matches worker events by queue and serial.

```sh
python3 benchmarks/summarize-raid-render-end.py CAPTURE_ROOT
```

| Render path | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| Calling-thread encoder | 130.87 | 12.45 ms |
| Queued encoder with synchronous close | 128.93 | 12.78 ms |
| Close and commit on the worker | 132.81 | 12.58 ms |

Deferring the close gained **3.68 and 4.07 FPS** over the queued encoder with synchronous close, and **1.42 and 2.47 FPS** over the calling-thread encoder. P99 shortened **0.08 and 0.34 ms** against the queued encoder. Against the calling-thread encoder, p99 changed by **+0.34 and −0.08 ms**. The renderer build held fixed in this experiment has PGO off. [Live results and paired differences](results/raid-winemetalgl-defer-render-end-ab.json)

Inside the diagnostic completion windows, the game thread spent a median **27.67 µs** finalizing a buffer with synchronous closure and **6.44 µs** with deferred closure. The deferred path matched **6,746** producer records to worker commits. In **6,743** cases the worker started after the producer returned. Reservation to commit return took **22.54 µs** at the median and **52.13 µs** at p99. These are elapsed times around the CPU-side flush. The six uninstrumented runs supply the frame timing comparison. [Producer and worker timestamps](results/raid-winemetalgl-defer-render-end-ab.json)

Each mode passed **343** GPU tests, including fence waits and shared-context uploads. The indexed replay returned the same RGBA hash and **161,327** colored pixels. The two diagnostics and six timing windows verified the saved camera, overlapping spell effects, all 26 timing rows, consecutive GPU completions and zero visible windows or activations. [Correctness checks](results/raid-winemetalgl-defer-render-end-ab.json)

### Verify deferred Metal finalization with a shared PGO build

Train with the queued encoder, batch reuse and deferred finalization patches applied. Use the same source for the two arm64 Release ThinLTO builds. Generate the initial LLVM IR profiles with `WMGL_PGO_GENERATE=ON`. Run one warmed 60-second full-raid launch under `training-control` with direct encoding and another under `training-deferred` with the pooled queue and deferred finalization. Use a fresh `LLVM_PROFILE_FILE=wmgl-%p%c.profraw` path inside each capture directory. Merge both continuous profiles at equal weight with `llvm-profdata merge`, then build with `WMGL_PGO_GENERATE=OFF` and `WMGL_PGO_PROFILE` set to the merged file.

Time six 60-second full-raid launches in reference, control, deferred, deferred, control, reference order on the same signed AArchX executable. Reference uses the previously accepted PGO renderer as-is. Control and deferred use the same new profiled renderer and differ by the encoder path. Collect a 25-second deferred diagnostic with both timestamp logs enabled as in the preceding experiment. Repeat the GPU suite and indexed replay for the three modes. Keep the profile counters, matched compiler flags and artifact hashes in `build-flags.json` and `artifact-sha256.json`.

```sh
python3 benchmarks/summarize-raid-render-end-pgo.py CAPTURE_ROOT
```

| Profiled renderer and encode path | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| Previous PGO renderer, direct encoding | 138.85 | 12.18 ms |
| Jointly trained PGO renderer, direct encoding | 138.23 | 12.38 ms |
| Jointly trained PGO renderer, deferred finalization | 141.32 | 11.79 ms |

On the jointly trained build, deferred finalization gained **2.02 and 4.15 FPS** and cut p99 by **0.10 and 1.09 ms**. Against the previous PGO renderer it gained **3.87 and 1.07 FPS** and cut p99 by **0.19 and 0.59 ms**. Use the jointly trained deferred mode as the selected local renderer for subsequent raid measurements that preserve this stack. Keep the reference renderer available for comparisons. [Per-run timings and renderer hashes](results/raid-winemetalgl-defer-render-end-pgo-ab.json)

The control and deferred training windows completed **6,276** and **6,311** GPU outputs. The merged LLVM IR profile contained **11,062** indexed functions. All **79** renderer compile commands matched after removing the generation and use flags. The deferred commit routine and its worker block had active counters in the deferred training run. The diagnostic on the final binary matched **7,100** producer records to worker commits, with **5.75 µs** median finalization time on the game thread. [Training and finalization data](results/raid-winemetalgl-defer-render-end-pgo-ab.json)

All training and timing windows and the deferred diagnostic verified the saved camera, overlapping effects, all 26 timed rows, consecutive GPU completions and zero visible windows or activations. Each mode passed **343** GPU tests and returned the same indexed replay RGBA hash and pixel count. [Validation details](results/raid-winemetalgl-defer-render-end-pgo-ab.json)

### Attribute completion tails on the selected deferred PGO renderer

Run two 45-second verified launches on the selected deferred PGO build with submission and deferred-finalization logging enabled. Record a 22-second Metal System Trace in each launch after the fixture loads. Add Thread Activity for the `scheduler` capture and export its `thread-state` table to `thread-states.xml`. Use the standard Metal template for `stages`. Export `metal-gpu-intervals` to `gpu-intervals.xml` in both directories.

The analyzer matches each final render buffer to its verified GPU completion and to its producer and worker timestamps. It separates the interval between producer submissions from the change in worker handoff latency, delay from encoder close to GPU start, and final GPU span. The scheduler trace also separates main-thread running and nonrunning time. It selected **3,050** contiguous outputs. Among its **31** p99-tail gaps, main-thread running time provided the largest increase above its typical median in **15**, final GPU span in **12**, and main-thread nonrunning time in **4**. Median worker scheduling delay moved from **7.58 to 8.92 µs** in that tail. [Host and GPU attribution](results/raid-winemetalgl-deferred-tail-attribution.json)

The Metal-only capture selected **3,003** contiguous outputs, with its GPU clocks matched within **42 ns**. The slowest **31** gaps had **3.23 ms** of median overlap between the client's GPU work and WindowServer, compared with **0.60 ms** across all intervals. Comparing each tail interval to regular intervals with the same draw counts per submission gave a median **1.79 ms** increase in final GPU span and **2.72 ms** more overlap with WindowServer. Within those matched draw-count groups, effect work and GPU scheduling can vary. [GPU stage and overlap comparison](results/raid-winemetalgl-deferred-tail-attribution.json)

The earlier test's completion log independently brackets the scheduler capture's quiet GPU interval. Its pause lease expired while Instruments saved that archive, **after** the verified output window. The second launch held the lease through its workload and released it before waiting for Instruments to finish writing. Neither GPU trace recorded work from the earlier test. Both verified the saved camera, overlapping effects, all 26 timing rows and zero visible windows or activations.

```sh
python3 benchmarks/summarize-raid-deferred-tail.py CAPTURE_ROOT --legacy-pid EARLIER_TEST_PID
```

### Join the scene preparation roots to the live frame tail

Apply `patches/aarchx-m32-raid-phase-tail-probe.patch` after the preparation probe and rebuild the diagnostic AArchX runtime. Use the selected deferred PGO renderer and its three queue switches. Set `OCERZ_M32_RAID_PHASE_LOG` to `phase-calls.bin`. Arm its ring for **36 seconds** during a verified 45-second launch under `phase`. Keep the submission and worker logs from the preceding experiment and record the armed duration and row count in `phase-gate.json`. Save the general AArchX checks in `general.log`.

The probe pairs the direct calls to `0x73f8e0` and `0x97dbc0` with their returns. It covers **5,004** selected outputs and matches **20,016** calls on one guest CPU. The original disassembly shows a conditional branch into `0x6bf943`; that site accounts for **5,004** of the **5,005** unpaired return-site records. Across final-buffer submission intervals, the two roots together occupied **4.96 ms** at the median. Among the **51** p99-tail gaps, the final GPU span contributed the largest increase in **34**, producer time outside both measured roots in **16**, and the second root in **one**. The root timings are elapsed intervals on the game thread. The diagnostic passed **805** general i386 checks and verified the full scene and cast schedule. [Preparation phases and completion gaps](results/raid-aarchx-preparation-root-tail.json)

```sh
python3 benchmarks/summarize-raid-tail-phases.py CAPTURE_ROOT I386_CLIENT
```

### Measure work inside the first preparation root

Apply `patches/aarchx-m32-raid-scene-work-probe.patch` after the phase-tail probe. Rebuild the same diagnostic AArchX runtime with the selected deferred PGO renderer. Set `OCERZ_M32_RAID_ROOT_WORK_LOG` to a fresh `root-work.bin` in the `phase` directory. Arm the ring for **30 seconds** starting two seconds after the fixture is ready during a verified 45-second run. Keep the renderer mode, artifact, submission, and visibility records and save the gate count to `phase-gate.json`. Save the general AArchX check output to `general.log`.

The launch produced **6,171** consecutive GPU completions and verified the camera, overlapping effects, 26 script rows, and zero trial window visibility or activations. The trace paired call and return sites on one calling CPU using the original guest stack and recorded **4,098** complete producer intervals. It includes the caller's original item pointer for both the update and consumer calls. The diagnostic runtime passed **805** general i386 checks. These timings include the inserted JIT boundaries and logging. [Call timings and artifact hashes](results/raid-aarchx-scene-work.json)

| Inside the first preparation root | Median elapsed time per producer interval |
| --- | ---: |
| Entire root `0x73f8e0` | 2.408 ms |
| Scene pass `0x97e390` | 1.309 ms |
| Object updates `0x964ca0` within that pass | 0.420 ms |
| Consumer calls `0x967f20` within that pass | 0.384 ms |
| Three heapsort calls within that pass | 0.168 ms |
| Largest other measured sibling `0x738830` | 0.360 ms |

All measured updates in each scene pass completed before the first consumer. At the median, the pass updated **56** objects and called the consumer **179** times. An optimistic eight-worker schedule preserves the observed gaps between updates and waits for the last result at the old update boundary. With a hypothetical 500 ns delivery cost per object it could save **0.355 ms** at the median, about **7 producer intervals per second** averaged over the joined trace. This assumes the worker inputs are available without waiting for earlier results. Dependencies through omitted callees and worker costs beyond delivery remain unverified. Removing the *entire* 1.309 ms scene pass from the recorded producer intervals gives only **166.4 intervals per second** with all other costs held fixed. [Batch model](results/raid-aarchx-scene-work.json) · [Item data dependencies](results/raid-aarchx-i386-item-memory.json)

The preceding two-root trace measured **4.98 ms** mean across both roots and **2.21 ms** mean of other producer time per frame. A fourfold speedup across both roots with the remainder held fixed would give **289** producer intervals per second. Even zero time for both roots gives **452**. These are producer throughput estimates from recorded intervals. The 2.0 ms completion target also needs reductions elsewhere on the producer and in GPU tails. In the two-root trace, the final command buffer's GPU span had a **6.18 ms p99**. [Two-root timings](results/raid-aarchx-preparation-root-tail.json) · [GPU scheduling and contention](results/raid-winemetalgl-deferred-tail-attribution.json)

```sh
python3 benchmarks/summarize-raid-scene-work.py CAPTURE_ROOT I386_CLIENT
```

### Measure mapped particle groups before ordered publication

Apply `patches/aarchx-m32-raid-particle-paths-probe.patch` after the scene-work probe and use the same selected deferred PGO renderer. Set `OCERZ_M32_RAID_PARTICLE_PATHS_LOG` to a fresh `particle-paths.bin` in the `phase` directory. Arm the ring for **8 seconds**, beginning two seconds after the fixture is ready during a verified 45-second launch. Use the same gate metadata, artifact and visibility checks as for the scene-work recording. The first 30-second gate exceeded the ring capacity and was discarded.

The three largest dispatcher paths ran through separate calls to `0x97caa0`, `0x979130` and `0x97b970`. In **1,091** complete producer intervals they accounted for median elapsed times of **0.928**, **0.833** and **0.796 ms**, respectively. The particle path in this window went through the grouped fill under `0x97c340`. The standalone fill path had zero recorded calls in the joined window. [Call times and verified GPU outputs](results/raid-aarchx-particle-paths.json)

The grouped path maps a buffer, advances its cursor by the number of bytes reported after each fill, then unmaps and publishes. The trace matched **316,049** fills in **57,686** mapped groups. It checks the mapped pointer returned by the original call, each cursor advance and the matching unmap. The medians were **52 groups**, **274 fills**, and **393,504 bytes of vertex cursor advance** per producer interval. Geometry fills took **0.587 ms**. The quad batch nested within those fills took **0.522 ms**. Mapping took **0.092 ms**, and the following publication call took **0.168 ms**. These are elapsed intervals on one calling CPU. [Mapped lifetimes and call timings](results/raid-aarchx-particle-paths.json)

Across **314,533** fills matched to the same emitter address and occurrence in the immediately preceding frame, **84.2%** had the same pair of vertex advance and logical index size. Address reuse can affect the match. A new implementation needs a current-frame size or independently owned output storage before assigning ranges. The eight-worker model gives median savings of **0.228 ms** when every fill in each individual group is eligible, original inter-call time stays on the producer, and dispatch costs **500 ns per fill**. It preserves the final group barrier. This estimate leaves shared emitter state, byte-for-byte equivalence, memory traffic and staging to a candidate implementation. [Output sizes and model](results/raid-aarchx-particle-paths.json)

The diagnostic verified **6,195** consecutive full-resolution GPU completions, the saved camera, overlapping effects, all 26 cast timing rows, and zero visible trial windows or activations. The earlier trial was paused during the capture and resumed after it. The runtime passed **805** general i386 checks.

```sh
python3 benchmarks/summarize-raid-particle-paths.py CAPTURE_ROOT I386_CLIENT
```

### Reuse both i386 memory bases in straight-line blocks

Apply `patches/aarchx-m32-dual-base-hoist-trial.patch` to the selected AArchX runtime source layers. Use the same arm64 runtime build and selected deferred PGO WineMetalGL library in both modes. Both modes keep `OCERZ_M32_GENERAL_BASE_HOIST=1` and `OCERZ_TCACHE=off`. The candidate adds `OCERZ_M32_DUAL_BASE_HOIST=1`.

The translator selects a second pinned base register when at least six supported memory operands use it before reassignment. Each base is checked at entry with the existing 32-bit wrap guard. The generated loads and stores use the original width and order. Each register's guarded prefix stops at its own reassignment. A guard rejection retires the block and resumes through the ordinary translation.

Run four warmed 60-second full raid measurements in control, candidate, candidate, control order. Record the new switch and logging switch in `dual-base-mode.txt`. Keep submission logging enabled for both modes and turn on `OCERZ_M32_DUAL_BASE_HOIST_LOG=1` only in the separate 25-second candidate diagnostic. The diagnostic emitted a second base in the preparation path at `0x97b9a7`, in its descendant at `0x2aaddf`, and in the matrix path at `0x8c19fa`.

The three focused `test_jit32` comparisons run in fresh processes with `OCERZ_TEST_JIT32_DUAL_BASE` set to `overlap`, `underflow`, or `upper`. They check aliased writes, scalar and SSE moves, register reassignment, and both ends of the guest address range against the interpreter. Use `OCERZ_LOW_HOIST_LOG=1` and the candidate compilation log to verify both fallback paths. Save the outputs to `focused-0-CASE.log` and `focused-1-CASE.log`. Keep the `test_m32` output in `general.log`. The existing all-in-one `test_jit32` run hits the translator's self-modifying-code blacklist when it repeatedly rewrites the same guest page. The dedicated comparisons each use a fresh JIT.

| i386 address mode | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| One base | 141.17 | 12.14 ms |
| Two bases | 141.23 | 12.14 ms |

The paired changes were **−1.07** and **+1.18 FPS**, with p99 changes of **+0.195** and **−0.188 ms**. Use the existing one-base mode for subsequent raid trials. The four timed runs verified **33,891** consecutive completions across their four windows, the saved camera, overlapping effects, all 26 timing rows, and zero visible trial windows or activations. The six targeted comparisons and **805** general i386 checks passed. The separate diagnostic saw **144** distinct blocks with a second base, including **three** first recorded after the final fixture readback. [Paired timing and code-generation witnesses](results/raid-aarchx-dual-base-ab.json)

```sh
python3 benchmarks/summarize-raid-dual-base.py CAPTURE_ROOT
```

### Set the first submission from the preceding frame's triangle count

Apply `patches/winemetalgl-triangle-balanced-first-submission-trial.patch` to the selected deferred PGO renderer source. Build one arm64 Release ThinLTO library with its existing merged profile. For this comparison, allow profile-mismatch warnings by replacing `-Werror=profile-instr-out-of-date` with `-Wno-error=profile-instr-out-of-date` in the trial checkout. The build reports stale profiling records. All runs use the same compiled library and the selected PGO AArchX executable.

Enable `WINEMETALGL_TRIANGLE_SPLIT=1` for the candidate and leave it unset for control. On a continuing default-framebuffer pass, the candidate can submit when the queue is idle and the current command buffer reaches half the preceding frame's count of direct triangle-list primitives. It checks every 32 draws from the ordinary draw minimum, stays inside the normal continuing-pass limit, and transfers unused draw credit as in the ordinary early-submit path. The first frame and frames with no recorded triangles use the ordinary draw threshold. The policy leaves explicit synchronization and attachment load and store decisions to the existing command stream.

Keep `WINEMETALGL_COMMIT_DRAWS=64` and `WINEMETALGL_IDLE_FIRST_DRAWS=256` in both modes. Time four warmed 60-second raid launches in control, candidate, candidate, control order. Keep submission logging off during timing. In two additional 25-second diagnostics, set `WINEMETALGL_TRIANGLE_SPLIT_LOG` to `triangle-split.tsv` and enable the existing submission log. Record the policy and logging values in `triangle-split-mode.txt`. The triangle trace includes the stream, command buffer serial, draw count, number of direct-list triangles and preceding-frame target. The analyzer checks that the target reflects the preceding recorded frame.

Run the full GPU test suite and the ordered indexed replay with three outputs in flight in each mode. For the replay also enable the triangle and submission logs. Save the results as `gpu-tests-MODE.log`, `replay-MODE.log` and `replay-MODE-triangles.tsv`, using `0` for control and `1` for candidate. Both modes passed **343** GPU tests, and the replay produced the same RGBA hash and **161,327** colored pixels with distinct submission sizes.

| First submission policy | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| Ordinary early draw threshold | 138.15 | 12.20 ms |
| Preceding-frame triangle budget | 139.84 | 12.00 ms |

The paired throughput changes were **−0.08** and **+3.47 FPS**. The p99 completion gap shortened by **0.238** and **0.151 ms**. The four timed launches verified **33,363** consecutive GPU completions across their four windows with the saved camera, overlapping effects, all 26 timing rows, and zero visible windows or activations. Keep the previously selected deferred PGO library for the raid baseline. [Timed results and verified replay](results/raid-winemetalgl-triangle-balanced-first-submission-ab.json)

In the diagnostic pair the most frequent first split moved from **256** to **224 draws**, and the median share of tracked triangles in the first buffer fell from **63.9% to 57.5%**. The first buffer's median GPU span was **0.646 and 0.645 ms**. The final buffer's median span rose from **1.20 to 1.27 ms**, and the median union of recorded render-buffer spans rose from **2.30 to 2.53 ms**. In the diagnostic p99 tail, the median union was **8.00 and 7.88 ms**. These spans come from GPU start and end timestamps and can include scheduling stalls. The two diagnostic windows sample separate periods of desktop activity. [Submission budgets and GPU spans](results/raid-winemetalgl-triangle-balanced-first-submission-ab.json)

```sh
python3 benchmarks/summarize-raid-triangle-split.py CAPTURE_ROOT
```

### Specialize the compatibility fragment shader for inactive alpha testing

Apply `patches/winemetalgl-inactive-compat-alpha-variant-trial.patch` to the selected deferred PGO renderer source. Build an arm64 Release ThinLTO renderer with the existing merged profile, allowing stale-profile warnings in the trial checkout. Both timing modes use the same library and the selected PGO AArchX executable.

The generated legacy fragment wrapper declares a Metal function constant for the compatibility alpha check. The ordinary function explicitly specializes it to true. With `WINEMETALGL_ALPHA_VARIANT=1`, the pipeline can specialize it to false for draws with disabled alpha testing or `GL_ALWAYS`. The cached pipeline key includes that choice. The shader's own discard, the fixed-function uniform layout, depth and stencil state, and draw order use the existing paths. Keep the ordinary function as fallback if creating the specialized function fails.

Measure four warmed 60-second raid windows in control, variant, variant, control order with logging disabled. In two separate 25-second diagnostic launches, set `WINEMETALGL_ALPHA_VARIANT_LOG` to `alpha-variant.tsv` and enable the existing submission log. Record the mode and logging switches in `alpha-variant-mode.txt`. The counters sample each 4,096th pipeline preparation on the calling thread. They count total preparations, use of the legacy fragment wrapper, inactive alpha eligibility, and selection of the variant. The diagnostic joins the covered counter samples to verified GPU completions.

Run `compat_alpha_variant_keeps_color_depth_and_user_discard` in both modes with compilation logging enabled. It checks color and depth for disabled alpha testing, `GL_NEVER`, `GL_ALWAYS`, and a discard from the shader itself. Save those logs as `focus-0.log` and `focus-1.log`. Save both complete suites as `gpu-tests-MODE.log` and the ordered indexed replays with three outputs in flight as `replay-MODE.log`. Both modes passed **344** GPU tests and matched the replay's **161,327** colored pixels and RGBA hash.

| Compatibility alpha path | Median GPU-completed FPS | Median p99 completion gap |
| --- | ---: | ---: |
| Ordinary uniform-controlled check | 139.80 | 12.15 ms |
| Specialized inactive check | 138.16 | 12.15 ms |

The variant lost **1.03** and **2.25 FPS** in the two pairs. P99 changed by **−0.011** and **+0.008 ms**. Use the previous deferred PGO library for subsequent raid trials. The four timing windows verified **33,359** consecutive GPU completions across their four launches, the saved camera and effects, all 26 timing rows, and zero trial windows or activations. [Paired timings and replay verification](results/raid-winemetalgl-inactive-alpha-variant-ab.json)

In the candidate diagnostic, **1,140,059** of **2,850,816** covered pipeline preparations selected the specialized function, or **40.0%**. Twelve variants compiled during that launch. The median final buffer's GPU span changed from **1.218 to 1.189 ms**, and the median in the one-percent completion tail moved from **7.068 to 7.146 ms**. These spans include scheduling and come from separate diagnostic windows. [Shader selection and GPU spans](results/raid-winemetalgl-inactive-alpha-variant-ab.json)

```sh
python3 benchmarks/summarize-raid-alpha-variant.py CAPTURE_ROOT
```

### Measure the remaining stack reload and preparation scheduling opportunities

The stack-slot analyzer reuses the two 60-second weighted guest and ARM64 stack recordings. It decodes **917** sampled functions from the verified original client. It recognizes fixed negative EBP offsets in functions with a frame-pointer prologue. It starts a new value history at control-flow boundaries, clears it for potential aliasing writes, and invalidates recorded values that overlap a local store. This identifies candidate reads for a register-promotion pass. It cannot establish memory ownership or precise state restoration for a candidate.

Across the two captures, recognized local-slot reads accounted for **3.32% and 3.75%** of sampled CPU weight. The subset with an earlier access in the same block accounted for **0.29% and 0.27%**. Actual ARM64 loads at those candidate sites accounted for **0.28% and 0.23%**. Within the two preparation call trees, the same-block subset accounted for **0.22% and 0.18%** of total sampled CPU. Use larger work units for the next CPU optimization. [Stack slot coverage](results/raid-aarchx-i386-frame-slots.json)

The scheduling analyzer reuses the explicit memory operands from the two verified item-memory captures. It groups dispatcher iterations in original index order and keeps index-changing iterations and transitions between work groups serial. The list model keeps consumers in publication order and requires an update before the consumer of that same item. It uses both equal costs and weights from the recordings for sensitivity. The timing-only costs come from **428** complete frames in the earlier, lighter probe and are grouped by the dispatch input tag.

For the completed dispatcher tasks, the analyzer first schedules whole tasks using the observed read and write dependencies. A second model assumes ordered publication happens separately and keeps the read-after-write dependencies. An additional lower bound allows a task to start before an earlier task finishes if every recorded input is ready by its original read offset. Only the separate context-isolation scenario omits the observed dependencies in offsets `0x50–0x9f`. That scenario requires an equivalent producer for those fields. A production implementation must preserve the original publication order.

| Dispatcher model across 13 frames | Work left using costs from the timing-only trace |
| --- | ---: |
| Schedule whole tasks with the observed reads and writes | 100% |
| Use the original read offsets and assume ordered publication | 79–80% |
| Also make the shared context fields available independently | 65–67% |
| Also merge identical flag clears | 46–47% |

The first row is the simulated four-worker schedule. The other rows are lower bounds, with zero dispatch, synchronization and merge costs. Their ranges come from the median and mean timing-only cost for each tag. Using elapsed times from the memory-instrumented run puts the last scenario at **86%** work left. All **2,341** dispatcher tasks called code outside the memory probe. Those missing accesses and callees still need to be audited before changing execution order. [Calibrated scheduling bounds](results/raid-aarchx-i386-item-critical-path.json)

The ordinary access-offset model attributes **35%** of its modeled start delays to the `0x97d3b9 → 0x9791bf` edge at context offset `0x5c`. The original dispatcher copies that field from `0x58` after the item call. Once the context fields are excluded, about **70%** of the remaining delay comes from `0x97bec9 → 0x97bd8f`. Both instructions clear bit zero in the same graphics-state word. The last scenario validates six original clear instructions and merges the dependency only when the recorded accesses to the aligned word in both tasks consist entirely of those clears. A prior ordinary write to the word remains a dependency. These observations identify fields to test for early publication before building workers. [Binding edges and instruction witnesses](results/raid-aarchx-i386-item-critical-path.json)

```sh
python3 benchmarks/summarize-raid-i386-frame-slots.py I386_CLIENT STACK_CAPTURE_1 STACK_CAPTURE_2
python3 benchmarks/summarize-raid-item-critical-path.py MEMORY_CAPTURE_ROOT I386_CLIENT TIMING_CAPTURE_ROOT
```
