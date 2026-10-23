from __future__ import annotations

"""X1 X-Ray 大屏的页面本体（``GET /trace``）。

自包含：内联 CSS + 原生 JS，不引任何外部资源（路演现场可能没有外网）。
深色底、大字号——评委站在三米外看投影也要能读清"她为什么是这个情绪"。

页面自己从 URL 的 ``?token=`` 取令牌去轮询 ``/trace/data``，所以分享链接时
令牌会留在地址栏里；这正是它只用于现场投影、不进仓库文件的原因。
"""

TRACE_PAGE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lily X-Ray</title>
<style>
  :root {
    --bg: #0a0e14;
    --panel: #131a24;
    --line: #223041;
    --ink: #e8eef6;
    --dim: #8fa3b8;
    --accent: #ffb454;
    --good: #6fd08c;
    --bad: #ff7b72;
    --why: #7aa2f7;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; background: var(--bg); color: var(--ink); }
  body {
    font-family: "PingFang SC", "Microsoft YaHei", "Noto Sans CJK SC", system-ui, sans-serif;
    padding: 20px 24px 28px;
    min-height: 100vh;
  }
  header {
    display: flex; align-items: baseline; justify-content: space-between;
    border-bottom: 2px solid var(--line); padding-bottom: 10px; margin-bottom: 18px;
  }
  h1 { font-size: 30px; margin: 0; letter-spacing: 2px; }
  .clock { font-size: 22px; color: var(--dim); font-variant-numeric: tabular-nums; }
  .grid { display: grid; grid-template-columns: 5fr 6fr; gap: 18px; }
  .panel {
    background: var(--panel); border: 1px solid var(--line); border-radius: 12px;
    padding: 18px 20px;
  }
  .panel h2 {
    margin: 0 0 12px; font-size: 20px; color: var(--dim);
    letter-spacing: 3px; font-weight: 600;
  }
  .mood { font-size: 76px; line-height: 1.05; font-weight: 700; }
  .mood .intensity { font-size: 34px; color: var(--accent); margin-left: 14px; }
  .cause { font-size: 27px; margin-top: 10px; line-height: 1.35; }
  .chain {
    margin-top: 16px; padding-top: 14px; border-top: 1px dashed var(--line);
    font-size: 21px; color: var(--dim); line-height: 1.5;
  }
  .chain b { color: var(--accent); font-weight: 600; }
  .state { margin-top: 16px; display: flex; flex-wrap: wrap; gap: 10px 22px; }
  .state span { font-size: 22px; font-variant-numeric: tabular-nums; }
  .state span b { color: var(--good); font-weight: 600; }
  .ledger { max-height: 52vh; overflow-y: auto; }
  .row {
    display: grid; grid-template-columns: 92px 1fr; gap: 12px;
    padding: 10px 12px; border-radius: 8px; font-size: 21px; line-height: 1.4;
    border-left: 4px solid transparent;
  }
  .row + .row { margin-top: 6px; }
  .row .time { color: var(--dim); font-variant-numeric: tabular-nums; }
  .row .body .cause { font-size: 21px; margin: 0; }
  .row .body .changes { font-size: 19px; color: var(--dim); margin-top: 2px; }
  .row.new { background: #1d2b1f; border-left-color: var(--good); animation: flash 3s ease-out; }
  .row.why { border-left-color: var(--why); background: #16203a; }
  .row.why .why-tag { color: var(--why); font-size: 18px; margin-right: 8px; }
  @keyframes flash { from { background: #2f5d38; } to { background: #1d2b1f; } }
  .bottom { display: grid; grid-template-columns: 5fr 6fr; gap: 18px; margin-top: 18px; }
  .slot { display: flex; gap: 14px; font-size: 22px; padding: 7px 10px; border-radius: 8px; }
  .slot .t { color: var(--dim); font-variant-numeric: tabular-nums; min-width: 108px; }
  .slot .why { color: var(--dim); font-size: 19px; margin-left: 10px; }
  .slot.current { background: #2a2416; }
  .slot.current .t { color: var(--accent); }
  .note {
    font-size: 24px; padding: 10px 12px; border-radius: 8px;
    background: #1a2331; border: 1px solid var(--line);
  }
  .note + .note { margin-top: 8px; }
  .note .src { font-size: 17px; color: var(--dim); margin-right: 10px; }
  .empty { color: var(--dim); font-size: 21px; }
  .why-banner {
    margin-bottom: 12px; padding: 10px 14px; border-radius: 8px;
    background: #16203a; border: 1px solid var(--why); font-size: 21px; color: var(--why);
  }
  .err { color: var(--bad); font-size: 22px; }
</style>
</head>
<body>
<header>
  <h1>LILY X-RAY</h1>
  <div class="clock" id="clock">--:--:--</div>
</header>

<div class="grid">
  <section class="panel">
    <h2>现在的情绪</h2>
    <div class="mood" id="mood">—<span class="intensity" id="intensity"></span></div>
    <div class="cause" id="cause">正在读取生活账本…</div>
    <div class="chain" id="chain"></div>
    <div class="state" id="state"></div>
  </section>

  <section class="panel">
    <h2>生活账本流水</h2>
    <div class="why-banner" id="why-banner" hidden></div>
    <div class="ledger" id="ledger"><div class="empty">还没有流水</div></div>
  </section>
</div>

<div class="bottom">
  <section class="panel">
    <h2>今日日程</h2>
    <div id="plan"><div class="empty">今天没有安排</div></div>
  </section>
  <section class="panel">
    <h2>便签</h2>
    <div id="notes"><div class="empty">没有便签</div></div>
  </section>
</div>

<script>
(function () {
  "use strict";
  var TOKEN = new URLSearchParams(window.location.search).get("token") || "";
  var POLL_MS = 2000;
  var HIGHLIGHT_MS = 3000;
  var seen = {};
  var whyKeys = {};

  function el(id) { return document.getElementById(id); }
  function esc(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  function timeText(value) {
    if (!value) return "";
    var d = new Date(value * 1000);
    function pad(n) { return (n < 10 ? "0" : "") + n; }
    return pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
  }

  function renderClock(data) {
    el("clock").textContent = data.server_time_text || timeText(data.server_time);
  }

  function renderMood(data) {
    var e = data.emotion || {};
    el("mood").firstChild.nodeValue = e.label || e.emotion || "—";
    el("intensity").textContent = e.intensity ? "强度 " + e.intensity : "";
    el("cause").textContent = e.cause || "（还没有记录到原因）";
    var chain = (e.last_resolution || {}).chain || "";
    el("chain").innerHTML = chain ? "评估链：<b>" + esc(chain) + "</b>" : "评估链：这一轮还没定";
    var tracked = ((data.state || {}).tracked) || {};
    var names = { mood: "心情", energy: "体力", satiety: "饱腹", stress: "压力", trust: "信任" };
    var html = "";
    Object.keys(names).forEach(function (key) {
      if (tracked[key] === undefined) return;
      html += "<span>" + names[key] + " <b>" + esc(tracked[key]) + "</b></span>";
    });
    el("state").innerHTML = html;
  }

  function renderWhy(data) {
    var why = data.why || {};
    whyKeys = {};
    (why.event_keys || []).forEach(function (key) { whyKeys[key] = true; });
    var banner = el("why-banner");
    if (!why.event_keys || !why.event_keys.length) {
      banner.hidden = true;
      return;
    }
    banner.hidden = false;
    banner.textContent = "她刚被问：" + (why.question || "为什么") + " —— 高亮的账本行就是她的回答依据";
  }

  function renderLedger(data) {
    var box = el("ledger");
    var rows = data.ledger || [];
    if (!rows.length) {
      box.innerHTML = '<div class="empty">还没有流水</div>';
      return;
    }
    var html = "";
    rows.forEach(function (row) {
      var key = row.event_key || (row.time + "|" + row.title);
      var isNew = !seen[key];
      seen[key] = true;
      var isWhy = !!whyKeys[row.event_key];
      var cls = "row" + (isNew ? " new" : "") + (isWhy ? " why" : "");
      var stamp = (row.time || "").slice(11) || row.time || "--:--";
      html += '<div class="' + cls + '" data-key="' + esc(key) + '">'
        + '<div class="time">' + esc(stamp) + "</div>"
        + '<div class="body">'
        + '<div class="cause">' + (isWhy ? '<span class="why-tag">◀ 问她为什么</span>' : "")
        + esc(row.title || row.event_type || "") + (row.cause ? " · " + esc(row.cause) : "") + "</div>"
        + (row.changes && row.changes.length
            ? '<div class="changes">' + esc(row.changes.join("，")) + "</div>" : "")
        + "</div></div>";
    });
    box.innerHTML = html;
    var fresh = box.querySelectorAll(".row.new");
    Array.prototype.forEach.call(fresh, function (node) {
      window.setTimeout(function () { node.classList.remove("new"); }, HIGHLIGHT_MS);
    });
  }

  function renderPlan(data) {
    var box = el("plan");
    var slots = data.plan || [];
    if (!slots.length) {
      box.innerHTML = '<div class="empty">今天没有安排</div>';
      return;
    }
    var html = "";
    slots.forEach(function (slot) {
      var stamp = (slot.time || "").slice(11) || slot.time || "--:--";
      html += '<div class="slot' + (slot.current ? " current" : "") + '">'
        + '<div class="t">' + esc(stamp) + "</div>"
        + "<div>" + esc(slot.title || "")
        + (slot.why_text ? '<span class="why">' + esc(slot.why_text) + "</span>" : "")
        + "</div></div>";
    });
    box.innerHTML = html;
  }

  function renderNotes(data) {
    var box = el("notes");
    var notes = data.notes || [];
    if (!notes.length) {
      box.innerHTML = '<div class="empty">没有便签</div>';
      return;
    }
    var html = "";
    notes.forEach(function (note) {
      html += '<div class="note"><span class="src">'
        + esc(note.source === "agent" ? "后台结论" : "你说的") + "</span>"
        + esc(note.text || "") + "</div>";
    });
    box.innerHTML = html;
  }

  function tick() {
    fetch("/trace/data?token=" + encodeURIComponent(TOKEN), { cache: "no-store" })
      .then(function (res) {
        if (res.status === 401 || res.status === 403 || res.status === 404) {
          throw new Error("令牌不对（HTTP " + res.status + "）");
        }
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.json();
      })
      .then(function (data) {
        el("cause").classList.remove("err");
        renderClock(data);
        renderMood(data);
        renderWhy(data);
        renderLedger(data);
        renderPlan(data);
        renderNotes(data);
      })
      .catch(function (err) {
        el("cause").classList.add("err");
        el("cause").textContent = "读不到数据：" + err.message;
      });
  }

  tick();
  window.setInterval(tick, POLL_MS);
})();
</script>
</body>
</html>
"""
