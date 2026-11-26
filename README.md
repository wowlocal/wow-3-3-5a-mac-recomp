# WoW 3.3.5a on Apple silicon

Run the original Intel Mac client for World of Warcraft 3.3.5a (build 12340) through AArchX and connect to a local AzerothCore realm. The patch covers Carbon keyboard and mouse events, QuickDraw coordinates, and AGL/OpenGL behavior used by this client. It also adds optional frame-time and settings diagnostics.

The trial was tested on an M4 Pro running macOS 27.2.1. It signed in to the local realm, ran in Northshire for ten minutes, and preserved settings and license choices through clean relaunches. See the [Northshire report](benchmarks/northshire.md) and [raw benchmark summaries](benchmarks/results).

## Build the trial runtime

Use the Xcode command-line tools. Fetch the tested AArchX revision into a dedicated checkout and build the local launcher bundle.

```sh
git init build/AArchX
git -C build/AArchX fetch --depth 1 https://github.com/mont127/AArchX.git \
  96754ec605d2cf57e2a05d0cf0f6d2e1c8177741
git -C build/AArchX checkout --detach FETCH_HEAD
./scripts/build-runtime.sh build/AArchX 'build/Wrath Trial.app'
```

Extract `aarchx-macos-arm64.tar.gz` from [AArchX 1.0.2](https://github.com/mont127/AArchX/releases/tag/v1.0.2) and locate its `aarchx/runtime` directory. Use the upstream helper to install the 32-bit C++ libraries into that runtime.

```sh
runtime_dir='/path/to/extracted/aarchx/runtime'
build/AArchX/tools/fetch_guest32_cxx.sh "$runtime_dir/guest32"
```

## Set up the game and local realm

Use your own writable copy of the Mac 3.3.5a game folder with `World of Warcraft.app`, `Data`, and `WTF`. In `Data/<locale>/realmlist.wtf`, set the realm and patch addresses to loopback.

```text
set realmlist 127.0.0.1
set patchlist 127.0.0.1
```

Set `gxWindow` to `1` and `movie` to `0` in `WTF/Config.wtf`. The launcher starts with sound disabled.

Follow the [AzerothCore Docker guide](https://www.azerothcore.org/wiki/install-with-docker) to set up the realm and a test account. The trial used AzerothCore revision `b4cbbfb593a4f64714c2713f165500a245e0a612`. Apply [the Compose override](support/docker-compose.loopback.yml) as the last Compose file to bind the auth and world ports to `127.0.0.1` and keep MySQL inside the Docker network. Set a local database password in the AzerothCore checkout’s `.env`, and set both addresses in its `realmlist` row to `127.0.0.1` with build `12340`.

## Launch and measure

```sh
./scripts/launch.sh 'build/Wrath Trial.app' "$runtime_dir" \
  '/path/to/writable Mac game folder' ruRU --profile
```

Use the locale matching your client, such as `enUS` or `ruRU`. Add `--check` to check the paths and realm setting before launch. Use `--trace-settings` to diagnose file access. Press Escape to dismiss the old CPU compatibility notice. Quit from within WoW so the game saves its preferences.

`--profile` prints the swap log path. For a two-minute run, pass the client PID, log path, and a fresh JSON output path to `scripts/measure.py`.

```sh
python3 scripts/measure.py PID '/path/to/run.frames.ns' results.json
```

The recorder reports completed frame swaps, frame times, stalls, client CPU use, and sampled physical footprint. With the current patch, it also splits each frame into the host AGL swap and time between swaps. See the [route](benchmarks/northshire.md) for reproducible inputs.

## Known limitations

Sound and intro playback are disabled. The CPU compatibility notice appears on startup. Resizing or moving the old Carbon window can make it disappear. The published frame-time results cover the Northshire route.

The compatibility patch is based on [AArchX](https://github.com/mont127/AArchX) and carries its [LGPL 2.1 license](LICENSE).
