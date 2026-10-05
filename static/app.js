/*
 * 朝夕 · 前端
 *
 * 三块拼在一起：
 *   今天   —— 关怀语 + 一整天的时间轴（日程 / 休息点 / 「现在」标尺）
 *   了解你 —— 状态、作息、日程、音乐偏好
 *   休息   —— 用 Tone.js 当场生成音乐，带频谱可视化
 *
 * 音乐部分不在这里：生成的活由 js/engine.js + js/soundscapes.js + js/theory.js
 * 干，这个文件只负责「什么时候放、放哪一段、界面上显示什么」。
 */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const pad2 = (n) => String(n).padStart(2, "0");
  const STORE_KEY = "zhaoxi.v1";

  // ── 通用小工具 ────────────────────────────────────────────

  const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ESCAPES[ch]);

  /** 带本地时区偏移的 ISO8601。后端的 datetime.fromisoformat 要求有时区。 */
  function toLocalIso(date) {
    const offset = -date.getTimezoneOffset();
    const sign = offset >= 0 ? "+" : "-";
    const abs = Math.abs(offset);
    return (
      `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}` +
      `T${pad2(date.getHours())}:${pad2(date.getMinutes())}:${pad2(date.getSeconds())}` +
      `${sign}${pad2(Math.floor(abs / 60))}:${pad2(abs % 60)}`
    );
  }

  const clockOf = (iso) => {
    const date = new Date(iso);
    return Number.isNaN(date.getTime()) ? "--:--" : `${pad2(date.getHours())}:${pad2(date.getMinutes())}`;
  };

  /** FNV-1a：把休息点 id 变成种子，于是同一个休息点每次都生成同一段音乐。 */
  function seedOf(text) {
    let hash = 2166136261;
    const value = String(text);
    for (let index = 0; index < value.length; index += 1) {
      hash ^= value.charCodeAt(index);
      hash = Math.imul(hash, 16777619);
    }
    return hash >>> 0;
  }

  /** 450 → 「7 小时 30 分」，读起来比「450 分钟」省力。 */
  function humanMinutes(total) {
    const value = Math.max(0, Math.round(Number(total) || 0));
    if (value < 60) return `${value} 分钟`;
    const hours = Math.floor(value / 60);
    const minutes = value % 60;
    return minutes ? `${hours} 小时 ${minutes} 分` : `${hours} 小时`;
  }

  async function api(path, { method = "GET", body } = {}) {
    const response = await fetch(path, {
      method,
      headers: body === undefined ? undefined : { "Content-Type": "application/json; charset=utf-8" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const text = await response.text();
    let data = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      data = null;
    }
    if (!response.ok) throw new Error((data && data.error) || `请求失败（${response.status}）`);
    return data;
  }

  let toastTimer = 0;
  function toast(message) {
    const node = $("toast");
    node.textContent = message;
    node.hidden = false;
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => {
      node.hidden = true;
    }, 2600);
  }

  // ── 本地留存的个人设置 ────────────────────────────────────
  // 只存「你告诉过我们的事」，日程和状态以后端为准（server 那边落了盘）。

  const prefs = {
    wake: "06:40",
    sleep: "23:00",
    chronotype: "",
    state: { energy: 60, stress: 30, focus: 55, sleep_hours: 7.5, sedentary_minutes: 60 },
    preferred: "",
  };

  function loadPrefs() {
    try {
      const raw = window.localStorage.getItem(STORE_KEY);
      if (!raw) return;
      const saved = JSON.parse(raw);
      if (saved && typeof saved === "object") {
        Object.assign(prefs, saved);
        if (saved.state && typeof saved.state === "object") Object.assign(prefs.state, saved.state);
      }
    } catch {
      /* 本地存储坏了不该让页面打不开，用默认值继续。 */
    }
  }

  function savePrefs() {
    try {
      window.localStorage.setItem(STORE_KEY, JSON.stringify(prefs));
    } catch {
      /* 隐私模式下写不进去，忽略即可。 */
    }
  }

  // ── 页面状态 ──────────────────────────────────────────────

  const app = {
    plan: null,
    events: [],
    catalog: [], // 后端 /api/soundscapes 返回的数组
    lastRequest: null, // 上一次播放的参数，暂停后接着放用
    breakContext: null, // 正在休息的那个休息点 id，用来给手动切换音景播种
  };

  // ── 视图切换 ──────────────────────────────────────────────

  function go(name) {
    for (const view of document.querySelectorAll(".view")) {
      const active = view.id === `view-${name}`;
      view.hidden = !active;
      view.classList.toggle("is-active", active);
    }
    for (const tab of document.querySelectorAll(".tab")) {
      const active = tab.dataset.goto === name;
      tab.classList.toggle("is-active", active);
      if (active) tab.setAttribute("aria-current", "page");
      else tab.removeAttribute("aria-current");
    }
    window.scrollTo(0, 0);
    if (name === "rest") startViz();
  }

  // ── 今天 ──────────────────────────────────────────────────

  const WEEKDAYS = ["日", "一", "二", "三", "四", "五", "六"];

  function dayLabel() {
    const now = new Date();
    return `${now.getMonth() + 1} 月 ${now.getDate()} 日 · 周${WEEKDAYS[now.getDay()]}`;
  }

  function phaseLabel(hour) {
    if (hour < 6) return "凌晨";
    if (hour < 11) return "上午";
    if (hour < 14) return "中午";
    if (hour < 18) return "下午";
    if (hour < 22) return "晚上";
    return "深夜";
  }

  function renderCare(care) {
    const node = $("care-card");
    node.dataset.tone = care.tone || "calm";
    $("care-when").textContent = `${dayLabel()} · ${phaseLabel(new Date().getHours())}`;
    $("care-line").textContent = care.greeting || "今天也在这里。";
    $("care-detail").textContent = care.detail || "";
    $("care-suggest").textContent = care.suggestion || "";
    $("care-detail").hidden = !care.detail;
  }

  function renderStats(stats) {
    const list = $("day-stats");
    if (!stats) {
      list.innerHTML = "";
      return;
    }
    // 用 protected_minutes（当天所有日程）而不是 focus_minutes（只算学习/工作）：
    // 后者是编排休息点的依据，但挂在「有安排的时间」这个标签下会和旁边那个
    // 「空档」加不拢——用户拿日程一算就会发现少了社团活动那一小时。
    const cells = [
      { value: humanMinutes(stats.protected_minutes), label: "有安排的时间" },
      { value: `${stats.break_count} 次`, label: `休息，共 ${humanMinutes(stats.break_minutes)}` },
      { value: humanMinutes(stats.free_minutes), label: "空档" },
    ];
    if (stats.last_event) cells.push({ value: stats.last_event, label: "最后一件事开始" });
    list.innerHTML = cells
      .map((cell) => `<li><b>${esc(cell.value)}</b><span>${esc(cell.label)}</span></li>`)
      .join("");
  }

  function scapeName(id) {
    const lib = window.MCSoundscapes;
    const entry = lib && typeof lib.get === "function" ? lib.get(id) : null;
    if (entry) return entry.name;
    const fromServer = app.catalog.find((item) => item.id === id);
    return fromServer ? fromServer.name : id || "—";
  }

  function eventRow(item) {
    const where = item.location ? ` · ${item.location}` : "";
    const span = `${clockOf(item.start)} – ${clockOf(item.end)}${where}`;
    return `
      <li class="tl-item">
        <span class="tl-time">${esc(clockOf(item.start))}</span>
        <div class="tl-body">
          <p class="tl-title">${esc(item.title)}</p>
          <p class="tl-meta">${esc(span)}</p>
        </div>
      </li>`;
  }

  function breakRow(item, past) {
    const done = item.status === "done";
    const meta = [scapeName(item.soundscape), item.bpm ? `${item.bpm} BPM` : ""]
      .filter(Boolean)
      .join(" · ");
    // 过去的休息点只留一个安静的补记入口（实心按钮由 CSS 隐掉）：
    // 那会儿的休息已经过去了，写成「已完成」像是在等一件不会发生的事。
    const actions = done
      ? `<button type="button" class="ghost-btn" data-break-reopen="${esc(item.id)}">还没完</button>`
      : `<button type="button" class="ghost-btn" data-break-done="${esc(item.id)}">${past ? "补记" : "已完成"}</button>
         <button type="button" class="solid-btn" data-break-play="${esc(item.id)}">开始休息</button>`;
    return `
      <li class="tl-item is-break${done ? " is-done" : ""}">
        <span class="tl-time">${esc(clockOf(item.start))}</span>
        <div class="tl-body">
          <div class="tl-break-top">
            <span class="tl-label">${esc(item.label || item.title)}</span>
            <span class="tl-duration">${esc(humanMinutes(item.duration_minutes))}</span>
          </div>
          ${item.reason ? `<p class="tl-reason">${esc(item.reason)}</p>` : ""}
          <div class="tl-break-foot">
            <span class="tl-scape">
              <svg class="icon" aria-hidden="true"><use href="#i-note" /></svg>${esc(meta)}
            </span>
            <div class="tl-actions">${actions}</div>
          </div>
        </div>
      </li>`;
  }

  function renderTimeline(plan) {
    const list = $("timeline");
    const items = (plan && plan.timeline) || [];
    $("timeline-empty").hidden = items.length > 0;
    if (!items.length) {
      list.innerHTML = "";
      return;
    }

    const now = new Date();
    let markerPlaced = false;
    const html = [];

    for (const item of items) {
      const start = new Date(item.start);
      const end = new Date(item.end);
      if (!markerPlaced && start > now) {
        html.push(
          `<li class="tl-now"><span>现在 ${esc(clockOf(now.toISOString()))}</span><i></i></li>`
        );
        markerPlaced = true;
      }
      const past = end < now;
      const row = item.kind === "break" ? breakRow(item, past) : eventRow(item);
      html.push(past ? row.replace('class="tl-item', 'class="tl-item is-past') : row);
    }
    if (!markerPlaced) {
      html.push(`<li class="tl-now"><span>现在 ${esc(clockOf(now.toISOString()))}</span><i></i></li>`);
    }
    list.innerHTML = html.join("");
  }

  function renderConflicts(conflicts) {
    const node = $("conflict-note");
    const list = conflicts || [];
    if (!list.length) {
      node.hidden = true;
      return;
    }
    node.hidden = false;
    node.textContent = `有 ${list.length} 处日程时间重叠，休息点已经避开它们。`;
  }

  async function loadDay() {
    const now = new Date();
    const dayStart = new Date(now);
    dayStart.setHours(6, 0, 0, 0);
    const dayEnd = new Date(now);
    dayEnd.setHours(23, 59, 0, 0);

    // events 省略不传：后端用它自己保存的日程，避免两份数据打架。
    const body = {
      now: toLocalIso(now),
      day_start: toLocalIso(dayStart),
      day_end: toLocalIso(dayEnd),
      state: Object.assign({ captured_at: toLocalIso(now), source: "manual" }, prefs.state),
      profile: {
        wake: prefs.wake,
        sleep: prefs.sleep,
        chronotype: prefs.chronotype,
      },
    };
    if (prefs.preferred) body.soundscape_hint = prefs.preferred;

    const plan = await api("/api/day", { method: "POST", body });
    app.plan = plan;
    renderCare(plan.care || {});
    renderStats(plan.stats);
    renderTimeline(plan);
    renderConflicts(plan.conflicts);
    return plan;
  }

  async function setBreakStatus(id, status) {
    await api("/api/day/break-status", { method: "POST", body: { id, status } });
    const item = app.plan && app.plan.timeline.find((entry) => entry.id === id);
    if (item) item.status = status;
    renderTimeline(app.plan);
  }

  // ── 了解你 ────────────────────────────────────────────────

  async function loadEvents() {
    const data = await api("/api/calendar/events");
    app.events = (data && data.events) || [];
    renderEvents();
  }

  function renderEvents() {
    const list = $("event-list");
    $("event-empty").hidden = app.events.length > 0;
    list.innerHTML = app.events
      .slice()
      .sort((a, b) => String(a.start).localeCompare(String(b.start)))
      .map(
        (item) => `
        <li class="event-item">
          <span class="event-when">${esc(clockOf(item.start))} – ${esc(clockOf(item.end))}</span>
          <span>${esc(item.title)}</span>
          <button type="button" class="icon-btn" data-event-del="${esc(item.id)}" aria-label="删除 ${esc(item.title)}">
            <svg class="icon" aria-hidden="true"><use href="#i-trash" /></svg>
          </button>
        </li>`
      )
      .join("");
  }

  function syncStateInputs() {
    const state = prefs.state;
    $("state-energy").value = state.energy;
    $("out-energy").textContent = state.energy;
    $("state-stress").value = state.stress;
    $("out-stress").textContent = state.stress;
    $("state-focus").value = state.focus;
    $("out-focus").textContent = state.focus;
    $("state-sleep").value = state.sleep_hours;
    $("state-sedentary").value = state.sedentary_minutes;
    $("profile-wake").value = prefs.wake;
    $("profile-sleep").value = prefs.sleep;
    for (const chip of document.querySelectorAll("#chronotype-chips .chip")) {
      const on = chip.dataset.chrono === prefs.chronotype;
      chip.classList.toggle("is-on", on);
      chip.setAttribute("aria-checked", on ? "true" : "false");
    }
  }

  function readStateInputs() {
    prefs.state = {
      energy: Number($("state-energy").value),
      stress: Number($("state-stress").value),
      focus: Number($("state-focus").value),
      sleep_hours: Number($("state-sleep").value) || 0,
      sedentary_minutes: Number($("state-sedentary").value) || 0,
    };
    prefs.wake = $("profile-wake").value || "06:40";
    prefs.sleep = $("profile-sleep").value || "23:00";
  }

  function renderPreferChips() {
    const box = $("prefer-chips");
    const list = (window.MCSoundscapes && window.MCSoundscapes.list) || [];
    const chips = [
      `<button type="button" class="chip${prefs.preferred ? "" : " is-on"}" data-prefer="">交给状态</button>`,
    ];
    for (const scape of list) {
      const on = prefs.preferred === scape.id;
      chips.push(
        `<button type="button" class="chip${on ? " is-on" : ""}" data-prefer="${esc(scape.id)}">${esc(scape.name)}</button>`
      );
    }
    box.innerHTML = chips.join("");
  }

  // ── 休息 ──────────────────────────────────────────────────
  // 音乐引擎是单例（window.MCEngine），这里只做「谁在放、放什么、显示什么」。

  const engine = window.MCEngine;

  function currentMeta() {
    if (!engine || !engine.recipe) return "";
    const recipe = engine.recipe;
    const parts = [
      `<b>${esc(recipe.soundscape.name)}</b>`,
      `${esc(String(recipe.bpm))} BPM`,
      `调性 ${esc(engine.key || "—")}`,
    ];
    const shift = typeof engine.describeShift === "function" ? engine.describeShift() : null;
    if (shift) {
      parts.push(shift.direction === "slower" ? `比常规慢 ${Math.abs(shift.delta)}` : `比常规快 ${shift.delta}`);
    }
    return parts.join(" · ");
  }

  function syncTransport() {
    const playing = Boolean(engine && engine.isPlaying());
    const button = $("play-btn");
    button.innerHTML = `<svg class="icon" aria-hidden="true"><use href="#i-${playing ? "pause" : "play"}" /></svg>`;
    button.setAttribute("aria-label", playing ? "暂停" : "开始播放");
    $("player-meta").innerHTML = playing ? currentMeta() : "按一下开始。音乐会一直生成下去，不会循环同一段。";

    const recipe = engine && engine.recipe;
    if (recipe) {
      const tempo = $("player-tempo");
      tempo.min = recipe.soundscape.bpm[0];
      tempo.max = recipe.soundscape.bpm[1];
      tempo.value = recipe.bpm;
      $("out-tempo").textContent = String(recipe.bpm);
      $("player-density").value = Math.round(recipe.intensity * 100);
      $("out-density").textContent = `${Math.round(recipe.intensity * 100)}%`;
      $("player-title").textContent = recipe.soundscape.name;
      $("player-blurb").textContent = recipe.soundscape.blurb || recipe.soundscape.character || "";
    }

    // 还没有曲子的时候，快慢/疏密没有可调的对象，读数也只是个「–」。
    // 把滑杆一并禁掉：能拖但拖了没反应，比灰着更像坏了。
    // 音量例外——它现在就有意义（先调好，一按播放就是这个响度）。
    $("player-tempo").disabled = !recipe;
    $("player-density").disabled = !recipe;

    // 一次都没放过的时候，频谱区是一条 92px 的空白——看着像没加载出来。
    // 收成一条细线，等真的开始出声再展开（这是整个页面上唯一一处动画，
    // 用在「声音来了」这件事上是值得的）。
    $("player").classList.toggle("is-idle", !recipe);

    for (const card of document.querySelectorAll(".scape")) {
      const on = Boolean(recipe && card.dataset.scape === recipe.soundscape.id);
      card.classList.toggle("is-on", on);
    }
  }

  async function playScape(id, options = {}) {
    if (!engine) {
      toast("音频引擎没加载起来，刷新一下试试");
      return;
    }
    app.lastRequest = {
      soundscapeId: id,
      bpm: options.bpm,
      intensity: options.intensity ?? 0.45,
      seed: options.seed ?? 0,
    };
    await engine.play(app.lastRequest, { crossfade: options.crossfade ?? 1.6 });
    if (options.context) {
      $("player-context").textContent = options.context;
    } else {
      $("player-context").textContent = "手动挑的";
    }
    syncTransport();
    startViz();
  }

  // when / moods 在数据里是英文 id（跟后端 soundscape.py 的清单对齐），
  // 直接渲染会漏出 "morning,afternoon,evening" 这种字符串——整页中文里
  // 夹一段英文机器值，是最容易露馅的地方。这里翻译成给人看的写法。
  const WHEN_LABEL = {
    morning: "早",
    afternoon: "午",
    evening: "晚",
    night: "夜",
    any: "全天",
  };

  function whenPhrase(when) {
    const slots = Array.isArray(when) ? when : [];
    if (!slots.length || slots.includes("any")) return "全天都好";
    return `${slots.map((slot) => WHEN_LABEL[slot] || slot).join(" / ")}时段`;
  }

  function renderScapes() {
    const list = (window.MCSoundscapes && window.MCSoundscapes.list) || [];
    $("scape-grid").innerHTML = list
      .map(
        (scape) => `
        <button type="button" class="scape" data-scape="${esc(scape.id)}">
          <span class="scape-name">${esc(scape.name)}</span>
          <span class="scape-char">${esc(scape.character || "")}</span>
          <span class="scape-when">${esc(whenPhrase(scape.when))} · <span class="scape-tempo">${scape.bpm[0]}–${scape.bpm[1]} BPM</span></span>
        </button>`
      )
      .join("");

    const stats = window.MCSoundscapes && window.MCSoundscapes.libraryStats();
    if (stats) {
      $("library-note").textContent = `${stats.soundscapes} 个音景 · ${stats.combinations} 种组合`;
    }
  }

  // 频谱可视化：64 段 dB 值映射成细柱，带一点回落阻尼，
  // 比直接画实时值耐看（否则每帧抖成噪点）。
  const viz = { raf: 0, running: false, bars: [], edges: [], bins: 0 };
  // 频谱只画到这条线左边——再往上的频段实测已经低于噪声底，画出来是空白。
  const TOP_FRACTION = 0.42;

  function drawViz() {
    const canvas = $("viz");
    if (!canvas) return;
    const width = canvas.clientWidth;
    const height = canvas.clientHeight;
    if (width < 4 || height < 4) return;

    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    if (canvas.width !== Math.round(width * dpr) || canvas.height !== Math.round(height * dpr)) {
      canvas.width = Math.round(width * dpr);
      canvas.height = Math.round(height * dpr);
    }
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);

    const spectrum = engine && typeof engine.getSpectrum === "function" ? engine.getSpectrum() : null;
    if (!spectrum || spectrum.length < 8) {
      ctx.strokeStyle = "rgba(30, 28, 25, 0.12)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(0, height - 0.5);
      ctx.lineTo(width, height - 0.5);
      ctx.stroke();
      return;
    }

    const bins = spectrum.length;
    const groups = Math.min(52, bins);

    // 两个真实的约束，都是量出来的，不是拍的：
    //   1. FFT 的 bin 是线性分频，而听感是对数的。按线性画，能量会全挤在
    //      左边一小撮。所以按 i^1.8 划分边界，低频窄、高频宽。
    //   2. 这些音色是电钢/铺底，实测 bin 11（约 1 kHz）往上就落进 -120 dB
    //      以下的噪声底了。若是照着整条 0–22 kHz 的轴画，一半以上的宽度会
    //      是一马平川。所以只呈现到 TOP_FRACTION 处，也就是真正有声音的那段。
    const topBin = Math.max(groups + 1, Math.round(bins * TOP_FRACTION));
    if (viz.edges.length !== groups + 1 || viz.bins !== bins) {
      const edges = [0];
      for (let index = 1; index <= groups; index += 1) {
        const target = Math.round(Math.pow(index / groups, 1.8) * (topBin - 1)) + 1;
        edges.push(Math.min(topBin, Math.max(edges[index - 1] + 1, target)));
      }
      viz.edges = edges;
      viz.bins = bins;
      viz.bars = new Array(groups).fill(0);
    }

    const gap = 2;
    const barWidth = Math.max(1.5, (width - gap * (groups - 1)) / groups);
    const FLOOR_DB = -145;
    const SPAN_DB = 110;

    for (let index = 0; index < groups; index += 1) {
      let peak = -Infinity;
      for (let slot = viz.edges[index]; slot < viz.edges[index + 1]; slot += 1) {
        if (Number.isFinite(spectrum[slot])) peak = Math.max(peak, spectrum[slot]);
      }
      // 上下限来自实测：静音段约 -150 dB，最响的 bin 到过 -35 dB 左右。
      // 早先按 -92 当下限，结果一半以上的柱子被夹成 0。
      const level = Number.isFinite(peak)
        ? Math.max(0, Math.min(1, (peak - FLOOR_DB) / SPAN_DB))
        : 0;
      // 上冲要快、回落要慢，视觉上才像「声音在动」而不是噪声。
      viz.bars[index] = level > viz.bars[index] ? level : viz.bars[index] * 0.86 + level * 0.14;
    }

    const gradient = ctx.createLinearGradient(0, height, 0, 0);
    gradient.addColorStop(0, "rgba(168, 85, 47, 0.28)");
    gradient.addColorStop(1, "rgba(168, 85, 47, 0.82)");
    ctx.fillStyle = gradient;

    for (let index = 0; index < groups; index += 1) {
      const barHeight = Math.max(2, viz.bars[index] * (height - 6));
      const x = index * (barWidth + gap);
      const y = height - barHeight;
      ctx.beginPath();
      if (typeof ctx.roundRect === "function") ctx.roundRect(x, y, barWidth, barHeight, barWidth / 2);
      else ctx.rect(x, y, barWidth, barHeight);
      ctx.fill();
    }
  }

  function startViz() {
    if (viz.running) return;
    viz.running = true;
    const tick = () => {
      drawViz();
      viz.raf = window.requestAnimationFrame(tick);
    };
    viz.raf = window.requestAnimationFrame(tick);
  }

  function stopViz() {
    viz.running = false;
    window.cancelAnimationFrame(viz.raf);
  }

  // ── 事件绑定 ──────────────────────────────────────────────

  function bind() {
    for (const trigger of document.querySelectorAll("[data-goto]")) {
      trigger.addEventListener("click", () => go(trigger.dataset.goto));
    }

    // 今天
    $("refresh-day").addEventListener("click", async () => {
      const button = $("refresh-day");
      button.disabled = true;
      try {
        await loadDay();
        toast("按现在的日程重排好了");
      } catch (error) {
        toast(error.message);
      } finally {
        button.disabled = false;
      }
    });

    $("timeline").addEventListener("click", async (event) => {
      const play = event.target.closest("[data-break-play]");
      const done = event.target.closest("[data-break-done]");
      const reopen = event.target.closest("[data-break-reopen]");

      if (play) {
        const id = play.dataset.breakPlay;
        const item = app.plan.timeline.find((entry) => entry.id === id);
        if (!item) return;
        go("rest");
        app.breakContext = id;
        try {
          await playScape(item.soundscape, {
            bpm: item.bpm || undefined,
            intensity: typeof item.intensity === "string" && item.intensity.includes("低") ? 0.35 : 0.55,
            seed: seedOf(id) % 997,
            context: `${clockOf(item.start)} 的休息 · ${item.label || item.title}`,
          });
          await setBreakStatus(id, "started");
        } catch (error) {
          toast(error.message);
        }
        return;
      }
      if (done || reopen) {
        const id = (done || reopen).dataset[done ? "breakDone" : "breakReopen"];
        try {
          await setBreakStatus(id, done ? "done" : "planned");
        } catch (error) {
          toast(error.message);
        }
      }
    });

    // 了解你：滑杆
    for (const [inputId, outputId] of [
      ["state-energy", "out-energy"],
      ["state-stress", "out-stress"],
      ["state-focus", "out-focus"],
    ]) {
      $(inputId).addEventListener("input", () => {
        $(outputId).textContent = $(inputId).value;
      });
    }

    $("chronotype-chips").addEventListener("click", (event) => {
      const chip = event.target.closest(".chip");
      if (!chip) return;
      prefs.chronotype = chip.dataset.chrono;
      for (const other of $("chronotype-chips").querySelectorAll(".chip")) {
        const on = other === chip;
        other.classList.toggle("is-on", on);
        other.setAttribute("aria-checked", on ? "true" : "false");
      }
    });

    $("prefer-chips").addEventListener("click", (event) => {
      const chip = event.target.closest(".chip");
      if (!chip) return;
      prefs.preferred = chip.dataset.prefer;
      renderPreferChips();
    });

    // 了解你：加一条日程
    $("event-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const error = $("event-error");
      error.hidden = true;
      const title = $("event-title").value.trim();
      const start = $("event-start").value;
      const end = $("event-end").value;
      if (!title || !start || !end) {
        error.textContent = "标题和起止时间都要填。";
        error.hidden = false;
        return;
      }
      if (end <= start) {
        error.textContent = "结束时间要晚于开始时间。";
        error.hidden = false;
        return;
      }
      const today = new Date();
      const [sh, sm] = start.split(":").map(Number);
      const [eh, em] = end.split(":").map(Number);
      const startDate = new Date(today);
      startDate.setHours(sh, sm, 0, 0);
      const endDate = new Date(today);
      endDate.setHours(eh, em, 0, 0);

      try {
        await api("/api/calendar/events", {
          method: "POST",
          body: {
            id: `manual-${Date.now().toString(36)}`,
            title,
            start: toLocalIso(startDate),
            end: toLocalIso(endDate),
            category: $("event-category").value,
            source: "manual",
          },
        });
        $("event-form").reset();
        await loadEvents();
        toast("加进去了");
      } catch (err) {
        error.textContent = err.message;
        error.hidden = false;
      }
    });

    $("event-list").addEventListener("click", async (event) => {
      const button = event.target.closest("[data-event-del]");
      if (!button) return;
      try {
        await api(`/api/calendar/events/${encodeURIComponent(button.dataset.eventDel)}`, { method: "DELETE" });
        await loadEvents();
      } catch (error) {
        toast(error.message);
      }
    });

    // 了解你：导入 .ics
    $("ics-file").addEventListener("change", async (event) => {
      const file = event.target.files && event.target.files[0];
      if (!file) return;
      try {
        const content = await file.text();
        const result = await api("/api/calendar/import-ics", { method: "POST", body: { content } });
        await loadEvents();
        toast(`导入了 ${result.imported} 条日程`);
      } catch (error) {
        toast(error.message);
      } finally {
        event.target.value = "";
      }
    });

    // 了解你：保存
    $("save-profile-btn").addEventListener("click", async () => {
      const button = $("save-profile-btn");
      button.disabled = true;
      readStateInputs();
      savePrefs();
      try {
        // 状态同时存到后端一份，换浏览器打开也还在。
        await api("/api/state/manual", {
          method: "POST",
          body: Object.assign(
            { captured_at: toLocalIso(new Date()), source: "manual" },
            prefs.state
          ),
        });
        await loadDay();
        $("save-hint").textContent = "已保存";
        window.setTimeout(() => {
          $("save-hint").textContent = "";
        }, 2200);
        go("today");
      } catch (error) {
        toast(error.message);
      } finally {
        button.disabled = false;
      }
    });

    // 休息：播放 / 暂停 / 换一段
    $("play-btn").addEventListener("click", async () => {
      if (!engine) return;
      if (engine.isPlaying()) {
        await engine.stop(0.9);
        syncTransport();
        $("player-context").textContent = "已停下";
        return;
      }
      const last = app.lastRequest;
      try {
        if (last) {
          await playScape(last.soundscapeId, {
            bpm: last.bpm,
            intensity: last.intensity,
            seed: last.seed,
          });
        } else {
          const fallback = (app.plan && app.plan.stats && app.plan.stats.soundscape) || undefined;
          await playScape(fallback, {});
        }
      } catch (error) {
        toast(error.message);
      }
    });

    $("shuffle-btn").addEventListener("click", async () => {
      if (!engine || !engine.recipe) {
        toast("先选一个音景");
        return;
      }
      engine.shuffle();
      syncTransport();
    });

    $("player-tempo").addEventListener("input", () => {
      if (!engine || !engine.recipe) return;
      engine.setBpm(Number($("player-tempo").value));
      $("out-tempo").textContent = String(engine.recipe.bpm);
      syncTransport();
    });

    $("player-density").addEventListener("input", () => {
      if (!engine || !engine.recipe) return;
      const value = Number($("player-density").value) / 100;
      engine.setIntensity(value);
      $("out-density").textContent = `${Math.round(value * 100)}%`;
    });

    $("player-volume").addEventListener("input", () => {
      if (!engine) return;
      const value = Number($("player-volume").value) / 100;
      engine.setVolume(value);
      $("out-volume").textContent = String(Math.round(value * 100));
    });

    $("scape-grid").addEventListener("click", async (event) => {
      const card = event.target.closest("[data-scape]");
      if (!card) return;
      const seed = app.breakContext ? seedOf(app.breakContext) % 997 : 0;
      try {
        await playScape(card.dataset.scape, { seed });
      } catch (error) {
        toast(error.message);
      }
    });

    // 休息：让 AI 生成一段（可选路径，没配 key 会自动回落到本地循环）
    $("ai-gen-btn").addEventListener("click", async () => {
      const button = $("ai-gen-btn");
      const status = $("ai-status");
      button.disabled = true;
      status.textContent = "正在生成，可能要等十几秒…";
      try {
        const response = await fetch("/api/generate-ai-audio", {
          method: "POST",
          headers: { "Content-Type": "application/json; charset=utf-8" },
          body: JSON.stringify({
            target_bpm: engine && engine.recipe ? engine.recipe.bpm : 80,
            duration_seconds: Number($("ai-duration").value) || 30,
            music_style: $("ai-style").value.trim() || "安静、温暖的纯音乐",
            genres: ["治愈纯音乐"],
            instruments: ["钢琴"],
            vocal_mode: "纯音乐 / 无人声",
            scene: "休息",
          }),
        });
        if (!response.ok) {
          const text = await response.text();
          let message = `生成失败（${response.status}）`;
          try {
            message = JSON.parse(text).error || message;
          } catch {
            /* 保留默认文案 */
          }
          throw new Error(message);
        }
        const blob = await response.blob();
        const audio = $("ai-audio");
        if (audio.dataset.url) URL.revokeObjectURL(audio.dataset.url);
        const url = URL.createObjectURL(blob);
        audio.dataset.url = url;
        audio.src = url;
        audio.hidden = false;
        const source = response.headers.get("X-Music-Source") || "";
        status.textContent =
          source === "ai" ? "生成好了，在下面播放。" : "AI 暂时不可用，已改用本地循环。";
      } catch (error) {
        status.textContent = error.message;
      } finally {
        button.disabled = false;
      }
    });

    // 清除
    $("clear-all-btn").addEventListener("click", async () => {
      if (!window.confirm("会删掉本机保存的日程、状态和偏好，确定吗？")) return;
      try {
        window.localStorage.removeItem(STORE_KEY);
        for (const item of app.events) {
          await api(`/api/calendar/events/${encodeURIComponent(item.id)}`, { method: "DELETE" });
        }
        window.location.reload();
      } catch (error) {
        toast(error.message);
      }
    });

    // 标签页切走时停掉可视化循环，省电
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) stopViz();
      else if (!$("view-rest").hidden) startViz();
    });

    window.addEventListener("resize", () => {
      if (!$("view-rest").hidden) drawViz();
    });
  }

  // ── 启动 ──────────────────────────────────────────────────

  async function boot() {
    loadPrefs();
    syncStateInputs();
    renderPreferChips();
    renderScapes();
    bind();
    syncTransport();

    try {
      app.catalog = await api("/api/soundscapes");
    } catch {
      app.catalog = [];
    }
    try {
      await loadEvents();
    } catch (error) {
      toast(error.message);
    }
    try {
      await loadDay();
    } catch (error) {
      $("care-line").textContent = "今天的数据没读上来。";
      $("care-detail").textContent = error.message;
    }
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
