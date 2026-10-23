from __future__ import annotations

"""X1 X-Ray 大屏的页面本体（``GET /trace``）。

自包含：内联 CSS + 原生 JS + 内联情绪脸（data URI），不引任何外部资源
（路演现场可能没有外网）。版面固定 1920×1080，按窗口等比缩放，投影仪
分辨率不同也不会挤乱。评委站在三米外要一眼看懂：她现在是什么情绪、
这个情绪是怎么一步步定下来的、落到了哪些数值上、账本里有哪些真实记录。

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
    --bg: #07090f;
    --panel: rgba(20, 26, 38, 0.78);
    --panel-edge: rgba(130, 160, 200, 0.12);
    --ink: #eef3fa;
    --dim: #8a9bb3;
    --faint: #4d5b70;
    --up: #5fd38d;
    --down: #ff7a72;
    --gold: #ffcf5a;
    --emo: #8fb8c8;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { width: 100%; height: 100%; background: var(--bg); overflow: hidden; }
  body {
    color: var(--ink);
    font-family: "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Noto Sans CJK SC", sans-serif;
    font-variant-numeric: tabular-nums;
  }
  #stage {
    position: absolute; left: 50%; top: 50%;
    width: 1920px; height: 1080px;
    transform-origin: 0 0;
    padding: 34px 40px 36px;
    display: grid;
    grid-template-columns: 520px 620px 1fr;
    grid-template-rows: 78px 520px 128px 1fr;
    gap: 20px;
    background:
      radial-gradient(900px 700px at 260px 360px, color-mix(in srgb, var(--emo) 18%, transparent), transparent 70%),
      radial-gradient(1200px 800px at 1700px 1100px, rgba(80, 110, 170, 0.10), transparent 70%),
      var(--bg);
  }
  .panel {
    background: var(--panel);
    border: 1px solid var(--panel-edge);
    border-radius: 22px;
    padding: 22px 26px;
    position: relative;
    overflow: hidden;
  }
  .kicker { font-size: 19px; letter-spacing: 6px; color: var(--dim); font-weight: 600; }
  .kicker b { color: var(--ink); font-weight: 600; letter-spacing: 2px; }

  /* ── header ── */
  header { grid-column: 1 / -1; display: flex; align-items: center; gap: 28px; }
  .brand { display: flex; align-items: baseline; gap: 14px; }
  .brand .name { font-size: 46px; font-weight: 800; letter-spacing: 4px; }
  .brand .xray { font-size: 30px; font-weight: 300; letter-spacing: 10px; color: var(--emo); transition: color 1.2s; }
  .tagline { font-size: 22px; color: var(--dim); padding-left: 24px; border-left: 2px solid var(--panel-edge); }
  .spacer { flex: 1; }
  .place {
    font-size: 24px; padding: 10px 22px; border-radius: 999px;
    background: rgba(255, 255, 255, 0.05); border: 1px solid var(--panel-edge);
  }
  .place i { font-style: normal; color: var(--dim); margin: 0 10px; }
  .live { display: flex; align-items: center; gap: 10px; font-size: 18px; letter-spacing: 4px; color: var(--dim); }
  .live .dot { width: 12px; height: 12px; border-radius: 50%; background: var(--up); box-shadow: 0 0 12px var(--up); animation: blink 2s infinite; }
  .live.stale .dot { background: var(--down); box-shadow: 0 0 12px var(--down); animation: none; }
  .clock { font-size: 44px; font-weight: 300; letter-spacing: 2px; }
  @keyframes blink { 50% { opacity: 0.25; } }

  /* ── hero: her face ── */
  #hero { grid-column: 1; grid-row: 2; display: flex; flex-direction: column; align-items: center; justify-content: center; }
  .face-wrap {
    position: relative; width: 300px; height: 300px; border-radius: 34px;
    background: #f4f1ea; padding: 10px;
    box-shadow: 0 0 0 4px color-mix(in srgb, var(--emo) 70%, transparent), 0 0 70px color-mix(in srgb, var(--emo) 45%, transparent);
    transition: box-shadow 1.2s;
  }
  .face-wrap img { width: 100%; height: 100%; border-radius: 24px; display: block; }
  .face-wrap.pulse { animation: pulse 1.6s ease-out; }
  @keyframes pulse {
    0% { transform: scale(1); }
    25% { transform: scale(1.06); box-shadow: 0 0 0 10px var(--emo), 0 0 120px var(--emo); }
    100% { transform: scale(1); }
  }
  .emo-label { margin-top: 26px; font-size: 96px; font-weight: 800; line-height: 1; color: var(--emo); transition: color 1.2s; }
  .emo-meta { margin-top: 18px; display: flex; align-items: center; gap: 18px; font-size: 22px; color: var(--dim); }
  .pips { display: flex; gap: 7px; }
  .pips span { width: 26px; height: 10px; border-radius: 5px; background: rgba(255, 255, 255, 0.12); }
  .pips span.on { background: var(--emo); box-shadow: 0 0 10px var(--emo); }

  /* ── pipeline: how the emotion was decided ── */
  #chain { grid-column: 2; grid-row: 2; }
  .steps { margin-top: 12px; position: relative; }
  .steps::before {
    content: ""; position: absolute; left: 17px; top: 18px; bottom: 18px; width: 2px;
    background: linear-gradient(var(--faint), color-mix(in srgb, var(--emo) 80%, transparent));
  }
  .step { position: relative; display: grid; grid-template-columns: 36px 112px 1fr; align-items: start; gap: 14px; padding: 6px 0; }
  .step .n {
    width: 36px; height: 36px; border-radius: 50%; display: grid; place-items: center;
    font-size: 17px; font-weight: 700; background: #1c2535; border: 2px solid var(--faint); color: var(--dim);
    position: relative; z-index: 1;
  }
  .step.on .n { border-color: var(--emo); color: var(--ink); }
  .step.final .n { background: var(--emo); border-color: var(--emo); color: #0b0f16; }
  .step .k { font-size: 20px; color: var(--dim); padding-top: 5px; }
  .step .v { font-size: 25px; line-height: 1.4; padding-top: 1px; }
  .step .v.off { color: var(--faint); font-size: 21px; padding-top: 5px; }
  .quote::before { content: "「"; color: var(--dim); }
  .quote::after { content: "」"; color: var(--dim); }
  .chip {
    display: inline-block; padding: 2px 14px; border-radius: 10px; font-weight: 700;
    background: color-mix(in srgb, var(--c, var(--emo)) 22%, transparent);
    color: var(--c, var(--emo)); border: 1px solid color-mix(in srgb, var(--c, var(--emo)) 55%, transparent);
  }
  .arrow { color: var(--dim); margin: 0 10px; }
  .effects { margin-top: 14px; display: grid; grid-template-columns: 1fr 1.5fr 92px; gap: 12px; }
  .effect { background: rgba(255, 255, 255, 0.04); border: 1px solid var(--panel-edge); border-radius: 14px; padding: 10px 16px; min-height: 86px; }
  .effect .t { font-size: 16px; color: var(--dim); letter-spacing: 3px; margin-bottom: 6px; }
  .effect .b { font-size: 22px; line-height: 1.35; }
  .effect.face { padding: 8px; display: grid; place-items: center; background: #f4f1ea; }
  .effect.face img { width: 74px; height: 74px; }
  .said { margin-top: 10px; font-size: 20px; color: var(--dim); line-height: 1.5; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
  .said b { color: var(--ink); font-weight: 400; }

  /* ── gauges ── */
  #gauges { grid-column: 1 / 3; grid-row: 3; display: grid; grid-template-columns: repeat(5, 1fr) 1.7fr; gap: 26px; align-items: center; padding: 14px 28px; }
  .g .top { display: flex; justify-content: space-between; align-items: baseline; }
  .g .l { font-size: 21px; color: var(--dim); }
  .g .num { font-size: 46px; font-weight: 700; line-height: 1.05; position: relative; }
  .g .bar { margin-top: 8px; height: 8px; border-radius: 4px; background: rgba(255, 255, 255, 0.08); overflow: hidden; }
  .g .bar i { display: block; height: 100%; border-radius: 4px; transition: width 1s ease, background 1s; }
  .g .d { position: absolute; right: 0; top: -26px; font-size: 22px; font-weight: 700; opacity: 0; }
  .g .d.show { animation: floatup 3.5s ease-out; }
  @keyframes floatup { 0% { opacity: 0; transform: translateY(14px); } 15% { opacity: 1; transform: none; } 75% { opacity: 1; } 100% { opacity: 0; transform: translateY(-10px); } }
  .drives { border-left: 1px solid var(--panel-edge); padding-left: 26px; }
  .drives .kicker { font-size: 15px; letter-spacing: 4px; margin-bottom: 4px; }
  .drives .row { display: grid; grid-template-columns: 1fr 1fr; gap: 0 28px; }
  .drives .cell { display: flex; justify-content: space-between; align-items: baseline; font-size: 18px; color: var(--dim); white-space: nowrap; line-height: 1.3; }
  .drives .cell b { font-size: 26px; color: var(--ink); font-weight: 600; }

  /* ── day timeline ── */
  #day { grid-column: 1 / 3; grid-row: 4; padding: 18px 28px; }
  .day-head { display: flex; align-items: baseline; gap: 26px; }
  .now-line { flex: 1; min-width: 0; font-size: 22px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .now-line .why { color: var(--dim); font-size: 19px; margin-left: 14px; }
  .now-line .why::before { content: "为什么："; color: var(--gold); }
  .axis { position: relative; height: 128px; margin: 8px 60px 0; }
  .axis .rail { position: absolute; left: -30px; right: -30px; top: 62px; height: 2px; background: var(--faint); }
  .axis .past { position: absolute; left: -30px; top: 61px; height: 4px; border-radius: 2px; background: linear-gradient(90deg, transparent, var(--emo)); }
  .slot { position: absolute; top: 0; height: 128px; transform: translateX(-50%); width: 132px; text-align: center; }
  .slot .dot { position: absolute; left: 50%; top: 56px; width: 14px; height: 14px; margin-left: -7px; border-radius: 50%; background: #1c2535; border: 2px solid var(--dim); }
  .slot .tm { font-size: 16px; color: var(--dim); }
  .slot .ti { font-size: 19px; line-height: 1.25; white-space: nowrap; }
  .slot.up .lbl { position: absolute; bottom: 76px; left: 0; right: 0; }
  .slot.down .lbl { position: absolute; top: 80px; left: 0; right: 0; }
  .slot.done { opacity: 0.45; }
  .slot.cur .dot { width: 24px; height: 24px; margin-left: -12px; top: 51px; background: var(--emo); border-color: var(--emo); box-shadow: 0 0 18px var(--emo); }
  .slot.cur .ti { color: var(--emo); font-weight: 700; }
  .nowmark { position: absolute; top: 44px; width: 2px; height: 40px; background: var(--ink); box-shadow: 0 0 10px var(--ink); }

  /* ── ledger stream ── */
  #ledger { grid-column: 3; grid-row: 2 / 4; display: flex; flex-direction: column; }
  .why-banner {
    display: none; margin-top: 14px; padding: 12px 16px; border-radius: 12px; font-size: 20px; line-height: 1.4;
    background: color-mix(in srgb, var(--gold) 14%, transparent); border: 1px solid color-mix(in srgb, var(--gold) 50%, transparent);
  }
  .why-banner.show { display: block; }
  .why-banner b { color: var(--gold); }
  .rows { margin-top: 12px; flex: 1; overflow: hidden; display: flex; flex-direction: column; gap: 8px;
    mask-image: linear-gradient(#000 82%, transparent); -webkit-mask-image: linear-gradient(#000 82%, transparent); }
  .row { display: grid; grid-template-columns: 66px 74px 1fr; gap: 12px; align-items: start; padding: 10px 12px; border-radius: 12px; border: 1px solid transparent; }
  .row .tm { font-size: 19px; color: var(--dim); padding-top: 3px; }
  .row .src { font-size: 15px; padding: 3px 0; text-align: center; border-radius: 7px; font-weight: 700; margin-top: 3px;
    color: var(--c); background: color-mix(in srgb, var(--c) 16%, transparent); }
  .row .ti { font-size: 22px; line-height: 1.35; }
  .row .cz { font-size: 17px; color: var(--dim); margin-top: 2px; line-height: 1.35; }
  .row .ch { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 6px; }
  .row .ch span { font-size: 17px; padding: 1px 10px; border-radius: 8px; font-weight: 600; }
  .row .ch .u { color: var(--up); background: rgba(95, 211, 141, 0.12); }
  .row .ch .w { color: var(--down); background: rgba(255, 122, 114, 0.12); }
  .row.minor { opacity: 0.55; padding-top: 4px; padding-bottom: 4px; }
  .row.minor .ti { font-size: 18px; }
  .row.minor .cz, .row.minor .ch { display: none; }
  .row.fresh { animation: fresh 3s ease-out; }
  @keyframes fresh { 0% { background: color-mix(in srgb, var(--emo) 35%, transparent); transform: translateX(-12px); } 100% { background: transparent; transform: none; } }
  .row.cited { border-color: color-mix(in srgb, var(--gold) 70%, transparent); background: color-mix(in srgb, var(--gold) 10%, transparent); box-shadow: 0 0 24px color-mix(in srgb, var(--gold) 25%, transparent); }
  .row.cited .ti::after { content: "她在引用"; margin-left: 12px; font-size: 14px; padding: 2px 8px; border-radius: 6px; background: var(--gold); color: #1a1406; vertical-align: 3px; font-weight: 700; }

  /* ── notes ── */
  #notes { grid-column: 3; grid-row: 4; }
  .notes { margin-top: 14px; display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; }
  .note { min-height: 128px; padding: 16px 16px 12px; border-radius: 6px 6px 18px 6px; background: #f6e7a8; color: #2b2410; font-size: 22px; line-height: 1.35;
    box-shadow: 0 10px 22px rgba(0, 0, 0, 0.35); display: flex; flex-direction: column; justify-content: space-between; }
  .note:nth-child(1) { transform: rotate(-1.6deg); }
  .note:nth-child(2) { transform: rotate(1.2deg); background: #f3d9e4; }
  .note:nth-child(3) { transform: rotate(-0.8deg); background: #d8ecdf; }
  .note .by { font-size: 14px; white-space: nowrap; color: rgba(43, 36, 16, 0.6); letter-spacing: 1px; }
  .note.empty { background: transparent; border: 2px dashed var(--faint); color: var(--faint); box-shadow: none; transform: none; font-size: 18px; justify-content: center; text-align: center; }
  .note.fresh { animation: drop 0.9s cubic-bezier(.2, 1.4, .4, 1); }
  @keyframes drop { 0% { transform: translateY(-40px) rotate(-8deg); opacity: 0; } 100% { opacity: 1; } }
  .error { position: absolute; right: 40px; bottom: 10px; font-size: 16px; color: var(--down); }
</style>
</head>
<body>
<div id="stage">
  <header>
    <div class="brand"><span class="name">LILY</span><span class="xray">X-RAY</span></div>
    <div class="tagline">她的每一个情绪，都能在账本里查到出处</div>
    <div class="spacer"></div>
    <div class="place" id="place">—</div>
    <div class="live" id="live"><span class="dot"></span>LIVE</div>
    <div class="clock" id="clock">--:--</div>
  </header>

  <section class="panel" id="hero">
    <div class="face-wrap" id="faceWrap"><img id="face" alt=""></div>
    <div class="emo-label" id="emoLabel">—</div>
    <div class="emo-meta"><div class="pips" id="pips"></div><span id="emoAt"></span></div>
  </section>

  <section class="panel" id="chain">
    <div class="kicker"><b>她为什么是这个情绪</b>　DECISION</div>
    <div class="steps" id="steps"></div>
    <div class="effects" id="effects"></div>
    <div class="said" id="said"></div>
  </section>

  <section class="panel" id="ledger">
    <div class="kicker"><b>生活账本</b>　每一次变化都有原因</div>
    <div class="why-banner" id="whyBanner"></div>
    <div class="rows" id="rows"></div>
  </section>

  <section class="panel" id="gauges"></section>

  <section class="panel" id="day">
    <div class="day-head"><div class="kicker"><b>今天的日程</b></div><div class="now-line" id="nowLine"></div></div>
    <div class="axis" id="axis"></div>
  </section>

  <section class="panel" id="notes">
    <div class="kicker"><b>桌面便签</b>　说一句就贴上设备</div>
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
    drive_blend: ["驱动", "#8a9bb3"], dialogue: ["对话", "#c3a6ff"], interaction: ["互动", "#c3a6ff"],
    emotion: ["情绪", "#ff9ecf"], note: ["便签", "#ffcf5a"]
  };
  var GAUGES = ["mood", "energy", "satiety", "stress", "trust"];
  var DRIVES = ["social_need", "curiosity", "privacy_sensitivity", "resource_comfort"];
  // 轮询 <页面路径>/data，即 /trace/data；挂在 nginx 前缀下也能对上。
  var DATA_URL = location.pathname.replace(/\/+$/, "") + "/data" + location.search;
  var POLL_MS = 2000;
  var prev = null;
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
  function labelOf(data, emotion) { return (data.emotion_labels || {})[emotion] || emotion || "—"; }
  function emoChip(data, emotion) {
    return '<span class="chip" style="--c:' + (COLORS[emotion] || "#8a9bb3") + '">' + esc(labelOf(data, emotion)) + "</span>";
  }
  function changeText(c) {
    return esc(c.label) + " " + c.before + "→" + c.after;
  }

  function renderHero(data, now) {
    var emo = data.emotion || {};
    var key = FACES[emo.emotion] ? emo.emotion : "neutral";
    document.documentElement.style.setProperty("--emo", COLORS[key]);
    var img = $("face");
    if (img.dataset.key !== key) {
      img.dataset.key = key;
      img.src = FACES[key];
      if (prev) {
        var wrap = $("faceWrap");
        wrap.classList.remove("pulse"); void wrap.offsetWidth; wrap.classList.add("pulse");
      }
    }
    $("emoLabel").textContent = emo.label || labelOf(data, key);
    var pips = "";
    for (var i = 1; i <= 5; i++) pips += '<span class="' + (i <= (emo.intensity || 0) ? "on" : "") + '"></span>';
    $("pips").innerHTML = pips;
    $("emoAt").textContent = emo.at ? "强度 " + (emo.intensity || 0) + " · " + ago(emo.at, now) : "还没有对话";
  }

  // reason 形如 "LLM 自评；体力 20 不到 30，兴奋收成高兴；兴奋还没过去，先不跳太高"
  function chainSegments(reason) {
    var parts = String(reason || "").split("；");
    var out = { head: parts[0] || "", modulate: "", inertia: "" };
    parts.slice(1).forEach(function (p) {
      if (p.indexOf("还没过去") >= 0) out.inertia = p; else out.modulate = p;
    });
    return out;
  }

  function step(n, key, value, cls) {
    return '<div class="step ' + (cls || "") + '"><div class="n">' + n + '</div><div class="k">' + key +
      '</div><div class="v' + (cls === "off" ? " off" : "") + '">' + value + "</div></div>";
  }

  function renderChain(data) {
    var emo = data.emotion || {};
    var res = emo.last_resolution || {};
    var ex = data.exchange || {};
    var html = step(1, "她听到", ex.heard ? '<span class="quote">' + esc(clip(ex.heard, 18)) + "</span>" : "还没有对话", ex.heard ? "on" : "off");
    if (res.source) {
      var seg = chainSegments(res.reason);
      var judged = res.source === "llm"
        ? "模型给自己打的标签" + '<span class="arrow">→</span>' + emoChip(data, res.modulated_from)
        : esc(clip(seg.head.replace("规则兜底：", ""), 12)) + "，按关键词判" + '<span class="arrow">→</span>' + emoChip(data, res.modulated_from);
      html += step(2, res.source === "llm" ? "模型自评" : "规则兜底", judged, "on");
      html += seg.modulate
        ? step(3, "状态调制", esc(seg.modulate), "on")
        : step(3, "状态调制", "体力、心情、压力都正常，不调整", "off");
      html += seg.inertia
        ? step(4, "情绪惯性", esc(seg.inertia), "on")
        : step(4, "情绪惯性", "上一份情绪不拖后腿", "off");
    } else {
      html += step(2, "模型自评", "下一轮对话后显示完整评估链", "off");
      html += step(3, "状态调制", "—", "off");
      html += step(4, "情绪惯性", "—", "off");
    }
    html += step(5, "定为", emo.emotion
      ? emoChip(data, emo.emotion) + '<span class="arrow">强度</span>' + (emo.intensity || 0)
      : "平静", "final");
    $("steps").innerHTML = html;

    var fx = emo.effects || {};
    var changes = (fx.changes || []).map(function (c) {
      return '<span style="color:var(--' + (c.after >= c.before ? "up" : "down") + ')">' + changeText(c) + "</span>";
    }).join("　") || '<span style="color:var(--faint)">没有数值变化</span>';
    $("effects").innerHTML =
      '<div class="effect"><div class="t">写进账本</div><div class="b">' + changes + "</div></div>" +
      '<div class="effect"><div class="t">她的语气</div><div class="b">' + esc(fx.voice_tone || "平常的语气") + "</div></div>" +
      '<div class="effect face"><img alt="" id="miniFace"></div>';
    $("miniFace").src = FACES[FACES[emo.emotion] ? emo.emotion : "neutral"];
    $("said").innerHTML = ex.said ? "她说：<b>" + esc(clip(ex.said, 56)) + "</b>" : "";
  }

  function renderGauges(data) {
    var tracked = (data.state || {}).tracked || {};
    var drives = (data.state || {}).hidden_drives || {};
    var labels = data.field_labels || {};
    var before = prev ? ((prev.state || {}).tracked || {}) : null;
    var host = $("gauges");
    if (!host.dataset.built) {
      host.dataset.built = "1";
      host.innerHTML = GAUGES.map(function (f) {
        return '<div class="g" id="g-' + f + '"><div class="top"><span class="l">' + esc(labels[f] || f) +
          '</span><span class="num"><span class="val">-</span><span class="d"></span></span></div><div class="bar"><i></i></div></div>';
      }).join("") + '<div class="drives"><div class="kicker">内在驱动</div><div class="row" id="driveRow"></div></div>';
    }
    GAUGES.forEach(function (f) {
      var el = $("g-" + f);
      var v = Number(tracked[f] || 0);
      el.querySelector(".val").textContent = v;
      var bad = f === "stress" ? v >= 60 : v < 35;
      var bar = el.querySelector(".bar i");
      bar.style.width = Math.max(0, Math.min(100, v)) + "%";
      bar.style.background = bad ? "var(--down)" : (f === "stress" ? "var(--up)" : "var(--emo)");
      if (before && before[f] !== undefined && Number(before[f]) !== v) {
        var d = el.querySelector(".d");
        var diff = v - Number(before[f]);
        var good = f === "stress" ? diff < 0 : diff > 0;
        d.textContent = (diff > 0 ? "+" : "") + diff;
        d.style.color = good ? "var(--up)" : "var(--down)";
        d.classList.remove("show"); void d.offsetWidth; d.classList.add("show");
      }
    });
    $("driveRow").innerHTML = DRIVES.map(function (f) {
      return '<div class="cell">' + esc(labels[f] || f) + "<b>" + Number(drives[f] || 0) + "</b></div>";
    }).join("");
  }

  function renderLedger(data, now) {
    var why = data.why || {};
    var keys = why.event_keys || [];
    var whyFresh = why.at && now - why.at < 600 && keys.length;
    var cited = {};
    if (whyFresh) keys.forEach(function (k) { cited[k] = true; });
    var banner = $("whyBanner");
    if (whyFresh) {
      banner.className = "why-banner show";
      banner.innerHTML = "你问<b>「" + esc(clip(why.question, 14)) + "」</b>，她只能按下面 <b>" +
        keys.length + "</b> 条真实记录回答";
    } else {
      banner.className = "why-banner";
    }
    var first = seenKeys === null;
    var nextSeen = {};
    $("rows").innerHTML = (data.ledger || []).slice(0, 14).map(function (r) {
      var key = r.event_key || (r.created_at + r.title);
      nextSeen[key] = true;
      var src = SOURCES[r.source] || [r.source || "记录", "#8a9bb3"];
      var cls = ["row"];
      if (r.source === "drive_blend" && !cited[r.event_key]) cls.push("minor");
      if (!first && !seenKeys[key]) cls.push("fresh");
      if (cited[r.event_key]) cls.push("cited");
      var chips = (r.change_items || []).map(function (c) {
        return '<span class="' + (c.after >= c.before ? "u" : "w") + '">' + changeText(c) + "</span>";
      }).join("");
      var cause = r.cause && r.cause !== r.title ? '<div class="cz">' + esc(clip(r.cause, 30)) + "</div>" : "";
      return '<div class="' + cls.join(" ") + '"><div class="tm">' + esc(hm(r.created_at)) +
        '</div><div class="src" style="--c:' + src[1] + '">' + esc(src[0]) + '</div><div><div class="ti">' +
        esc(clip(r.title, 16)) + "</div>" + cause + (chips ? '<div class="ch">' + chips + "</div>" : "") + "</div></div>";
    }).join("");
    seenKeys = nextSeen;
  }

  function renderDay(data, now) {
    var plan = data.plan || [];
    var cur = null;
    plan.forEach(function (p) { if (p.current) cur = p; });
    $("nowLine").innerHTML = cur
      ? "此刻日程：<b>" + esc(cur.title) + "</b>　@" + esc(cur.location) + '<span class="why">' + esc(clip(cur.why_text, 30)) + "</span>"
      : (plan.length ? "今天的日程还没开始" : "今天还没有日程");
    if (!plan.length) { $("axis").innerHTML = ""; return; }
    // 轴只覆盖当天第一个到最后一个日程（前后各留半小时），早上几个挤在一起的槽能分开。
    var times = plan.map(function (p) { return p.scheduled_at; });
    var start = Math.min.apply(null, times) - 1800;
    var end = Math.max.apply(null, times) + 1800;
    function pct(t) { return Math.max(0, Math.min(100, (t - start) / (end - start) * 100)); }
    var html = '<div class="rail"></div><div class="past" style="width:calc(' + pct(now) + '% + 30px)"></div>';
    // 标签贪心分上下两道：哪道的上一个标签已经让开就放哪道，避免字叠在一起。
    var axisW = $("axis").clientWidth || 1000;
    var laneEnd = [-1e9, -1e9];
    plan.forEach(function (p) {
      var x = pct(p.scheduled_at) / 100 * axisW;
      var half = Math.min(7, String(p.title || "").length) * 9.5 + 8;
      var lane = laneEnd[0] <= x - half ? 0 : (laneEnd[1] <= x - half ? 1 : (laneEnd[0] <= laneEnd[1] ? 0 : 1));
      laneEnd[lane] = x + half;
      p._lane = lane;
    });
    plan.forEach(function (p) {
      var cls = ["slot", p._lane ? "down" : "up"];
      if (p.current) cls.push("cur");
      else if (p.scheduled_at < now) cls.push("done");
      html += '<div class="' + cls.join(" ") + '" style="left:' + pct(p.scheduled_at) + '%"><div class="dot"></div><div class="lbl"><div class="tm">' +
        esc(hm(p.scheduled_at)) + '</div><div class="ti">' + esc(clip(p.title, 6)) + "</div></div></div>";
    });
    if (now >= start && now <= end) html += '<div class="nowmark" style="left:' + pct(now) + '%"></div>';
    $("axis").innerHTML = html;
  }

  function renderNotes(data, now) {
    var notes = data.notes || [];
    var first = seenNotes === null;
    var nextSeen = {};
    var html = "";
    for (var i = 0; i < 3; i++) {
      var n = notes[i];
      if (!n) {
        html += '<div class="note empty">说「帮我记一下…」</div>';
        continue;
      }
      var key = n.text + "|" + n.at;
      nextSeen[key] = true;
      html += '<div class="note' + (!first && !seenNotes[key] ? " fresh" : "") + '"><div>' + esc(n.text) +
        '</div><div class="by">' + (n.source === "agent" ? "Agent 查的" : "你说的") + " · " + esc(ago(n.at, now)) + "</div></div>";
    }
    seenNotes = nextSeen;
    $("noteBox").innerHTML = html;
  }

  function render(data) {
    var now = Number(data.server_time) || Date.now() / 1000;
    var place = data.place || {};
    $("place").innerHTML = place.location
      ? "在 <b>" + esc(place.location) + "</b>" + (place.activity ? "<i>·</i>" + esc(place.activity) : "") +
        (place.weather ? "<i>·</i>" + esc(place.weather) : "")
      : "—";
    $("clock").textContent = hm(now);
    renderHero(data, now);
    renderChain(data);
    renderGauges(data);
    renderLedger(data, now);
    renderDay(data, now);
    renderNotes(data, now);
    prev = data;
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

  window.addEventListener("resize", fit);
  fit();
  poll();
})();
</script>
</body>
</html>
"""

TRACE_PAGE_HTML = _TEMPLATE.replace("__FACES__", json.dumps(TRACE_FACES))
