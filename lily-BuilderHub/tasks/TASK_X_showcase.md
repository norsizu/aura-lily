# 任务书 X：路演三件套——X-Ray 大屏、“问她为什么”、语音便签板（交给 step-5-preview）

先读 `lily-BuilderHub/DESIGN.md`、`PLAN.md`。仓库 `aura-lily-public-next`，分支 `builderhub/lily-world-emotion`（世界账本 b57543a、情绪引擎 acd4ddc 已合入）。
**只改 `integrations/`、`tools/`、`tests/`，不碰 `firmware/`**（另一个 step-5 正在并行改固件）。不改数据库表结构，新数据走 `metadata` / `payload_json`。不新增第三方依赖。服务器只有 1GB 内存，不要引入常驻线程/大缓存。

## 基线
开发时只跑相关测试文件。全量测试会吃本地对话配额（`.aura/persona/config/dialogue_quota.sqlite3`，5 小时 50 次）；`test_lily_http_handler_limits_concurrency`、`test_smoke_test_runs_cli_and_http` 两个用例若因 `dialogue quota exceeded` 失败属环境问题，报告里注明即可。最后跑一次全量。

---

## X1 X-Ray 大屏（路演投影用，只读）
目的：评委看不清 4.2 寸屏，大屏实时展示“她为什么是这个情绪 / 数值为什么变了”。

- 在 `integrations/hermes_lily_cli/server.py` 加：
  - `GET /trace` → 一个自包含 HTML 页（内联 CSS/JS，无外部资源/CDN）。
  - `GET /trace/data` → JSON。
  - 鉴权：环境变量 `AURA_TRACE_TOKEN`；未设置时两个路由都返回 404；请求必须带 `?token=<值>`（页面里的 JS 从自身 URL 取 token 去拉 data），不匹配返回 403。用 `hmac.compare_digest`。**token 不能写进仓库任何文件**。
- `/trace/data` 内容（数据读取尽量复用 `tools/world_trace.py` 的函数，必要时把它们挪进 `integrations/aura_persona_gateway/` 里一个小模块再让 tool 引用，别复制两份）：
  - `state`：mood / energy / satiety / stress / trust 等当前数值。
  - `emotion`：`metadata.emotion_state`（emotion、label、intensity、cause、at）+ **最近一轮的情绪评估链**（source=llm|rule、raw、modulated_from、inertia_from、reason）。找一下这些 debug 现在落在哪（消息 metadata / turn debug），没有持久化就在 `_record_emotion_turn` 里把这几项一起写进 `emotion_state`（字段名 `last_resolution`）。
  - `ledger`：最近 15 条账本事件（时间、source、title/cause、delta、before→after）。
  - `plan`：今天的日程槽（时间、标题、why），当前槽标出来。
  - `notes`：X3 的便签。
  - `why`：X2 最近一次“问为什么”用到的账本 event_key 列表（大屏高亮这些行）。
  - `server_time`。
- 页面：深色背景、大字号（投影 3 米外可读）、2 秒轮询；左上“现在的情绪”（中文标签 + 强度 + 一句原因），下方评估链一行（例：`LLM 自评 happy → 状态调制：心情 28<30 → neutral`）；右侧账本流水，新出现的行高亮 3 秒；底部今日日程与便签。纯原生 JS。
- SQLite 只读（`mode=ro` URI）打开，每次请求开关连接，不缓存。

## X2 问她为什么（可追溯，用语音）
用户问 Lily 自身状态的原因时，她必须**按账本里真实记录的原因**回答，不能编。

- 意图识别（规则，放在 `integrations/aura_persona_gateway/` 合适的模块）：例如 `你为什么(不开心|难过|生气|委屈|开心|高兴|这么累|累|饿|兴奋|害羞)`、`你怎么(了|不开心了|生气了|这么累)`、`你心情(怎么样|为什么)`、`你刚才为什么…`。写成可测试的函数 `is_self_why_question(text) -> bool`。注意别把“为什么天是蓝的”这类问题判进来（必须是问 Lily 自己）。
- 命中时，在本轮 prompt 里加一节（只在命中时加，不要每轮都加）：
  `## 你最近状态变化的真实原因（来自你的生活账本，回答时只能据此说，不要编造其他原因）`
  内容：`emotion_state` 一行 + 最近 6 小时内与 mood/energy/stress/satiety 相关的最多 6 条账本事件（时间、原因、变化 before→after，用口语化中文，例如“14:20 去公园散步，体力 80→73”）。没有记录时写“最近没有记录到明显变化”，并要求她照实说。
- 确认这类问题不会被 `voice_turn` 规则快答截走、不会被当成后台查资料（`AGENT_LOOKUP_RE` / `[后台]` 标记）；流式（`run_direct_turn_stream`）和非流式路径都要生效。
- evidence / debug 里带 `why_trace: [event_key...]`，X1 大屏用它高亮。
- 情绪标签指令、上一轮情绪那行照常保留。

## X3 语音便签板（设备信息板 3 格便签，固件不改）
固件现状：`status_update.payload.notes`（字符串数组，最多 3 条，每条设备端缓冲 64 字节）显示在信息板便签栏；服务端现在只放提醒（`gateway.py` `pending_info_board_notes`）。看一下 `firmware/esp32/main/ui/panels_info_board.c` 便签一行能显示多宽，定一个字数上限（预计 ≤14 个汉字），超出截断加“…”。

- 存储：companion state `metadata.pinned_notes`：`[{text, source: "voice"|"agent", at}]`，最多 3 条，新的在前，挤掉最旧的。
- 来源 a（语音）：`记在便签上…` / `帮我记一下：…` / `记到黑板上…` / `贴个便签…` → 把内容（去掉指令词）钉上，Lily 简短确认（“记好啦，贴在便签上了”）。`清空便签` / `把便签删了` → 清空。这类指令走规则快答，不进 LLM、不进后台。
- 来源 b（Agent）：Hermes 后台任务完成、结果存为 background_task_result 时，自动生成一条 ≤上限字数的结论便签（例如“北京明天 小雨 12-18℃”）。生成方式：优先用已有的快模型（aura_model，超时 ≤5 秒）做一句话压缩，失败就截取结果第一句。只对 `agent_lookup` 类任务自动钉，`agent_create`（文档/代码）只钉“《标题》已完成”。
- 下发：`status_update_payload` 的 `notes` = 提醒（按时间）+ 便签（新在前），合计最多 3 条。钉上/清空后**立即**给设备推一次 `status_update`（找现有推送机制：`send_status_update` / `poll_world_status_updates`；gateway 与 bridge 是两个进程，确认 gateway 能读到 bridge 写入的 metadata）。
- 每次钉便签写一条账本/生活事件（`source="note"`，cause 写来源和内容），X1 能看到。

---

## 测试（新建 `tests/test_showcase_trace.py`、`tests/test_self_why.py`、`tests/test_pinned_notes.py`）
- X1：未设 token → 404；错 token → 403；对 token → data 含上面各字段；HTML 不引用外部 URL；只读打开（写操作失败）。
- X2：意图正例/反例各 ≥6；命中时 prompt 含账本原因且 why_trace 对得上；未命中时 prompt 不含该节；无记录时的文案；不被快答/后台截走（流式 + 非流式各一个）。
- X3：语音钉/清空；最多 3 条挤掉最旧；字数截断；agent 结果自动钉（mock 快模型成功/失败两条路）；notes 合并顺序；钉完触发 status_update 推送；账本有 `source="note"`。
- 回归：已有测试不新增失败；任何发往设备的 notes 不含 `<emo:`。

## 完成标准
- 不要 git commit。
- 结束时输出：改动文件清单、新增测试数、全量测试结果、`/trace` 使用方法（不要写出任何真实 token）、没做/做不到的项。
