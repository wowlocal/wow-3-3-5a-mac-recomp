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
