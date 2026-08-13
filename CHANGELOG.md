# Changelog

All notable public-release changes are documented here.

## v0.17.1-public - 2026-08-14

### Fixed

- Treat the five-minute information board as a low-priority idle surface.
- Menus, recording, dialogue, scheduled reminder audio, Agent panels, scene
  changes, pose/emotion changes, and shop or wardrobe screens now leave the
  information board immediately.
- A single BOOT press now opens the menu directly while the information board
  is visible; a second press is no longer required.

### Firmware assets

- Firmware application version: `0.17.16`.
- `aura-lily-v0.17.1-public-full.bin` is the complete first-flash image for
  address `0x0000`.
- `aura_doudou.bin` is the OTA application image.
- `assets.bin` is unchanged from the previous public release.

## v0.17.0-public - 2026-08-13

### Added

- Idle information board with live weather, up to three reminders, sleep-time inversion, and button wake-up.
- Native ASR/TTS provider pool for Aliyun NLS, Volcengine, Baidu, MiniMax, Tencent Cloud, Qwen3-ASR, OpenAI-compatible, and self-hosted endpoints.
- Provider options in the local admin UI, with sensitive values masked after they are saved.

### Changed

- Public native runtime state is now stored under the Git-ignored project-local `.aura/` directory.
- The public admin UI no longer presents provider-specific subscription or Plan shortcuts.
- The README now includes explicit admin login and first-flash instructions.

### Firmware assets

- `aura-lily-v0.17.0-public-full.bin` is the complete first-flash image for address `0x0000`.
- `aura_doudou.bin` is the OTA application image and is not a complete first-flash image.
- `assets.bin` contains the resource partition for manual recovery or development flashing.
