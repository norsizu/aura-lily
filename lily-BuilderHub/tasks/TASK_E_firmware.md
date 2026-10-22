# 任务书 E-firmware：设备显示真实情绪脸（交给 step-5-preview）

先读 `lily-BuilderHub/DESIGN.md` 第 3 节（E1、E4）。仓库根目录 `aura-lily-public-next`，分支 `builderhub/lily-world-emotion`。
服务端已完成（提交 acd4ddc）：`dialogue.payload.emotion` 永远是 E1 八个值之一：
`neutral / happy / excited / shy / sad / worried / pouty / surprised`。

**只改 `firmware/esp32/main/`、`tools/`（新脚本）和 `tests/firmware/`。不碰 `integrations/`、不碰 `partitions.csv`、`sdkconfig*`、`lily-BuilderHub/sdkconfig.builderhub`、`Kconfig.projbuild`、`network/net_discovery.c`。**
另一个 step-5 正在并行改 `integrations/` 和 `tests/`（非 firmware），不要动那些文件。

## 现状（已核实，行号可能有偏差）
- `network/ws_client.c` ~3015：`dialogue` 消息解析了 `text/pose/scene/coins_earned/...`，**没读 `emotion`**。
- `network/ws_client.c` ~2995：已有独立的 `{"type":"emotion","emotion":"..."}` 消息，写 `g_state.current_emotion`（`display/renderer.h` 里 `char current_emotion[16]`）。
- `ui/panels.c`：
  - `face_115.bin`：9 套服装 × 115×115 8 位灰度，`draw_face(outfit_idx, emotion_idx)` **优先画服装脸**，只有该资源缺失才回落 `draw_emoji`。所以现在情绪在屏幕上根本看不到。
  - `emoji_48.bin`：9 × 48×48 8 位灰度，`emotion_to_index` 名表为 `neutral, proud, thinking, surprised, apologetic, assertive, shy, excited, relaxed`（旧词表）。
  - 对话头像在 ~1149 行：`emotion_to_index(state->current_emotion)` → `draw_face(..., 52px)`。

## 要做的事
### F1 协议
`dialogue` 解析里读 `payload.emotion`（字符串）：存在且是 E1 值时写入 `g_state.current_emotion`（写法和现有 emotion 消息处理保持一致，注意长度与 `\0`）；字段缺失时**不改**当前值（旧服务端兼容）；非法值当 `neutral`。保持把情绪和对应那轮对话一起生效（不要让上一轮的情绪盖在新字幕上——看 `ws_buffer_pending_dialogue` 的缓冲机制，必要时把 emotion 一并缓冲，在字幕真正显示时再设）。

### F2 情绪脸资源 `emotion_115.bin`（可选资源，可能不存在）
- 格式：8 × 115×115 8 位灰度，顺序严格按 E1：`neutral, happy, excited, shy, sad, worried, pouty, surprised`，总长 8×13225 = 105800 字节。放在 `/spiffs/emotion_115.bin`（源文件 `firmware/esp32/assets/emotion_115.bin`）。
- 加载方式照抄 `load_face`（PSRAM、只尝试一次、长度不对就放弃）。
- 新脚本 `tools/build_emotion_faces.py`：输入一个目录里的 8 张 PNG（文件名 = E1 值），居中裁成正方形 → 115×115 → 灰度 → 写出 bin；缺任何一张就报错退出。再写一个 `--preview out.png` 选项把 bin 拼成一行预览图。只用 Pillow。**这次不需要你生成图片**，资源由 Claude 另外做。

### F3 绘制优先级（`draw_face` 或其调用处）
1. `current_emotion` 是 E1 值且 `emotion_115.bin` 已加载 → 画情绪脸（`neutral` 也画情绪脸的 neutral）。
2. 否则如果 `current_emotion` 不是 `neutral`/空 → 用 `emoji_48` 兜底，E1 → emoji 索引映射：
   `neutral→0 neutral, happy→1 proud(闭眼笑), excited→7 excited, shy→6 shy, sad→4 apologetic(低头), worried→2 thinking, pouty→5 assertive, surprised→3 surprised`。
   旧词表（proud/thinking/...）继续能用：先按 E1 查，查不到再按旧名表查。
3. 否则（neutral / 空）→ 现在的服装脸，行为不变。

### F4 情绪何时回到平静
情绪保持到下一轮对话带来新情绪为止；设备进入睡眠/待机超过 10 分钟没对话时回到 `neutral`（找现有的空闲计时机制，不要新开任务/定时器，找不到合适位置就只做“保持到下一轮”并在报告里说明）。

## 测试
- `tests/firmware/` 下新建 `test_emotion_face_contract.py`，风格参照 `tests/firmware/test_passport_button_contract.py`（读 C 源码做契约检查）：dialogue 解析读了 `"emotion"`；E1 八个名字都在映射表里且顺序与上面一致；`emotion_115.bin` 路径和 105800 长度；neutral 走服装脸。
- `tools/build_emotion_faces.py` 的 pytest：用 Pillow 现场造 8 张假 PNG → 生成 bin 长度 105800；缺一张报错。
- 跑 `python3 -m pytest -q tests/firmware tests/test_build_emotion_faces.py`（或你起的文件名）全绿。**不要跑全量 pytest**（另一个任务在跑，而且会吃对话配额）。

## 编译验证（必须做，不要刷机）
```
cd firmware/esp32 && . ~/esp/esp-idf/export.sh >/dev/null && \
idf.py -B build-builderhub -D SDKCONFIG=build-builderhub/sdkconfig \
  -D SDKCONFIG_DEFAULTS="sdkconfig.defaults;../../lily-BuilderHub/sdkconfig.builderhub" build
```
编译必须 0 error，新增代码不要引入新 warning。**不要执行 flash、不要碰串口**（设备由 Claude 刷）。

## 完成标准
- 不要 git commit。
- 结束时输出：改动文件清单、新增测试数、编译结果（app bin 大小）、F4 做到了哪一步、任何没做的项。
