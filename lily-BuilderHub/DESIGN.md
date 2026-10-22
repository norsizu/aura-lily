# Lily BuilderHub 改进设计：可追溯的世界模型 + 真实情绪

基线：Aura Lily v0.18.1-public（commit cd2987f），分支 `builderhub/lily-world-emotion`。
分工：Claude Opus 负责设计、任务拆分、审查和真机验收；StepFun step-5-preview（以 Claude Code 子进程运行）负责实现。

## 0. 现状审计（2026-10-22）

| 方面 | 0.18.1 现状 | 问题 |
| --- | --- | --- |
| 日程 | `generate_day_plan` 生成 5 个锚点 + 4–8 个动态槽，按 `城市+日期` 做种子，结果可复现 | 槽位为什么出现（饿了、累了、好奇心高、天气）没有记录，事后解释不了 |
| 事件 | `companion_life_event` 记录槽位结算、零食、对话改场景，带 `delta_json` | 只记增量，不记前后值；`_update_drives`、天气影响、对话的 `state_rules` 增量都是静默修改，没有流水 |
| 回顾 | prompt 只带最近 3 条 `world.*` 事件 | 用户隔几个小时再来，Lily 说不出“你不在时我做了什么”，即使账本里有 |
| 设定密度 | `world/home.json` 有房间和物件 | 物件只有标签和位置，没有习惯、来历、感官细节，LLM 能借用的“锚点”很少 |
| 情绪 | 固件有 9 个身体姿势（只有 `happy`、`proud` 带情绪）；对话立绘 `face_115.bin` 每套服装 1 张脸，不随情绪变化；网关只有规则回复带 `emotion`，LLM 回复永远不带 | 情绪只停在纸面上：屏幕上看不到伤心、害羞，声音也没有情绪 |

## 1. 参考：《提升 AI Agent 应用活人感的工程化思考》带来的启发

文章公式：**活人感 = 身份深度 × 行为一致性 × 环境响应**，对应三层：角色设定系统、规则引擎/状态机、事件驱动系统。我们取其中能落到 Lily 上的五点：

1. **密度制造约束力**：设定越细，LLM 漂移空间越小。→ 给 canon 物件补“习惯/来历/感官”字段，只把**当前场景**的锚点注入 prompt，控制 token。
2. **DayScript 预规划，换来一致性、可追溯、离线运转**：Lily 已有日程表，缺的是“可追溯”。→ 每个槽位写入 `why`（触发它的状态值和规则）。
3. **离线挂机式回顾**：“她不是因为你看她才存在的”。→ 重连时从账本生成确定性的“你不在时”摘要，只能讲账本里有的事。
4. **兴趣驱动，不靠骰子**：→ 情绪由输入 + 回复 + 当前状态共同评估，不是随机挑姿势。
5. **人格描述胜过离散规则**：→ 情绪标签由 LLM 在人格上下文里自己判断，规则只做兜底和约束（体力低压低兴奋度等）。

## 2. 世界模型改进（模块 W）

### W1 世界账本（World Ledger）——一切状态变化都有出处
- 新增 `integrations/aura_persona_gateway/world_ledger.py`：`apply_world_change(store, scope, state, delta, *, cause, source, title, event_key=None, parent_key=None, payload=None)`。
- 记录 `before` / `after` / `delta` / `cause`（人话原因）/ `source`（`world_clock`、`hunger_drive`、`weather`、`drive_blend`、`dialogue`、`emotion`、`interaction`）/ `parent_key`（因果链）。
- 写入现有 `companion_life_event`（`event_type="ledger.<source>"`，before/after/cause 放 `payload_json`），**不改表结构**，保证老库兼容。
- 改造调用点：`_settle_completed_world_state`、`_maybe_settle_hunger_snack`、`_settle_temporary_care`、`_apply_weather_influence`、`_update_drives`（只有变化 ≥3 才记，避免刷屏）、`state_rules.apply_user_interaction_delta` / `apply_agent_reply_delta` 的调用方。
- 不变量（测试覆盖）：对任一数值字段，按时间重放账本 `delta` 必须得到当前值；同一 `event_key` 重复结算不重复记账。

### W2 日程理由（DayScript why）
- `generate_day_plan` 每个槽位 `payload.why = {"rule": "...", "inputs": {"satiety": 42, ...}, "text": "饱腹 42 ≤ 45，下午加一顿零食"}`。
- 锚点槽写 `rule="anchor"`；随机选择写清种子来源，保证可复现。

### W3 离线回顾（Offline Recap）
- `build_offline_recap(store, scope, since_ts, now)`：用户上次互动距今 ≥ 60 分钟时，从账本取这段时间的事件，按时间合并成 ≤ 4 条短句（例如“14:20 去公园散步，体力 -7，心情 +5”）。
- 注入 prompt：“你不在的这段时间（只能讲以下发生过的事，不要编造）”。只在本轮第一次对话注入一次。

### W4 设定集密度
- `world/home.json` 的物件新增可选字段 `habit`、`story`、`sense`；内置默认 canon 为 Lily 补 12–16 个带细节的物件（书房西窗、阳台的绿萝、厨房的旧砂锅……），全部是虚构公寓，不涉及真实地址。
- `_scene_prompt_text` 只渲染当前场景最多 3 个物件的细节。

### W5 追溯工具
- `tools/world_trace.py --db <sqlite> [--day YYYY-MM-DD] [--json]`：输出 Alice Debug Console 式时间线——今日日程（含 why 和状态）、账本流水（before→after、原因、因果链）、当前情绪。
- 只读，不改库。

## 3. 真实情绪（模块 E）

### E1 情绪集合（与固件对齐）
`neutral` 平静、`happy` 高兴、`excited` 兴奋、`shy` 害羞、`sad` 难过（含伤心）、`worried` 担心、`pouty` 小生气/委屈、`surprised` 惊讶。旧值兼容：`warm→happy`、`firm/proud→neutral`（身体姿势仍用 proud）、`calm/focused/attentive→neutral`、`curious→surprised`。

### E2 情绪从哪里来（评估链）
1. **LLM 自评**：系统提示要求回复**第一行**输出 `<emo:shy>` 这类标签（只允许 E1 中的值），网关在流式输出里剥离标签，不送 TTS、不进字幕、不进历史。
2. **规则兜底**：LLM 没给或给了非法值时，`emotion_engine.classify(user_text, reply_text)` 用关键词判断（被夸→shy，坏消息/告别→sad，用户凶→pouty……），都不命中就是 neutral。
3. **状态调制**：体力 < 25 时 `excited` 降为 `happy`；心情 < 30 时 `happy` 降为 `neutral`（勉强不起来）；压力 > 75 时 `surprised` 改成 `worried`。调制前后都写进 debug。
4. **情绪惯性**：上一轮是 sad，本轮 LLM 判 happy 但用户只说了一句普通话 → 保留 neutral 过渡（不会从难过一步跳到兴奋）。规则：上一轮情绪的强度衰减系数 0.5，存放在 `metadata.emotion_state`。

### E3 情绪的后果（写回世界模型，可追溯）
- 每种情绪对应小幅心情增量（happy +2、excited +3、shy +1、sad −3、worried −2、pouty −2、surprised 0），经 W1 账本写入，`source="emotion"`，`cause` 写触发句摘要。
- 当前情绪进入下一轮 prompt：“你刚才的情绪：难过（因为用户说要搬走）”，让情绪有延续。

### E4 情绪的表达通道
| 通道 | 实现 |
| --- | --- |
| 对话立绘（屏幕右侧头像） | 新资源 `faces/emotion_115.bin`：8 种情绪 × 115×115 8 位灰度（与 `face_115.bin` 同格式），由现有立绘为参考生成后抖动成 1-bit 风格；固件 `draw_face` 先查情绪脸，缺资源时回落到服装脸 |
| 身体姿势 | `happy/excited → happy`，其余不强制（保持随机说话姿势） |
| 声音 | MiniMax `voice_setting.emotion`（happy/sad/angry/surprised/fearful/neutral）与火山 `emotion` 参数；不支持的服务商忽略 |
| 协议 | `dialogue.payload.emotion` 新字段；旧固件忽略未知字段，完全兼容 |

## 4. 四天节奏

| 天 | 内容 | 负责 |
| --- | --- | --- |
| Day 1 | 设备刷 0.18.1、基线审计、读文章、出设计和任务书 | Claude |
| Day 2 | W1–W5 世界模型（账本、日程理由、离线回顾、设定集、追溯工具）+ 测试 | step-5 实现，Claude 审查 |
| Day 3 | E1–E4 情绪引擎（服务端 + TTS + 固件 + 情绪脸资源）+ 测试 | step-5 实现，Claude 审查 |
| Day 4 | 构建固件、真机验收、文档、回传 BuilderHub | Claude + step-5 |

## 5. 约束
- 不改数据库表结构；新数据走 `payload_json` / `metadata_json`。
- 旧固件与新服务端、新固件与旧服务端都要能正常对话。
- 全部改动有 pytest；现有测试不能新增失败（先记录基线失败数）。
- 不把任何密钥、API Key 写进仓库。
