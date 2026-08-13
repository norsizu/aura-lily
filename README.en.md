# Aura Lily

[简体中文](README.md) | [English](README.en.md) | [日本語](README.ja.md)

> An open-source voice companion that lives on your desk: it listens, speaks, shows its state, and moves through a day of its own.

Aura Lily is built for the Waveshare ESP32-S3-RLCD-4.2. It is not a chat window squeezed onto a small display. It joins voice turns, character state, scenes, and a daily rhythm in a self-hosted ESP32-S3 device. The device handles recording, sound, display, and local interaction; your own service handles the voice pipeline, model calls, and the optional world state.

## Project scope

Aura Lily is an independent, open-source, self-hosted desk voice-companion project for personal deployments and ESP32-S3 hardware. The repository includes voice interaction, character state, scenes, schedules, and a configurable gateway for a computer, NAS, or server you control, while providing a base for hardware, interface, and character-resource development.

## Demo

Promo video 1

https://github.com/user-attachments/assets/97038f7a-a477-40c2-a7de-cabaafbf24f7

Promo video 2

https://github.com/user-attachments/assets/ee1e3867-b63f-47fb-a90f-763486a94bb2

## Hardware and credits

Aura Lily is built around the Waveshare [ESP32-S3-RLCD-4.2 board](https://docs.waveshare.net/ESP32-S3-RLCD-4.2/). The enclosure was made by [Siagfried (黄木匠)](https://makerworld.com.cn/zh/@Siagfried); see the [Waveshare 4.2-inch reflective-display enclosure](https://makerworld.com.cn/zh/models/2726139-wei-xue-4-2cun-quan-fan-she-ping-kai-fa-ban-wai-ke#profileId-3216633).

<table>
  <tr>
    <td><img src="docs/media/hardware/waveshare-esp32-s3-rlcd-4.2.webp" width="220" alt="Waveshare ESP32-S3-RLCD-4.2"></td>
    <td><img src="docs/media/posters/aura-braun.jpg" width="155" alt="Aura Lily Braun palette"></td>
    <td><img src="docs/media/posters/aura-pixel-green.jpg" width="155" alt="Aura Lily natural green pixel palette"></td>
    <td><img src="docs/media/posters/aura-famicom.jpg" width="155" alt="Aura Lily Nintendo palette"></td>
    <td><img src="docs/media/posters/aura-macintosh.jpg" width="155" alt="Aura Lily Macintosh palette"></td>
  </tr>
  <tr>
    <td align="center">Original board</td>
    <td align="center">Braun</td>
    <td align="center">Natural green pixel</td>
    <td align="center">Famicom</td>
    <td align="center">Macintosh</td>
  </tr>
</table>

## What makes it different

- **Conversation is tied to state.** Aura has mood, energy, satiety, stress, affinity, and beans. Talking, eating, resting, spending, and completing scheduled activities change parts of that state.
- **A day is not a fixed script.** The world layer keeps five daily anchors (wake, three meals, and nightly wind-down), then generates four to eight dynamic activities from time, weather, mood, energy, satiety, stress, affinity, and funds.
- **Language is a complete route.** Chinese, English, and Japanese UI text, ASR results, replies, and TTS output follow the active conversation language together.
- **The device is part of the experience.** The 400 x 300 reflective 1-bit display presents the character, outfits, scenes, subtitles, status, and information board. Short local prompts do not need an extra TTS request.

## Included capabilities

| Area | Included |
| --- | --- |
| Voice turns | Device recording, Opus uplink, ASR, streamed text replies, and TTS audio return. Captions progress with actual audio playback. |
| Three languages | Chinese, English, and Japanese UI and speech routing, including localized quota prompts. |
| Everyday world | Optional state, schedule, and world layer. Scheduled meals, rest, outings, and purchases settle real state effects. |
| Local networking | Two saved Wi-Fi credential slots with real SSID labels and manual switching. |
| OTA | Dual application partitions, application and asset OTA, SHA-256 verification, and boot rollback. An old single-partition device needs one complete wired flash before its first OTA migration. |
| Self-hosting | A local admin UI for Hermes, the dialogue model, ASR, TTS, dialogue quota, and an optional Soul. Firmware builds have no baked-in server endpoint. |

## Architecture

```text
ESP32-S3 device
  microphone / buttons / RLCD / speaker
            | WebSocket
            v
Aura Lily gateway
  ASR -> conversation model -> TTS
            |
            +-- optional Aura state and daily-world layer
```

Aura Lily runs as native Python processes on a computer, NAS, or server you control; Docker is not required. Choose and configure your own model and voice providers. The device must use a LAN, Tailscale, or public address, never `127.0.0.1`.

## Quick start

### 1. Start the service

Requirements: Python 3.11+, a working `hermes` CLI, and an OpenAI-compatible model endpoint or another Hermes provider. Firmware builds need ESP-IDF 5.x.

```bash
./tools/install_native.sh
# Edit .env and add your Hermes/provider, ASR, and TTS settings.
.venv/bin/python tools/run_native.py
```

Verify it from another terminal:

```bash
curl -s http://127.0.0.1:8765/health
```

#### Open the admin UI

Before the first start, copy the example configuration and set an admin password:

```bash
cp .env.example .env
# Edit .env and set at least:
# AURA_LILY_ADMIN_USER=admin
# AURA_LILY_ADMIN_PASSWORD=replace-with-a-strong-password
```

Then start the services:

```bash
.venv/bin/python tools/run_native.py
```

On the host machine, open `http://127.0.0.1:8765/admin`. From your LAN or the internet, use `http://<host-or-ip>:8765/admin`. Sign in with `AURA_LILY_ADMIN_USER` and `AURA_LILY_ADMIN_PASSWORD`. Port `8787` is only the device WebSocket gateway; it is not the admin UI.

Before exposing the admin UI to the internet, put it behind an HTTPS reverse proxy and restrict source IPs. Never expose the admin port without a password. Model credentials remain in your local runtime environment and are never supplied by this repository.

### 2. Enable the optional world layer

The basic Hermes bridge runs on its own. To enable Aura state, scenes, and scheduling, set this in `.env`:

```bash
AURA_PERSONA_ENABLED=1
```

Soul starts empty. Add your own through the local admin UI or create `.aura/persona/persona/soul.md`. State and schedules live in the Git-ignored `.aura/` runtime directory.

### 3. Build and flash the device

The repository includes the complete firmware source and device assets. Most users should download the prebuilt full Web-flash image from [Releases](https://github.com/norsizu/aura-lily/releases); developers can build it with the commands below. The Web-flash image targets the Waveshare ESP32-S3-RLCD-4.2. A first wired flash replaces the firmware partitions, so provisioning must be completed again afterwards.

```bash
cd firmware/esp32
source "$HOME/esp/esp-idf/export.sh"
idf.py set-target esp32s3
idf.py menuconfig
idf.py build
idf.py -p /dev/cu.usbmodemXXXX flash monitor
```

Each Release includes the complete Web-flash image and its SHA-256 checksum. The firmware does not connect to a project server by default; enter your own WebSocket address during first-run provisioning.

#### For most users: flash with ESP LaunchPad

You do not need to install ESP-IDF for a first flash:

1. Use Chrome or Edge with a USB data cable, and download the [complete `.bin` image from the Release](https://github.com/norsizu/aura-lily/releases/tag/v0.17.0-public).
2. Open [Espressif ESP LaunchPad](https://espressif.github.io/esp-launchpad/), click **Connect**, select the device USB serial port, and grant access.
3. Open **DIY**, change Flash Address to `0x0000`, and choose the downloaded full `.bin` file.
4. Click **Program** and wait for completion. Reset from the Console tab or unplug and reconnect the device.

This is one complete merged image, so add only this single file. Do not keep LaunchPad's default `0x1000` address. First boot requires provisioning again with your own WebSocket address.

Set your WebSocket and OTA manifest URLs in `menuconfig > Aura Lily`, or save them from the first-run provisioning page. Use your LAN, Tailscale, or public address instead of `127.0.0.1`.

### 4. Wi-Fi and OTA

Successful provisioning retains two Wi-Fi credentials and shows their SSIDs in the device menu. There is no default OTA server. Configure your own HTTPS manifest URL in `menuconfig > Aura Lily`, then use `tools/make_ota_release.py` to create firmware and asset manifests. Upload all artifacts before publishing `manifest.json`.

For the detailed Hermes bridge contract and smoke test, read the [Hermes bridge guide](integrations/hermes_lily_cli/README.md).

## Repository layout

```text
firmware/esp32/                     ESP32-S3 firmware, display, audio and local assets
integrations/hermes_lily_cli/       Hermes bridge, HTTP/WS gateway and local admin UI
integrations/aura_persona_gateway/  Optional Aura state, reminders, weather and world schedule
tests/                              Focused gateway, world, Wi-Fi, OTA and quota tests
tools/                              Asset, voice, diagnostics and OTA release tools
```

## Configuration safety

The repository does not provide model keys, a default service endpoint, or personal character data. Keep `.env`, `.aura/`, device backups, and build artifacts in your own private environment.

## Verify

```bash
python3 -m pytest -q tests
```

Before publishing firmware, also run `idf.py build` and confirm that the application image fits a `0x280000` OTA partition.

## Community

Open-source promotion and community link: [LINUX DO](https://linux.do/).

For hardware ports, deployment, character assets, and self-hosting discussion, join the "Xianhua AI | Aura" QQ group: `951895791`.

<p align="center">
  <img src="docs/community/qq-group.jpg" width="250" alt="Xianhua AI Aura QQ group 951895791">
</p>
