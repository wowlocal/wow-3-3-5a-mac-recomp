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
