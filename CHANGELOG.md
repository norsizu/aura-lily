# Changelog

All notable public-release changes are documented here.

## v0.19.1-public - 2026-10-27

Server-side release. Firmware and `assets` stay at 0.19.0; no reflash is
needed if the device already runs 0.19.0.

### Added

- Saying goodbye ends continuous dialogue. Wrap-ups such as "回头再聊",
  "我去忙了", "拜拜", or "晚安" send `continue_listening=false`, so after
  her goodbye the device goes back to idle instead of opening another
  recording round. The model can mark the turn with `<emo:X|end>`, and a
  deterministic phrase check covers turns where it forgets.
- The LLM stream is retried once when it fails before producing any text.

### Changed

- The content quality guard is removed. Normal model replies are no longer
  held back, rewritten, or replaced with topic templates. Only service
  failures are caught: an empty reply or a provider error now says that the
  service failed and suggests rephrasing or checking the server logs,
  instead of a canned "我在。" that sounds like her own answer.
- A half-finished trailing sentence in a streamed reply is trimmed back to
  the last complete sentence.
- StepFun WebSocket TTS buffers briefly before playback (tunable with
  `AURA_TTS_START_HOLD_*`) so slow synthesis no longer stutters after the
  device prefetch drains.

### Fixed

- "你今天怎么样" is no longer treated as a weather question.

## v0.19.0-public - 2026-10-24

### Added

- Emotion chain: each reply starts from the model's first reaction, then is
  adjusted by body/stress state and the inertia of her recent mood. The
  result drives the dialogue portrait and the TTS tone.
- World ledger: every mood, energy, and location change is stored with
  before/after values and a reason. Day plans keep the reason for each
  activity, and after an hour or more away she can recap what she did.
- Voice device settings: one sentence changes the sleep timeout; the device
  applies it and sends a receipt. The request never reaches the model.
- Background lookups: weather questions run in the background and the answer
  comes back once she is idle. Reminders are written to the ledger.
- Self-hosted X-Ray screen at `/trace?token=...` (set `AURA_TRACE_TOKEN`).
  An optional token-free `/trace/live` exists only with `AURA_TRACE_PUBLIC=1`.

### Changed

- Outfits use the 320px multi-user atlas set (960x960, 2-bit packed).
- Dialogue emotion faces are head-only, so they fit every outfit. Sources
  live in `art/emotion_device/`.

### Upgrading from v0.18.1

- The outfit atlas format changed, so the application and `assets` must be
  updated together. Flash `aura-lily-v0.19.0-public-full.bin` by wire (for
  example with ESP LaunchPad), then set up Wi-Fi again.

## v0.18.1-public - 2026-09-01

### Fixed

- Rebuilt all nine outfit atlases from the current 1-bit nine-pose source
  sheets, including the Professional replacement for the retired `casual_b`
  slot. Every atlas keeps the 600x900 header, packed pixel format, shared
  crop, bottom anchor, and device-side Bayer 8x8 Smooth rendering path.
- Added per-pose resource validation so no listening, thinking, or reply pose
  can silently become an empty cell.

### Firmware assets

- `aura-lily-v0.18.1-public-full.bin` is the complete first-flash image.
- `aura_doudou.bin` is the application-only OTA image.
- `assets.bin` contains all 11 scenes and all 9 rebuilt outfits.

## v0.18.0-public - 2026-09-01

### Added

- Added the riverside scene, bringing the firmware world set to 11 scenes.
- Added the Professional outfit in the former outfit slot 4; the public asset
  set now contains 9 outfits and no longer ships `casual_b`.

### Changed

- Rebuilt all world backgrounds from the current 1-bit source artwork while
  retaining the firmware's 8x8 Bayer character dithering and crop rules.
- Added `outside.riverside` to the public world canon and device protocol.

### Firmware assets

- `aura_doudou.bin` is the application-only OTA image for devices already using
  the dual-slot OTA layout.
- `assets.bin` contains the matching 11-scene and 9-outfit resource partition.
- `aura-lily-v0.18.0-public-full.bin` is the complete first-flash image for
  address `0x0000`.

## v0.17.16-public - 2026-08-31

### Fixed

- Nighttime automatic sleepwear now overrides the same-day manual outfit pin,
  so the companion can reliably settle into sleepwear during the night phase.

### Firmware assets

- `aura-0.17.16.bin` is the application-only OTA image for devices already
  using the dual-slot OTA layout.
- The resource partition is unchanged from the previous public release.

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
