# 任务书 W：可追溯的世界模型（交给 step-5-preview）

先完整阅读 `lily-BuilderHub/DESIGN.md` 第 0、2、5 节。仓库根目录是 `aura-lily-public-next`，分支 `builderhub/lily-world-emotion`。只改 Python 服务端和测试，**不碰 firmware/**。

## 基线
`python3 -m pytest -q tests` 当前 453 passed。完成后必须仍然全绿，并新增测试。

## 要做的事

### W1 世界账本
1. 新建 `integrations/aura_persona_gateway/world_ledger.py`：
   - `TRACKED_FIELDS = ("mood","energy","satiety","stress","trust","beans","affinity_xp")`，隐藏驱动 `("social_need","curiosity","privacy_sensitivity","resource_comfort")` 存在 `state["metadata"]`。
   - `apply_world_change(store, scope, state, delta, *, source, cause, title, event_key=None, parent_key=None, payload=None, location="", activity="") -> tuple[dict, dict|None]`：复制 state，按现有 `_apply_delta` 的规则（0–100 截断；beans 不截上限、下限 0）应用 delta，算出每个字段的 before/after/实际变化（截断后的真实变化），实际变化全为 0 时不记账；否则调用 `store.record_life_event(scope, event_type=f"ledger.{source}", event_key=..., title=title, description=cause, location=..., activity=..., visibility="private", delta=<实际变化>, payload={"before":..., "after":..., "cause":cause, "source":source, "parent_key":parent_key, **payload})`。返回 (新 state, 事件摘要)。同一 event_key 已存在（`store.has_life_event`）时不重复应用也不记账。
   - `replay_ledger(events, initial) -> dict`：按时间顺序把 ledger 事件的 delta 累加到 initial 上（用于测试不变量）。
2. 改造 `world.py` 调用点，让以下改动都经过账本（保留原有的 `world.slot_effect` 等 event_key 语义，避免重复结算；可以直接把原来的 record_life_event 换成 apply_world_change，event_type 变为 `ledger.world_clock` 等，但要同步修改 `list_recent_life_events(event_prefix="world.")` 的读取方，让 prompt 里的“近期世界变化”仍能读到这些事件——建议 recent_events 同时读 `world.` 和 `ledger.` 前缀，或在 store 增加多前缀查询）：
   - `_settle_completed_world_state`（source=world_clock，cause 写“按日程完成：<title>”）
   - `_maybe_settle_hunger_snack`（source=hunger_drive，cause 写“饱腹 X ≤ 30，自己补充食物”）
   - `_settle_temporary_care`（source=world_clock）
   - `_apply_weather_influence`（source=weather，先读懂它现在怎么改 state）
   - `_update_drives`：隐藏驱动变化绝对值 ≥ 3 时记一条 `ledger.drive_blend`，event_key 用 `drive:<字段>:<int(now//300)>` 防刷屏。
3. 找到对话回合里调用 `state_rules.apply_user_interaction_delta` / `apply_agent_reply_delta` 的地方（`rg -n "apply_user_interaction_delta|apply_agent_reply_delta|apply_time_recovery" integrations`），在保存 state 前计算 before/after 差异，用 source=interaction 记账（cause：“用户互动”/“完成一次回复”/“时间恢复”）。不改变原有数值规则。

### W2 日程理由
`generate_day_plan` 每个槽位写 `payload["why"] = {"rule": str, "inputs": {...}, "text": 中文一句话}`：
- 锚点（wake/breakfast/lunch/dinner/night_settle）：rule="anchor"。午饭写出 lunch_out 的依据（energy、satiety、天气）。
- morning_focus（阳台 or 书房的依据）、afternoon（`_choose_outing_axis` 的输入）、evening、personal_window、snack（satiety≤45 或随机 25%）、recharge（energy<48 或随机 22%）、social_pause（social_need≥65 等）、curiosity_project（curiosity≥65 或 mood≥78 随机）。
- 随机触发时 text 写“今天的日程种子抽中了 …”，inputs 带 `seed_roll`。
不改变任何槽位的生成结果（同种子同输入必须生成与改动前完全相同的 slot_key/时间/地点/标题；写一个测试固定 3 组输入对比改动前后结果——改动前的期望值请先运行旧代码得到，写死在测试里）。

### W3 离线回顾
`world.py` 新增 `build_offline_recap(store, scope, *, since_ts, now, city, limit=4) -> list[str]`：取 since_ts 之后的 ledger/world 事件，按时间排序，格式 `HH:MM 标题（心情+5，体力-7）`，只保留有意义的字段，最多 limit 条。
在 `build_world_snapshot` 中：读 `state["metadata"]["last_user_interaction_at"]`（如不存在，找现有字段，如 `_recent_times` 用到的互动时间；没有就新增并在用户互动时写入），间隔 ≥ 3600 秒且本次 recap 未注入过（`metadata["offline_recap_for"] != last_ts`）时生成 recap，放进 snapshot["offline_recap"] 并在 `render_world_prompt` 增加：
“你不在的这段时间（距上次聊天约 N 小时）。只能讲以下真实发生过的事，不要编造：\n- …”
注入后写 `offline_recap_for` 防重复。recap 为空则不注入。

### W4 设定集密度
`world_assets.default_world_canon()` 给物件增加可选 `habit`/`story`/`sense` 字段，补到 12–16 个物件，分布在 11 个场景里的 5 个家中场景为主（书房西窗、阳台绿萝、厨房旧砂锅、卧室床头的小夜灯、客厅的旧唱片机等，风格温柔日常，虚构）。`_scene_prompt_text` 对当前场景最多 3 个物件渲染“物件：习惯/细节”。`home.json` 用户自定义仍可覆盖。

### W5 追溯工具
`tools/world_trace.py --db PATH [--scope-user X] [--day YYYY-MM-DD] [--json]`：只读打开 sqlite（`file:...?mode=ro`），输出：
1. 当前状态（companion_state 最新行 + metadata 里的隐藏驱动和 emotion_state，如有）
2. 今日日程：时间、标题、地点、status、why.text
3. 账本流水：时间、source、title、before→after、cause、parent_key
先读 `store.py` 了解表结构和 scope 字段。

## 测试（新建 `tests/test_world_ledger.py`、`tests/test_world_recap.py`、`tests/test_world_trace.py`）
- 账本：截断后的真实变化；全 0 不记账；重复 event_key 不重复；replay 不变量（跑一整天 world tick：用现有测试里的 fake/真实 sqlite store，固定 now 序列，最后 replay(初始) == 当前值，覆盖 mood/energy/satiety/beans）。
- why：每个槽位都有 why.rule 和 why.text；生成结果与改动前一致。
- recap：间隔 < 1h 不注入；≥ 1h 注入一次，第二次不重复；只包含窗口内事件。
- trace：对临时库运行 `main(["--db", path, "--json"])` 能输出包含 plan/ledger 的 JSON。
参考 `tests/test_aura_persona_gateway.py` 里现有的 store/config 构造方式。

## 完成标准
- `python3 -m pytest -q tests` 全绿。
- 不要 git commit（由 Claude 审查后提交）。
- 结束时输出：改动文件清单、新增测试数、设计中做不到或有疑问的地方。
