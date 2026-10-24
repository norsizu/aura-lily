from __future__ import annotations

"""X1 X-Ray 大屏的页面本体（``GET /trace``）。

自包含：内联 CSS + 原生 JS + 内联情绪脸（data URI），不引任何外部资源
（路演现场可能没有外网）。版面固定 1920×1080，按窗口等比缩放。

一屏讲清楚三件事：
- 她在想：每来一轮新情绪，把真实的评估链当场演一遍（原话逐字打进来，
  第一反应 → 身心修正 → 上一刻心情 → 此刻心情 依次点亮，最后换脸、数值
  跳动）。按空格或点这一栏重播最近一轮，只重播真实数据。
- 她在哪：家的平面图，她所在的房间亮着，换房间时小点会走过去；每间屋
  写着今天在里面待了多久。
- 她的今天：底部 0 点到 24 点的时间轴（位置、心情、日程三条泳道）。拖动
  或点击时间轴、点账本里任一条，整屏回到那一刻：脸、数值、房间、账本都
  从账本流水倒推出来，不编数据；闲置一会儿自动回到实时。

页面自己从 URL 的 ``?token=`` 取令牌去轮询 ``<页面路径>/data``（即
``/trace/data``；挂在 nginx 前缀下也能自动对上），所以分享链接时令牌会
留在地址栏里；这正是它只用于现场投影、不进仓库文件的原因。
"""

import json

from .trace_faces import TRACE_FACES

_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Aura X-Ray</title>
<style>
  /* 暖黑底、一种墨色；情绪色是屏上唯一的彩色，落在她的情绪和她所在的地方。
     她说的话用宋体，像书里的对白。 */
  :root {
    --ground: #0f0e0d;
    --surface: #181715;
    --raised: #22201e;
    --line: #2f2c29;
    --ink: #f1f1ef;
    --muted: #b3b0aa;
    --faint: #8c8984;
    --negative: #ef6a5f;
    --emo: #8fb8c8;
    --serif: "Songti SC", "STSong", "Noto Serif CJK SC", "Source Han Serif SC", serif;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { width: 100%; height: 100%; background: var(--ground); overflow: hidden; cursor: default; }
  body {
    color: var(--ink);
    font-family: "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Noto Sans CJK SC", sans-serif;
    font-variant-numeric: tabular-nums;
  }
  #stage {
    position: absolute; left: 50%; top: 50%;
    width: 1920px; height: 1080px; overflow: hidden;
    transform-origin: 0 0;
    padding: 26px 48px 28px;
    display: grid;
    grid-template-columns: 440px 1fr 600px;
    grid-template-rows: 52px 1fr 268px;
    gap: 22px 0;
    background: var(--ground);
  }
  h2 { font-size: 20px; font-weight: 600; color: var(--ink); }
  h2 small { font-size: 16px; font-weight: 400; color: var(--faint); margin-left: 12px; }

  /* ── 顶栏 ── */
  header { grid-column: 1 / -1; display: flex; align-items: center; gap: 28px; }
  h1 { font-size: 28px; font-weight: 600; letter-spacing: -0.01em; }
  .tagline { font-size: 19px; color: var(--muted); }
  .spacer { flex: 1; }
  .sync { display: flex; align-items: center; gap: 10px; font-size: 17px; color: var(--muted); }
  .sync .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--ink); }
  .sync .dot.beat { animation: beat 0.9s ease-out; }
  @keyframes beat { 0% { transform: scale(2.2); opacity: 0.4; } 100% { transform: none; opacity: 1; } }
  .sync.stale { color: var(--negative); }
  .sync.stale .dot { background: var(--negative); }
  .weather { font-size: 19px; color: var(--muted); }
  .clock { font-size: 36px; font-weight: 500; min-width: 150px; text-align: right; }
  .clock s { text-decoration: none; color: var(--faint); font-size: 24px; margin-left: 2px; }
  .back-pill { display: none; height: 40px; line-height: 40px; padding: 0 20px; border-radius: 20px; font-size: 18px; font-weight: 500;
    background: var(--emo); color: var(--ground); cursor: pointer; }
  .viewing .back-pill { display: block; }

  /* ── 左栏：那台墨水屏 ── */
  #colA { grid-column: 1; grid-row: 2; display: flex; flex-direction: column; min-height: 0; padding-right: 40px; }
  .face-holder { position: relative; width: 340px; height: 340px; }
  .face { position: absolute; inset: 0; border-radius: 50%; background: #2b2926; padding: 14px; animation: breathe 5s ease-in-out infinite; }
  /* 呼吸灯：一圈心情色的光慢慢亮起又暗下去，外面再荡开两道涟漪 */
  @keyframes breathe {
    0%, 100% { box-shadow: 0 0 0 3px color-mix(in srgb, var(--emo) 40%, transparent), 0 0 16px color-mix(in srgb, var(--emo) 14%, transparent); transform: none; }
    50% { box-shadow: 0 0 0 4px var(--emo), 0 0 64px color-mix(in srgb, var(--emo) 48%, transparent); transform: scale(1.012); }
  }
  .face-holder .ring { position: absolute; inset: -4px; border-radius: 50%; border: 2px solid var(--emo); opacity: 0; pointer-events: none; transition: border-color 1.2s; }
  .face-holder .ring.r1 { animation: ripple 5s ease-out infinite; }
  .face-holder .ring.r2 { animation: ripple 5s ease-out 2.5s infinite; }
  @keyframes ripple { 0% { transform: scale(0.98); opacity: 0.5; } 100% { transform: scale(1.16); opacity: 0; } }
  @media (prefers-reduced-motion: reduce) { .face, .face-holder .ring { animation: none; } }
  .face img { width: 100%; height: 100%; border-radius: 50%; display: block; background: #fff; object-fit: cover; }
  .face .flash { position: absolute; inset: 14px; border-radius: 50%; background: #fff; opacity: 0; pointer-events: none; }
  /* 墨水屏换图会先闪一下白 */
  .face.swap .flash { animation: flash 0.9s ease-out; }
  @keyframes flash { 0% { opacity: 1; } 100% { opacity: 0; } }
  .emo-label { margin-top: 26px; font-family: var(--serif); font-size: 84px; font-weight: 700; line-height: 1; color: var(--emo); transition: color 1.2s; }
  .emo-label.swap { animation: labelin 0.6s ease-out; }
  @keyframes labelin { 0% { transform: translateY(14px); opacity: 0; } 100% { opacity: 1; } }
  .emo-meta { margin-top: 16px; display: flex; flex-direction: column; gap: 10px; font-size: 17px; color: var(--muted); min-height: 48px; }
  .pips { display: flex; gap: 5px; }
  .pips span { width: 22px; height: 6px; border-radius: 3px; background: var(--raised); transition: background 0.6s; }
  .pips span.on { background: var(--emo); }
  .gauges { margin-top: auto; display: grid; grid-template-columns: repeat(5, 1fr); padding-top: 18px; border-top: 1px solid var(--line); }
  .g { padding: 0 10px; border-left: 1px solid var(--line); min-width: 0; }
  .g:first-child { padding-left: 0; border-left: none; }
  .g .l { font-size: 16px; color: var(--muted); }
  .g .num { margin-top: 2px; font-size: 32px; font-weight: 500; line-height: 1.15; position: relative; display: inline-block; }
  .g .bar { margin-top: 8px; height: 4px; border-radius: 2px; background: var(--raised); overflow: hidden; }
  .g .bar i { display: block; height: 100%; border-radius: 2px; transition: width 1.2s ease, background 1s; }
  .g .d { position: absolute; left: 100%; top: 0; margin-left: 2px; font-size: 17px; font-weight: 500; color: var(--emo); opacity: 0; white-space: nowrap; }
  .g .d.show { animation: floatup 4s ease-out; }
  @keyframes floatup { 0% { opacity: 0; transform: translateY(10px); } 12% { opacity: 1; transform: none; } 80% { opacity: 1; } 100% { opacity: 0; } }

  /* ── 中栏：她在想 ── */
  #mind { grid-column: 2; grid-row: 2; position: relative; display: flex; flex-direction: column; min-height: 0;
    padding: 0 40px; border-left: 1px solid var(--line); border-right: 1px solid var(--line); cursor: pointer; }
  #mind > * { transition: opacity 0.4s; }
  .hint { position: absolute; right: 40px; top: 2px; font-size: 15px; color: var(--faint); }
  #mind h2 small { color: var(--emo); }
  .chain { position: relative; margin-top: 20px; padding-left: 50px; }
  .chain .track { position: absolute; left: 11px; top: 20px; bottom: 28px; width: 2px; background: var(--raised); }
  .chain .fill { position: absolute; left: 11px; top: 20px; width: 2px; height: 0; background: var(--emo); transition: height 0.7s ease; }
  .chain .spark { position: absolute; left: 6px; top: 16px; width: 12px; height: 12px; border-radius: 50%; background: var(--ink);
    opacity: 0; transition: top 0.7s ease, opacity 0.3s; }
  .node { position: relative; min-height: 78px; padding: 4px 0 10px; opacity: 0.3; transition: opacity 0.5s; }
  .node::before { content: ""; position: absolute; left: -46px; top: 12px; width: 16px; height: 16px; border-radius: 50%;
    background: var(--ground); border: 2px solid var(--raised); transition: background 0.5s, border-color 0.5s; }
  .node.lit { opacity: 1; }
  .node.lit::before { border-color: var(--emo); }
  .node.skip.lit { opacity: 0.72; }
  .node.skip.lit::before { border-color: var(--faint); }
  .node.final.lit::before { background: var(--emo); }
  .node .k { font-size: 17px; color: var(--muted); }
  .node .k i { font-style: normal; color: var(--faint); margin-left: 10px; }
  .node .k i::before { content: "· "; }
  .node .v { margin-top: 4px; font-size: 28px; font-weight: 600; line-height: 1.3;
    opacity: 0; transform: translateY(8px); transition: opacity 0.45s, transform 0.45s; }
  .node.lit .v { opacity: 1; transform: none; }
  .node.skip .v { font-size: 21px; font-weight: 400; color: var(--muted); }
  .node .v .q { font-family: var(--serif); font-size: 36px; font-weight: 700; }
  .node .v .q::before { content: "「"; color: var(--faint); }
  .node .v .q::after { content: "」"; color: var(--faint); }
  .caret { display: inline-block; width: 3px; height: 1em; margin-left: 4px; vertical-align: -0.12em; background: var(--ink); animation: blink 0.8s infinite; }
  @keyframes blink { 50% { opacity: 0.2; } }
  .chip { font-weight: 600; color: var(--ink); }
  .node.final .chip { color: var(--emo); }
  .arrow { color: var(--faint); margin: 0 12px; font-weight: 400; }
  .landed { margin-top: 8px; display: flex; flex-wrap: wrap; gap: 4px 24px; font-size: 18px; color: var(--muted);
    opacity: 0; transform: translateY(8px); transition: opacity 0.6s, transform 0.6s; }
  .landed.show { opacity: 1; transform: none; }
  .landed b { color: var(--ink); font-weight: 500; margin-left: 6px; }
  .said { margin-top: auto; font-family: var(--serif); font-size: 23px; line-height: 1.55; color: var(--ink); min-height: 72px; }
  .said:not(:empty) { padding: 14px 20px; border-radius: 16px; background: var(--surface); }
  /* 只露两行；说得长就像提词器一样一行行往上走，走完停一下再回到开头 */
  .said .win { height: 3.1em; overflow: hidden; }
  .said em { font-family: "PingFang SC", "Hiragino Sans GB", sans-serif; font-style: normal; font-size: 16px; color: var(--faint); margin-right: 12px; }

  /* ── 右栏：家 + 账本 ── */
  #colC { grid-column: 3; grid-row: 2; display: flex; flex-direction: column; min-height: 0; padding-left: 40px; }
  .home { margin-top: 12px; height: 236px; }
  .home svg { display: block; width: 100%; height: 100%; overflow: visible; }
  .home text { font-family: "PingFang SC", "Hiragino Sans GB", sans-serif; }
  .home .layer { transition: opacity 0.8s; }
  .home .layer.off { opacity: 0; pointer-events: none; }
  /* 户型图：照房产户型图的画法，实心墙、窗是墙里的细线、门画开门弧线 */
  .home .room path { fill: #1e1c1a; transition: fill 0.8s; }
  .home .room.was path { fill: #252320; }
  .home .room.here path { fill: color-mix(in srgb, var(--emo) 24%, var(--surface)); }
  .home .room.deco path { fill: color-mix(in srgb, var(--surface) 70%, var(--ground)); }
  .home .room.floor path { fill: #1e1c1a; }
  .home .floor .rl { font-size: 15px; fill: var(--faint); font-weight: 400; }
  .home .wall { fill: none; stroke: #d8d3c8; stroke-linecap: square; }
  .home .wall.o { stroke-width: 8; }
  .home .wall.i { stroke-width: 4; }
  .home .win { stroke: #d8d3c8; stroke-width: 1; }
  .home .win-bg { stroke: var(--ground); stroke-width: 8; }
  .home .door { fill: none; stroke: var(--faint); stroke-width: 1.2; }
  .home .rail { fill: none; stroke: #d8d3c8; stroke-width: 1.2; }
  .home .rl { font-size: 18px; fill: var(--muted); font-weight: 500; }
  .home .here .rl { fill: var(--ink); font-weight: 600; }
  .home .deco .rl { font-size: 14px; fill: var(--faint); font-weight: 400; }
  .home .rt { font-size: 13px; fill: var(--faint); }
  .home .ra { font-size: 14px; fill: var(--ink); }
  .home .tag { font-size: 12px; fill: var(--faint); }
  .home .tag.river { fill: #8fc0dd; }
  /* 外面：一张虚拟的街区地图，只标她此刻在的地方 */
  .home .m-block { fill: #1c1a18; }
  .home .m-bldg { fill: #262421; }
  .home .m-park { fill: color-mix(in srgb, #7fae7a 14%, var(--surface)); }
  .home .m-tree { fill: color-mix(in srgb, #7fae7a 30%, var(--surface)); stroke: color-mix(in srgb, #7fae7a 45%, var(--surface)); stroke-width: 1; }
  .home .m-water { fill: color-mix(in srgb, #5b8fb0 26%, var(--ground)); stroke: color-mix(in srgb, #8fc0dd 40%, transparent); stroke-width: 1; }
  .home .m-path { fill: none; stroke: color-mix(in srgb, #d8d3c8 16%, var(--surface)); stroke-width: 7; stroke-linecap: round; }
  .home .m-road { stroke: #2b2926; stroke-width: 12; fill: none; }
  .home .m-roadc { stroke: #3d3a36; stroke-width: 1; stroke-dasharray: 5 7; fill: none; }
  .home .m-zebra { fill: none; stroke: #45423d; stroke-width: 5; }
  .home .m-wall { fill: none; stroke: #d8d3c8; stroke-width: 5; stroke-linecap: square; }
  .home .m-shop { fill: none; stroke: #6a6660; stroke-width: 2; }
  .home .m-shelf { fill: none; stroke: var(--faint); stroke-width: 3; stroke-linecap: round; }
  .home .m-table { fill: none; stroke: var(--faint); stroke-width: 1.4; }
  .home .m-void { fill: var(--ground); stroke: var(--faint); stroke-width: 1.2; stroke-dasharray: 4 4; }
  .home .m-lot { fill: none; stroke: #3d3a36; stroke-width: 1.2; }
  .home .m-homeb { fill: #2e2b28; stroke: var(--muted); stroke-width: 1; }
  .home .m-river { fill: color-mix(in srgb, #5b8fb0 26%, var(--ground)); }
  .home .m-wave { fill: none; stroke: color-mix(in srgb, #8fc0dd 60%, transparent); stroke-width: 1.4; stroke-linecap: round; stroke-dasharray: 10 16; animation: flow 6s linear infinite; }
  .home .m-wave.w2 { animation-duration: 8s; }
  @keyframes flow { to { stroke-dashoffset: 52; } }
  .home .m-walk { fill: none; stroke: color-mix(in srgb, #a7c99a 70%, transparent); stroke-width: 2.2; stroke-dasharray: 1 6; stroke-linecap: round; }
  .home .m-spot { fill: color-mix(in srgb, var(--emo) 20%, var(--surface)); transition: fill 1.2s; }
  .home .m-homet { font-size: 13px; fill: var(--muted); }
  .home .m-pill rect { fill: var(--ink); }
  .home .m-pill text { font-size: 16px; font-weight: 600; fill: var(--ground); }
  .home .m-t { font-size: 13px; fill: var(--faint); }
  @media (prefers-reduced-motion: reduce) { .home .m-wave { animation: none; } }
  .home #me { transition: transform 1.6s cubic-bezier(.45, 0, .25, 1); }
  .home #me .dot { fill: var(--emo); stroke: var(--ground); stroke-width: 3; transition: fill 1.2s; }
  .home #me .ping { fill: none; stroke: var(--emo); stroke-width: 2; transform-box: fill-box; transform-origin: center; animation: ping 2.4s ease-out infinite; }
  @keyframes ping { 0% { transform: scale(1); opacity: 0.9; } 100% { transform: scale(3.2); opacity: 0; } }
  .viewing .home #me .ping { animation: none; opacity: 0; }
  .doing { margin-top: 14px; }
  .doing .t { font-size: 26px; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .doing .t small { font-size: 18px; font-weight: 400; color: var(--muted); margin-left: 12px; }
  .doing .s { margin-top: 4px; font-size: 17px; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .doing .s b { color: var(--faint); font-weight: 400; margin-right: 8px; }

  #ledger { position: relative; flex: 1; min-height: 0; margin-top: 18px; padding-top: 16px; border-top: 1px solid var(--line); display: flex; flex-direction: column; }
  .why-banner { display: none; margin-top: 10px; padding: 10px 16px; border-radius: 12px; font-size: 17px; line-height: 1.45; color: var(--muted); background: var(--surface); }
  .why-banner.show { display: block; animation: bannerin 0.6s ease-out; }
  @keyframes bannerin { 0% { transform: translateY(-10px); opacity: 0; } }
  .why-banner b { color: var(--ink); font-weight: 600; }
  .rows-wrap { position: relative; flex: 1; min-height: 0; margin-top: 8px; display: flex; flex-direction: column; }
  .rows { flex: 1; min-height: 0; overflow-y: auto; overflow-x: hidden; display: flex; flex-direction: column;
    padding-right: 28px; scrollbar-width: none; overscroll-behavior: contain; cursor: grab; user-select: none; }
  .rows::-webkit-scrollbar { display: none; }
  .rows.dragging { cursor: grabbing; }
  .day { flex: none; position: sticky; top: 0; z-index: 2; padding: 4px 0; font-size: 15px; color: var(--faint); background: var(--ground); border-bottom: 1px solid var(--line); }
  .scrub { position: absolute; right: 0; top: 0; bottom: 0; width: 20px; cursor: ns-resize; touch-action: none; }
  .scrub .rail { position: absolute; left: 9px; top: 0; bottom: 0; width: 2px; background: var(--raised); }
  .scrub .knob { position: absolute; left: 5px; width: 10px; height: 26px; margin-top: -13px; border-radius: 5px; background: var(--ink); transition: top 0.25s ease; }
  .scrub.live .knob { background: var(--faint); }
  .scrub.drag .knob { transition: none; }
  .scrub .at { position: absolute; right: 26px; margin-top: -14px; white-space: nowrap; font-size: 15px; line-height: 28px; padding: 0 10px;
    border-radius: 8px; background: var(--ink); color: var(--ground); opacity: 0; transition: opacity 0.3s; pointer-events: none; }
  .scrub.drag .at { opacity: 1; }
  .now-btn { position: absolute; right: 0; top: 12px; z-index: 3; padding: 0 14px; height: 30px; line-height: 30px; border-radius: 15px;
    font-size: 15px; font-weight: 500; background: var(--ink); color: var(--ground); cursor: pointer; opacity: 0; pointer-events: none; transition: opacity 0.3s; }
  .now-btn.show { opacity: 1; pointer-events: auto; }
  .row { display: grid; grid-template-columns: 52px 1fr; column-gap: 12px; padding: 8px 6px 8px 0; border-top: 1px solid var(--line); cursor: pointer; transition: background 0.3s; }
  .day + .row { border-top: none; }
  .rows .empty { padding: 18px 6px; font-size: 18px; color: var(--faint); }
  .row:hover { background: var(--surface); }
  .row .meta { grid-row: 1 / span 3; font-size: 15px; color: var(--faint); line-height: 26px; padding-left: 6px; }
  .row .meta b { display: none; }
  .row .ti { font-size: 20px; line-height: 26px; }
  .row .ti u { text-decoration: none; font-size: 15px; color: var(--faint); margin-left: 10px; }
  .row .ch { margin-top: 2px; display: flex; flex-wrap: wrap; font-size: 16px; color: var(--muted); }
  .row .ch span { white-space: nowrap; }
  .row .ch span + span::before { content: " · "; color: var(--faint); }
  .row.minor { padding: 4px 6px 4px 0; }
  .row.minor .ti { font-size: 16px; line-height: 22px; color: var(--faint); }
  .row.minor .meta { line-height: 22px; }
  .row.minor .ch, .row.minor .ti u { display: none; }
  .row.at { background: var(--raised); box-shadow: inset 3px 0 var(--emo); }
  .row.fresh { animation: fresh 3s ease-out; }
  @keyframes fresh { 0% { background: color-mix(in srgb, var(--emo) 30%, transparent); } 100% { background: transparent; } }
  .row.cited { background: var(--surface); }
  .row.cited .ti::after { content: "回答依据"; margin-left: 10px; font-size: 14px; padding: 1px 8px; border-radius: 9px; background: var(--ink); color: var(--ground); vertical-align: 2px; }

  /* ── 底部：她的今天 ── */
  #today { grid-column: 1 / -1; grid-row: 3; position: relative; border-top: 1px solid var(--line); padding-top: 14px; }
  #today .head { display: flex; align-items: baseline; gap: 20px; }
  #today .head .spacer { flex: 1; }
  #today .tip { font-size: 16px; color: var(--faint); }
  /* 便签：亮底深字的小纸条，屏上一眼能看到；待响提醒带情绪色边和倒计时。 */
  #today .notes { display: flex; gap: 12px; align-items: center; min-width: 0; }
  #today .notes .slip { display: flex; align-items: baseline; gap: 10px; max-width: 420px; padding: 7px 14px; border-radius: 6px;
    background: var(--ink); color: var(--ground); font-size: 19px; font-weight: 500; line-height: 26px; white-space: nowrap; overflow: hidden;
    }
  #today .notes .slip i { font-style: normal; font-size: 14px; font-weight: 600; color: #5a5752; }
  #today .notes .slip span { font-size: 15px; font-weight: 400; color: #5a5752; }
  #today .notes .slip.rem { background: var(--raised); color: var(--ink); box-shadow: inset 0 0 0 2px var(--emo); }
  #today .notes .slip.rem i, #today .notes .slip.rem span { color: var(--emo); }
  #today .notes .slip.new { animation: slipin 0.6s ease-out; }
  @keyframes slipin { 0% { transform: translateY(-10px) rotate(-2deg); opacity: 0; } }
  #today .notes .none { font-size: 16px; color: var(--faint); }
  .river { position: relative; margin-top: 8px; height: 214px; cursor: crosshair; touch-action: none; user-select: none; }
  .river svg { display: block; width: 100%; height: 100%; overflow: visible; }
  .river text { font-family: "PingFang SC", "Hiragino Sans GB", sans-serif; }
  .river .lane { font-size: 15px; fill: var(--faint); }
  .river .hour { font-size: 14px; fill: var(--faint); }
  .river .grid { stroke: var(--line); stroke-width: 1; stroke-dasharray: 2 6; }
  .river .seg { fill: var(--raised); }
  .river .seg.cur { fill: color-mix(in srgb, var(--emo) 45%, var(--raised)); }
  .river .seg.plan { fill: none; stroke: var(--line); stroke-width: 1.5; stroke-dasharray: 4 4; }
  .river .segl { font-size: 15px; fill: var(--ink); pointer-events: none; }
  .river .segl.plan { fill: var(--faint); }
  .river .mood { fill: none; stroke: var(--ink); stroke-width: 2; }
  .river .moodv { font-size: 14px; fill: var(--muted); }
  .river .mood.ghost { opacity: .18; }
  .river .ecg { fill: none; stroke: var(--emo); stroke-width: 2.5; stroke-linejoin: round; stroke-linecap: round; pointer-events: none; }
  .river .ecg-head { fill: var(--emo); pointer-events: none; }
  .river .tick { stroke: var(--faint); stroke-width: 1.5; }
  .river .quiet { fill: var(--surface); }
  .river .quietl { font-size: 13px; fill: var(--faint); letter-spacing: 0.5px; }
  .river .tick.fresh { stroke: var(--emo); stroke-width: 3; }
  .river .emo-dot { stroke: var(--ground); stroke-width: 3; }
  .river .emo-l { font-size: 15px; font-weight: 600; }
  .river .bead { fill: var(--ground); stroke: var(--muted); stroke-width: 2; }
  .river .bead.done { fill: var(--faint); stroke: var(--faint); }
  .river .bead.cur { fill: var(--emo); stroke: var(--emo); }
  .river .beadl { font-size: 14px; fill: var(--muted); }
  .river .beadl.cur { fill: var(--ink); font-weight: 600; }
  .river .note-pin { fill: var(--ink); }
  .river .rem { stroke: var(--ink); stroke-width: 2; fill: var(--ink); }
  .river .rem.pending { fill: var(--ground); stroke: var(--emo); }
  .river .rem.drop { fill: var(--negative); stroke: var(--negative); }
  .river .reml { font-size: 14px; fill: var(--ink); }
  .alert { display: none; height: 40px; line-height: 40px; padding: 0 20px; border-radius: 20px; font-size: 18px; font-weight: 600;
    background: var(--ink); color: var(--ground); }
  .alert.show { display: block; animation: alertin 0.5s ease-out, ring 1.2s ease-in-out 0.5s 3; }
  @keyframes alertin { 0% { transform: translateY(-16px); opacity: 0; } }
  @keyframes ring { 0%, 100% { transform: none; } 20% { transform: rotate(-3deg); } 40% { transform: rotate(3deg); } 60% { transform: rotate(-2deg); } 80% { transform: rotate(2deg); } }
  .river .future { fill: var(--ground); opacity: 0.55; pointer-events: none; }
  .river .now-line { stroke: var(--ink); stroke-width: 2; }
  .river .now-tab { fill: var(--ink); }
  .river .now-txt { font-size: 14px; font-weight: 600; fill: var(--ground); }
  .river .view-line { stroke: var(--emo); stroke-width: 2; }
  .river .view-tab { fill: var(--emo); }
  .river .hover-line { stroke: var(--faint); stroke-width: 1; }
  .tipbox { position: absolute; z-index: 5; top: 2px; padding: 6px 12px; border-radius: 10px; background: var(--raised); font-size: 16px; line-height: 1.4;
    color: var(--ink); white-space: nowrap; pointer-events: none; opacity: 0; transition: opacity 0.15s; }
  .tipbox.show { opacity: 1; }
  .tipbox span { color: var(--muted); }
  .error { position: absolute; right: 48px; top: 6px; font-size: 15px; color: var(--negative); z-index: 3; }
</style>
</head>
<body>
<div id="stage">
  <header>
    <h1>Aura X-Ray</h1>
    <div class="tagline">她的每一种情绪，都能在账本里找到来由</div>
    <div class="spacer"></div>
    <div class="alert" id="alert"></div>
    <div class="back-pill" id="backPill">回到实时</div>
    <div class="weather" id="weather"></div>
    <div class="sync" id="sync"><span class="dot" id="syncDot"></span><span id="syncText">连接中…</span></div>
    <div class="clock" id="clock">--:--</div>
  </header>

  <section id="colA">
    <div class="face-holder">
      <div class="ring r1"></div><div class="ring r2"></div>
      <div class="face" id="face"><img id="faceImg" alt=""><div class="flash"></div></div>
    </div>
    <div class="emo-label" id="emoLabel">—</div>
    <div class="emo-meta"><div class="pips" id="pips"></div><span id="emoAt"></span></div>
    <div class="gauges" id="gauges"></div>
  </section>

  <section id="mind">
    <h2>她在想<small id="chainAt"></small></h2>
    <div class="hint" id="mindHint">按空格或点这一栏 · 重放思考过程</div>
    <div class="chain" id="chain">
      <div class="track"></div><div class="fill" id="fill"></div><div class="spark" id="spark"></div>
      <div class="node" id="n1"><div class="k">她听到</div><div class="v" id="v1"></div></div>
      <div class="node" id="n2"><div class="k" id="k2">第一反应<i>模型自评</i></div><div class="v" id="v2"></div></div>
      <div class="node" id="n3"><div class="k">身心修正<i>状态调制</i></div><div class="v" id="v3"></div></div>
      <div class="node" id="n4"><div class="k">上一刻心情<i>情绪惯性</i></div><div class="v" id="v4"></div></div>
      <div class="node final" id="n5"><div class="k">此刻心情</div><div class="v" id="v5"></div><div class="landed" id="landed"></div></div>
    </div>
    <div class="said" id="said"></div>
  </section>

  <section id="colC">
    <h2>她在哪</h2>
    <div class="home" id="home"></div>
    <div class="doing">
      <div class="t" id="doingTitle"></div>
      <div class="s" id="doingSub"></div>
      <div class="s" id="doingNext"></div>
    </div>
    <section id="ledger">
      <h2>生活账本<small>点今天的一条，整屏回到那一刻</small></h2>
      <div class="now-btn" id="nowBtn">回到最新 ↑</div>
      <div class="why-banner" id="whyBanner"></div>
      <div class="rows-wrap">
        <div class="rows" id="rows"></div>
        <div class="scrub live" id="scrub"><div class="rail"></div><div class="knob" id="knob"></div><div class="at" id="scrubAt"></div></div>
      </div>
    </section>
  </section>

  <section id="today">
    <div class="head">
      <h2 id="todayTitle">她的今天</h2>
      <div class="tip" id="todayTip">拖动或点击时间轴，回到今天的任意时刻</div>
      <div class="spacer"></div>
      <div class="notes" id="notesLine"></div>
    </div>
    <div class="river" id="river"></div>
    <div class="tipbox" id="tipbox"></div>
  </section>
  <div class="error" id="err"></div>
</div>
<script>
(function () {
  "use strict";
  var FACES = __FACES__;
  var COLORS = {
    neutral: "#8fb8c8", happy: "#ffc857", excited: "#ff8a5c", shy: "#ff9ecf",
    sad: "#6aa6ff", worried: "#d9bf7f", pouty: "#ff6b6b", surprised: "#5fe3d0"
  };
  var SOURCES = {
    world_clock: "日常", weather: "天气", hunger_drive: "饥饿",
    drive_blend: "状态", dialogue: "对话", interaction: "互动",
    emotion: "情绪", note: "便签", wallet: "金币",
    reminder: "提醒", open_meteo: "天气"
  };
  // 早期账本把天气枚举原样写进了标题，展示时翻成中文。
  var WEATHER_WORDS = { clear: "晴", sunny: "晴", cloudy: "多云", overcast: "阴", rain: "下雨", snow: "下雪", fog: "有雾", heat: "炎热", cold: "寒冷", mild: "温和" };
  function plainTitle(text) {
    return String(text || "").replace(/[a-z_]+/g, function (w) { return WEATHER_WORDS[w] || w; })
      .replace(/^情绪：(.+)$/, "心情变得$1");
  }
  var GAUGES = ["mood", "energy", "satiety", "stress", "trust"];
  // 户型图（SVG 坐标，宽 560 高 236，上北下南）：一卧一书房，照概念户型图横向压扁。
  // 西侧一列从北到南是厨房、玄关、书房，入户门开在玄关西墙；玄关向东敞开接餐厅，
  // 卫生间在餐厅北面，卧室在东北角朝江；客厅接着餐厅往东，落地门出去是阳台，再往东是湘江。
  // pts 是地板多边形，lx/ly 是房名位置，cx/cy 是她站的位置，fit 是这间屋能写下几个字的活动。
  var ROOMS = [
    { label: "厨房", pts: [[8, 8], [140, 8], [140, 92], [8, 92]], lx: 22, ly: 34, cx: 116, cy: 34, fit: 4 },
    { label: "书房", pts: [[8, 142], [140, 142], [140, 228], [8, 228]], lx: 22, ly: 168, cx: 116, cy: 204, fit: 4 },
    { label: "卧室", pts: [[240, 8], [436, 8], [436, 110], [240, 110]], lx: 262, ly: 32, cx: 410, cy: 82, fit: 7 },
    { label: "客厅", pts: [[240, 110], [436, 110], [436, 228], [240, 228]], lx: 262, ly: 142, cx: 410, cy: 200, fit: 7 },
    { label: "阳台", pts: [[436, 110], [500, 110], [500, 228], [436, 228]], lx: 446, ly: 138, cx: 468, cy: 198, fit: 3 }
  ];
  var BATH = { label: "卫生间", pts: [[140, 8], [240, 8], [240, 80], [140, 80]], lx: 152, ly: 34 };
  // 餐厅连着玄关，一块地面
  var DINING = { label: "餐厅", pts: [[8, 92], [140, 92], [140, 80], [240, 80], [240, 228], [140, 228], [140, 142], [8, 142]], lx: 168, ly: 152 };
  // 出门时整块换成那个地点的近景图（咖啡店、便利店、商场、公园、江边、家楼下），x/y 是她站的位置。
  // 认不出的外出地点用“住处附近”那张图，标它自己的名字。
  var PLACES = [
    { label: "附近咖啡店", short: "咖啡店", scene: "cafe", x: 196, y: 146 },
    { label: "社区小店", short: "便利店", scene: "shop", x: 340, y: 88 },
    { label: "附近商场", short: "商场", scene: "mall", x: 150, y: 116 },
    { label: "附近公园", short: "公园", scene: "park", x: 292, y: 188 },
    { label: "河岸步道", short: "河岸步道", scene: "river", x: 296, y: 150 },
    { label: "住处附近", short: "住处附近", scene: "hood", x: 330, y: 142 }
  ];
  // 轮询 <页面路径>/data，即 /trace/data；挂在 nginx 前缀下也能对上。
  var DATA_URL = location.pathname.replace(/\/+$/, "") + "/data" + location.search;
  var POLL_MS = 2000;
  var STEP_MS = 900;
  var VIEW_IDLE_MS = 45000;
  var latest = null;
  var model = null;
  var playedAt = null;
  var timers = [];
  var shown = {};
  var seenKeys = null;
  var seenTicks = null;
  var lastOk = 0;
  var clockOffset = 0;
  var viewAt = null;
  var viewTouched = 0;

  function $(id) { return document.getElementById(id); }
  function esc(text) {
    return String(text == null ? "" : text).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }
  function clip(text, n) { text = String(text || ""); return text.length > n ? text.slice(0, n) + "…" : text; }
  var saidRoll = null;
  function setSaid(text) {
    var box = $("said");
    text = clip(text, 400);
    if (box.getAttribute("data-t") === text) return;
    box.setAttribute("data-t", text);
    if (saidRoll) { saidRoll.cancel(); saidRoll = null; }
    if (!text) { box.innerHTML = ""; return; }
    box.innerHTML = '<div class="win"><div class="roll"><em>她说</em>' + esc(text) + "</div></div>";
    var win = box.firstChild, roll = win.firstChild;
    var line = parseFloat(getComputedStyle(roll).lineHeight) || 36;
    win.style.height = Math.floor(line * 2) + "px";
    var dist = roll.scrollHeight - win.clientHeight;
    if (dist <= 2 || !roll.animate || matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    // 每行停留约 2.6 秒：开头先停 4 秒，走到底停 4 秒，再回到开头
    var move = Math.max(1, Math.ceil(dist / line)) * 2600, hold = 4000, total = hold * 2 + move + 600;
    saidRoll = roll.animate([
      { transform: "translateY(0)", offset: 0 },
      { transform: "translateY(0)", offset: hold / total },
      { transform: "translateY(" + -dist + "px)", offset: (hold + move) / total },
      { transform: "translateY(" + -dist + "px)", offset: (hold * 2 + move) / total },
      { transform: "translateY(0)", offset: 1 }
    ], { duration: total, iterations: Infinity, easing: "linear" });
  }
  function pad(n) { return String(n).padStart(2, "0"); }
  function hm(stamp) {
    if (!stamp) return "";
    var d = new Date(stamp * 1000);
    return pad(d.getHours()) + ":" + pad(d.getMinutes());
  }
  function span(sec) {
    sec = Math.max(0, Math.round(sec));
    if (sec < 60) return "不到 1 分钟";
    var m = Math.floor(sec / 60);
    if (m < 60) return m + " 分钟";
    var h = Math.floor(m / 60);
    return h + " 小时" + (m % 60 ? " " + (m % 60) + " 分" : "");
  }
  function ago(stamp, now) {
    if (!stamp) return "";
    var s = Math.max(0, now - stamp);
    if (s < 60) return "刚刚";
    if (s < 3600) return Math.floor(s / 60) + " 分钟前";
    if (s < 86400) return Math.floor(s / 3600) + " 小时前";
    return Math.floor(s / 86400) + " 天前";
  }
  function nowS() { return Date.now() / 1000 + clockOffset; }
  function fit() {
    var k = Math.min(window.innerWidth / 1920, window.innerHeight / 1080);
    $("stage").style.transform = "scale(" + k + ") translate(-50%, -50%)";
  }
  function stageScale() { return $("stage").getBoundingClientRect().width / 1920 || 1; }
  function faceKey(e) { return FACES[e] ? e : "neutral"; }
  function labelOf(data, e) { return (data.emotion_labels || {})[e] || e || "—"; }
  function keyOfLabel(data, label) {
    var labels = data.emotion_labels || {};
    for (var k in labels) if (labels[k] === label) return k;
    return null;
  }
  function chip(data, e) { return '<span class="chip">' + esc(labelOf(data, e)) + "</span>"; }
  function later(fn, ms) { timers.push(setTimeout(fn, ms)); }
  function stopAll() { timers.forEach(clearTimeout); timers = []; }

  // ── 从账本流水倒推"那一刻" ──
  // 账本每条都记了改动前后的值和地点切换，所以任意时刻的数值、房间、
  // 情绪都能从现在往回倒推出来，不需要也不允许编。
  function afterOf(r) { var d = r.delta || {}; return d.after && typeof d.after === "object" ? d.after : null; }
  function beforeOf(r) { var d = r.delta || {}; return d.before && typeof d.before === "object" ? d.before : null; }
  function buildModel(data, now) {
    var rows = (data.ledger || []).slice().sort(function (a, b) { return a.created_at - b.created_at; });
    var d = new Date(now * 1000);
    var mid = new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime() / 1000;
    var moves = rows.filter(function (r) { var a = afterOf(r); return a && a.location_label; });
    // 今天零点时她在哪：零点前最后一次落脚；没有就看今天第一次挪动之前在哪。
    var startLoc = null, startAct = "";
    moves.forEach(function (r) { if (r.created_at < mid) { startLoc = afterOf(r).location_label; startAct = afterOf(r).activity_label || ""; } });
    if (!startLoc) {
      var firstToday = moves.filter(function (r) { return r.created_at >= mid; })[0];
      var b = firstToday && beforeOf(firstToday);
      if (b && b.location_label) { startLoc = b.location_label; startAct = b.activity_label || ""; }
    }
    var stays = [];
    var cur = startLoc ? { loc: startLoc, act: startAct, from: mid, acts: [[mid, startAct]] } : null;
    moves.forEach(function (r) {
      if (r.created_at < mid) return;
      var a = afterOf(r);
      if (cur && cur.loc === a.location_label) { cur.acts.push([r.created_at, a.activity_label || cur.act]); return; }
      if (cur) { cur.to = r.created_at; stays.push(cur); }
      cur = { loc: a.location_label, act: a.activity_label || "", from: r.created_at, acts: [[r.created_at, a.activity_label || ""]] };
    });
    var place = data.place || {};
    if (place.location && (!cur || cur.loc !== place.location)) {
      var since = Math.max(mid, Math.min(now, place.since || now));
      if (cur) { cur.to = Math.max(cur.from, since); stays.push(cur); }
      cur = { loc: place.location, act: place.activity || "", from: since, acts: [[since, place.activity || ""]] };
    }
    if (cur) { cur.to = now; cur.live = true; stays.push(cur); }
    var weather = null;
    moves.forEach(function (r) { var w = afterOf(r).weather; if (w && w.condition) weather = w; });
    // 提醒：定下时记了到点时间；之后有"到点/没送到"或"全部取消"就算结束。
    var reminders = {}, order = [];
    rows.forEach(function (r) {
      if (r.source !== "reminder") return;
      var parts = String(r.event_key || "").split(":");
      var id = parts.slice(2).join(":");
      if (r.action === "set" && r.fire_at) { reminders[id] = { id: id, label: String(r.title || "").replace(/^定了提醒：/, ""), fire_at: r.fire_at, state: "pending" }; order.push(id); }
      else if ((r.action === "fire" || r.action === "drop") && reminders[id]) { reminders[id].state = r.action; reminders[id].done_at = r.created_at; }
      else if (r.action === "cancel") order.forEach(function (k) { if (reminders[k].state === "pending") reminders[k].state = "cancel"; });
    });
    return { rows: rows, mid: mid, stays: stays, weather: weather, reminders: order.map(function (k) { return reminders[k]; }) };
  }
  function valuesAt(data, t) {
    var tracked = (data.state || {}).tracked || {};
    var vals = {};
    GAUGES.forEach(function (f) { vals[f] = Number(tracked[f] || 0); });
    for (var i = model.rows.length - 1; i >= 0; i--) {
      var r = model.rows[i];
      if (r.created_at <= t) break;
      (r.change_items || []).forEach(function (c) { if (c.field in vals) vals[c.field] = Number(c.before); });
    }
    return vals;
  }
  function stayAt(t) {
    var hit = null;
    model.stays.forEach(function (s) { if (s.from <= t) hit = s; });
    return hit;
  }
  function activityAt(stay, t) {
    if (!stay) return "";
    var act = "";
    stay.acts.forEach(function (p) { if (p[0] <= t) act = p[1]; });
    return act || stay.act;
  }
  // 一句话的情绪只在说完后挂一会儿；过了就回到整体心情的脸，不让一句难过一直挂着。
  var EMO_HOLD_S = 900;
  function moodFace(v) { return v >= 75 ? "happy" : (v >= 45 ? "neutral" : (v >= 30 ? "worried" : "sad")); }
  function liveTurn(data) { return { emotion: data.emotion || {}, exchange: data.exchange || {}, live: true }; }
  // 回看某一刻：取那一刻之前最近的一轮对话（今天的），带着它当时的情绪和评估链。
  function turnAt(data, t) {
    var hit = null;
    (data.exchanges || []).forEach(function (x) { if (x.heard_at <= t + 5) hit = x; });
    return hit ? { emotion: hit.emotion || {}, exchange: hit, live: false } : null;
  }
  // 某一刻该挂哪张脸：刚说完的一轮用那一轮的情绪，否则看当时的心情值。
  function faceAt(data, t, turn) {
    var emo = (turn && turn.emotion) || {};
    if (emo.at && emo.at <= t + 5 && t - emo.at < EMO_HOLD_S) return { key: faceKey(emo.emotion), intensity: emo.intensity || 0, turn: true, at: emo.at };
    var mood = model ? valuesAt(data, t).mood : Number(((data.state || {}).tracked || {}).mood || 0);
    return { key: moodFace(mood), intensity: 0, turn: false, mood: mood };
  }
  function applyLiveFace(data, now, animate) {
    var f = faceAt(data, now, liveTurn(data));
    setFace(data, f.key, animate);
    setPips(f.intensity);
    return f;
  }

  // ── 主角 ──
  function setFace(data, emotion, animate) {
    var key = faceKey(emotion);
    document.documentElement.style.setProperty("--emo", COLORS[key]);
    if ($("faceImg").getAttribute("data-k") !== key) { $("faceImg").src = FACES[key]; $("faceImg").setAttribute("data-k", key); }
    $("emoLabel").textContent = labelOf(data, key);
    if (animate) {
      ["face", "emoLabel"].forEach(function (id) {
        var el = $(id); el.classList.remove("swap"); void el.offsetWidth; el.classList.add("swap");
      });
    }
  }
  function setPips(n) {
    var html = "";
    for (var i = 1; i <= 5; i++) html += '<span class="' + (i <= n ? "on" : "") + '"></span>';
    $("pips").innerHTML = html;
  }

  // ── 评估链 ──
  // reason 形如 "LLM 自评；体力 22 不到 30，兴奋收成高兴；难过还没过去，先不跳太高"
  function chainSteps(data, turn) {
    var emo = turn.emotion || {};
    var res = emo.last_resolution || {};
    var parts = String(res.reason || "").split("；");
    var modulate = "", inertia = "";
    parts.slice(1).forEach(function (p) { if (p.indexOf("还没过去") >= 0) inertia = p; else modulate = p; });
    var has = !!res.source;
    var heard = (turn.exchange || {}).heard || "";
    var none = turn.live ? "跟她说一句话，就能看到完整的思考过程" : "这一轮没有留下情绪评估记录";
    return [
      { typed: heard, skip: !heard },
      has ? (res.source === "llm"
        ? { k: "第一反应<i>模型自评</i>", v: "模型读出" + '<span class="arrow">→</span>' + chip(data, res.modulated_from) }
        : { k: "第一反应<i>规则判断</i>", v: esc(clip((parts[0] || "").replace("规则兜底：", ""), 10)) + '<span class="arrow">→</span>' + chip(data, res.modulated_from) })
        : { k: "第一反应<i>模型自评</i>", v: none, skip: true },
      modulate ? { v: esc(modulate) } : { v: has ? "身心状态平稳，保持第一反应" : "—", skip: true },
      inertia ? { v: esc(inertia) } : { v: has ? "没有上一刻的心事拖着" : "—", skip: true },
      { v: emo.emotion ? chip(data, emo.emotion) + '<span class="arrow">强度</span>' + (emo.intensity || 0) : "—", skip: !emo.emotion }
    ];
  }
  function lightTo(i) {
    // 光走到第 i 个节点（0..4）
    var node = $("n" + (i + 1));
    var top = node.offsetTop + 25;
    $("fill").style.height = Math.max(0, top - 26) + "px";
    var spark = $("spark");
    spark.style.top = (top - 11) + "px";
    spark.style.opacity = i < 4 ? "1" : "0";
    node.classList.add("lit");
  }
  function typeInto(el, text, done) {
    var i = 0;
    var speed = Math.max(28, Math.min(70, 1300 / Math.max(1, text.length)));
    (function tick() {
      el.innerHTML = '<span class="q">' + esc(text.slice(0, i)) + '<span class="caret"></span></span>';
      if (i++ < text.length) later(tick, speed);
      else { el.innerHTML = '<span class="q">' + esc(text) + "</span>"; if (done) done(); }
    })();
  }
  function fillLanded(data, turn) {
    var emo = turn.emotion || {};
    if (!emo.emotion) { $("landed").innerHTML = ""; return; }
    var fx = emo.effects || {};
    var parts = (fx.changes || []).map(function (c) {
      return "记入<b>" + esc(c.label) + " " + c.before + "→" + c.after + "</b>";
    });
    // voice_tone 形如"难过，语气低落，语速放慢"，开头的情绪词上面已经写过了。
    var label = labelOf(data, faceKey(emo.emotion));
    var tone = String(fx.voice_tone || "平常的语气").replace(new RegExp("^" + label + "[，,、]\\s*"), "");
    parts.push("说话<b>" + esc(clip(tone, 12)) + "</b>");
    parts.push("墨水屏<b>换上「" + esc(label) + "」表情</b>");
    $("landed").innerHTML = parts.map(function (p) { return "<span>" + p + "</span>"; }).join("");
  }
  var playing = false;
  var chainKey = null;
  function renderChain(data, animate, turn) {
    stopAll();
    turn = turn || liveTurn(data);
    chainKey = turn.live ? "live" : "at:" + (turn.exchange || {}).heard_at;
    playing = false;
    var steps = chainSteps(data, turn);
    var emo = turn.emotion || {};
    var res = emo.last_resolution || {};
    var said = (turn.exchange || {}).said;
    $("k2").innerHTML = steps[1].k;
    for (var i = 0; i < 5; i++) {
      var node = $("n" + (i + 1));
      node.classList.remove("lit");
      node.classList.toggle("skip", !!steps[i].skip);
      $("v" + (i + 1)).innerHTML = i === 0 ? "" : steps[i].v;
    }
    $("landed").classList.remove("show");
    fillLanded(data, turn);
    $("chainAt").textContent = turn.live ? "" : "回看 " + hm((turn.exchange || {}).heard_at) + " 这一轮";

    if (!animate) {
      $("v1").innerHTML = steps[0].typed ? '<span class="q">' + esc(clip(steps[0].typed, 18)) + "</span>" : "等待第一句对话…";
      for (var j = 0; j < 5; j++) $("n" + (j + 1)).classList.add("lit");
      lightTo(4);
      $("landed").classList.add("show");
      setSaid(said);
      if (viewAt === null) applyLiveFace(data, nowS(), false);
      return;
    }
    playing = true;

    // 演一遍：脸先停在上一份情绪，光走完五步再换脸。
    setFace(data, res.inertia_from || emo.emotion, false);
    setPips(0);
    setSaid("");
    $("fill").style.height = "0px";
    lightTo(0);
    typeInto($("v1"), clip(steps[0].typed || "……", 18), function () {
      var t = 350;
      [1, 2, 3].forEach(function (k) {
        later(function () { lightTo(k); }, t);
        t += steps[k].skip ? STEP_MS * 0.5 : STEP_MS;
      });
      later(function () {
        lightTo(4);
        playing = false;
        if (viewAt === null) {
          setFace(data, emo.emotion, true);
          setPips(emo.intensity || 0);
          popGauges(data);
        }
        later(function () { $("landed").classList.add("show"); }, 500);
        if (said) later(function () { setSaid(said); }, 900);
      }, t);
    });
  }

  // ── 数值 ──
  function buildGauges(data) {
    var labels = data.field_labels || {};
    $("gauges").innerHTML = GAUGES.map(function (f) {
      return '<div class="g" id="g-' + f + '"><div class="l">' + esc(labels[f] || f) +
        '</div><div class="num"><span class="val">-</span><span class="d"></span></div><div class="bar"><i></i></div></div>';
    }).join("");
  }
  function setGauge(f, v, from, delta) {
    var el = $("g-" + f);
    if (!el) return;
    var bad = f === "stress" ? v >= 60 : v < 35;
    var bar = el.querySelector(".bar i");
    bar.style.width = Math.max(0, Math.min(100, v)) + "%";
    bar.style.background = bad ? "var(--negative)" : "var(--muted)";
    var val = el.querySelector(".val");
    shown[f] = v;
    if (from === undefined || from === v) { val.textContent = v; return; }
    var diff = v - from;
    var shownDiff = delta === undefined ? diff : delta;
    var d = el.querySelector(".d");
    d.textContent = (shownDiff > 0 ? "+" : "") + shownDiff;
    d.classList.remove("show"); void d.offsetWidth; d.classList.add("show");
    var start = performance.now();
    (function step(now) {
      var p = Math.min(1, (now - start) / 1200);
      val.textContent = Math.round(from + diff * (1 - Math.pow(1 - p, 3)));
      if (p < 1) requestAnimationFrame(step);
    })(start);
  }
  function renderGauges(data) {
    if (!$("g-mood")) buildGauges(data);
    if (viewAt !== null) {
      var vals = valuesAt(data, viewAt);
      GAUGES.forEach(function (f) { setGauge(f, vals[f]); });
      return;
    }
    var tracked = (data.state || {}).tracked || {};
    GAUGES.forEach(function (f) { setGauge(f, Number(tracked[f] || 0), shown[f]); });
  }
  function popGauges(data) {
    // 演到"此刻心情"时，让这份情绪写进账本的那几项从之前的值重新跳一次。
    var tracked = (data.state || {}).tracked || {};
    (((data.emotion || {}).effects || {}).changes || []).forEach(function (c) {
      if (GAUGES.indexOf(c.field) >= 0) setGauge(c.field, Number(tracked[c.field] || c.after), c.before, c.after - c.before);
    });
  }

  // ── 她在哪：平面图 ──
  function roomOf(label) {
    for (var i = 0; i < ROOMS.length; i++) if (ROOMS[i].label === label) return ROOMS[i];
    return null;
  }
  function placeOf(label) {
    for (var i = 0; i < PLACES.length; i++) if (PLACES[i].label === label || PLACES[i].short === label) return PLACES[i];
    return null;
  }
  var HOOD = PLACES[PLACES.length - 1];
  // 户型图里格子小，时长写紧一点：10时55分 / 44分
  function spanShort(sec) {
    var m = Math.max(1, Math.round(sec / 60)), h = Math.floor(m / 60);
    return (h ? h + "时" : "") + (m % 60 || !h ? (m % 60) + "分" : "");
  }
  function roomSvg(id, r, cls) {
    return '<g class="room' + (cls ? " " + cls : "") + '" id="' + id + '"><path d="M' + r.pts.map(function (p) { return p.join(" "); }).join(" L") + ' Z"/>' +
      '<text class="rl" x="' + r.lx + '" y="' + r.ly + '">' + r.label + "</text>" +
      '<text class="ra" x="' + r.lx + '" y="' + (r.ly + 22) + '"></text>' +
      '<text class="rt" x="' + r.lx + '" y="' + (r.ly + 40) + '"></text></g>';
  }
  // 墙按中线画，留出门洞和窗洞；窗是墙洞里三道细线，门是一扇门板加开门弧线
  var PLAN_WALLS =
    '<path class="wall o" d="M8 8 H40 M100 8 H160 M210 8 H436 V24 M436 96 V120 M436 218 V228 H230 M180 228 H110 M40 228 H8 V134 M8 100 V8"/>' +
    '<path class="wall i" d="M8 92 H96 M124 92 H140 M140 8 V92 M140 80 H200 M226 80 H240 M240 8 V80 M240 106 V110 H436 M8 142 H112 M138 142 H140 V228"/>';
  function winH(x1, x2, y) { return '<path class="win-bg" d="M' + x1 + " " + y + " H" + x2 + '"/><path class="win" d="M' + x1 + " " + (y - 3) + " H" + x2 + " M" + x1 + " " + y + " H" + x2 + " M" + x1 + " " + (y + 3) + " H" + x2 + '"/>'; }
  function winV(x, y1, y2) { return '<path class="win-bg" d="M' + x + " " + y1 + " V" + y2 + '"/><path class="win" d="M' + (x - 3) + " " + y1 + " V" + y2 + " M" + x + " " + y1 + " V" + y2 + " M" + (x + 3) + " " + y1 + " V" + y2 + '"/>'; }
  var PLAN_OPENINGS =
    winH(40, 100, 8) + winH(160, 210, 8) + winH(40, 110, 228) + winH(180, 230, 228) + winV(436, 24, 96) +
    // 客厅到阳台：整面落地推拉门
    '<path class="win-bg" d="M436 120 V218"/><path class="win" d="M433 120 V172 M439 166 V218"/>' +
    '<path class="door" d="M8 100 L42 100 M8 134 A34 34 0 0 0 42 100"/>' +       // 入户门
    '<path class="door" d="M124 92 L124 64 M96 92 A28 28 0 0 1 124 64"/>' +      // 厨房
    '<path class="door" d="M226 80 L226 54 M200 80 A26 26 0 0 1 226 54"/>' +     // 卫生间
    '<path class="door" d="M240 80 L266 80 M240 106 A26 26 0 0 0 266 80"/>' +    // 卧室
    '<path class="door" d="M138 142 L138 168 M112 142 A26 26 0 0 0 138 168"/>' + // 书房
    // 阳台栏杆：外沿两道细线
    '<path class="rail" d="M436 110 H500 V228 H436 M440 114 H496 V224 H440"/>' +
    // 东边的湘江
    '<path class="m-river" d="M520 0 C512 60 528 140 516 200 C512 220 518 230 516 236 H560 V0 Z"/>' +
    '<path class="m-wave" d="M536 236 C540 176 528 120 538 60 C542 36 536 16 538 0"/><path class="m-wave w2" d="M552 236 C554 180 546 112 552 52 C554 32 550 14 552 0"/>';
  function road(d) { return '<path class="m-road" d="' + d + '"/><path class="m-roadc" d="' + d + '"/>'; }
  function blk(x, y, w, h) {
    return '<rect class="m-block" x="' + x + '" y="' + y + '" width="' + w + '" height="' + h + '" rx="6"/>' +
      '<rect class="m-bldg" x="' + (x + 8) + '" y="' + (y + 8) + '" width="' + (w - 16) + '" height="' + Math.round((h - 16) * 0.6) + '" rx="3"/>';
  }
  function trees(list) { return list.map(function (t) { return '<circle class="m-tree" cx="' + t[0] + '" cy="' + t[1] + '" r="' + (t[2] || 11) + '"/>'; }).join(""); }
  function river(x) {
    return '<path class="m-river" d="M' + (x + 16) + " 0 C" + x + " 70 " + (x + 40) + " 140 " + (x + 12) + " 200 C" + (x + 4) + " 220 " + (x + 10) + " 230 " + (x + 8) + ' 236 H560 V0 Z"/>' +
      '<path class="m-wave" d="M' + (x + 44) + " 236 C" + (x + 50) + " 176 " + (x + 34) + " 120 " + (x + 46) + " 60 C" + (x + 50) + " 36 " + (x + 44) + " 16 " + (x + 46) + ' 0"/>' +
      '<path class="m-wave w2" d="M' + (x + 76) + " 236 C" + (x + 78) + " 180 " + (x + 68) + " 112 " + (x + 76) + " 52 C" + (x + 78) + " 32 " + (x + 74) + " 14 " + (x + 76) + ' 0"/>';
  }
  function zebra(x1, x2, y1, y2) { var d = ""; for (var x = x1; x <= x2; x += 10) d += "M" + x + " " + y1 + " V" + y2 + " "; return '<path class="m-zebra" d="' + d + '"/>'; }
  function homeb(x, y) { return '<rect class="m-homeb" x="' + x + '" y="' + y + '" width="56" height="34" rx="4"/><text class="m-homet" x="' + (x + 28) + '" y="' + (y + 22) + '" text-anchor="middle">家</text>'; }
  // 每个外出地点的近景，画法和户型图一致：墙、玻璃、门弧，店里只画货架/吧台/桌这类认得出店的线
  function sceneSvg(key) {
    if (key === "cafe") return road("M0 196 H560") + road("M420 0 V236") + zebra(372, 404, 190, 202) +
      blk(0, 0, 124, 180) + blk(432, 0, 128, 180) +
      '<path class="m-spot" d="M140 20 H400 V176 H140 Z"/>' +
      '<path class="m-wall" d="M140 20 H400 V176 H330 M300 176 H290 M160 176 H140 V20"/>' + winH(160, 290, 176) +
      '<path class="door" d="M330 176 L330 146 M300 176 A30 30 0 0 1 330 146"/>' +
      '<path class="m-shelf" d="M262 44 H380 V100"/>' +
      [[176, 62], [220, 62], [176, 108], [220, 108], [262, 138]].map(function (c) { return '<circle class="m-table" cx="' + c[0] + '" cy="' + c[1] + '" r="9"/>'; }).join("");
    if (key === "shop") return road("M0 206 H560") + zebra(270, 330, 200, 212) +
      blk(20, 20, 136, 156) + blk(404, 20, 136, 156) +
      '<path class="m-spot" d="M170 40 H390 V170 H170 Z"/>' +
      '<path class="m-wall" d="M170 40 H390 V170 H320 M260 170 H170 V40"/>' +
      '<path class="win-bg" d="M260 170 H320"/><path class="win" d="M260 167 H294 M286 173 H320"/>' +
      '<path class="m-shelf" d="M196 70 H306 M196 100 H306 M196 130 H306 M374 58 V152 M326 142 H360 V122"/>';
    if (key === "mall") {
      var lot = "";
      for (var x = 382; x <= 532; x += 18) lot += "M" + x + " 40 V84 M" + x + " 140 V184 ";
      return road("M356 0 V236") +
        '<path class="m-spot" d="M40 20 H340 V200 H40 Z"/>' +
        '<path class="m-wall" d="M40 20 H340 V92 M340 132 V200 H210 M170 200 H40 V20"/>' +
        '<path class="m-shop" d="M40 70 H110 V20 M170 20 V58 M230 20 V58 M290 20 V70 H340 M40 150 H100 V200 M290 200 V150 H340"/>' +
        '<ellipse class="m-void" cx="190" cy="118" rx="64" ry="40"/>' +
        '<path class="m-shelf" d="M166 128 L206 106 M176 136 L216 114"/>' +
        '<rect class="m-lot" x="372" y="30" width="170" height="164" rx="4"/><path class="m-lot" d="' + lot + '"/>';
    }
    if (key === "park") return '<rect class="m-park" x="0" y="0" width="440" height="236"/>' + road("M452 0 V236") + river(470) +
      '<path class="m-water" d="M170 110 C170 70 240 60 280 80 C320 100 310 150 270 160 C230 172 170 150 170 110 Z"/>' +
      '<path class="m-path" d="M140 236 C150 210 180 194 220 196 C280 200 360 180 350 120 C340 60 270 30 200 50 C130 70 120 150 140 236 M350 120 H440 M0 182 C60 172 100 192 132 188 M0 44 C70 40 110 50 150 66"/>' +
      trees([[40, 96, 13], [76, 124], [30, 146], [92, 84, 9], [384, 40, 14], [414, 74], [376, 184], [412, 204, 13], [62, 18], [304, 218, 9], [254, 22], [96, 222, 13], [418, 150, 9], [22, 212]]);
    if (key === "river") return road("M110 0 V236") + blk(0, 0, 98, 112) + blk(0, 124, 98, 112) +
      '<rect class="m-park" x="122" y="0" width="190" height="236"/>' + homeb(150, 26) + river(318) +
      '<path class="m-path" d="M308 0 C282 70 330 140 300 200 C292 222 296 230 295 236"/><path class="m-walk" d="M308 0 C282 70 330 140 300 200 C292 222 296 230 295 236"/>' +
      trees([[262, 30], [252, 92, 13], [268, 150], [250, 208, 13], [218, 120, 9], [226, 186, 9]]);
    return road("M0 120 H440") + road("M300 0 V236") + zebra(260, 290, 114, 126) + river(452) +
      blk(0, 0, 140, 108) + blk(152, 0, 136, 108) + blk(0, 132, 140, 104) + blk(152, 132, 136, 104) + blk(312, 132, 128, 104) +
      '<rect class="m-block" x="312" y="0" width="128" height="108" rx="6"/>' + homeb(340, 40);
  }
  function buildHome() {
    var h = '<svg viewBox="0 0 560 236">';
    // 家
    h += '<g class="layer" id="plan">';
    h += roomSvg("dining", DINING, "floor") + roomSvg("bath", BATH, "deco");
    ROOMS.forEach(function (r, i) { h += roomSvg("room" + i, r); });
    h += PLAN_WALLS + PLAN_OPENINGS + "</g>";
    // 外面：近景图在 renderHome 里按地点换
    h += '<g class="layer off" id="map"><g id="scene"></g>' +
      '<g class="m-pill" id="mPill"><rect rx="13" height="26"/><text></text></g>' +
      '<text class="m-t" id="mapT" x="10" y="226"></text></g>';
    h += '<g id="me"><circle class="ping" cx="0" cy="0" r="9"/><circle class="dot" cx="0" cy="0" r="9"/></g></svg>';
    $("home").innerHTML = h;
  }
  function renderHome(t) {
    if (!$("me")) buildHome();
    var stay = stayAt(t);
    var loc = stay ? stay.loc : "";
    var act = activityAt(stay, t);
    var totals = {}, outTotal = 0;
    model.stays.forEach(function (s) {
      var end = Math.min(s.to, t);
      if (end <= s.from) return;
      if (roomOf(s.loc)) totals[s.loc] = (totals[s.loc] || 0) + (end - s.from);
      else outTotal += end - s.from;
    });
    var hereRoom = roomOf(loc);
    var outside = !!loc && !hereRoom;
    $("plan").setAttribute("class", "layer" + (outside ? " off" : ""));
    $("map").setAttribute("class", "layer" + (outside ? "" : " off"));
    ROOMS.forEach(function (r, i) {
      var g = $("room" + i);
      var here = !!loc && r === hereRoom;
      g.setAttribute("class", "room" + (here ? " here" : (totals[r.label] ? " was" : "")));
      g.querySelector(".ra").textContent = here ? clip(act, r.fit) : "";
      g.querySelector(".rt").textContent = totals[r.label] ? spanShort(totals[r.label]) : "";
    });
    var x = 0, y = 0;
    if (hereRoom) { x = hereRoom.cx; y = hereRoom.cy; }
    if (outside) {
      var p = placeOf(loc) || HOOD;
      x = p.x; y = p.y;
      if ($("scene").getAttribute("data-k") !== p.scene) {
        $("scene").setAttribute("data-k", p.scene);
        $("scene").innerHTML = sceneSvg(p.scene);
      }
      var name = placeOf(loc) ? p.short : clip(loc, 8);
      var pill = $("mPill"), w = name.length * 16 + 24;
      var px = Math.max(4, Math.min(556 - w, x - w / 2)), py = y > 120 ? y - 50 : y + 22;
      pill.querySelector("rect").setAttribute("x", px); pill.querySelector("rect").setAttribute("y", py);
      pill.querySelector("rect").setAttribute("width", w);
      var tx = pill.querySelector("text");
      tx.setAttribute("x", px + 12); tx.setAttribute("y", py + 19); tx.textContent = name;
      $("mapT").textContent = "今天在外 " + spanShort(outTotal);
    }
    $("me").style.transform = "translate(" + x + "px," + y + "px)";
    $("me").style.display = loc ? "" : "none";
    return { stay: stay, loc: loc, act: act };
  }
  function renderDoing(data, t, where) {
    var plan = data.plan || [];
    if (viewAt !== null) {
      $("doingTitle").innerHTML = where.loc ? esc(where.loc) + (where.act ? " · " + esc(where.act) : "") + "<small>" + hm(t) + " 那会儿</small>" : "那会儿还没有位置记录";
      $("doingSub").innerHTML = where.stay ? "<b>在这里</b>" + esc(hm(where.stay.from)) + " 到 " + (where.stay.live ? "现在" : esc(hm(where.stay.to))) + "，共 " + esc(span(where.stay.to - where.stay.from)) : "";
      $("doingNext").innerHTML = "";
      return;
    }
    var cur = null, next = null;
    plan.forEach(function (p) { if (p.current) cur = p; if (!next && p.scheduled_at > t && !p.current) next = p; });
    var place = data.place || {};
    $("doingTitle").innerHTML = place.location ? esc(place.location) + (place.activity ? " · " + esc(place.activity) : "") +
      (where.stay ? "<small>已经 " + esc(span(t - where.stay.from)) + "</small>" : "") : "还不知道她在哪";
    $("doingSub").innerHTML = cur && cur.why_text ? "<b>为什么</b>" + esc(String(cur.why_text).replace("每日固定锚点", "日常习惯")) : "";
    $("doingNext").innerHTML = next ? "<b>接下来</b>" + esc(hm(next.scheduled_at)) + " " + esc(next.title) + (next.location ? " @" + esc(next.location) : "") + " · " + esc(span(next.scheduled_at - t)) + "后"
      : (plan.length ? "" : "<b>接下来</b>今天还没排日程");
  }

  // ── 她的今天：时间轴 ──
  var RX = 64, RW = 1824 - 64 - 6;
  var LANE = { loc: [6, 44], mood: [62, 122], plan: 160 };
  // 弹性时间轴：按 5 分钟一格分配宽度。有事发生的格子撑开（越密越宽），
  // 一整段没动静（比如睡觉）压成窄条并写上“睡着 0:00–10:40”，不再占掉半条时间轴。
  var BIN = 300, BINS = 288;
  var axis = null;
  function buildAxis(data, now) {
    var counts = [], plans = [];
    for (var i = 0; i < BINS; i++) { counts.push(0); plans.push(0); }
    function hit(t, arr) {
      var k = Math.floor((t - model.mid) / BIN);
      if (k >= 0 && k < BINS) arr[k]++;
    }
    model.rows.forEach(function (r) { hit(r.created_at, counts); });
    (data.exchanges || []).forEach(function (x) { hit(Number(x.heard_at), counts); });
    model.stays.forEach(function (st) { if (st.from > model.mid) hit(st.from, counts); });
    model.reminders.forEach(function (m) { hit(m.fire_at, plans); });
    (data.plan || []).forEach(function (pl) { hit(pl.scheduled_at, plans); });
    hit(now, counts);
    var w = [], nowBin = Math.floor((now - model.mid) / BIN);
    for (var j = 0; j < BINS; j++) {
      var c = counts[j];
      var base = j > nowBin ? (plans[j] ? 1.4 : 0.14) : 0.06;
      w.push(c ? 1 + Math.min(6, (c - 1) * 0.8) : base);
    }
    // 有事的格子两边留点呼吸，过渡不至于一下子断崖
    var soft = w.slice();
    for (var m = 0; m < BINS; m++) {
      if (counts[m]) {
        if (m > 0) soft[m - 1] = Math.max(soft[m - 1], 0.45);
        if (m + 1 < BINS) soft[m + 1] = Math.max(soft[m + 1], 0.45);
      }
    }
    var cum = [0];
    soft.forEach(function (v) { cum.push(cum[cum.length - 1] + v); });
    // 过去里连续 2 小时以上没动静的段落，单独标出来
    var quiet = [], start = -1;
    for (var q = 0; q <= BINS; q++) {
      var still = q < BINS && q < nowBin && !counts[q];
      if (still && start < 0) start = q;
      if (!still && start >= 0) {
        if (q - start >= 24) quiet.push([model.mid + start * BIN, model.mid + q * BIN]);
        start = -1;
      }
    }
    axis = { w: soft, cum: cum, scale: RW / cum[BINS], quiet: quiet };
  }
  function xOf(t) {
    var f = Math.max(0, Math.min(BINS, (t - model.mid) / BIN));
    if (!axis) return RX + f / BINS * RW;
    var i = Math.min(BINS - 1, Math.floor(f));
    return RX + (axis.cum[i] + (f - i) * axis.w[i]) * axis.scale;
  }
  function tOf(px) {
    var u = Math.max(0, Math.min(RW, px - RX));
    if (!axis) return model.mid + u / RW * 86400;
    u /= axis.scale;
    var lo = 0, hi = BINS - 1;
    while (lo < hi) { var mi = (lo + hi + 1) >> 1; if (axis.cum[mi] <= u) lo = mi; else hi = mi - 1; }
    return model.mid + (lo + Math.min(1, (u - axis.cum[lo]) / axis.w[lo])) * BIN;
  }
  function quietLabel(a, b) {
    var stay = stayAt((a + b) / 2);
    var where = stay ? stay.loc + (activityAt(stay, (a + b) / 2) || "") : "";
    var night = new Date(a * 1000).getHours() < 6;
    return (night || /睡|卧室/.test(where) ? "睡着 " : "安静 ") + hm(a) + "–" + hm(b);
  }
  // 心情线按今天的最低/最高值撑满泳道，起伏才看得出来；至少留 12 分的跨度，免得一两分的抖动被放大成大起大落。
  var moodScale = [0, 100];
  function moodY(v) {
    var lo = moodScale[0], hi = moodScale[1];
    return LANE.mood[1] - Math.max(0, Math.min(1, (v - lo) / (hi - lo))) * (LANE.mood[1] - LANE.mood[0]);
  }
  function fitMood(values) {
    var lo = Math.min.apply(null, values), hi = Math.max.apply(null, values);
    var pad = Math.max(2, (12 - (hi - lo)) / 2);
    lo -= pad; hi += pad;
    if (lo < 0) { hi -= lo; lo = 0; }
    if (hi > 100) { lo = Math.max(0, lo - (hi - 100)); hi = 100; }
    moodScale = [lo, hi];
  }
  function segLabel(x1, w, text, cls) {
    var n = Math.floor((w - 16) / 15);
    return n >= 2 ? '<text class="segl' + cls + '" x="' + (x1 + 10) + '" y="31">' + esc(clip(text, n)) + "</text>" : "";
  }
  function renderRiver(data, now) {
    buildAxis(data, now);
    var html = '<svg viewBox="0 0 1824 214">';
    // 轴是弹性的：整点刻度离上一个够远才画，压缩段里不挤一排字
    var lastGrid = -999, lastHour = -999;
    for (var h = 0; h <= 24; h++) {
      var gx = xOf(model.mid + h * 3600);
      if (gx - lastGrid >= 36) { html += '<line class="grid" x1="' + gx + '" x2="' + gx + '" y1="0" y2="186"/>'; lastGrid = gx; }
      if (gx - lastHour >= 64 && (h === 24 || RX + RW - gx >= 40)) {
        html += '<text class="hour" x="' + gx + '" y="206" text-anchor="' + (h === 0 ? "start" : (h === 24 ? "end" : "middle")) + '" data-h="' + h + '">' + h + ":00</text>";
        lastHour = gx;
      }
    }
    axis.quiet.forEach(function (qr) {
      var qx1 = xOf(qr[0]), qx2 = xOf(qr[1]);
      html += '<rect class="quiet" x="' + qx1 + '" y="' + (LANE.mood[1] + 4) + '" width="' + Math.max(0, qx2 - qx1 - 2) + '" height="18" rx="9"/>';
      var label = quietLabel(qr[0], qr[1]);
      if (qx2 - qx1 >= label.length * 9 + 16) html += '<text class="quietl" x="' + ((qx1 + qx2) / 2) + '" y="' + (LANE.mood[1] + 18) + '" text-anchor="middle">' + esc(label) + "</text>";
    });
    html += '<text class="lane" x="0" y="31">位置</text><text class="lane" x="0" y="98">心情</text><text class="lane" x="0" y="' + (LANE.plan + 5) + '">日程</text>';
    // 位置泳道：走过的是实心，计划里还没去的是虚线
    model.stays.forEach(function (s) {
      var x1 = xOf(s.from), w = Math.max(2, xOf(s.to) - x1 - 2);
      html += '<rect class="seg' + (s.live ? " cur" : "") + '" x="' + x1 + '" y="' + LANE.loc[0] + '" width="' + w + '" height="' + (LANE.loc[1] - LANE.loc[0]) + '" rx="4"/>';
      html += segLabel(x1, w, s.loc, "");
    });
    var plan = (data.plan || []).filter(function (p) { return p.scheduled_at > now; });
    plan.forEach(function (p, i) {
      var nx = i + 1 < plan.length ? plan[i + 1].scheduled_at : model.mid + 86400;
      var x1 = xOf(p.scheduled_at) + 2, w = Math.max(2, xOf(nx) - x1 - 4);
      html += '<rect class="seg plan" x="' + x1 + '" y="' + (LANE.loc[0] + 1) + '" width="' + w + '" height="' + (LANE.loc[1] - LANE.loc[0] - 2) + '" rx="4"/>';
      if (p.location) html += segLabel(x1, w, p.location, " plan");
    });
    // 心情泳道：从零点的值一路画到现在；情绪落点标在线上
    var v = valuesAt(data, model.mid).mood;
    var pts = [[xOf(model.mid), v]];
    model.rows.forEach(function (r) {
      if (r.created_at < model.mid) return;
      (r.change_items || []).forEach(function (c) {
        if (c.field !== "mood") return;
        var x = xOf(r.created_at);
        pts.push([x, v]); v = Number(c.after); pts.push([x, v]);
      });
    });
    pts.push([xOf(now), v]);
    fitMood(pts.map(function (p) { return p[1]; }));
    moodPts = pts.map(function (p) { return [p[0], moodY(p[1])]; });
    html += '<path class="mood" d="M' + pts.map(function (p) { return p[0].toFixed(1) + " " + moodY(p[1]).toFixed(1); }).join(" L") + '"/>';
    html += '<text class="moodv" x="' + (RX - 6) + '" y="' + (LANE.mood[0] + 5) + '" text-anchor="end">' + Math.round(moodScale[1]) + '</text>' +
      '<text class="moodv" x="' + (RX - 6) + '" y="' + (LANE.mood[1] + 2) + '" text-anchor="end">' + Math.round(moodScale[0]) + "</text>";
    html += '<text class="moodv" x="' + (xOf(now) + 8) + '" y="' + (moodY(v) + 5) + '">心情 ' + v + "</text>";
    var nextTicks = {};
    var emoLabelX = -999;
    model.rows.forEach(function (r) {
      if (r.created_at < model.mid) return;
      var key = r.event_key || (r.created_at + r.title);
      nextTicks[key] = true;
      var x = xOf(r.created_at);
      html += '<line class="tick' + (seenTicks && !seenTicks[key] ? " fresh" : "") + '" x1="' + x + '" x2="' + x + '" y1="' + (LANE.mood[1] + 8) + '" y2="' + (LANE.mood[1] + 18) + '"/>';
      if (r.source === "emotion") {
        var k = keyOfLabel(data, String(r.title || "").replace(/^情绪：/, "")) || "neutral";
        var ey = moodY(valuesAt(data, r.created_at).mood);
        html += '<circle class="emo-dot" cx="' + x + '" cy="' + ey + '" r="8" fill="' + COLORS[k] + '"/>';
        if (x - emoLabelX > 40) {
          html += '<text class="emo-l" x="' + x + '" y="' + (ey + 28) + '" text-anchor="middle" fill="' + COLORS[k] + '">' + esc(labelOf(data, k)) + "</text>";
          emoLabelX = x;
        }
      }
    });
    seenTicks = nextTicks;
    (data.notes || []).forEach(function (n) {
      if (n.at >= model.mid) html += '<rect class="note-pin" x="' + (xOf(n.at) - 4) + '" y="' + (LANE.mood[1] + 6) + '" width="8" height="12" rx="1"/>';
    });
    // 提醒：菱形落在到点时刻，还没响的空心并写上内容，响过的实心
    var remLabelEnd = -999;
    model.reminders.forEach(function (m) {
      if (m.fire_at < model.mid || m.fire_at > model.mid + 86400 || m.state === "cancel") return;
      var x = xOf(m.fire_at), y = LANE.mood[1] + 13;
      html += '<path class="rem ' + m.state + '" d="M' + x + " " + (y - 7) + " L" + (x + 7) + " " + y + " L" + x + " " + (y + 7) + " L" + (x - 7) + " " + y + ' Z"/>';
      if (m.state === "pending" && x - 10 > remLabelEnd) {
        var text = "提醒 " + hm(m.fire_at) + " " + clip(m.label, 6);
        html += '<text class="reml" x="' + (x + 12) + '" y="' + (y + 5) + '">' + esc(text) + "</text>";
        remLabelEnd = x + 12 + text.length * 13;
      }
    });
    // 日程泳道：标签放不下就省略，现在这一项和下一项一定写
    // 每个标签只占到下一颗珠子之前，前一个标签没写完的地方不再叠字。
    var lastEnd = -999, all = data.plan || [];
    all.forEach(function (p, i) {
      var x = xOf(p.scheduled_at);
      var cls = p.current ? "cur" : (p.scheduled_at < now ? "done" : "");
      html += '<circle class="bead ' + cls + '" cx="' + x + '" cy="' + LANE.plan + '" r="' + (p.current ? 8 : 6) + '"/>';
      var room = (i + 1 < all.length ? xOf(all[i + 1].scheduled_at) : RX + RW + 40) - x - 26;
      var n = Math.min(String(p.title).length, Math.floor(room / 14));
      if (x + 12 < lastEnd || n < 2) return;
      var text = n < String(p.title).length ? clip(p.title, n - 1) : p.title;
      html += '<text class="beadl ' + cls + '" x="' + (x + 12) + '" y="' + (LANE.plan + 5) + '">' + esc(text) + "</text>";
      lastEnd = x + 12 + text.length * 14 + 8;
    });
    // 还没到的时间压暗一层
    var nx0 = xOf(now);
    html += '<rect class="future" x="' + nx0 + '" y="' + (LANE.mood[0] - 6) + '" width="' + Math.max(0, RX + RW - nx0) + '" height="' + (LANE.mood[1] - LANE.mood[0] + 26) + '"/>';
    html += '<g id="nowG"><line class="now-line" x1="0" x2="0" y1="0" y2="186"/><rect class="now-tab" x="-30" y="190" width="60" height="22" rx="11"/>' +
      '<text class="now-txt" x="0" y="206" text-anchor="middle" id="nowTxt"></text></g>';
    if (viewAt !== null) {
      var vx = xOf(viewAt);
      html += '<g><line class="view-line" x1="' + vx + '" x2="' + vx + '" y1="0" y2="186"/><rect class="view-tab" x="' + (vx - 30) + '" y="190" width="60" height="22" rx="11"/>' +
        '<text class="now-txt" x="' + vx + '" y="206" text-anchor="middle">' + hm(viewAt) + "</text></g>";
    }
    html += '<line class="hover-line" id="hoverLine" x1="-10" x2="-10" y1="0" y2="186"/></svg>';
    $("river").innerHTML = html;
    ecgLayer = null;
    tickRiver();
  }
  // 心电图：光点从零点往"现在"扫，扫过的地方才画出实线，前面只留一道淡淡的底稿。
  // 线上每隔一段跳一次心，心跳间隔跟着压力走，波峰高低跟着心情走；
  // 离光点越远的心跳越平，远处落回干净的心情线。扫到头停一会儿再重画。
  var moodPts = [], ecgLayer = null, ecgStart = performance.now();
  var ECG_SPEED = 260, ECG_HOLD = 1600, ECG_DECAY = 360;
  function moodYAt(x) {
    var p = moodPts;
    if (!p.length) return null;
    if (x <= p[0][0]) return p[0][1];
    for (var i = 1; i < p.length; i++) {
      if (x <= p[i][0]) {
        var a = p[i - 1], b = p[i];
        return b[0] - a[0] < 0.01 ? b[1] : a[1] + (b[1] - a[1]) * (x - a[0]) / (b[0] - a[0]);
      }
    }
    return p[p.length - 1][1];
  }
  function ecgWave(u) {
    // u 为这一拍开始后的距离（像素），从左到右：Q 小下探、R 尖峰、S 下探、平缓的 T 波
    if (u < 0) return 0;
    if (u < 4) return -u / 4 * 3;
    if (u < 10) return -3 + (u - 4) / 6 * 33;
    if (u < 16) return 30 - (u - 10) / 6 * 42;
    if (u < 22) return -12 + (u - 16) / 6 * 12;
    if (u < 50) return Math.sin((u - 22) / 28 * Math.PI) * 6;
    return 0;
  }
  function ecgFrame(ts) {
    requestAnimationFrame(ecgFrame);
    var svg = $("river").querySelector("svg");
    if (!svg || moodPts.length < 2 || !model) return;
    if (!ecgLayer || !ecgLayer.isConnected) {
      ecgLayer = document.createElementNS(svg.namespaceURI, "g");
      ecgLayer.innerHTML = '<path class="ecg"/><circle class="ecg-head" r="4"/>';
      svg.insertBefore(ecgLayer, $("nowG"));
      var base = svg.querySelector(".mood");
      if (base) base.classList.add("ghost");
    }
    var x0 = moodPts[0][0], x1 = xOf(viewAt !== null ? viewAt : nowS());
    var vals = (latest && latest.state && latest.state.tracked) || {};
    var stress = Math.max(0, Math.min(100, Number(vals.stress || 0)));
    var mood = Math.max(0, Math.min(100, Number(vals.mood || 60)));
    var gap = 150 - stress * 0.7;                       // 心跳间隔（像素），压力大跳得密
    var amp = 0.55 + mood / 100 * 0.45;
    var span = Math.max(1, x1 - x0), cycle = span / ECG_SPEED * 1000 + ECG_HOLD;
    var head = x0 + Math.min(span, ((ts - ecgStart) % cycle) / 1000 * ECG_SPEED);
    function yAt(x) {
      var fade = Math.max(0, 1 - (head - x) / ECG_DECAY);
      return moodYAt(x) - (fade ? ecgWave((x - x0) % gap) * amp * fade : 0);
    }
    var d = "";
    for (var x = x0; x < head; x += 2) d += (d ? " L" : "M") + x.toFixed(1) + " " + yAt(x).toFixed(1);
    d += (d ? " L" : "M") + head.toFixed(1) + " " + yAt(head).toFixed(1);
    ecgLayer.childNodes[0].setAttribute("d", d);
    var c = ecgLayer.childNodes[1];
    c.setAttribute("cx", head.toFixed(1)); c.setAttribute("cy", yAt(head).toFixed(1));
  }
  if (!matchMedia("(prefers-reduced-motion: reduce)").matches) requestAnimationFrame(ecgFrame);
  function tickRiver() {
    var g = $("nowG");
    if (!g || !model) return;
    var now = nowS();
    var x = xOf(now);
    g.setAttribute("transform", "translate(" + x + ",0)");
    $("nowTxt").textContent = hm(now);
    // 别让整点刻度跟"现在"/回看的标签叠在一起
    var vx = viewAt !== null ? xOf(viewAt) : -999;
    var hours = $("river").querySelectorAll(".hour");
    for (var i = 0; i < hours.length; i++) {
      var hx = xOf(model.mid + Number(hours[i].getAttribute("data-h")) * 3600);
      hours[i].style.opacity = Math.abs(hx - x) < 56 || Math.abs(hx - vx) < 56 ? 0 : 1;
    }
  }
  function nearestEvent(t) {
    var best = null, bestD = 14, tx = xOf(t);
    model.rows.forEach(function (r) {
      if (r.created_at < model.mid) return;
      var d = Math.abs(xOf(r.created_at) - tx);
      if (d < bestD) { bestD = d; best = r; }
    });
    return best;
  }
  function wireRiver() {
    var river = $("river");
    var down = false;
    function local(e) {
      var r = river.getBoundingClientRect();
      return (e.clientX - r.left) / r.width * 1824;
    }
    function hover(e) {
      if (!model || !latest) return;
      var t = Math.min(tOf(local(e)), nowS());
      var hl = $("hoverLine");
      if (hl) { hl.setAttribute("x1", xOf(t)); hl.setAttribute("x2", xOf(t)); }
      var r = nearestEvent(t);
      var stay = stayAt(t);
      var tip = $("tipbox");
      var html = "<b>" + hm(r ? r.created_at : t) + "</b> ";
      if (r) html += esc(clip(plainTitle(r.title), 14)) + ((r.change_items || []).length ? " <span>" + esc(r.change_items.slice(0, 2).map(function (c) { return c.label + " " + c.before + "→" + c.after; }).join(" · ")) + "</span>" : "");
      else html += stay ? "在" + esc(stay.loc) + (activityAt(stay, t) ? " <span>" + esc(activityAt(stay, t)) + "</span>" : "") : "<span>这时候还没有记录</span>";
      tip.innerHTML = html;
      tip.style.left = Math.max(0, Math.min(1824 - tip.offsetWidth, xOf(t) - tip.offsetWidth / 2)) + "px";
      tip.classList.add("show");
      if (down) setView(r && Math.abs(r.created_at - t) < 600 ? r.created_at : t);
    }
    river.addEventListener("pointermove", hover);
    river.addEventListener("pointerdown", function (e) { down = true; river.setPointerCapture(e.pointerId); hover(e); });
    river.addEventListener("pointerup", function () { down = false; });
    river.addEventListener("pointercancel", function () { down = false; });
    river.addEventListener("pointerleave", function () {
      $("tipbox").classList.remove("show");
      var hl = $("hoverLine"); if (hl) { hl.setAttribute("x1", -10); hl.setAttribute("x2", -10); }
    });
  }

  // ── 回到某一刻 ──
  function setView(t) {
    viewTouched = Date.now();
    var live = t === null || t >= nowS() - 60;
    var was = viewAt;
    viewAt = live ? null : t;
    $("stage").classList.toggle("viewing", viewAt !== null);
    if (!latest) return;
    render(latest, true);
    if (viewAt === null && was !== null) renderChain(latest, false);
    if (viewAt === null) applyLiveFace(latest, nowS(), false);
  }
  function renderMoment(data, now) {
    var t = viewAt !== null ? viewAt : now;
    var where = renderHome(t);
    renderDoing(data, t, where);
    if (viewAt !== null) {
      var turn = turnAt(data, viewAt);
      var f = faceAt(data, viewAt, turn);
      setFace(data, f.key, false);
      setPips(f.intensity);
      $("emoAt").textContent = hm(viewAt) + " 那会儿 · " + (f.turn ? "刚说完那句，" + labelOf(data, f.key) : "整体心情 " + f.mood + "，" + labelOf(data, f.key));
      var key = turn ? "at:" + turn.exchange.heard_at : "none";
      if (key !== chainKey) {
        if (turn) renderChain(data, false, turn);
        else { renderChain(data, false, { emotion: {}, exchange: {}, live: false }); chainKey = "none"; $("chainAt").textContent = "这一刻之前今天还没有对话"; }
      }
      $("todayTitle").textContent = "她的今天 · 回看 " + hm(viewAt);
      $("mindHint").textContent = "点账本里的一条，看那一轮她怎么想的 · 点这一栏回到实时";
      $("todayTip").textContent = "45 秒不动会自动回到实时 · Esc 立刻回来";
      $("backPill").textContent = "回看 " + hm(viewAt) + " · 回到实时";
      markRow(viewAt);
    } else {
      $("todayTitle").textContent = "她的今天";
      $("mindHint").textContent = "按空格或点这一栏 · 重放思考过程";
      $("todayTip").textContent = "拖动或点击时间轴，回到今天的任意时刻";
      markRow(null);
    }
  }
  function markRow(t) {
    var box = $("rows");
    var rows = box.querySelectorAll(".row");
    var hit = null;
    for (var i = 0; i < rows.length; i++) {
      rows[i].classList.remove("at");
      if (t !== null && !hit && Number(rows[i].getAttribute("data-at")) <= t + 1) hit = rows[i];
    }
    if (hit) {
      hit.classList.add("at");
      var want = Math.max(0, hit.offsetTop - box.offsetTop - 40);
      if (Math.abs(box.scrollTop - want) > 4) box.scrollTop = want;
    }
  }

  // ── 账本 ──
  function renderLedger(data, now) {
    var why = data.why || {};
    var keys = why.event_keys || [];
    var whyFresh = why.at && now - why.at < 600 && keys.length;
    var cited = {};
    if (whyFresh) keys.forEach(function (k) { cited[k] = true; });
    var banner = $("whyBanner");
    var bannerHtml = whyFresh ? "你问<b>「" + esc(clip(why.question, 12)) + "」</b> · 她正根据下面 <b>" + keys.length + "</b> 条真实记录回答" : "";
    if (banner.dataset.html !== bannerHtml) {
      banner.dataset.html = bannerHtml;
      banner.innerHTML = bannerHtml;
      banner.className = whyFresh ? "why-banner show" : "why-banner";
    }
    var first = seenKeys === null;
    var nextSeen = {};
    // 列表只列今天；更早的流水留在 model 里，只用来倒推零点时的状态。
    var today = (data.ledger || []).filter(function (r) { return r.created_at >= model.mid; });
    var html = today.map(function (r) {
      var key = r.event_key || (r.created_at + r.title);
      nextSeen[key] = true;
      var src = SOURCES[r.source] || r.source || "记录";
      var cls = ["row"];
      if (r.source === "drive_blend" && !cited[r.event_key]) cls.push("minor");
      if (!first && !seenKeys[key]) cls.push("fresh");
      if (cited[r.event_key]) cls.push("cited");
      var chips = (r.change_items || []).map(function (c) {
        return "<span>" + esc(c.label) + " " + c.before + "→" + c.after + "</span>";
      }).join("");
      return '<div class="' + cls.join(" ") + '" data-at="' + (Number(r.created_at) || 0) + '"><div class="meta">' + esc(hm(r.created_at)) +
        "<b>" + esc(src) + '</b></div><div class="ti">' + esc(clip(plainTitle(r.title), 13)) + "<u>" + esc(src) + "</u></div>" +
        (chips ? '<div class="ch">' + chips + "</div>" : "") + "</div>";
    }).join("") || '<div class="empty">今天还没有记录</div>';
    seenKeys = nextSeen;
    if (html === ledgerHtml) return;
    ledgerHtml = html;
    var box = $("rows");
    if (viewAt === null && (box.scrollTop <= FOLLOW_PX || scrubDragging)) {
      box.innerHTML = html;
      if (!scrubDragging) box.scrollTop = 0;
    } else {
      // 正在回看更早的记录：新记录插在顶上，但视野要钉在原来那一行上，不能跳。
      var anchor = topRow(box);
      var at = anchor ? anchor.getAttribute("data-at") : null;
      var offset = anchor ? anchor.offsetTop - box.scrollTop : 0;
      box.innerHTML = html;
      var again = at ? box.querySelector('.row[data-at="' + at + '"]') : null;
      if (again) box.scrollTop = again.offsetTop - offset;
    }
    syncScrub();
  }

  // ── 账本时间轴：滚轮 / 拖动列表 / 拖右侧拖条都能回溯，闲置一会儿自己回到此刻 ──
  var FOLLOW_PX = 8;
  var IDLE_RETURN_MS = 45000;
  var ledgerHtml = "";
  var scrubDragging = false;
  var lastTouch = 0;
  function dayOf(stamp) { var d = new Date(stamp * 1000); return d.getFullYear() + "-" + d.getMonth() + "-" + d.getDate(); }
  function midnight(stamp) { var d = new Date(stamp * 1000); return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime(); }
  function dayLabel(stamp, now) {
    var diff = Math.round((midnight(now) - midnight(stamp)) / 86400000);
    var d = new Date(stamp * 1000);
    var md = (d.getMonth() + 1) + "月" + d.getDate() + "日";
    if (diff === 0) return "今天 · " + md;
    if (diff === 1) return "昨天 · " + md;
    if (diff === 2) return "前天 · " + md;
    return md;
  }
  function shortStamp(stamp, now) {
    if (!stamp) return "";
    var label = dayLabel(stamp, now).replace(/ /g, "");
    if (label.indexOf("今天") === 0) label = "今天";
    else if (label.indexOf("·") > 0) label = label.split("·")[0];
    return label + " " + hm(stamp);
  }
  function topRow(box) {
    var rows = box.querySelectorAll(".row");
    // 顶上压着吸顶日期头（约 30px），被它盖住的那一行不算"看得见"。
    for (var i = 0; i < rows.length; i++) if (rows[i].offsetTop - box.offsetTop + rows[i].offsetHeight / 2 > box.scrollTop + 30) return rows[i];
    return null;
  }
  function syncScrub() {
    var box = $("rows");
    var max = box.scrollHeight - box.clientHeight;
    var scrub = $("scrub");
    var back = box.scrollTop > FOLLOW_PX;
    var p = max > 0 ? Math.min(1, box.scrollTop / max) : 0;
    var h = scrub.clientHeight;
    $("knob").style.top = (p * h) + "px";
    $("scrubAt").style.top = (p * h) + "px";
    var row = topRow(box);
    var stamp = row ? shortStamp(Number(row.getAttribute("data-at")), nowS()) : "";
    $("scrubAt").textContent = stamp ? "翻看至 · " + stamp : "";
    $("nowBtn").textContent = (back && stamp ? stamp + " · " : "") + "回到最新 ↑";
    scrub.classList.toggle("live", !back);
    scrub.classList.toggle("back", back);
    scrub.style.display = max > 0 ? "" : "none";
    $("nowBtn").classList.toggle("show", back && viewAt === null);
  }
  function backToNow() { $("rows").scrollTo({ top: 0, behavior: "smooth" }); }
  function touched() { lastTouch = Date.now(); }
  function wireTimeline() {
    var box = $("rows");
    box.addEventListener("scroll", function () { syncScrub(); }, { passive: true });
    box.addEventListener("wheel", touched, { passive: true });
    box.addEventListener("touchstart", touched, { passive: true });
    // 鼠标按住列表上下拖（触屏本来就能拖）；没拖动就算点了那一条
    var dragY = null, dragTop = 0, moved = 0;
    box.addEventListener("mousedown", function (e) { dragY = e.clientY; dragTop = box.scrollTop; moved = 0; box.classList.add("dragging"); touched(); });
    window.addEventListener("mousemove", function (e) {
      if (dragY === null) return;
      moved = Math.max(moved, Math.abs(e.clientY - dragY));
      box.scrollTop = dragTop - (e.clientY - dragY) / stageScale();
      touched();
    });
    window.addEventListener("mouseup", function () { dragY = null; box.classList.remove("dragging"); });
    box.addEventListener("click", function (e) {
      if (moved > 5) return;
      var row = e.target.closest(".row");
      if (!row || !model) return;
      var t = Number(row.getAttribute("data-at"));
      if (t >= model.mid) setView(t);
    });
    var scrub = $("scrub");
    function seek(clientY) {
      var r = scrub.getBoundingClientRect();
      var p = Math.max(0, Math.min(1, (clientY - r.top) / r.height));
      box.scrollTop = p * (box.scrollHeight - box.clientHeight);
      touched();
    }
    scrub.addEventListener("pointerdown", function (e) {
      scrubDragging = true; scrub.classList.add("drag"); scrub.setPointerCapture(e.pointerId); seek(e.clientY);
    });
    scrub.addEventListener("pointermove", function (e) { if (scrubDragging) seek(e.clientY); });
    function release() { scrubDragging = false; scrub.classList.remove("drag"); }
    scrub.addEventListener("pointerup", release);
    scrub.addEventListener("pointercancel", release);
    $("nowBtn").addEventListener("click", backToNow);
    setInterval(function () {
      if (viewAt === null && box.scrollTop > FOLLOW_PX && !scrubDragging && dragY === null && Date.now() - lastTouch > IDLE_RETURN_MS) backToNow();
    }, 5000);
  }

  var ALERT_MS = 90;
  function renderReminders(now) {
    var pending = model.reminders.filter(function (m) { return m.state === "pending" && m.fire_at > now - 60; });
    var rang = model.reminders.filter(function (m) { return m.state === "fire" && now - m.done_at < ALERT_MS; }).pop();
    var alert = $("alert");
    var text = rang ? "到点提醒 · " + clip(rang.label, 10) + " · " + hm(rang.done_at) : "";
    if (alert.dataset.text !== text) {
      alert.dataset.text = text;
      alert.textContent = text;
      alert.className = text ? "alert show" : "alert";
    }
    return pending;
  }
  // 底栏便签：还没响的提醒 + 用户让她记下的便签；查个天气这种一次性的结果不上墙。
  var seenSlips = null;
  function renderNotesLine(data, now) {
    var pending = renderReminders(now);
    var slips = pending.slice(0, 3).map(function (m) {
      return { key: "rem:" + m.id, cls: "rem", tag: "提醒 " + hm(m.fire_at), text: clip(m.label, 12),
        note: m.fire_at > now ? span(m.fire_at - now) + "后" : "就是现在" };
    });
    (data.notes || []).slice(0, Math.max(0, 3 - slips.length)).forEach(function (n) {
      slips.push({ key: "note:" + n.at, cls: "", tag: "便签", text: clip(n.text, 14), note: ago(n.at, now) });
    });
    var next = {};
    var html = slips.map(function (x) {
      next[x.key] = true;
      var fresh = seenSlips && !seenSlips[x.key] ? " new" : "";
      return '<div class="slip ' + x.cls + fresh + '"><i>' + esc(x.tag) + "</i>" + esc(x.text) + "<span>" + esc(x.note) + "</span></div>";
    }).join("") || '<div class="none">还没有便签 · 对她说「帮我记一下…」或「几点提醒我…」就会贴上来</div>';
    seenSlips = next;
    var box = $("notesLine");
    if (box.dataset.html !== html) { box.dataset.html = html; box.innerHTML = html; }
  }

  function render(data, local) {
    var now = local ? nowS() : (Number(data.server_time) || Date.now() / 1000);
    model = buildModel(data, now);
    var w = model.weather;
    var place = data.place || {};
    $("weather").textContent = w ? (w.city || data.city || "") + " " + (w.temperature != null ? Math.round(w.temperature) + "° " : "") + w.condition
      : ((data.city || "") + " " + (place.weather || "")).trim();
    var emo = data.emotion || {};
    latest = data;
    renderGauges(data);
    renderLedger(data, now);
    renderRiver(data, now);
    renderNotesLine(data, now);
    renderMoment(data, now);
    if (viewAt === null) {
      // 刚说完一句：挂那一轮的情绪；过了 15 分钟回到整体心情的脸。
      var f = faceAt(data, now, liveTurn(data));
      if (!playing) { setFace(data, f.key, false); setPips(f.intensity); }
      if (chainKey === "live") $("chainAt").textContent = emo.at && !f.turn ? "上一轮 " + hm(emo.at) : "";
      $("emoAt").textContent = f.turn ? "这一轮 · 强度 " + (emo.intensity || 0) + " · " + ago(emo.at, now)
        : "整体心情 " + f.mood + (emo.at ? " · 上一轮 " + hm(emo.at) + " " + labelOf(data, faceKey(emo.emotion)) : "");
    }
    // 后台查询的结果晚于情绪回来：实时视图里“她说”跟着换成结果。
    if (viewAt === null && chainKey === "live" && !playing) {
      setSaid((data.exchange || {}).said);
    }
    if (local) return;
    var stamp = emo.at || 0;
    if (playedAt === null || stamp !== playedAt) {
      // 打开页面先演一遍最近这一轮；之后每来一轮新情绪再演（回看时只换数据不演）。
      playedAt = stamp;
      renderChain(data, !!stamp && viewAt === null);
    }
  }

  function poll() {
    fetch(DATA_URL, { cache: "no-store" })
      .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .then(function (data) {
        if (data.ok === false) throw new Error(data.error || "no data");
        lastOk = Date.now();
        if (data.server_time) clockOffset = Number(data.server_time) - Date.now() / 1000;
        $("err").textContent = "";
        var dot = $("syncDot"); dot.classList.remove("beat"); void dot.offsetWidth; dot.classList.add("beat");
        render(data, false);
      })
      .catch(function (e) { $("err").textContent = "连接稍有延迟，正在重连…"; console.warn("trace data:", e); })
      .then(function () { setTimeout(poll, POLL_MS); });
  }

  // 每秒走一下：时钟、同步状态、"现在"的竖线、已经待了多久
  function tick() {
    var now = nowS();
    var d = new Date(now * 1000);
    $("clock").innerHTML = pad(d.getHours()) + ":" + pad(d.getMinutes()) + "<s>:" + pad(d.getSeconds()) + "</s>";
    var since = lastOk ? (Date.now() - lastOk) / 1000 : null;
    $("sync").className = since !== null && since > 3 * POLL_MS / 1000 ? "sync stale" : "sync";
    $("syncText").textContent = since === null ? "连接中…" : (since < 3 ? "实时同步中" : "上次同步 " + Math.round(since) + " 秒前");
    tickRiver();
    if (latest && model && viewAt === null) renderDoing(latest, now, { stay: stayAt(now) });
    if (viewAt !== null && Date.now() - viewTouched > VIEW_IDLE_MS) setView(null);
  }

  function replay() { if (latest && viewAt === null) renderChain(latest, true); }
  document.addEventListener("keydown", function (e) {
    if (e.code === "Space") { e.preventDefault(); replay(); }
    if (e.code === "Escape") setView(null);
  });
  $("mind").addEventListener("click", function () { if (viewAt !== null) setView(null); else replay(); });
  $("backPill").addEventListener("click", function () { setView(null); });
  window.addEventListener("resize", function () { fit(); syncScrub(); });
  fit();
  wireTimeline();
  wireRiver();
  poll();
  setInterval(tick, 1000);
  tick();
})();
</script>
</body>
</html>
"""

TRACE_PAGE_HTML = _TEMPLATE.replace("__FACES__", json.dumps(TRACE_FACES))
