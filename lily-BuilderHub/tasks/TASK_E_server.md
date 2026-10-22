# 任务书 E-server：服务端真实情绪（交给 step-5-preview）

先完整阅读 `lily-BuilderHub/DESIGN.md` 第 3、5 节和 `lily-BuilderHub/PLAN.md` 的“范围”。仓库根目录 `aura-lily-public-next`，分支 `builderhub/lily-world-emotion`（世界账本已在 b57543a 合入，`world_ledger.apply_world_change` 可直接用）。只改 Python 服务端和测试，**不碰 firmware/**。

## 基线
`python3 -m pytest -q tests` 当前 503 passed。完成后必须全绿并新增测试。
注意：全量测试会占本地对话配额（`.aura/persona/config/dialogue_quota.sqlite3`，5 小时 50 次）。不要反复跑全量；开发时只跑相关测试文件，最后跑一次全量。出现 429 类失败先检查配额。

## 要做的事

### E1 情绪集合与兼容映射
新建 `integrations/aura_persona_gateway/emotion_engine.py`：
- `EMOTIONS = ("neutral","happy","excited","shy","sad","worried","pouty","surprised")`
- `normalize_emotion(value) -> str`：合法值原样返回；旧值映射 `warm→happy`、`firm/proud→neutral`、`calm/focused/attentive→neutral`、`curious→surprised`；其他→`""`（表示无效）。
- 中文标签 `EMOTION_LABELS`（平静、高兴、兴奋、害羞、难过、担心、委屈、惊讶）。

### E2 情绪评估链
1. **LLM 自评标签**：在 persona 系统提示里（找到组装 system prompt 的地方，`rg -n "system_prompt|build_.*prompt" integrations/aura_persona_gateway`）加一段：回复第一行必须先输出 `<emo:X>`，X 只能取 E1 里的值，之后正常说话。
2. **流式剥离**：`emotion_engine.EmotionTagStripper`，逐块喂入文本 `feed(chunk) -> str`（返回可安全输出的文本），`finish() -> str`，属性 `emotion`。要求：
   - 标签只在回复开头识别（允许前导空白）；标签可能被拆到多个分片（`<em` + `o:sh` + `y>你好`）。
   - 开头不是 `<` 时立刻放行，不能增加首字延迟；开头像标签时最多缓冲 24 个字符，超出仍未闭合就当普通文本放行。
   - 非法值：剥掉标签，emotion 记为无效。
   - 回复中间出现的 `<emo:...>` 也要删掉（模型偶尔会重复），但不影响已确定的 emotion。
   找到 LLM 流式输出进入 TTS / 字幕 / 历史的所有路径（`turn.py` 的流式函数、`hermes_lily_cli/gateway.py` 的 send_dialogue_and_tts 等），确保**任何路径**都看不到标签；非流式路径用 `strip_emotion_tag(text) -> (clean_text, emotion)`。
3. **规则兜底**：`classify(user_text, reply_text) -> str`，关键词规则：被夸（好看/可爱/喜欢你/真棒）→shy；坏消息、失去、告别（走丢/去世/分手/难过/哭/搬走）→sad；担忧（手术/生病/考试/面试/医院/害怕）→worried；用户凶/骂（笨/讨厌你/闭嘴/滚）→pouty；意外好事（中奖/彩票/居然/竟然/真的吗）→surprised；好消息（升职/通过/成功/开心/太好了）→happy，带感叹或“太/超级”→excited；都不中→neutral。用户文本优先于回复文本。
4. **状态调制** `modulate(emotion, state) -> (emotion, reason)`：energy<25 时 excited→happy；mood<30 时 happy→neutral；stress>75 时 surprised→worried。
5. **情绪惯性** `apply_inertia(emotion, previous, *, user_text)`：上一轮是 sad/worried/pouty 且本轮用户文本被 classify 判为 neutral 时，happy/excited 降为 neutral。上一轮情绪存 `state["metadata"]["emotion_state"] = {"emotion", "label", "intensity", "cause", "at"}`（intensity 1–5，每轮衰减 ×0.5 取整）。**这一项是可砍项，前面都做完再做。**
6. 合成入口 `resolve_turn_emotion(*, llm_emotion, user_text, reply_text, state) -> dict`，返回 `{"emotion","source":"llm|rule","raw","modulated_from","reason"}`，写进 turn 的 debug。

### E3 情绪后果
- 每轮确定情绪后，用 `world_ledger.apply_world_change(..., source="emotion")` 记心情增量：happy +2、excited +3、shy +1、sad −3、worried −2、pouty −2、surprised 0、neutral 0（0 不记账）。cause 写“情绪：害羞（用户：你今天真好看）”，用户文本截断 20 字。event_key 用 `emotion:<request_id>` 防重复。speculative 回合不记。
- 下一轮 prompt 带上一行：“你刚才的情绪：难过（因为用户说……）”。来源是 `metadata.emotion_state`。

### E4 协议与声音
- `dialogue` 消息 payload 新增 `"emotion": "<E1 值>"`（`gateway.py` 里构造 dialogue 的地方，现在有 `"pose": voice_turn_pose(...)` 的 3 处，见 2600、3961、7175 行附近）。旧固件忽略未知字段。
- `emotion_to_pose`：happy/excited/warm → "happy"；firm/proud → "proud"；其他 → None（保持随机说话姿势）。保留旧值兼容。
- TTS：`voice_providers.synthesize_minimax` 支持可选 `emotion` 参数，映射到 `voice_setting.emotion`：happy/excited→happy，sad→sad，worried→fearful，pouty→angry，surprised→surprised，shy/neutral→不传。`synthesize_volcengine` 同理加 `emotion` 参数（先读懂现有请求体；火山请求体里情绪参数如果不确定格式，就只在 debug 里记录、不发送，并在结束报告里说明）。其余服务商忽略。把 emotion 从 turn 结果一路传到 `synthesize_and_stream_tts`。
- 规则回复（voice_turn.py 里已有 `emotion` 字段的 fastpath）也走 `normalize_emotion`，保证协议里只出现 E1 值。

## 测试（新建 `tests/test_emotion_engine.py`、`tests/test_emotion_protocol.py`）
- 剥离器：完整标签、跨 3 个分片、无标签首字立即放行、非法值、超长伪标签放行、中途重复标签删除、只有标签没有正文。
- classify：每种情绪至少 2 个用例；用户文本优先。
- modulate 三条规则；inertia。
- resolve：LLM 合法值优先；非法/缺失走规则。
- 账本：情绪产生 `ledger.emotion` 流水，心情 delta 正确，neutral 不记，同 request_id 不重复。
- 协议：dialogue payload 含 emotion 且为 E1 值；pose 映射；TTS 请求体（mock HTTP）里 MiniMax emotion 正确。
- 回归：已有测试全部通过，**任何发往 TTS、字幕、消息历史的文本都不含 `<emo:`**（写一个端到端测试，用假 LLM 流返回 `<emo:sad>我在` + `听。`）。

## 完成标准
- `python3 -m pytest -q tests` 全绿。
- 不要 git commit（由 Claude 审查后提交）。
- 结束时输出：改动文件清单、新增测试数、没做/做不到的项（尤其 E2.5 和火山情绪参数）。
