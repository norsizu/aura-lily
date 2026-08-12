# Changelog

All notable public-release changes are documented here.

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
