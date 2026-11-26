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

Use a fixed camera and the same saved entry position. Keep VSync off and use the same graphics options for all runs. Start with 24 casters at 1365 × 768, then use a 16:9 lower resolution such as 1024 × 576. Restore the original resolution and repeat the full fixture to check drift. For the last condition, use `create --casters 12` at the original resolution. That version retains three casters per row and two per column, with all five spell types and the original positions, delays, and intervals for the selected actors.

After warmup, record each stationary condition for 60 seconds with `scripts/measure.py PID FRAME_LOG OUTPUT --seconds 60`. Capture Metal System Trace separately with Points of Interest and swap signposts enabled. Pass the pixel dimensions to `trace-frames.py` to verify the rendering surface dimensions and count completed frames. Restore the original preferences and remove the fixture when the comparison is over.
