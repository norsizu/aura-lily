# Aura Lily

[简体中文](README.md) | [English](README.en.md) | [日本語](README.ja.md)

> 一个放在桌面上的开源语音陪伴设备：它能听、能说、会显示，也会按照自己的状态与日程度过一天。

Aura Lily 面向 Waveshare ESP32-S3-RLCD-4.2。它不是把聊天窗口搬到一块小屏幕上，而是把语音对话、角色状态、场景与日常节奏放到同一台可自托管的 ESP32-S3 设备里。设备负责录音、声音、屏幕与本地交互；你自己的服务端负责语音链路、模型调用和可选的世界状态。

## 演示

<p align="center">
  <a href="docs/media/AuraHero-v13-full-english-answer.mp4">
    <img src="docs/media/AuraHero-v13-cover.jpg" width="320" alt="Aura Lily device demo">
  </a>
</p>

<p align="center">
  点击封面在 GitHub 中打开 58 秒演示视频，或 <a href="docs/media/AuraHero-v13-full-english-answer.mp4">下载 MP4</a>。也可观看 <a href="https://youtube.com/shorts/B0PyZoMU2sw">YouTube Shorts</a>。
</p>

GitHub README 不稳定支持原生视频播放器，因此这里采用与许多硬件开源项目相同的“封面图链接视频”方式：README 可正常渲染，视频仍以仓库文件保存。

## 它和普通语音助手有什么不同

- **对话不脱离状态。** Aura 有心情、体力、饱腹度、压力、好感度与豆子等运行状态；对话、吃饭、休息、消费和日程完成都会改变其中的一部分。
- **每天不是固定剧本。** 世界层保留起床、三餐和睡前整理五个生活锚点，再根据时间、天气、心情、体力、饱腹度、压力、好感度和资金生成 4 至 8 个动态活动。天气不是单一开关，状态也不会只映射到一个场景。
- **语言是一条完整链路。** 中文、英语、日语的界面文本、语音识别结果、回复和 TTS 输出按当前会话语言协同工作。
- **设备本身是体验的一部分。** 400 x 300 反射式 1-bit 屏幕会呈现人物、服装、场景、字幕、状态和信息板；本地提示音不需要为了每个短提示再请求一次 TTS。

## 已实现的能力

| 模块 | 公开版实际包含 |
| --- | --- |
| 语音回合 | 设备录音、Opus 上行、ASR、流式文本回复和 TTS 音频回传；字幕按实际音频播放推进。 |
| 三语体验 | 中文、English、日本語界面与语音路由；额度提示等本地文本也有对应翻译。 |
| 日常世界 | 可选的状态、日程和世界层；日程推进会结算进食、休息、外出、购物等状态效果。 |
| 本地连接 | 两个已保存 Wi-Fi 槽位，菜单显示真实 SSID，可在家中网络和手机热点之间切换。 |
| OTA | 双应用分区、应用与资源 OTA、SHA-256 校验与启动回滚。旧单分区设备首次迁移需要一次完整有线刷写。 |
| 自托管配置 | 本地管理页面可配置 Hermes、对话模型、ASR、TTS、对话额度和可选 Soul。公开构建不写入任何默认服务地址。 |

## 架构

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

服务端只需运行在你可控制的电脑、NAS 或服务器上。模型和语音服务由你自行选择和配置；设备不会把 `127.0.0.1` 当作服务端地址。

## 快速开始

### 1. 启动本地服务

要求：Python 3.11+、Docker Compose，以及已配置好的 `hermes` CLI 或 OpenAI-compatible 模型接口。固件编译需要 ESP-IDF 5.x。

```bash
cp .env.example .env
docker compose up --build
```

另开一个终端确认服务：

```bash
curl -s http://127.0.0.1:8765/health
```

默认 HTTP/管理页面端口是 `8765`，ESP32 WebSocket 网关端口是 `8787`。通过 `http://<你的服务器地址>:8765/admin` 配置模型、ASR、TTS 和管理员密码；模型密钥保存在你的本地运行环境中，不会被公开仓库提供。

### 2. 打开可选世界层

基础 Hermes 桥接可直接运行。要启用 Aura 的状态、场景和日程，请在 `.env` 中设置：

```bash
AURA_PERSONA_ENABLED=1
```

Soul 默认为空；你可以在本地管理页面填写自己的内容，或创建 `.docker/aura-persona/persona/soul.md`。状态与日程存放在被 Git 忽略的本地运行目录中。

### 3. 编译并刷写设备

```bash
cd firmware/esp32
source "$HOME/esp/esp-idf/export.sh"
idf.py set-target esp32s3
idf.py menuconfig
idf.py build
idf.py -p /dev/cu.usbmodemXXXX flash monitor
```

在 `menuconfig > Aura Lily` 中设置自己的 WebSocket 和 OTA 清单地址，或在首次启动后的配网页面保存它们。设备配置必须使用你的局域网、Tailscale 或公网地址，而不是 `127.0.0.1`。

### 4. 使用双 Wi-Fi 与 OTA

配网成功的网络会保留两个凭据槽位，菜单显示对应 SSID。公开版不提供默认 OTA 服务器；请在 `menuconfig > Aura Lily` 中配置自己的 HTTPS 清单 URL，再使用 `tools/make_ota_release.py` 生成可发布的固件与资源清单。先上传全部工件，最后才发布 `manifest.json`。

详细的 Hermes 桥接、HTTP 合约和冒烟测试见 [Hermes bridge guide](integrations/hermes_lily_cli/README.md)。

## 仓库结构

```text
firmware/esp32/                     ESP32-S3 firmware, display, audio and local assets
integrations/hermes_lily_cli/       Hermes bridge, HTTP/WS gateway and local admin UI
integrations/aura_persona_gateway/  Optional Aura state, reminders, weather and world schedule
tests/                              Focused gateway, world, Wi-Fi, OTA and quota tests
tools/                              Asset, voice, diagnostics and OTA release tools
```

## 公开范围与隐私边界

这是独立整理过 Git 历史的**无 RAG 公开版**。它不包含，也不能通过切换公开仓库旧提交恢复：

- RAG、知识库查询路由、向量数据库连接或语义长期记忆模块；
- 项目维护者的 Soul/人格、聊天数据库、运行状态、身份信息或 API Key；
- 私有域名、IP、SSID、OTA 地址、生产服务配置或部署文件。

本地状态、近期对话上下文和日程是设备运行数据，不是知识库或语义长期记忆。请将 `.env`、`.docker/`、设备备份和构建产物保持在你自己的私有环境中。

## 验证

服务端测试：

```bash
python3 -m pytest -q
```

发布前还应执行一次 `idf.py build`，并确认应用镜像能放入 `0x280000` 的 OTA 分区。

## 社区

欢迎交流硬件适配、部署、角色资源与自托管经验。扫码加入「闲话 AI | Aura」QQ 群：`951895791`。

<p align="center">
  <img src="docs/community/qq-group.jpg" width="250" alt="闲话 AI | Aura QQ group 951895791">
</p>
