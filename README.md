# Aura Lily

[中文](#中文) | [English](#english) | [日本語](#日本語)

Aura Lily 是一套面向 Waveshare ESP32-S3-RLCD-4.2 的开源语音陪伴设备固件与自托管服务。设备负责录音、显示、音频播放和本地交互，服务端通过 Hermes CLI 或 OpenAI-compatible 模型完成对话、语音和世界状态处理。

> 本公开仓库不包含 RAG/知识库路由、语义长期记忆模块、私有 Soul/人格内容、个人身份、API Key、私有域名/IP/SSID 或生产服务器配置。Git 仓库中也不会保留可回退恢复这些内容的旧历史。

## 中文

### 当前功能

- ESP32-S3 端到端语音对话：Opus 上行、流式 ASR、流式回复与 TTS 播放。
- 中文、English、日本語三语界面和语音通路；识别语言、回复语言与 TTS 语言保持一致。
- 字幕按照实际音频播放进度推进，播放结束后及时清理。
- 400 x 300 ST7305 反射式 1-bit 屏幕，场景图使用 Floyd-Steinberg 抖动。
- 十个世界场景、服装、表情、状态面板、甜品商店和本地提示音资源。
- 世界模型：五个生活锚点、每天四至八个动态活动，结合时间、天气、心情、体力、饱腹度、压力、好感度和资金生成并推进日程。
- 行为会真实改变状态，例如进食恢复饱腹度、消费扣除豆子、甜品具有不同的情绪/体力/饱腹效果。
- 两个 Wi-Fi 凭据槽位，菜单显示实际 SSID，可在家庭网络与手机热点间切换。
- 五小时滚动对话额度和可选供应商余额展示；限制由服务端真实执行，不只是界面数值。
- 应用与资源 OTA、双应用分区、SHA-256 校验和启动回滚。
- 本地管理页面可配置 Hermes、Aura 对话模型、ASR、TTS、额度和可选人格。

### 硬件与环境

固件针对以下硬件：

- Waveshare ESP32-S3-RLCD-4.2
- ESP32-S3，16 MB Flash，8 MB PSRAM
- 400 x 300 ST7305 RLCD
- ES8311 音频编解码器、ES7210 麦克风 ADC

服务端要求：

- Python 3.11+
- Docker Compose，或可直接运行的 Python 环境
- 已安装并配置的 `hermes` 命令，或一个 OpenAI-compatible 模型接口
- 编译固件需要 ESP-IDF 5.x

### 快速启动服务端

```bash
cp .env.example .env
docker compose up --build
```

默认端口：

- HTTP/API 与管理页面：`http://127.0.0.1:8765`
- ESP32 WebSocket 网关：`ws://<服务器局域网地址>:8787/ws`

健康检查：

```bash
curl -s http://127.0.0.1:8765/health
curl -s http://127.0.0.1:8765/turn \
  -H 'content-type: application/json' \
  -d '{"goal":"请回复：Aura Lily 已连接"}'
```

设备不能使用 `127.0.0.1` 访问电脑；请在配网页面填写电脑的局域网或 Tailscale 地址。

### 模型、ASR 与 TTS

先在 Hermes 中配置供应商：

```bash
hermes model
hermes status
hermes -z "请只回复：Hermes 可用。"
```

然后在 `.env` 中选择 `HERMES_PROVIDER`、`HERMES_MODEL`，或启动后访问：

```text
http://127.0.0.1:8765/admin
```

管理页面由 `AURA_LILY_ADMIN_USER` 和 `AURA_LILY_ADMIN_PASSWORD` 保护。API Key 写入本地私有运行目录，不会通过普通配置接口返回。

公开版不内置 TTS 音色或服务地址。可配置 StepFun、OpenAI、ElevenLabs、MiniMax、自托管 VoxCPM 或自定义 HTTP/OpenAI-compatible TTS。自托管 VoxCPM 必须填写自己的 `base_url` 和音色。ASR 可使用本地 Whisper、StepFun 或其他兼容接口。

### 可选人格与世界模型

普通 Hermes 桥接可直接使用。若需要 Aura 的状态、日程和世界模型：

```bash
AURA_PERSONA_ENABLED=1 docker compose up --build
```

Soul 默认为空。用户可以在管理页面输入自己的内容，或创建：

```text
.docker/aura-persona/persona/soul.md
```

本地状态和日程保存在被 Git 忽略的运行目录中。它们是设备运行状态，不是向量检索或语义长期记忆。本项目不会从 Hermes、旧版本目录或其他文件自动加载 Soul。

### 固件编译与刷写

```bash
cd firmware/esp32
source "$HOME/esp/esp-idf/export.sh"
idf.py set-target esp32s3
idf.py menuconfig
idf.py build
idf.py -p /dev/cu.usbmodemXXXX flash monitor
```

在 `menuconfig > Aura Lily` 中设置 WebSocket 地址，或在首次启动的设备配网页面保存自己的 `ws://` 或 `wss://` 地址；NVS 中保存的地址优先。公开构建不内置任何服务器地址。

首次从旧的单应用分区升级到双 OTA 分区必须完整有线刷写。完整刷写前应先备份设备 Flash。之后可通过设备菜单进行 OTA。

### 双 Wi-Fi

配网页面会把成功连接的网络保存到两个槽位。设备菜单显示已保存的真实 SSID，可选择指定网络重连。写入第三个网络时会替换较旧或被选中的槽位；密码只保存在设备 NVS 中。

### 发布自己的 OTA

公开版没有默认 OTA 服务器。先在 `menuconfig > Aura Lily` 配置自己的 HTTPS 清单地址：

```text
CONFIG_AURA_OTA_MANIFEST_URL="https://updates.example.com/aura/stable/manifest.json"
CONFIG_AURA_OTA_RESOURCES_MANIFEST_URL="https://updates.example.com/aura/stable/resources.json"
```

编译后生成发布目录：

```bash
python tools/make_ota_release.py \
  --version 0.16.13 \
  --assets-version 0.16.13 \
  --base-url https://updates.example.com/aura/stable \
  --build-dir build \
  --assets-dir assets \
  --output releases/ota/0.16.13
```

资源文件可通过重复使用 `--asset path/to/file.bin` 加入。上传时先上传固件和资源，最后替换 `manifest.json`。已发布的文件 URL 不应覆盖复用。

### 测试

```bash
python3 -m pytest -q
```

固件测试还应执行一次完整 `idf.py build`，并确认应用镜像小于任一 `0x280000` OTA 分区。

### 仓库结构

```text
firmware/esp32/                     ESP32-S3 固件与资源
integrations/hermes_lily_cli/       Hermes 桥接、HTTP/WS 网关和管理页面
integrations/aura_persona_gateway/  可选状态、日程与世界模型
tests/                              服务端与固件静态测试
tools/                              资源、语音、延迟和发布工具
Dockerfile / docker-compose.yml     自托管运行环境
```

### 隐私边界

- 不含 RAG、知识库查询路由或向量数据库连接代码。
- 不含语义长期记忆模块；Hermes 默认工具集也不启用 `memory`。
- 不附带 Soul、聊天数据库、运行时状态或模型密钥。
- 不附带项目维护者的服务器、内网、Wi-Fi 或 OTA 地址。
- `.docker/`、`.env`、构建目录和设备备份不会进入 Git。

---

## English

Aura Lily is an open-source voice companion firmware and self-hosted service for the Waveshare ESP32-S3-RLCD-4.2.

### Features

- End-to-end voice turns with Opus upload, streaming ASR, streamed replies and TTS playback.
- Chinese, English and Japanese UI and speech routing. The detected language controls both the reply and TTS language.
- Subtitles follow actual audio playback and clear when playback ends.
- Ten world scenes, outfits, expressions, status panels, a dessert shop and local prompt audio.
- A world model with five daily anchors and four to eight dynamic activities influenced by time, weather, mood, energy, satiety, stress, affinity and funds.
- State-changing actions: meals restore satiety, purchases spend beans, and desserts have independent mood, energy and satiety effects.
- Two saved Wi-Fi slots with real SSID labels and manual switching.
- A real server-enforced five-hour dialogue quota plus optional provider balance display.
- Application and resource OTA with dual app slots, SHA-256 verification and boot rollback.
- A local admin UI for Hermes, the Aura dialogue model, ASR, TTS, quota and optional persona settings.

### Scope and privacy

This public repository contains no RAG or knowledge-base router, no semantic long-term-memory module, no bundled Soul/persona, no personal identity, no API keys, and no private domain, IP, SSID or production deployment configuration. Its published Git history is kept clean so these features cannot be recovered by checking out an older public revision.

Local state and schedules are operational device state, not vector retrieval or semantic memory. The default Hermes toolsets do not enable `memory`.

### Requirements

- Waveshare ESP32-S3-RLCD-4.2, 16 MB Flash and 8 MB PSRAM
- Python 3.11+
- Docker Compose or a direct Python environment
- A configured `hermes` CLI or an OpenAI-compatible model endpoint
- ESP-IDF 5.x for firmware builds

### Start the server

```bash
cp .env.example .env
docker compose up --build
curl -s http://127.0.0.1:8765/health
```

The HTTP/admin service listens on port `8765`; the ESP32 WebSocket gateway listens on `8787`. Configure the device with `ws://<server-lan-ip>:8787/ws`. Do not use `127.0.0.1` on the device.

Configure Hermes first:

```bash
hermes model
hermes status
hermes -z "Reply only: Hermes is ready."
```

Copy `.env.example` to `.env` for provider/model, ASR and TTS settings. The admin UI is available at `http://127.0.0.1:8765/admin` and is protected by `AURA_LILY_ADMIN_USER` and `AURA_LILY_ADMIN_PASSWORD`.

No TTS endpoint or voice is bundled. StepFun, OpenAI, ElevenLabs, MiniMax, a self-hosted VoxCPM server and custom compatible endpoints can be configured. VoxCPM users must provide their own base URL and voice.

### Optional persona and world state

Enable the Aura state and world layer with:

```bash
AURA_PERSONA_ENABLED=1 docker compose up --build
```

Soul content is empty by default. Add your own text through the admin UI or at `.docker/aura-persona/persona/soul.md`. The public build never imports Soul from Hermes or legacy project paths.

### Build and flash firmware

```bash
cd firmware/esp32
source "$HOME/esp/esp-idf/export.sh"
idf.py set-target esp32s3
idf.py menuconfig
idf.py build
idf.py -p /dev/cu.usbmodemXXXX flash monitor
```

Set the WebSocket URI under `menuconfig > Aura Lily`, or save your own URI later through device provisioning. Public builds do not contain a server endpoint. A migration from an old single-app partition layout requires one complete wired flash. Back up the complete Flash before that migration.

### Wi-Fi and OTA

Successful provisioning stores up to two Wi-Fi networks in NVS. The device menu shows their actual SSIDs and can reconnect to either slot.

The public build has no default update server. Configure your own HTTPS application and resource manifest URLs under `menuconfig > Aura Lily`, then create a release with an explicit host:

```bash
python tools/make_ota_release.py \
  --version 0.16.13 \
  --assets-version 0.16.13 \
  --base-url https://updates.example.com/aura/stable \
  --build-dir build \
  --assets-dir assets \
  --output releases/ota/0.16.13
```

Upload artifacts before publishing `manifest.json`. Never replace bytes at an already published immutable URL.

### Tests

```bash
python3 -m pytest -q
```

Also run `idf.py build` and verify that the application fits a `0x280000` OTA slot.

---

## 日本語

Aura Lily は、Waveshare ESP32-S3-RLCD-4.2 向けのオープンソース音声コンパニオン・ファームウェアとセルフホスト型サービスです。

### 主な機能

- Opus 音声送信、ストリーミング ASR、逐次応答、TTS 再生を含む音声対話。
- 中国語、英語、日本語の UI と音声経路。認識した言語に合わせて返答言語と TTS 言語を切り替えます。
- 実際の音声再生位置に同期する字幕と、再生終了時の自動消去。
- 10 種類の世界シーン、衣装、表情、ステータス画面、デザートショップ、ローカル案内音声。
- 5 個の生活アンカーと 4〜8 個の動的行動からなる世界モデル。時刻、天気、気分、体力、満腹度、ストレス、好感度、所持金を反映します。
- 食事、買い物、デザートなどの行動が実際の状態値に反映されます。
- 実 SSID を表示する 2 個の Wi-Fi 保存スロットと手動切り替え。
- サーバー側で実際に制限する 5 時間会話枠と、任意のプロバイダー残高表示。
- 2 面アプリ領域、SHA-256 検証、起動ロールバックを備えたアプリ/リソース OTA。
- Hermes、Aura 会話モデル、ASR、TTS、利用枠、任意人格を設定するローカル管理画面。

### 公開範囲とプライバシー

この公開リポジトリには、RAG/ナレッジベース経路、意味ベースの長期記憶モジュール、既定の Soul/人格、個人情報、API Key、非公開ドメイン/IP/SSID、本番サーバー設定は含まれません。過去の公開コミットへ戻ってもそれらを復元できない、クリーンな Git 履歴として公開します。

ローカルの状態値と予定は端末動作用データであり、ベクトル検索や意味記憶ではありません。Hermes の既定ツールセットでも `memory` は無効です。

### 必要環境

- Waveshare ESP32-S3-RLCD-4.2、16 MB Flash、8 MB PSRAM
- Python 3.11+
- Docker Compose または直接実行できる Python 環境
- 設定済みの `hermes` CLI、または OpenAI-compatible モデル API
- ファームウェア用 ESP-IDF 5.x

### サーバー起動

```bash
cp .env.example .env
docker compose up --build
curl -s http://127.0.0.1:8765/health
```

HTTP/管理画面は `8765`、ESP32 WebSocket ゲートウェイは `8787` を使用します。端末には `ws://<サーバーのLAN IP>:8787/ws` を設定してください。ESP32 から `127.0.0.1` は使用できません。

Hermes を先に設定します。

```bash
hermes model
hermes status
hermes -z "Hermes is ready. とだけ返答してください。"
```

`.env.example` を `.env` にコピーし、モデル、ASR、TTS を設定します。管理画面は `http://127.0.0.1:8765/admin` です。`AURA_LILY_ADMIN_USER` と `AURA_LILY_ADMIN_PASSWORD` で保護してください。

公開版には TTS サーバーや音色を内蔵しません。StepFun、OpenAI、ElevenLabs、MiniMax、セルフホスト VoxCPM、互換エンドポイントを設定できます。VoxCPM では自分の URL と音色を指定してください。

### 任意人格と世界モデル

```bash
AURA_PERSONA_ENABLED=1 docker compose up --build
```

Soul は初期状態で空です。管理画面、または `.docker/aura-persona/persona/soul.md` に自分の内容を追加できます。Hermes や旧プロジェクトのファイルから Soul を自動読込することはありません。

### ファームウェアのビルドと書き込み

```bash
cd firmware/esp32
source "$HOME/esp/esp-idf/export.sh"
idf.py set-target esp32s3
idf.py menuconfig
idf.py build
idf.py -p /dev/cu.usbmodemXXXX flash monitor
```

`menuconfig > Aura Lily` で WebSocket URI を設定するか、端末の初期設定ページから自分の URI を NVS に保存します。公開ビルドにはサーバーのエンドポイントを埋め込みません。旧単一アプリ構成から 2 面 OTA 構成への移行には、最初の 1 回だけ完全な有線書き込みが必要です。移行前に Flash 全体をバックアップしてください。

### Wi-Fi と OTA

初期設定に成功した Wi-Fi は最大 2 件まで NVS に保存され、端末メニューに実際の SSID が表示されます。

公開版には既定 OTA サーバーがありません。`menuconfig > Aura Lily` で自分の HTTPS マニフェスト URL を設定し、明示した公開先でリリースを生成します。

```bash
python tools/make_ota_release.py \
  --version 0.16.13 \
  --assets-version 0.16.13 \
  --base-url https://updates.example.com/aura/stable \
  --build-dir build \
  --assets-dir assets \
  --output releases/ota/0.16.13
```

ファームウェアとリソースを先にアップロードし、最後に `manifest.json` を公開してください。公開済みの固定 URL の内容は上書きしないでください。

### テスト

```bash
python3 -m pytest -q
```

さらに `idf.py build` を実行し、アプリが `0x280000` の OTA スロットに収まることを確認してください。

## License

MIT. See [LICENSE](LICENSE).
