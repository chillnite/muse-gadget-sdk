# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Read the AGENTS.md files first

Each subproject has an `AGENTS.md` that is the detailed, authoritative guide for coding agents. Read the one for the area you're touching before making changes:

- `esp32/AGENTS.md`: building, identifying and flashing boards, monitoring, adding commands, config gotchas, video, tests
- `esp32/devices/AGENTS.md`: porting the firmware to a new board (and the vendor-source table to read before adding a board feature)
- `linux/AGENTS.md`: the Python client, deploying to a Pi, pairing, and the Muse protocol details

This file only summarizes them and how they fit together.

## Repository layout

A Muse gadget is a device that pairs with the Muse phone app over BLE (community pairing v5), then holds an encrypted Noise XX session to the user's Muse VM. Muse calls the gadget's commands by name: the device lists them in `link.register` on `/link-control` and answers each `link.invoke` with `link.result`. The ESP32 firmware and the Linux client speak the same pairing and control protocols, so the same app pairs either one.

- `esp32/`: ESP-IDF v6.0.1 firmware (`muse-gadget`). Other IDF versions are unsupported.
  - `main/`: the core link. `main.c` is the entry point and `app.c` holds most of the logic, including the `on_ws_command()` dispatch. `noise_control.cpp` handles the control session and `build_register_json()`, and `noise_tunnel.cpp` handles the optional home-network tunnel. The rest covers pairing, Wi-Fi/BLE, LED and e-paper status, OTA and sensors.
  - `components/muse/`: the avatar/voice/settings UI ("boards with the full UI"). The per-board drivers are `boards/board_*.c`. `muse_video*.c` and `muse_vdec.c` are the phone-to-board video path.
  - `components/noise_core/`: the Noise protocol (C++, with mbedtls/PSA crypto backends). `tinyh264`, `libvpx_vp8` and `minimp3` are third-party decoders.
  - `devices/sdkconfig.*`: board overlays, layered after `sdkconfig.defaults`. Full-UI boards add `devices/sdkconfig.muse` before their own overlay.
  - `simulator/`: an SDL/LVGL desktop preview that compiles the production `muse_ui.c` with host adapters. It doesn't need IDF.
  - `tests/`: Python unittest harnesses that compile the firmware C/C++ against fakes (`*_fakes/`, `*_harness.c`) on the host.
- `linux/`: the `musegadget` Python package (src layout, `uv`), which turns a Linux box or Raspberry Pi into a gadget. Commands live in `executor.py` (`COMMAND_SPECS` and `Executor.run`).
- `android/musegadgetstest`, `ios/musegadgetstest`: Muse Gadgets Test, the phone app that streams H.264/VP8 video to a board over Wi-Fi. The protocol and its trust rules are in `protocols/VIDEO.md`.
- `web-flasher/`: a GitHub Pages flasher for the boards in `boards.json`. Each `board` there must match a board in the `.github/workflows/esp32.yml` build matrix, or CI fails.
- `skills/`: community Markdown-only device skills (`gadget-<device>/SKILL.md` with a matching YAML `name`), listed in `CATALOG.md`.

## Commands

### ESP32 (run from `esp32/`, with `. $IDF_PATH/export.sh` sourced)

```sh
idf.py build                                   # default board (C5 DevKitC-1) -> build/
tools/board.sh <board> build|flash|monitor|flash-monitor [PORT]   # non-UI boards, build-<board>/
tools/muse/board.sh build|flash <profile> [SERIAL|PORT]           # full-UI boards, build-muse-<profile>/
MUSE_BENCH=1 tools/muse/board.sh build <profile>                   # bench build with screenshots
MUSE_VIDEO=1 tools/muse/board.sh build <profile>                   # adds phone video

# Any board by hand (the same form CI uses):
idf.py -B build-X -DIDF_TARGET=esp32s3 -DSDKCONFIG=build-X/sdkconfig \
  -DSDKCONFIG_DEFAULTS="sdkconfig.defaults;devices/sdkconfig.muse;devices/sdkconfig.muse-<board>" build
```

Host tests don't need a board or IDF, but run one `idf.py build` (or `reconfigure`) first so `managed_components/` exists:

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
python3 -m unittest discover -s tests -p 'test_link_discovery.py'   # a single test file
```

`test_noise_core` needs `libmbedtls-dev` and `pkg-config`, and `test_link_pairing_handshake` needs `IDF_PATH` or `MBEDTLS_SOURCE_DIR`. Both skip quietly when these are missing, so check the summary for `skipped=`.

Simulator (from the repo root):

```sh
cmake -S esp32/simulator -B build/simulator -G Ninja -DCMAKE_BUILD_TYPE=Debug -DMUSE_SIM_WARNINGS_AS_ERRORS=ON
cmake --build build/simulator --parallel && ctest --test-dir build/simulator --output-on-failure
```

### Linux (run from `linux/`)

```sh
uv run --with pytest --with . pytest
uv run --with pytest --with . pytest tests/test_executor.py -k <name>   # a single test
shellcheck install.sh
```

CI also runs the tests on Python 3.9 with `cryptography==3.3.2 websockets==13.1`, the Debian 11 floor. Check against those pins if you touch pairing or the Noise code.

## Rules that span the codebase

- **Say Muse, never Hatch.** Anything a person reads (UI text, logs, Kconfig help, CLI output, docs) says Muse. Don't put `hatch` in new names. Server- and app-facing identifiers stay as they are: `hatch.metaaivm.com`, `hatch_refresh:`, `hatch-link-pairing-v*`, the `hatch_link` model, the `hatch.*` setup commands, and existing `muse_hatch_*` identifiers. Each AGENTS.md has the full list.
- **SDK token.** Every ESP32 app build needs `CONFIG_GADGET_SDK_TOKEN="mgst_…"` in that build directory's `sdkconfig`. Ask the user for it, and never commit or print it in full.
- **Generated `sdkconfig` overrides the defaults.** Once a build directory has an `sdkconfig`, edits to `sdkconfig.defaults` or the overlays don't apply. Delete it, or the whole build directory, after changing them. Don't commit generated `sdkconfig` files.
- **Shared `managed_components/`.** If the component manager fails after switching between full-UI and non-UI boards, delete `managed_components/` and `dependencies.lock` and rebuild.
- **Flashing.** Identify the attached board before flashing, using the procedure in `esp32/AGENTS.md` (running firmware's `--status`, the USB descriptor, `esptool chip-id`). Don't assume the default board. Never enable Secure Boot, flash encryption or eFuse pairing auth. Don't `erase-flash` unless you mean to wipe pairing. Write to the SenseCAP Watcher only through `tools/muse/paced_esptool.py` (or `board.sh`), and never interrupt that flash.
- **Adding a firmware command** takes two places that must use the same name: `build_register_json()` in `main/noise_control.cpp` and `on_ws_command()` in `main/app.c`. Validate the params yourself. Don't block the Noise task: do slow work in a task that returns `{"_async": true}` and later calls `noise_ctrl_send_command_result()` exactly once. Gate hardware-specific commands on a Kconfig option in both places, and keep `link.register` under 8 KB. Environmental sensors go through `sensors.read`. The Linux client must register as `platform: "linux"`, `device_family: "homehub"`. Never use family `link` or advertise `device.ota`: the server pushes ESP32 firmware to every `link` device.
- **Third-party code** (minimp3, tinyh264, libvpx_vp8, `pixel_font.c`, `epd_ssd1677.c`, the EchoEar init header) keeps its upstream license header and style. A new third-party file goes in the License tables of both `README.md` and `esp32/README.md`. The Jollybot avatar in `esp32/avatar/` carries only a Meta copyright line, with no Apache header. Don't commit `components/muse/avatar/`.
- **Video input is untrusted.** When changing the transport, `muse_vdec.c` or `fill()` in `muse_video.c`, keep the checks listed in the Trust section of `protocols/VIDEO.md`.

## Before handing back work

- ESP32: `idf.py build` passes, plus the board build you touched, with the app still fitting its partition. The host tests pass. If you flashed, the boot log reaches `starting` with no panic or reboot loop.
- Linux: the tests pass, on Python 3.9 with cryptography 3.3.2 too if you touched pairing or Noise. `install.sh` stays ShellCheck-clean. If you deployed, the log shows `registered with the Muse`.
