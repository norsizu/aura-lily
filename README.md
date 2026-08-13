# Aura Lily

[简体中文](README.md) | [English](README.en.md) | [日本語](README.ja.md)

> 一个放在桌面上的开源语音陪伴设备：它能听、能说、会显示，也会按照自己的状态与日程度过一天。

Aura Lily 面向 Waveshare ESP32-S3-RLCD-4.2。它不是把聊天窗口搬到一块小屏幕上，而是把语音对话、角色状态、场景与日常节奏放到同一台可自托管的 ESP32-S3 设备里。设备负责录音、声音、屏幕与本地交互；你自己的服务端负责语音链路、模型调用和可选的世界状态。

## 项目定位

Aura Lily 是一个独立、开源、可自托管的桌面语音陪伴设备项目，面向个人自托管与 ESP32-S3 硬件。仓库提供语音交互、角色状态、场景、日程和可配置网关等能力，方便在自己的电脑、NAS 或服务器上运行，也为硬件、界面和角色资源的二次开发提供基础。

## 演示

宣传视频 1

https://github.com/user-attachments/assets/97038f7a-a477-40c2-a7de-cabaafbf24f7

宣传视频 2

https://github.com/user-attachments/assets/ee1e3867-b63f-47fb-a90f-763486a94bb2

## 外观与硬件

Aura Lily 使用 Waveshare [ESP32-S3-RLCD-4.2 开发板](https://docs.waveshare.net/ESP32-S3-RLCD-4.2/)，外壳由 [黄木匠（Siagfried）](https://makerworld.com.cn/zh/@Siagfried) 制作，参考模型见 [微雪 4.2 寸全反射屏开发板外壳](https://makerworld.com.cn/zh/models/2726139-wei-xue-4-2cun-quan-fan-she-ping-kai-fa-ban-wai-ke#profileId-3216633)。

<table>
  <tr>
    <td><img src="docs/media/hardware/waveshare-esp32-s3-rlcd-4.2.webp" width="220" alt="Waveshare ESP32-S3-RLCD-4.2"></td>
    <td><img src="docs/media/posters/aura-braun.jpg" width="155" alt="Aura Lily Braun palette"></td>
    <td><img src="docs/media/posters/aura-pixel-green.jpg" width="155" alt="Aura Lily natural green pixel palette"></td>
    <td><img src="docs/media/posters/aura-famicom.jpg" width="155" alt="Aura Lily Nintendo palette"></td>
    <td><img src="docs/media/posters/aura-macintosh.jpg" width="155" alt="Aura Lily Macintosh palette"></td>
  </tr>
  <tr>
    <td align="center">原始开发板</td>
    <td align="center">Braun</td>
    <td align="center">自然绿像素</td>
    <td align="center">红白机</td>
    <td align="center">Macintosh</td>
  </tr>
</table>

## 它和普通语音助手有什么不同

- **对话不脱离状态。** Aura 有心情、体力、饱腹度、压力、好感度与豆子等运行状态；对话、吃饭、休息、消费和日程完成都会改变其中的一部分。
- **每天不是固定剧本。** 世界层保留起床、三餐和睡前整理五个生活锚点，再根据时间、天气、心情、体力、饱腹度、压力、好感度和资金生成 4 至 8 个动态活动。天气不是单一开关，状态也不会只映射到一个场景。
- **语言是一条完整链路。** 中文、英语、日语的界面文本、语音识别结果、回复和 TTS 输出按当前会话语言协同工作。
- **设备本身是体验的一部分。** 400 x 300 反射式 1-bit 屏幕会呈现人物、服装、场景、字幕、状态和信息板；本地提示音不需要为了每个短提示再请求一次 TTS。

## 已实现的能力

| 模块 | 实际包含 |
| --- | --- |
| 语音回合 | 设备录音、Opus 上行、ASR、流式文本回复和 TTS 音频回传；字幕按实际音频播放推进。 |
| 三语体验 | 中文、English、日本語界面与语音路由；额度提示等本地文本也有对应翻译。 |
| 日常世界 | 可选的状态、日程和世界层；日程推进会结算进食、休息、外出、购物等状态效果。 |
| 本地连接 | 两个已保存 Wi-Fi 槽位，菜单显示真实 SSID，可在家中网络和手机热点之间切换。 |
| OTA | 双应用分区、应用与资源 OTA、SHA-256 校验与启动回滚。旧单分区设备首次迁移需要一次完整有线刷写。 |
| 自托管配置 | 本地管理页面可配置 Hermes、对话模型、ASR、TTS、对话额度和可选 Soul。固件构建不写入任何默认服务地址。 |

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

服务端以原生 Python 进程运行在你可控制的电脑、NAS 或服务器上，不依赖 Docker。模型和语音服务由你自行选择和配置；设备不会把 `127.0.0.1` 当作服务端地址。

## 快速开始

### 1. 启动本地服务

要求：Python 3.11+、一个可用的 `hermes` CLI，以及 OpenAI-compatible 模型接口或其他 Hermes provider。固件编译需要 ESP-IDF 5.x。

```bash
./tools/install_native.sh
# 编辑 .env，填入 Hermes/provider、ASR 和 TTS 配置
.venv/bin/python tools/run_native.py
```

另开一个终端确认服务：

```bash
curl -s http://127.0.0.1:8765/health
```

#### 进入管理后台

首次启动前，先复制配置文件并设置一个管理密码：

```bash
cp .env.example .env
# 编辑 .env，至少设置：
# AURA_LILY_ADMIN_USER=admin
# AURA_LILY_ADMIN_PASSWORD=请替换为强密码
```

再启动服务：

```bash
.venv/bin/python tools/run_native.py
```

在部署这台电脑上打开 `http://127.0.0.1:8765/admin`；从局域网或公网访问时，打开 `http://<主机名或IP>:8765/admin`。用户名是 `AURA_LILY_ADMIN_USER`，密码是 `AURA_LILY_ADMIN_PASSWORD`。`8787` 只供设备连接 WebSocket 网关，不是管理后台端口。

如果要从公网访问后台，请先配置 HTTPS 反向代理并限制来源 IP。不要在没有密码保护的情况下暴露管理端口。模型密钥仅保存在你的本地运行环境中，仓库不会提供任何密钥。

### 2. 打开可选世界层

基础 Hermes 桥接可直接运行。要启用 Aura 的状态、场景和日程，请在 `.env` 中设置：

```bash
AURA_PERSONA_ENABLED=1
```

Soul 默认为空；你可以在本地管理页面填写自己的内容，或创建 `.aura/persona/persona/soul.md`。状态与日程存放在被 Git 忽略的 `.aura/` 本地运行目录中。

### 3. 编译并刷写设备

仓库包含完整固件源码和设备资源。普通用户可以从 [Releases](https://github.com/norsizu/aura-lily/releases) 下载预编译的完整 Web 刷机包；开发者也可以按下面的命令自行编译。Web 刷机包适用于 Waveshare ESP32-S3-RLCD-4.2，首次刷写会覆盖设备固件分区，刷完后需要重新配网。

```bash
cd firmware/esp32
source "$HOME/esp/esp-idf/export.sh"
idf.py set-target esp32s3
idf.py menuconfig
idf.py build
idf.py -p /dev/cu.usbmodemXXXX flash monitor
```

完整 Web 刷机包和 SHA-256 校验值随每个 Release 发布。固件默认不连接项目服务器，首次启动后请在配网页面填写你自己的 WebSocket 地址。

#### 普通用户：用 ESP LaunchPad 刷机

不安装 ESP-IDF 也可以直接刷写：

1. 准备 Chrome 或 Edge、USB 数据线，并下载 [Release 中的完整 `.bin` 刷机包](https://github.com/norsizu/aura-lily/releases/tag/v0.17.0-public)。
2. 打开 [Espressif ESP LaunchPad](https://espressif.github.io/esp-launchpad/)，点击顶部 **Connect**，选择设备的 USB 串口并授权。
3. 切换到 **DIY**，把 Flash Address 改为 `0x0000`，选择下载的完整 `.bin` 文件。
4. 点击 **Program**，等待进度完成；完成后在 Console 中重置设备，或拔插一次 USB。

这是一个完整合并镜像，只添加这一行文件即可，不要使用默认的 `0x1000` 地址。首次启动需要重新配网并填写你自己的 WebSocket 地址。

在 `menuconfig > Aura Lily` 中设置自己的 WebSocket 和 OTA 清单地址，或在首次启动后的配网页面保存它们。设备配置必须使用你的局域网、Tailscale 或公网地址，而不是 `127.0.0.1`。

### 4. 使用双 Wi-Fi 与 OTA

配网成功的网络会保留两个凭据槽位，菜单显示对应 SSID。默认不提供 OTA 服务器；请在 `menuconfig > Aura Lily` 中配置自己的 HTTPS 清单 URL，再使用 `tools/make_ota_release.py` 生成固件与资源清单。先上传全部工件，最后再发布 `manifest.json`。

详细的 Hermes 桥接、HTTP 合约和冒烟测试见 [Hermes bridge guide](integrations/hermes_lily_cli/README.md)。

## 仓库结构

```text
firmware/esp32/                     ESP32-S3 firmware, display, audio and local assets
integrations/hermes_lily_cli/       Hermes bridge, HTTP/WS gateway and local admin UI
integrations/aura_persona_gateway/  Optional Aura state, reminders, weather and world schedule
tests/                              Focused gateway, world, Wi-Fi, OTA and quota tests
tools/                              Asset, voice, diagnostics and OTA release tools
```

## 配置安全

仓库不提供模型密钥、默认服务地址或个人角色内容。请将 `.env`、`.aura/`、设备备份和构建产物留在自己的私有环境中。

## 验证

服务端测试：

```bash
python3 -m pytest -q tests
```

发布前还应执行一次 `idf.py build`，并确认应用镜像能放入 `0x280000` 的 OTA 分区。

## 社区

开源推广与交流链接：[LINUX DO](https://linux.do/)。

欢迎交流硬件适配、部署、角色资源与自托管经验。扫码加入「闲话 AI | Aura」QQ 群：`951895791`。

<p align="center">
  <img src="docs/community/qq-group.jpg" width="250" alt="闲话 AI | Aura QQ group 951895791">
</p>
