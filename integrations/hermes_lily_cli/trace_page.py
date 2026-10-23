from __future__ import annotations

"""X1 X-Ray 大屏的页面本体（``GET /trace``）。

自包含：内联 CSS + 原生 JS + 内联情绪脸（data URI），不引任何外部资源
（路演现场可能没有外网）。版面固定 1920×1080，按窗口等比缩放。

设计重心是"她在想"这一刻：每来一轮新情绪，页面把真实的评估链当场
演一遍——原话逐字打进来，光沿着 自评 → 调制 → 惯性 → 定为 依次点亮，
最后脸切换、数值跳动。按空格或点击屏幕可以重播最近一轮（只重播真实
数据，不造假数据）。

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
<title>Lily X-Ray</title>
<style>
  :root {
    --bg: #05070c;
    --ink: #f2f5fb;
    --dim: #8391a8;
    --faint: #3a4558;
    --up: #5fd38d;
    --down: #ff7a72;
    --gold: #ffcf5a;
    --emo: #8fb8c8;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { width: 100%; height: 100%; background: var(--bg); overflow: hidden; cursor: default; }
  body {
    color: var(--ink);
    font-family: "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Noto Sans CJK SC", sans-serif;
    font-variant-numeric: tabular-nums;
  }
  #stage {
    position: absolute; left: 50%; top: 50%;
    width: 1920px; height: 1080px; overflow: hidden;
    transform-origin: 0 0;
    padding: 30px 44px 34px;
    display: grid;
    grid-template-columns: 640px 1fr 450px;
    grid-template-rows: 60px 1fr 196px;
    gap: 22px 40px;
    background: var(--bg);
  }
  /* 氛围光：两团情绪色的光慢慢漂，整屏的"心情" */
  .aura { position: absolute; border-radius: 50%; filter: blur(110px); opacity: 0.38; pointer-events: none; transition: background 1.6s; background: var(--emo); }
  .aura.a { width: 900px; height: 900px; left: -260px; top: -120px; animation: drift-a 18s ease-in-out infinite alternate; }
  .aura.b { width: 700px; height: 700px; left: 700px; top: 520px; opacity: 0.16; animation: drift-b 22s ease-in-out infinite alternate; }
  @keyframes drift-a { to { transform: translate(140px, 90px) scale(1.12); } }
  @keyframes drift-b { to { transform: translate(-160px, -60px) scale(0.9); } }
  #stage > * { position: relative; z-index: 1; }
  #stage > .aura { position: absolute; z-index: 0; }

  .kicker { font-size: 18px; letter-spacing: 6px; color: var(--dim); font-weight: 600; }

  /* ── header ── */
  header { grid-column: 1 / -1; display: flex; align-items: center; gap: 26px; }
  .brand { font-size: 34px; font-weight: 800; letter-spacing: 6px; }
  .brand span { font-weight: 300; color: var(--emo); margin-left: 10px; transition: color 1.6s; }
  .tagline { font-size: 21px; color: var(--dim); }
  .spacer { flex: 1; }
  .place { font-size: 22px; color: var(--dim); }
  .place b { color: var(--ink); font-weight: 600; }
  .live { display: flex; align-items: center; gap: 10px; font-size: 16px; letter-spacing: 4px; color: var(--dim); }
  .live .dot { width: 10px; height: 10px; border-radius: 50%; background: var(--up); box-shadow: 0 0 12px var(--up); animation: blink 2s infinite; }
  .live.stale .dot { background: var(--down); box-shadow: 0 0 12px var(--down); animation: none; }
  .clock { font-size: 40px; font-weight: 200; letter-spacing: 2px; }
  @keyframes blink { 50% { opacity: 0.25; } }

  /* ── 主角：她的脸 ── */
  #hero { grid-column: 1; grid-row: 2; display: flex; flex-direction: column; align-items: center; justify-content: center; }
  .face-holder { position: relative; width: 440px; height: 440px; }
  .ring { position: absolute; inset: -18px; border-radius: 66px; border: 2px solid var(--emo); opacity: 0; transition: border-color 1.6s; }
  .ring.r1 { animation: ripple 4.5s ease-out infinite; }
  .ring.r2 { animation: ripple 4.5s ease-out 2.25s infinite; }
  @keyframes ripple { 0% { transform: scale(0.96); opacity: 0.55; } 100% { transform: scale(1.22); opacity: 0; } }
  .face {
    position: absolute; inset: 0; border-radius: 52px; background: #f4f1ea; padding: 14px;
    box-shadow: 0 0 0 5px var(--emo), 0 0 90px color-mix(in srgb, var(--emo) 55%, transparent), 0 30px 60px rgba(0, 0, 0, 0.5);
    animation: breathe 5s ease-in-out infinite;
    transition: box-shadow 1.6s;
  }
  @keyframes breathe { 50% { transform: scale(1.015) translateY(-4px); } }
  .face img { width: 100%; height: 100%; border-radius: 40px; display: block; }
  .face .flash { position: absolute; inset: 0; border-radius: 52px; background: #fff; opacity: 0; pointer-events: none; }
  .face.swap .flash { animation: flash 0.9s ease-out; }
  .face.swap img { animation: pop 0.9s cubic-bezier(.2, 1.5, .4, 1); }
  @keyframes flash { 0% { opacity: 0.95; } 100% { opacity: 0; } }
  @keyframes pop { 0% { transform: scale(0.86); } 100% { transform: scale(1); } }
  .emo-label { margin-top: 44px; font-size: 128px; font-weight: 900; line-height: 1; letter-spacing: 8px; color: var(--emo); transition: color 1.6s;
    text-shadow: 0 0 60px color-mix(in srgb, var(--emo) 45%, transparent); }
  .emo-label.swap { animation: labelin 0.8s cubic-bezier(.2, 1.4, .4, 1); }
  @keyframes labelin { 0% { transform: translateY(24px) scale(0.9); opacity: 0; } 100% { opacity: 1; } }
  .emo-meta { margin-top: 22px; display: flex; align-items: center; gap: 18px; font-size: 22px; color: var(--dim); }
  .pips { display: flex; gap: 8px; }
  .pips span { width: 34px; height: 10px; border-radius: 5px; background: rgba(255, 255, 255, 0.12); transition: background 0.6s, box-shadow 0.6s; }
  .pips span.on { background: var(--emo); box-shadow: 0 0 12px var(--emo); }

  /* ── 她在想：评估链 ── */
  #mind { grid-column: 2; grid-row: 2; display: flex; flex-direction: column; padding-top: 6px; }
  .chain { position: relative; margin-top: 26px; padding-left: 72px; }
  .chain .track { position: absolute; left: 25px; top: 26px; bottom: 26px; width: 3px; border-radius: 2px; background: var(--faint); }
  .chain .fill { position: absolute; left: 25px; top: 26px; width: 3px; height: 0; border-radius: 2px;
    background: linear-gradient(var(--faint), var(--emo)); box-shadow: 0 0 14px var(--emo); transition: height 0.7s ease; }
  .chain .spark { position: absolute; left: 18px; top: 19px; width: 17px; height: 17px; border-radius: 50%; background: #fff;
    box-shadow: 0 0 18px #fff, 0 0 40px var(--emo); opacity: 0; transition: top 0.7s ease, opacity 0.3s; }
  .node { position: relative; min-height: 96px; padding: 4px 0 14px; opacity: 0.22; transition: opacity 0.5s; }
  .node::before { content: ""; position: absolute; left: -58px; top: 12px; width: 26px; height: 26px; border-radius: 50%;
    background: var(--bg); border: 3px solid var(--faint); transition: all 0.5s; }
  .node.lit { opacity: 1; }
  .node.lit::before { border-color: var(--emo); background: color-mix(in srgb, var(--emo) 30%, var(--bg)); }
  .node.skip.lit { opacity: 0.45; }
  .node.skip.lit::before { border-color: var(--faint); background: var(--bg); }
  .node.final.lit::before { background: var(--emo); box-shadow: 0 0 22px var(--emo); }
  .node .k { font-size: 18px; letter-spacing: 4px; color: var(--dim); }
  .node .v { margin-top: 4px; font-size: 34px; font-weight: 600; line-height: 1.3;
    opacity: 0; transform: translateY(10px); transition: opacity 0.45s, transform 0.45s; }
  .node.lit .v { opacity: 1; transform: none; }
  .node.skip .v { font-size: 24px; font-weight: 400; color: var(--dim); }
  .node .v .q { font-weight: 500; }
  .node .v .q::before { content: "「"; color: var(--dim); }
  .node .v .q::after { content: "」"; color: var(--dim); }
  .caret { display: inline-block; width: 3px; height: 1em; margin-left: 4px; vertical-align: -0.12em; background: var(--emo); animation: blink 0.8s infinite; }
  .chip { display: inline-block; padding: 0 16px; border-radius: 12px; font-weight: 800;
    color: var(--c); background: color-mix(in srgb, var(--c) 18%, transparent); border: 2px solid color-mix(in srgb, var(--c) 60%, transparent); }
  .arrow { color: var(--dim); margin: 0 14px; font-weight: 300; }
  .landed { margin-top: 6px; display: flex; flex-wrap: wrap; gap: 12px; opacity: 0; transform: translateY(10px); transition: all 0.6s; }
  .landed.show { opacity: 1; transform: none; }
  .landed span { font-size: 21px; padding: 6px 14px; border-radius: 10px; background: rgba(255, 255, 255, 0.06); color: var(--dim); }
  .landed span b { color: var(--ink); font-weight: 600; }
  .said { margin-top: auto; font-size: 24px; line-height: 1.55; color: var(--ink); min-height: 76px;
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
  .said em { font-style: normal; color: var(--dim); margin-right: 8px; }
  .hint { position: absolute; right: 0; top: 4px; font-size: 15px; color: var(--faint); letter-spacing: 2px; }

  /* ── 账本 ── */
  #ledger { grid-column: 3; grid-row: 2 / 4; display: flex; flex-direction: column; padding-bottom: 170px; }
  .why-banner { display: none; margin-top: 16px; padding: 14px 18px; border-radius: 14px; font-size: 21px; line-height: 1.45;
    background: color-mix(in srgb, var(--gold) 14%, transparent); border: 1px solid color-mix(in srgb, var(--gold) 55%, transparent); }
  .why-banner.show { display: block; animation: bannerin 0.8s ease-out; }
  @keyframes bannerin { 0% { transform: translateY(-14px); opacity: 0; } }
  .why-banner b { color: var(--gold); }
  .rows { margin-top: 14px; flex: 1; overflow: hidden; display: flex; flex-direction: column; padding-left: 8px;
    mask-image: linear-gradient(#000 80%, transparent); -webkit-mask-image: linear-gradient(#000 80%, transparent); }
  .row { position: relative; padding: 12px 14px 12px 20px; border-left: 2px solid var(--faint); }
  .row::before { content: ""; position: absolute; left: -7px; top: 19px; width: 12px; height: 12px; border-radius: 50%; background: var(--c); }
  .row .meta { font-size: 16px; color: var(--dim); letter-spacing: 1px; }
  .row .meta b { color: var(--c); font-weight: 700; margin-left: 8px; }
  .row .ti { font-size: 23px; line-height: 1.35; margin-top: 2px; }
  .row .ch { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 6px; }
  .row .ch span { font-size: 17px; padding: 1px 10px; border-radius: 8px; font-weight: 600; }
  .row .ch .u { color: var(--up); background: rgba(95, 211, 141, 0.12); }
  .row .ch .w { color: var(--down); background: rgba(255, 122, 114, 0.12); }
  .row.minor { padding-top: 4px; padding-bottom: 4px; opacity: 0.45; }
  .row.minor::before { top: 11px; width: 8px; height: 8px; left: -5px; }
  .row.minor .ti { font-size: 17px; display: inline; margin-left: 10px; }
  .row.minor .meta { display: inline; }
  .row.minor .ch { display: none; }
  .row.fresh { animation: fresh 3s ease-out; }
  @keyframes fresh { 0% { background: color-mix(in srgb, var(--emo) 30%, transparent); transform: translateX(-16px); } 100% { background: transparent; transform: none; } }
  .row.cited { border-left-color: var(--gold); background: color-mix(in srgb, var(--gold) 9%, transparent); }
  .row.cited .ti::after { content: "她在引用"; margin-left: 10px; font-size: 14px; padding: 2px 8px; border-radius: 6px; background: var(--gold); color: #1a1406; vertical-align: 3px; font-weight: 700; }

  /* ── 底栏：数值 + 此刻 ── */
  #bottom { grid-column: 1 / 3; grid-row: 3; display: grid; grid-template-columns: 820px 1fr; gap: 40px; align-items: stretch;
    border-top: 1px solid rgba(130, 160, 200, 0.14); padding-top: 22px; }
  .gauges { display: grid; grid-template-columns: repeat(5, 1fr); gap: 28px; }
  .g .l { font-size: 20px; color: var(--dim); letter-spacing: 2px; }
  .g .num { font-size: 64px; font-weight: 700; line-height: 1.1; position: relative; display: inline-block; }
  .g .bar { margin-top: 6px; height: 6px; border-radius: 3px; background: rgba(255, 255, 255, 0.08); overflow: hidden; }
  .g .bar i { display: block; height: 100%; border-radius: 3px; transition: width 1.2s ease, background 1s; }
  .g .d { position: absolute; left: 100%; top: 2px; margin-left: 8px; font-size: 26px; font-weight: 800; opacity: 0; white-space: nowrap; }
  .g .d.show { animation: floatup 4s ease-out; }
  @keyframes floatup { 0% { opacity: 0; transform: translateY(16px); } 12% { opacity: 1; transform: none; } 80% { opacity: 1; } 100% { opacity: 0; transform: translateY(-8px); } }
  .now { display: flex; flex-direction: column; justify-content: space-between; min-width: 0; }
  .now .t { font-size: 32px; font-weight: 700; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .now .t small { font-size: 22px; font-weight: 400; color: var(--dim); margin-left: 12px; }
  .now .why { font-size: 20px; color: var(--dim); line-height: 1.45; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
  .now .why b { color: var(--gold); font-weight: 600; margin-right: 6px; }
  .beads { position: relative; height: 22px; margin-top: 6px; }
  .beads .rail { position: absolute; left: 0; right: 0; top: 10px; height: 2px; background: var(--faint); }
  .beads .past { position: absolute; left: 0; top: 9px; height: 4px; border-radius: 2px; background: linear-gradient(90deg, transparent, var(--emo)); }
  .beads i { position: absolute; top: 6px; width: 10px; height: 10px; margin-left: -5px; border-radius: 50%; background: var(--bg); border: 2px solid var(--dim); }
  .beads i.done { border-color: var(--faint); }
  .beads i.cur { top: 1px; width: 20px; height: 20px; margin-left: -10px; background: var(--emo); border-color: var(--emo); box-shadow: 0 0 16px var(--emo); }

  /* ── 便签：压在账本底部 ── */
  #notes { position: absolute; right: 44px; bottom: 34px; width: 450px; z-index: 2; }
  .notes { margin-top: 12px; display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; }
  .note { height: 118px; padding: 12px 12px 10px; border-radius: 4px 4px 16px 4px; background: #f6e7a8; color: #2b2410; font-size: 19px; line-height: 1.32;
    box-shadow: 0 10px 22px rgba(0, 0, 0, 0.45); display: flex; flex-direction: column; justify-content: space-between; overflow: hidden; }
  .note:nth-child(1) { transform: rotate(-2deg); }
  .note:nth-child(2) { transform: rotate(1.4deg); background: #f3d9e4; }
  .note:nth-child(3) { transform: rotate(-1deg); background: #d8ecdf; }
  .note .by { font-size: 13px; color: rgba(43, 36, 16, 0.6); white-space: nowrap; }
  .note.empty { background: transparent; border: 2px dashed var(--faint); color: var(--faint); box-shadow: none; transform: none; font-size: 16px; justify-content: center; text-align: center; }
  .note.fresh { animation: drop 0.9s cubic-bezier(.2, 1.4, .4, 1); }
  @keyframes drop { 0% { transform: translateY(-50px) rotate(-10deg); opacity: 0; } 100% { opacity: 1; } }
  .error { position: absolute; left: 44px; bottom: 8px; font-size: 15px; color: var(--down); z-index: 3; }
</style>
</head>
<body>
<div id="stage">
  <div class="aura a"></div><div class="aura b"></div>
  <header>
    <div class="brand">LILY<span>X-RAY</span></div>
    <div class="tagline">她的每一个情绪，都能在账本里查到出处</div>
    <div class="spacer"></div>
    <div class="place" id="place"></div>
    <div class="live" id="live"><span class="dot"></span>LIVE</div>
    <div class="clock" id="clock">--:--</div>
  </header>

  <section id="hero">
    <div class="face-holder">
      <div class="ring r1"></div><div class="ring r2"></div>
      <div class="face" id="face"><img id="faceImg" alt=""><div class="flash"></div></div>
    </div>
    <div class="emo-label" id="emoLabel">—</div>
    <div class="emo-meta"><div class="pips" id="pips"></div><span id="emoAt"></span></div>
  </section>

  <section id="mind">
    <div class="kicker">她 在 想</div>
    <div class="hint">空格 / 点击 重播</div>
    <div class="chain" id="chain">
      <div class="track"></div><div class="fill" id="fill"></div><div class="spark" id="spark"></div>
      <div class="node" id="n1"><div class="k">她听到</div><div class="v" id="v1"></div></div>
      <div class="node" id="n2"><div class="k" id="k2">模型自评</div><div class="v" id="v2"></div></div>
      <div class="node" id="n3"><div class="k">状态调制</div><div class="v" id="v3"></div></div>
      <div class="node" id="n4"><div class="k">情绪惯性</div><div class="v" id="v4"></div></div>
      <div class="node final" id="n5"><div class="k">定为</div><div class="v" id="v5"></div><div class="landed" id="landed"></div></div>
    </div>
    <div class="said" id="said"></div>
  </section>

  <section id="ledger">
    <div class="kicker">生 活 账 本</div>
    <div class="why-banner" id="whyBanner"></div>
    <div class="rows" id="rows"></div>
  </section>

  <section id="bottom">
    <div class="gauges" id="gauges"></div>
    <div class="now">
      <div class="kicker">此 刻 日 程</div>
      <div class="t" id="nowTitle"></div>
      <div class="why" id="nowWhy"></div>
      <div class="beads" id="beads"></div>
    </div>
  </section>

  <section id="notes">
    <div class="kicker">桌 面 便 签</div>
    <div class="notes" id="noteBox"></div>
  </section>
  <div class="error" id="err"></div>
</div>
<script>
(function () {
  "use strict";
  var FACES = __FACES__;
  var COLORS = {
    neutral: "#8fb8c8", happy: "#ffc857", excited: "#ff8a5c", shy: "#ff9ecf",
    sad: "#6aa6ff", worried: "#b59cff", pouty: "#ff6b6b", surprised: "#5fe3d0"
  };
  var SOURCES = {
    world_clock: ["世界", "#7fb3ff"], weather: ["天气", "#5fe3d0"], hunger_drive: ["饥饿", "#ffb86b"],
    drive_blend: ["驱动", "#8391a8"], dialogue: ["对话", "#c3a6ff"], interaction: ["互动", "#c3a6ff"],
    emotion: ["情绪", "#ff9ecf"], note: ["便签", "#ffcf5a"]
  };
  var GAUGES = ["mood", "energy", "satiety", "stress", "trust"];
  // 轮询 <页面路径>/data，即 /trace/data；挂在 nginx 前缀下也能对上。
  var DATA_URL = location.pathname.replace(/\/+$/, "") + "/data" + location.search;
  var POLL_MS = 2000;
  var STEP_MS = 900;
  var latest = null;
  var playedAt = null;
  var timers = [];
  var shown = {};
  var seenKeys = null;
  var seenNotes = null;
  var lastOk = 0;

  function $(id) { return document.getElementById(id); }
  function esc(text) {
    return String(text == null ? "" : text).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }
  function clip(text, n) { text = String(text || ""); return text.length > n ? text.slice(0, n) + "…" : text; }
  function hm(stamp) {
    if (!stamp) return "";
    var d = new Date(stamp * 1000);
    return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
  }
  function ago(stamp, now) {
    if (!stamp) return "";
    var s = Math.max(0, now - stamp);
    if (s < 60) return "刚刚";
    if (s < 3600) return Math.floor(s / 60) + " 分钟前";
    if (s < 86400) return Math.floor(s / 3600) + " 小时前";
    return Math.floor(s / 86400) + " 天前";
  }
  function fit() {
    var k = Math.min(window.innerWidth / 1920, window.innerHeight / 1080);
    $("stage").style.transform = "scale(" + k + ") translate(-50%, -50%)";
  }
  function faceKey(e) { return FACES[e] ? e : "neutral"; }
  function labelOf(data, e) { return (data.emotion_labels || {})[e] || e || "—"; }
  function chip(data, e) {
    return '<span class="chip" style="--c:' + (COLORS[e] || "#8391a8") + '">' + esc(labelOf(data, e)) + "</span>";
  }
  function later(fn, ms) { timers.push(setTimeout(fn, ms)); }
  function stopAll() { timers.forEach(clearTimeout); timers = []; }

  // ── 主角 ──
  function setFace(data, emotion, animate) {
    var key = faceKey(emotion);
    document.documentElement.style.setProperty("--emo", COLORS[key]);
    $("faceImg").src = FACES[key];
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
  function chainSteps(data) {
    var emo = data.emotion || {};
    var res = emo.last_resolution || {};
    var parts = String(res.reason || "").split("；");
    var modulate = "", inertia = "";
    parts.slice(1).forEach(function (p) { if (p.indexOf("还没过去") >= 0) inertia = p; else modulate = p; });
    var has = !!res.source;
    var heard = (data.exchange || {}).heard || "";
    return [
      { typed: heard, skip: !heard },
      has ? (res.source === "llm"
        ? { k: "模型自评", v: "她给自己打了" + '<span class="arrow">→</span>' + chip(data, res.modulated_from) }
        : { k: "规则兜底", v: esc(clip((parts[0] || "").replace("规则兜底：", ""), 10)) + '<span class="arrow">→</span>' + chip(data, res.modulated_from) })
        : { k: "模型自评", v: "下一轮对话后显示完整评估链", skip: true },
      modulate ? { v: esc(modulate) } : { v: has ? "体力、心情、压力都正常，不调整" : "—", skip: true },
      inertia ? { v: esc(inertia) } : { v: has ? "没有残留情绪拖着" : "—", skip: true },
      { v: emo.emotion ? chip(data, emo.emotion) + '<span class="arrow">强度</span>' + (emo.intensity || 0) : "平静" }
    ];
  }
  function lightTo(i) {
    // 光走到第 i 个节点（0..4）
    var node = $("n" + (i + 1));
    var top = node.offsetTop + 25;
    $("fill").style.height = Math.max(0, top - 26) + "px";
    var spark = $("spark");
    spark.style.top = (top - 8) + "px";
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
  function fillLanded(data) {
    var fx = (data.emotion || {}).effects || {};
    var parts = (fx.changes || []).map(function (c) {
      return '账本 <b style="color:var(--' + (c.after >= c.before ? "up" : "down") + ')">' + esc(c.label) + " " + c.before + "→" + c.after + "</b>";
    });
    parts.push("语气 <b>" + esc(clip(fx.voice_tone || "平常的语气", 12)) + "</b>");
    parts.push("设备 <b>换上这张脸</b>");
    $("landed").innerHTML = parts.map(function (p) { return "<span>" + p + "</span>"; }).join("");
  }
  function renderChain(data, animate) {
    stopAll();
    var steps = chainSteps(data);
    var emo = data.emotion || {};
    var res = emo.last_resolution || {};
    var said = (data.exchange || {}).said;
    $("k2").textContent = steps[1].k;
    for (var i = 0; i < 5; i++) {
      var node = $("n" + (i + 1));
      node.classList.remove("lit");
      node.classList.toggle("skip", !!steps[i].skip);
      $("v" + (i + 1)).innerHTML = i === 0 ? "" : steps[i].v;
    }
    $("landed").classList.remove("show");
    fillLanded(data);

    if (!animate) {
      $("v1").innerHTML = steps[0].typed ? '<span class="q">' + esc(clip(steps[0].typed, 20)) + "</span>" : "还没有对话";
      for (var j = 0; j < 5; j++) $("n" + (j + 1)).classList.add("lit");
      lightTo(4);
      $("landed").classList.add("show");
      $("said").innerHTML = said ? "<em>她说</em>" + esc(clip(said, 60)) : "";
      setFace(data, emo.emotion, false);
      setPips(emo.intensity || 0);
      return;
    }

    // 演一遍：脸先停在上一份情绪，光走完五步再换脸。
    setFace(data, res.inertia_from || emo.emotion, false);
    setPips(0);
    $("said").innerHTML = "";
    $("fill").style.height = "0px";
    lightTo(0);
    typeInto($("v1"), clip(steps[0].typed || "……", 20), function () {
      var t = 350;
      [1, 2, 3].forEach(function (k) {
        later(function () { lightTo(k); }, t);
        t += steps[k].skip ? STEP_MS * 0.5 : STEP_MS;
      });
      later(function () {
        lightTo(4);
        setFace(data, emo.emotion, true);
        setPips(emo.intensity || 0);
        popGauges(data);
        later(function () { $("landed").classList.add("show"); }, 500);
        if (said) later(function () { $("said").innerHTML = "<em>她说</em>" + esc(clip(said, 60)); }, 1000);
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
  function setGauge(f, v, from) {
    var el = $("g-" + f);
    if (!el) return;
    var bad = f === "stress" ? v >= 60 : v < 35;
    var bar = el.querySelector(".bar i");
    bar.style.width = Math.max(0, Math.min(100, v)) + "%";
    bar.style.background = bad ? "var(--down)" : (f === "stress" ? "var(--up)" : "var(--emo)");
    var val = el.querySelector(".val");
    shown[f] = v;
    if (from === undefined || from === v) { val.textContent = v; return; }
    var diff = v - from;
    var d = el.querySelector(".d");
    var good = f === "stress" ? diff < 0 : diff > 0;
    d.textContent = (diff > 0 ? "+" : "") + diff;
    d.style.color = good ? "var(--up)" : "var(--down)";
    d.classList.remove("show"); void d.offsetWidth; d.classList.add("show");
    var start = performance.now();
    (function step(now) {
      var p = Math.min(1, (now - start) / 1200);
      val.textContent = Math.round(from + diff * (1 - Math.pow(1 - p, 3)));
      if (p < 1) requestAnimationFrame(step);
    })(start);
  }
  function renderGauges(data) {
    var tracked = (data.state || {}).tracked || {};
    if (!$("g-mood")) buildGauges(data);
    GAUGES.forEach(function (f) { setGauge(f, Number(tracked[f] || 0), shown[f]); });
  }
  function popGauges(data) {
    // 演到"定为"时，让这份情绪写进账本的那几项从之前的值重新跳一次。
    var tracked = (data.state || {}).tracked || {};
    (((data.emotion || {}).effects || {}).changes || []).forEach(function (c) {
      if (GAUGES.indexOf(c.field) >= 0) setGauge(c.field, Number(tracked[c.field] || c.after), c.before);
    });
  }

  // ── 账本 ──
  function renderLedger(data, now) {
    var why = data.why || {};
    var keys = why.event_keys || [];
    var whyFresh = why.at && now - why.at < 600 && keys.length;
    var cited = {};
    if (whyFresh) keys.forEach(function (k) { cited[k] = true; });
    var banner = $("whyBanner");
    var bannerHtml = whyFresh ? "你问<b>「" + esc(clip(why.question, 12)) + "」</b><br>她只能按下面 <b>" + keys.length + "</b> 条真实记录回答" : "";
    if (banner.dataset.html !== bannerHtml) {
      banner.dataset.html = bannerHtml;
      banner.innerHTML = bannerHtml;
      banner.className = whyFresh ? "why-banner show" : "why-banner";
    }
    var first = seenKeys === null;
    var nextSeen = {};
    $("rows").innerHTML = (data.ledger || []).slice(0, 12).map(function (r) {
      var key = r.event_key || (r.created_at + r.title);
      nextSeen[key] = true;
      var src = SOURCES[r.source] || [r.source || "记录", "#8391a8"];
      var cls = ["row"];
      if (r.source === "drive_blend" && !cited[r.event_key]) cls.push("minor");
      if (!first && !seenKeys[key]) cls.push("fresh");
      if (cited[r.event_key]) cls.push("cited");
      var chips = (r.change_items || []).map(function (c) {
        return '<span class="' + (c.after >= c.before ? "u" : "w") + '">' + esc(c.label) + " " + c.before + "→" + c.after + "</span>";
      }).join("");
      return '<div class="' + cls.join(" ") + '" style="--c:' + src[1] + '"><div class="meta">' + esc(hm(r.created_at)) +
        "<b>" + esc(src[0]) + '</b></div><div class="ti">' + esc(clip(r.title, 14)) + "</div>" +
        (chips ? '<div class="ch">' + chips + "</div>" : "") + "</div>";
    }).join("");
    seenKeys = nextSeen;
  }

  // ── 此刻日程 ──
  function renderNow(data, now) {
    var plan = data.plan || [];
    var cur = null;
    plan.forEach(function (p) { if (p.current) cur = p; });
    $("nowTitle").innerHTML = cur ? esc(cur.title) + "<small>@" + esc(cur.location) + " · " + esc(hm(cur.scheduled_at)) + "</small>"
      : (plan.length ? "今天的日程还没开始" : "今天还没有日程");
    $("nowWhy").innerHTML = cur && cur.why_text ? "<b>为什么</b>" + esc(cur.why_text) : "";
    if (!plan.length) { $("beads").innerHTML = ""; return; }
    var times = plan.map(function (p) { return p.scheduled_at; });
    var start = Math.min.apply(null, times) - 1800;
    var end = Math.max.apply(null, times) + 1800;
    function pct(t) { return Math.max(0, Math.min(100, (t - start) / (end - start) * 100)); }
    var html = '<div class="rail"></div><div class="past" style="width:' + pct(now) + '%"></div>';
    plan.forEach(function (p) {
      var cls = p.current ? "cur" : (p.scheduled_at < now ? "done" : "");
      html += '<i class="' + cls + '" style="left:' + pct(p.scheduled_at) + '%"></i>';
    });
    $("beads").innerHTML = html;
  }

  // ── 便签 ──
  function renderNotes(data, now) {
    var notes = data.notes || [];
    var first = seenNotes === null;
    var nextSeen = {};
    var html = "";
    for (var i = 0; i < 3; i++) {
      var n = notes[i];
      if (!n) { html += '<div class="note empty">说「帮我<br>记一下…」</div>'; continue; }
      var key = n.text + "|" + n.at;
      nextSeen[key] = true;
      html += '<div class="note' + (!first && !seenNotes[key] ? " fresh" : "") + '"><div>' + esc(n.text) +
        '</div><div class="by">' + (n.source === "agent" ? "Agent" : "语音") + " · " + esc(ago(n.at, now)) + "</div></div>";
    }
    seenNotes = nextSeen;
    $("noteBox").innerHTML = html;
  }

  function render(data) {
    var now = Number(data.server_time) || Date.now() / 1000;
    var place = data.place || {};
    $("place").innerHTML = place.location
      ? "她在 <b>" + esc(place.location) + "</b>" + (place.activity ? " · " + esc(place.activity) : "") + (place.weather ? " · " + esc(place.weather) : "")
      : "";
    $("clock").textContent = hm(now);
    var emo = data.emotion || {};
    $("emoAt").textContent = emo.at ? "强度 " + (emo.intensity || 0) + " · " + ago(emo.at, now) : "还没有对话";
    renderGauges(data);
    renderLedger(data, now);
    renderNow(data, now);
    renderNotes(data, now);
    var stamp = emo.at || 0;
    latest = data;
    if (playedAt === null || stamp !== playedAt) {
      // 打开页面先演一遍最近这一轮；之后每来一轮新情绪再演。
      playedAt = stamp;
      renderChain(data, !!stamp);
    }
  }

  function poll() {
    fetch(DATA_URL, { cache: "no-store" })
      .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .then(function (data) {
        if (data.ok === false) throw new Error(data.error || "no data");
        lastOk = Date.now();
        $("err").textContent = "";
        render(data);
      })
      .catch(function (e) { $("err").textContent = "数据暂时拉不到：" + e.message; })
      .then(function () {
        $("live").className = Date.now() - lastOk > 3 * POLL_MS ? "live stale" : "live";
        setTimeout(poll, POLL_MS);
      });
  }

  function replay() { if (latest) renderChain(latest, true); }
  document.addEventListener("keydown", function (e) { if (e.code === "Space") { e.preventDefault(); replay(); } });
  document.addEventListener("click", replay);
  window.addEventListener("resize", fit);
  fit();
  poll();
})();
</script>
</body>
</html>
"""

TRACE_PAGE_HTML = _TEMPLATE.replace("__FACES__", json.dumps(TRACE_FACES))
