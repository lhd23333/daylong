/*
 * 朝夕 · 前端
 *
 * 四块拼在一起：
 *   对话   —— 一层 agent 入口：说一段话，它排成日程并配好音乐
 *   今天   —— 关怀语 + 一整天的时间轴（日程 / 休息点 / 「现在」标尺）
 *   了解你 —— 状态、作息、日程、音乐偏好
 *   休息   —— 用 Tone.js 当场生成音乐，带频谱可视化（自主微调都在这页）
 *
 * 「对话」是入口，「休息」是手动那一层：同一件事既能一句话说清楚，也能自己
 * 拧每一个旋钮——两层共用同一个引擎和同一份配方，不存在「AI 版」和「手动版」
 * 两套音乐。
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
    favorites: [], // 收藏的配方（从 /api/playlist 拉回来）
    driver: null, // 粒子驱动规则 id；null = 用 particles.js 的默认值
    reminder: null, // daymusic 的提醒调度器，第一次渲染时间轴时建
    reminderItem: null, // 当前弹在卡片上的那条日程
    reminderRecipe: null, // 它的配方，点「先听一段」时用
    routeQueue: null, // 今天的声音路线：整天节目单（dayqueue.build 的结果）
    routeStaging: false, // 路线自己正在换段——此时的手动播放拦截要放行
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

  /**
   * 一条日程配到的旋律。算一次很便宜（几个哈希），但一屏要渲染十几条，
   * 而且渲染会被「完成休息」之类的操作反复触发，所以按内容缓存。
   * 缓存键带上状态值：用户改了当天的精力/压力，配出来的曲子本就该跟着变。
   */
  const melodyCache = new Map();

  function melodyFor(item) {
    if (!window.MCDayMusic || !item || item.kind !== "event") return null;
    const state = prefs.state || {};
    const key = [item.id, item.start, item.end, item.title, state.energy, state.stress].join("|");
    if (!melodyCache.has(key)) {
      melodyCache.set(
        key,
        window.MCDayMusic.recipeFor(item, {
          date: String(item.start || "").slice(0, 10),
          energy: Number(state.energy),
          stress: Number(state.stress),
        })
      );
    }
    return melodyCache.get(key);
  }

  function eventRow(item) {
    const where = item.location ? ` · ${item.location}` : "";
    const span = `${clockOf(item.start)} – ${clockOf(item.end)}${where}`;
    const melody = melodyFor(item);
    // 旋律这一行只在真有配方时出现。window.MCSoundscapes 没加载起来的话，
    // 日程照常显示，只是没有配乐入口——不能因为音乐模块缺席就让日程看不见。
    const melodyRow = melody
      ? `<div class="tl-break-foot">
           <span class="tl-scape">
             <svg class="icon" aria-hidden="true"><use href="#i-note" /></svg>${esc(melody.soundscapeName)} · ${melody.bpm} BPM
           </span>
           <div class="tl-actions">
             <button type="button" class="ghost-btn" data-melody-play="${esc(item.id)}">放这首</button>
           </div>
         </div>`
      : "";
    return `
      <li class="tl-item">
        <span class="tl-time">${esc(clockOf(item.start))}</span>
        <div class="tl-body">
          <p class="tl-title">${esc(item.title)}</p>
          <p class="tl-meta">${esc(span)}</p>
          ${melodyRow}
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
      // 空日程也要把监听列表清掉，否则昨天那批条目会留在调度器里，
      // 明天到点提醒一件今天不存在的事。
      syncReminder([]);
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
    syncReminder(items);
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
    renderDayRoute(plan);
    renderTimeline(plan);
    renderConflicts(plan.conflicts);
    return plan;
  }

  // ── 提前提醒 ──────────────────────────────────────────────
  //
  // daymusic.js 负责「什么时候该响」，这里负责「响了长什么样」。分开是因为
  // 前者是纯逻辑（可以单测、不碰 DOM），后者纯展示。
  //
  // 只在页面开着的时候有效：网页关掉没有后台能力。这是形态的边界，使用说明
  // 里写明了，不假装自己是常驻 App。

  function syncReminder(items) {
    if (!window.MCDayMusic) return;
    const watchable = (items || []).filter((item) => item && item.id && item.start);
    if (!app.reminder) {
      app.reminder = window.MCDayMusic.createReminder({
        items: watchable,
        onDue: showReminder,
      }).start();
    } else {
      app.reminder.setItems(watchable);
    }
  }

  function showReminder(item, minutesLeft) {
    const recipe = melodyFor(item);
    app.reminderItem = item;
    app.reminderRecipe = recipe;
    $("reminder-when").textContent = `还有 ${Math.max(1, Math.round(minutesLeft))} 分钟`;
    $("reminder-title").textContent = item.title || item.label || "下一件事";
    // 说理句是「程序推导」和「玄学配乐」的分界线：为什么这会儿放这个，
    // 就写在提醒卡上，用户不用猜。
    $("reminder-why").textContent = recipe ? recipe.why : "";
    $("reminder-play").hidden = !recipe;
    $("reminder").hidden = false;
  }

  /** 把某个条目的旋律放到休息页去播。提醒卡和时间轴上的按钮共用这条路。 */
  async function playMelodyOf(item, label) {
    const recipe = melodyFor(item);
    if (!recipe) {
      toast("这段日程没配上旋律");
      return;
    }
    go("rest");
    app.breakContext = null; // 这是日程配乐，不是休息点，别让休息点的种子串进来
    try {
      await playScape(recipe.soundscapeId, {
        bpm: recipe.bpm,
        density: recipe.density,
        key: recipe.key,
        progressionIndex: recipe.progressionIndex,
        drums: recipe.drums,
        seed: recipe.seed,
        context: label,
      });
    } catch (error) {
      toast(error.message);
    }
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
  const SCAPES = window.MCSoundscapes;
  // 「今天的声音路线」的连播控制器。懒创建（见 routePlayer）——没用过这条
  // 路线的人，页面上不会多出一个订阅着引擎事件的空控制器。
  let dayPlayer = null;
  // 节拍/节奏量的全局硬边界。取不到就退回一份写死的同值副本，别让整页挂掉。
  const LIMITS = (SCAPES && SCAPES.LIMITS) || { bpm: [40, 200], density: [0, 1] };

  function currentMeta() {
    if (!engine || !engine.recipe) return "";
    const recipe = engine.recipe;
    // 刻意不再重复音景名——它就在正上方的标题里，同一张卡片上说两遍。
    // 省下的这几个字在窄屏上正好够让这一行不折成三行。
    const parts = [
      `<b>${esc(String(recipe.bpm))} BPM</b>`,
      `节奏量 ${Math.round(recipe.density * 100)}%`,
      `调性 ${esc(engine.key || "—")}`,
    ];
    const shift = typeof engine.describeShift === "function" ? engine.describeShift() : null;
    if (shift) {
      parts.push(
        shift.direction === "slower"
          ? `比推荐慢 ${Math.abs(shift.delta)}`
          : `比推荐快 ${shift.delta}`
      );
    }
    return parts.join(" · ");
  }

  /**
   * 把推荐区间画到滑杆轨道的对应位置上（CSS 读 --band-from / --band-to）。
   *
   * 坐标用滑杆自己的 min/max 换算，所以节拍从「各音景的窄区间」放开到
   * 40–200 之后这里不用跟着改。传 null 表示摘掉区间带。
   */
  function paintBand(id, band) {
    const input = $(id);
    if (!input) return;
    const low = Number(input.min);
    const high = Number(input.max);
    if (!band || !(high > low)) {
      input.removeAttribute("data-band");
      return;
    }
    const at = (value) => `${((value - low) / (high - low)) * 100}%`;
    input.style.setProperty("--band-from", at(band[0]));
    input.style.setProperty("--band-to", at(band[1]));
    input.setAttribute("data-band", "");
  }

  /**
   * 超出推荐区间时的说明。
   *
   * 刻意不拦着拖动、也不把值夹回去——解耦之后区间外本来就合法。这条提示
   * 只负责讲清楚「这段音乐本来是写给另一个速度的」，剩下的交给耳朵。
   */
  function updateBandHint(recipe) {
    const hint = $("band-hint");
    if (!hint) return;
    if (!recipe || !SCAPES || typeof SCAPES.recommendFor !== "function") {
      hint.hidden = true;
      return;
    }
    const band = SCAPES.recommendFor(recipe.soundscape.id);
    const notes = [];
    if (recipe.bpm < band.bpm[0]) notes.push(`节拍比它常用的 ${band.bpm[0]} 还慢`);
    else if (recipe.bpm > band.bpm[1]) notes.push(`节拍超出了它常用的 ${band.bpm[1]}`);
    if (recipe.density < band.density[0]) notes.push("节奏量比它平时更疏");
    else if (recipe.density > band.density[1]) notes.push("节奏量比它平时更密");
    if (!notes.length) {
      hint.hidden = true;
      return;
    }
    hint.hidden = false;
    hint.textContent = `${notes.join("，")}。这样也能放——只是「${recipe.soundscape.name}」原本不是为这个速度写的，可能会飘。`;
  }

  function syncTransport() {
    const playing = Boolean(engine && engine.isPlaying());
    const button = $("play-btn");
    button.innerHTML = `<svg class="icon" aria-hidden="true"><use href="#i-${playing ? "pause" : "play"}" /></svg>`;
    button.setAttribute("aria-label", playing ? "暂停" : "开始播放");
    $("player-meta").innerHTML = playing
      ? currentMeta()
      : "按一下开始。音乐会一直生成下去，不会循环同一段。";

    const recipe = engine && engine.recipe;
    const tempo = $("player-tempo");
    const density = $("player-density");

    if (recipe) {
      tempo.min = LIMITS.bpm[0];
      tempo.max = LIMITS.bpm[1];
      tempo.value = recipe.bpm;
      $("out-tempo").textContent = String(recipe.bpm);
      density.value = Math.round(recipe.density * 100);
      $("out-density").textContent = `${Math.round(recipe.density * 100)}%`;
      $("player-title").textContent = recipe.soundscape.name;
      $("player-blurb").textContent =
        recipe.soundscape.blurb || recipe.soundscape.character || "";

      const band = SCAPES.recommendFor(recipe.soundscape.id);
      paintBand("player-tempo", band.bpm);
      paintBand("player-density", [band.density[0] * 100, band.density[1] * 100]);

      $("disc-bpm").textContent = String(recipe.bpm);
      $("disc-key").textContent = engine.key ? `${engine.key} 调` : "";
    } else {
      // 没选音景时摘掉区间带，免得留着上一个音景的暖色段骗人。
      paintBand("player-tempo", null);
      paintBand("player-density", null);
      $("disc-bpm").textContent = "–";
      $("disc-key").textContent = "";
    }

    // 还没有曲子的时候，节拍/节奏量没有可调的对象，读数也只是个「–」。
    // 把滑杆一并禁掉：能拖但拖了没反应，比灰着更像坏了。
    // 音量例外——它现在就有意义（先调好，一按播放就是这个响度）。
    tempo.disabled = !recipe;
    density.disabled = !recipe;

    updateBandHint(recipe);
    $("player").classList.toggle("is-idle", !recipe);
    // 粒子只在真的出声时显出来。CSS 里挂的也是这个类，两边别写岔。
    $("player").classList.toggle("is-playing", playing);
    syncFavButton();

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
    // 路线在走的时候，任何**手动**播放都视为「我要听这个」：退出节目单，
    // 方向盘还给用户。routeStaging 是路线自己换段时打的标记，那种不算手动。
    if (dayPlayer && dayPlayer.isActive() && !app.routeStaging) {
      dayPlayer.stop("manual");
    }
    app.lastRequest = {
      soundscapeId: id,
      bpm: options.bpm,
      density: options.density,
      intensity: options.intensity ?? 0.45,
      seed: options.seed ?? 0,
      key: options.key ?? null,
      progressionIndex: options.progressionIndex ?? null,
      drums: options.drums ?? null,
      // 上下文行也存下来：暂停再恢复时，「为哪一刻放的」这句话不能丢
      // （丢了会退回「手动挑的」，连播时和连播条上的段号自相矛盾）。
      context: options.context || "手动挑的",
    };
    await engine.play(app.lastRequest, { crossfade: options.crossfade ?? 1.6 });
    $("player-context").textContent = options.context || "手动挑的";
    renderTuning();
    syncTransport();
    startViz();
  }

  /**
   * 把引擎当前的状态拼成一次 play() 请求。
   *
   * 暂停之后再按播放必须走这里，不能直接复用 app.lastRequest——用户可能
   * 在暂停前刚拖过节拍、换过调性，复用旧请求会把那些改动悄悄回滚。
   */
  function currentRequest() {
    const recipe = engine && engine.recipe;
    if (!recipe) return app.lastRequest || {};
    return {
      soundscapeId: recipe.soundscape.id,
      bpm: recipe.bpm,
      density: recipe.density,
      intensity: recipe.intensity,
      seed: recipe.seed,
      key: recipe.key,
      progressionIndex: recipe.progressionIndex,
      drums: recipe.drums,
    };
  }

  // ── 今天的声音路线 ────────────────────────────────────────
  //
  // dayqueue.js 管节目单和推进逻辑，这里只做接线：谁在放、界面上显示什么、
  // 手动操作怎么让路。节目单在 loadDay 时随计划一起刷新，不用用户手动重建。

  function routePlayer() {
    if (dayPlayer || !window.MCDayQueue) return dayPlayer;
    dayPlayer = window.MCDayQueue.createPlayer({
      engine,
      play: async (segment, meta) => {
        app.routeStaging = true;
        try {
          await playScape(segment.request.soundscapeId, {
            ...segment.request,
            // 上下文行直接告诉用户「这一段是为哪一刻放的」——节目单和随机
            // 播放器的区别全在这一行里。
            context: `今天的声音路线 ${meta.index + 1}/${meta.count} · ${segment.clock} ${segment.label}`,
          });
        } finally {
          app.routeStaging = false;
        }
      },
      onSegment: renderRouteBar,
      onEnd: (reason) => {
        renderRouteBar();
        if (reason === "ended") toast("今天的声音路线走完了。");
      },
    });
    return dayPlayer;
  }

  /** 休息页的连播条。路线没在走就整条收起来。 */
  function renderRouteBar() {
    const bar = $("route-bar");
    if (!bar) return;
    const state = dayPlayer && dayPlayer.state();
    if (!state || !state.active) {
      bar.hidden = true;
      return;
    }
    bar.hidden = false;
    $("route-pos").textContent = `今天的声音路线 · ${state.index + 1}/${state.count}`;
    $("route-seg").textContent = `${state.segment.clock} ${state.segment.label}`;
  }

  /** 今天页的路线卡片：摘要 + 前几段预览。段数太少就不值得连播，整卡隐藏。 */
  function renderDayRoute(plan) {
    const card = $("day-route");
    if (!card) return;
    if (!window.MCDayQueue) {
      card.hidden = true;
      return;
    }
    const state = prefs.state || {};
    // 不传 date：让每条用自己的开始日期算种子，和时间轴上「放这首」的
    // 配方逐字一致——同一条日程从哪进听到的都是同一段音乐。
    const queue = window.MCDayQueue.build(plan && plan.timeline, {
      energy: Number(state.energy),
      stress: Number(state.stress),
    });
    app.routeQueue = queue;
    if (queue.segments.length < 2) {
      card.hidden = true;
      return;
    }
    card.hidden = false;
    const minutes = Math.max(1, Math.round(queue.totalSeconds / 60));
    $("route-note").textContent =
      `从早到晚 ${queue.segments.length} 段串成一条，每段一两分钟自动接上，约 ${minutes} 分钟走完。`;

    const head = queue.segments.slice(0, 3);
    const rows = head.map(
      (segment) =>
        `<li><b>${esc(segment.clock)}</b><span>${esc(segment.label)} · ${esc(
          scapeName(segment.request.soundscapeId)
        )}</span></li>`
    );
    const rest = queue.segments.length - head.length;
    if (rest > 0) rows.push(`<li class="route-more"><span>还有 ${rest} 段</span></li>`);
    $("route-preview").innerHTML = rows.join("");
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

    const stats = SCAPES && SCAPES.libraryStats();
    if (stats) {
      // 组合数按**全局**节拍档位（40–200 共 161 档）算，不再是各音景自己的
      // 窄区间——解耦之后速度本来就能拖到推荐区间外，按推荐区间计数会小看自己。
      $("library-note").textContent =
        `${stats.soundscapes} 个音景 · ${stats.progressions} 条进行 · ` +
        `${stats.combinations} 种组合（${stats.bpm[0]}–${stats.bpm[1]} BPM 任意搭配）`;
    }
  }

  // ── 光碟 + 外圈频谱 ───────────────────────────────────────
  //
  // 频谱绕成一圈而不是拉成一条：播放器中间本来就该有个东西在转，而一条
  // 92px 的横条只占地方。绕成环之后它既是那个「在转的东西」，也仍然是真的
  // 频谱——读的是同一个 engine.getSpectrum()，dB 映射沿用原来那套实测参数。
  //
  // 碟的转速跟 BPM 走，一小节转 1/8 圈，于是「快」是看得出来的。

  const viz = {
    raf: 0,
    running: false,
    bars: [],
    edges: [],
    bins: 0,
    angle: 0,
    lastAt: 0,
    particles: null,
    features: null,
    paused: false,
  };

  // 频谱只画到这条线左边——再往上的频段实测已经低于噪声底，画出来是空白。
  const TOP_FRACTION = 0.42;
  // 上下限来自实测：静音段约 -150 dB，最响的 bin 到过 -35 dB 左右。
  // 早先按 -92 当下限，结果一半以上的柱子被夹成 0。
  const FLOOR_DB = -145;
  const SPAN_DB = 110;
  // 环上一圈画多少根柱子。比原来横条的 52 根多：围成一圈之后每根之间的
  // 弧长本来就比横向间距短，根数太少会连成一片锯齿。
  const RING_GROUPS = 96;
  const CLAY_RGB = "168, 85, 47";

  function drawDisc(now) {
    const canvas = $("disc");
    if (!canvas) return;
    const size = canvas.clientWidth;
    if (size < 40) return;

    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    if (canvas.width !== Math.round(size * dpr)) {
      canvas.width = Math.round(size * dpr);
      canvas.height = Math.round(size * dpr);
    }
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, size, size);

    const center = size / 2;
    const playing = Boolean(engine && engine.isPlaying());

    // 转速：一小节转 1/8 圈。60 BPM 时一小节 4 秒，一圈就是 32 秒——慢到
    // 几乎察觉不到在动，但盯着看确实在转。转太快会吵，这是刻意压下来的。
    const bpm = engine && engine.recipe ? engine.recipe.bpm : 80;
    const dt = viz.lastAt ? Math.min(0.1, (now - viz.lastAt) / 1000) : 0;
    viz.lastAt = now;
    if (playing && Number.isFinite(dt)) {
      viz.angle += ((dt * bpm) / 60) * ((Math.PI * 2) / 8);
    }

    const ringInner = size * 0.36;
    const ringMax = size * 0.11;

    ctx.save();
    ctx.translate(center, center);
    ctx.rotate(viz.angle);

    // 碟面：一圈很淡的径向渐变，中心提亮。
    const face = ctx.createRadialGradient(0, 0, ringInner * 0.18, 0, 0, ringInner);
    face.addColorStop(0, "rgba(30, 28, 25, 0.045)");
    face.addColorStop(0.75, "rgba(30, 28, 25, 0.03)");
    face.addColorStop(1, "rgba(30, 28, 25, 0.075)");
    ctx.beginPath();
    ctx.arc(0, 0, ringInner, 0, Math.PI * 2);
    ctx.fillStyle = face;
    ctx.fill();

    // 音轨：几圈细同心圆。「光碟」这个意象全靠它们，没有就是个大圆。
    ctx.strokeStyle = "rgba(30, 28, 25, 0.08)";
    ctx.lineWidth = 1;
    for (let ratio = 0.42; ratio < 0.99; ratio += 0.115) {
      ctx.beginPath();
      ctx.arc(0, 0, ringInner * ratio, 0, Math.PI * 2);
      ctx.stroke();
    }

    // 一道扫过盘面的高光。它在跟着转，所以「在转」一眼就看得出来。
    const sweep = ctx.createLinearGradient(-ringInner, -ringInner, ringInner, ringInner);
    sweep.addColorStop(0, "rgba(255, 255, 255, 0)");
    sweep.addColorStop(0.44, "rgba(255, 255, 255, 0)");
    sweep.addColorStop(0.5, "rgba(255, 255, 255, 0.55)");
    sweep.addColorStop(0.56, "rgba(255, 255, 255, 0)");
    sweep.addColorStop(1, "rgba(255, 255, 255, 0)");
    ctx.beginPath();
    ctx.arc(0, 0, ringInner, 0, Math.PI * 2);
    ctx.fillStyle = sweep;
    ctx.fill();

    // 碟心那块漆。这里是空的——BPM 读数由 HTML 叠在上面，理由见 CSS 注释。
    ctx.beginPath();
    ctx.arc(0, 0, ringInner * 0.37, 0, Math.PI * 2);
    ctx.fillStyle = "#f5f2ea";
    ctx.fill();
    ctx.strokeStyle = "rgba(30, 28, 25, 0.13)";
    ctx.lineWidth = 1;
    ctx.stroke();
    ctx.restore();

    // —— 外圈频谱 ——
    const spectrum = engine && typeof engine.getSpectrum === "function" ? engine.getSpectrum() : null;
    if (!spectrum || spectrum.length < 8) return;

    const bins = spectrum.length;
    const groups = Math.min(RING_GROUPS, bins);

    // 两个真实的约束，都是量出来的，不是拍的：
    //   1. FFT 的 bin 是线性分频，而听感是对数的。按线性排，能量会全挤在
    //      一小撮里。所以按 i^1.8 划分边界，低频窄、高频宽。
    //   2. 这些音色是电钢/铺底，实测 bin 11（约 1 kHz）往上就落进 -120 dB
    //      以下的噪声底了。照整条 0–22 kHz 的轴画，大半圈会是一马平川。
    //      所以只呈现到 TOP_FRACTION 处，也就是真正有声音的那段。
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

    for (let index = 0; index < groups; index += 1) {
      let peak = -Infinity;
      for (let slot = viz.edges[index]; slot < viz.edges[index + 1]; slot += 1) {
        if (Number.isFinite(spectrum[slot])) peak = Math.max(peak, spectrum[slot]);
      }
      const level = Number.isFinite(peak)
        ? Math.max(0, Math.min(1, (peak - FLOOR_DB) / SPAN_DB))
        : 0;
      // 上冲要快、回落要慢，不然每帧的实时值会抖成噪点。
      viz.bars[index] = level > viz.bars[index] ? level : viz.bars[index] * 0.86 + level * 0.14;
    }

    // 柱子沿半径向外长。从正上方起、顺时针铺开，于是低频在顶端，顺着看
    // 下去频率越来越高——和一条横着的频谱在直觉上是一致的，只是绕了一圈。
    ctx.save();
    ctx.translate(center, center);
    ctx.lineCap = "round";
    const stroke = Math.max(1.6, ((Math.PI * 2 * ringInner) / groups) * 0.5);
    for (let index = 0; index < groups; index += 1) {
      const angle = -Math.PI / 2 + (index / groups) * Math.PI * 2;
      const level = viz.bars[index];
      // 下限用 ringMax 的相对值而不是写死的像素：这一版的音色是电钢/铺底，
      // 实测每根柱子的 level 稳定落在 0.32–0.95（从不接近 0），所以这个下限
      // 平时并不生效；写成相对值是为了换画布、换音色之后仍然成立——写死绝对值
      // 会在小画布上占掉整根柱子、在大画布上又等于没有。
      const length = ringMax * Math.max(0.08, level);
      const cos = Math.cos(angle);
      const sin = Math.sin(angle);
      ctx.strokeStyle = `rgba(${CLAY_RGB}, ${(0.22 + level * 0.62).toFixed(3)})`;
      ctx.lineWidth = stroke;
      ctx.beginPath();
      ctx.moveTo(cos * ringInner, sin * ringInner);
      ctx.lineTo(cos * (ringInner + length), sin * (ringInner + length));
      ctx.stroke();
    }
    ctx.restore();
  }

  // ── 粒子 ─────────────────────────────────────────────────
  //
  // 粒子只读 MCFeatures 归纳出来的标量（bass / mid / treble / rms / 谱心 /
  // 拍点），完全不碰原始 FFT。这个分层是从 Audio Shader Studio（MIT）那类
  // 项目借来的思路：一套音频分析驱动任意多套视觉，换视觉不用改分析。

  function ensureParticles() {
    if (viz.particles || !window.MCParticles) return viz.particles;
    const canvas = $("particles");
    if (!canvas) return null;
    viz.particles = window.MCParticles.create(canvas, {
      driver: app.driver || "orbit",
      // 背景色必须和卡片底色一致：拖尾靠半透明覆盖实现，颜色对不上会积出
      // 一层洗不掉的灰。
      background: "#fbf9f3",
    });
    return viz.particles;
  }

  function ensureFeatures() {
    if (!viz.features && window.MCFeatures) {
      // 256 是 engine 里 Tone.Analyser 的 fftSize，别改。
      viz.features = window.MCFeatures.createExtractor({ binCount: 256 });
    }
    return viz.features;
  }

  function drawParticles() {
    const particles = ensureParticles();
    if (!particles) return;
    if (!engine || !engine.isPlaying()) {
      // 暂停时让粒子停住。只喊一次，不要每帧都去 pause。
      if (!viz.paused) {
        viz.paused = true;
        particles.pause();
      }
      return;
    }
    viz.paused = false;
    const features = ensureFeatures();
    const spectrum = engine.getSpectrum();
    particles.frame(features ? features.update(spectrum) : { active: false });
  }

  function startViz() {
    if (viz.running) return;
    viz.running = true;
    const tick = (now) => {
      drawDisc(now || 0);
      drawParticles();
      viz.raf = window.requestAnimationFrame(tick);
    };
    viz.raf = window.requestAnimationFrame(tick);
  }

  function stopViz() {
    viz.running = false;
    window.cancelAnimationFrame(viz.raf);
  }

  // ── 完全自定义：调性 / 和声进行 / 粒子 ─────────────────────
  //
  // 音景、节拍、节奏量是三条主轴，这里再挂两个自由维度。都不强制：不选就是
  // 「自动」，由种子决定，每次「换一段」会落到新的组合上；选了就钉住，之后
  // 「换一段」不会动它（引擎里 shuffle() 会看有没有钉）。
  //
  // 调性清单和 theory.js 的 PITCH_CLASSES 必须逐字一致——那边只认升号。
  const KEY_CHOICES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];

  /** 和声进行在人眼里的简写：1maj7 / 5add9 / 6min7 / 4maj7 → 1·5·6·4 */
  function progressionLabel(progression) {
    return progression
      .map((symbol) => (/^([1-7])/.exec(String(symbol)) || ["", "?"])[1])
      .join("·");
  }

  /** 鼓点档位的中文名。查不到就写「自动」——宁可含糊，也不要露出一个裸 id。 */
  function drumName(id) {
    const levels = (window.MCSoundscapes && window.MCSoundscapes.DRUM_LEVELS) || [];
    const level = levels.find((item) => item.id === id);
    return level ? level.name : "自动";
  }

  function renderTuning() {
    if (!$("key-chips")) return;
    const recipe = engine && engine.recipe;
    const suggestedKeys = recipe ? recipe.soundscape.keys : [];
    const suggestedProgression =
      recipe && Number.isInteger(recipe.progressionIndex) ? recipe.progressionIndex : null;

    // 收起状态下的那行小字要能替代面板本身，所以写**当前实际生效的值**，
    // 而不是「自动 / 自动 / 默认」三个同义词——后者等于什么都没说。
    // 哪些是被钉住的、哪些是自动挑的，看下面哪颗芯片亮着就知道。
    if ($("tune-summary")) {
      if (!recipe) {
        $("tune-summary").textContent = "音景、节拍、节奏量、鼓点完全解耦，怎么搭都行";
      } else {
        const drivers = (window.MCParticles && window.MCParticles.DRIVERS) || [];
        const driverId = app.driver || (drivers[0] && drivers[0].id);
        const driver = drivers.find((item) => item.id === driverId);
        // 调性写**正在生效**的值（engine.key 不会逐小节变，写出来是稳定的）；
        // 和声进行只在被钉住时写具体的，选「自动」时写「进行自动」——自动时
        // 引擎每一小节都会换一条进行，这里写死某一小节的值会是假的。
        const pinnedProgression =
          Number.isInteger(recipe.progressionIndex) &&
          recipe.soundscape.progressions[recipe.progressionIndex];
        // 鼓点写**当下真正在打的档位**（没钉住时就是音景的推荐档位），和调性
        // 一样写实值：面板收起后，这行小字要能替代面板本身。
        const drums = engine.currentDrums();
        $("tune-summary").textContent = [
          engine.key ? `${engine.key} 调` : "调性自动",
          pinnedProgression ? progressionLabel(pinnedProgression) : "进行自动",
          drums === "none" ? "无鼓点" : `鼓点${drumName(drums)}`,
          driver ? driver.name : "—",
        ].join(" · ");
      }
    }

    // 调性：自动 + 十二个。推荐调排在前面——先给方向，再给全集。
    const currentKey = recipe ? recipe.key : null;
    const orderedKeys = suggestedKeys.concat(KEY_CHOICES.filter((key) => !suggestedKeys.includes(key)));
    $("key-chips").innerHTML =
      `<button type="button" class="chip${currentKey ? "" : " is-on"}" data-key="">自动</button>` +
      orderedKeys
        .map((key) => {
          const on = currentKey === key;
          const mark = suggestedKeys.includes(key) ? " is-suggested" : "";
          return `<button type="button" class="chip${on ? " is-on" : ""}${mark}" data-key="${esc(key)}">${esc(key)}</button>`;
        })
        .join("");

    // 和声进行：自动 + 当前音景的那几条。
    const progressions = recipe ? recipe.soundscape.progressions : [];
    $("progression-chips").innerHTML =
      `<button type="button" class="chip${suggestedProgression === null ? " is-on" : ""}" data-progression="">自动</button>` +
      progressions
        .map(
          (progression, index) =>
            `<button type="button" class="chip${suggestedProgression === index ? " is-on" : ""}" ` +
            `data-progression="${index}" title="${esc(progression.join(" → "))}">` +
            `${esc(progressionLabel(progression))}</button>`
        )
        .join("");

    // 鼓点：自动 + 四档。语法和调性那一行完全一样——音景推荐的档位标出来
    // （is-suggested）但不锁死，「自动」就是回到那个推荐值。
    const drumBox = $("drum-chips");
    if (drumBox) {
      const levels = (window.MCSoundscapes && window.MCSoundscapes.DRUM_LEVELS) || [];
      const pinnedDrums = recipe ? recipe.drums : null;
      const suggestedDrums = recipe ? recipe.soundscape.groove.drumLevel : null;
      drumBox.innerHTML =
        `<button type="button" class="chip${pinnedDrums ? "" : " is-on"}" data-drums="">自动</button>` +
        levels
          .map((level) => {
            const on = pinnedDrums === level.id;
            const mark = suggestedDrums === level.id ? " is-suggested" : "";
            return (
              `<button type="button" class="chip${on ? " is-on" : ""}${mark}" ` +
              `data-drums="${esc(level.id)}" title="${esc(level.blurb)}">${esc(level.name)}</button>`
            );
          })
          .join("");
    }

    // 粒子：驱动规则由 particles.js 提供，没加载就整行留空。
    const particleBox = $("driver-chips");
    const drivers = (window.MCParticles && window.MCParticles.DRIVERS) || [];
    if (!drivers.length) {
      particleBox.innerHTML = `<span class="panel-foot">粒子模块没加载起来。</span>`;
      return;
    }
    const currentDriver = app.driver || drivers[0].id;
    particleBox.innerHTML = drivers
      .map(
        (driver) =>
          `<button type="button" class="chip${currentDriver === driver.id ? " is-on" : ""}" ` +
          `data-driver="${esc(driver.id)}" title="${esc(driver.blurb || "")}">${esc(driver.name)}</button>`
      )
      .join("");
  }

  // ── 收藏 ─────────────────────────────────────────────────
  //
  // 存的是**配方**不是音频：因为整条合成链路都是确定性的（种子固定的伪随机
  // 数），同一组 {音景, 节拍, 节奏量, 调性, 进行, 鼓点, 种子} 永远得到同一段
  // 音乐。所以「收藏一首好听的曲子」就是存这七个字段——不占空间，也不怕文件丢。

  /** 一条配方的身份。用于判断「现在放的这段是不是已经收藏过了」。 */
  function recipeKey(recipe) {
    if (!recipe) return "";
    return [
      recipe.soundscape.id,
      recipe.bpm,
      Number(recipe.density).toFixed(3),
      recipe.key || "",
      Number.isInteger(recipe.progressionIndex) ? recipe.progressionIndex : "",
      recipe.drums || "",
      recipe.seed,
    ].join("|");
  }

  function isFavorite(recipe) {
    const key = recipeKey(recipe);
    return Boolean(key) && app.favorites.some((item) => recipeKey(item.__recipe) === key);
  }

  function syncFavButton() {
    const button = $("fav-btn");
    if (!button) return;
    const recipe = engine && engine.recipe;
    button.disabled = !recipe;
    const on = isFavorite(recipe);
    $("fav-label").textContent = on ? "已收藏" : "收藏";
    // 「收藏 / 已收藏」是个开关，只换文字的话读屏用户听不出当前状态。
    button.setAttribute("aria-pressed", on ? "true" : "false");
    button.classList.toggle("is-on", on);
  }

  async function loadFavorites() {
    try {
      const data = await api("/api/playlist");
      app.favorites = (Array.isArray(data) ? data : data.items || []).map((item) => ({
        ...item,
        // 后端存的是扁平字段，这里还原成引擎认的配方形状。
        __recipe: {
          soundscape: SCAPES.get(item.styleId) || SCAPES.list[0],
          bpm: item.bpm,
          density: item.density,
          key: item.key || null,
          progressionIndex: Number.isInteger(item.progressionIndex)
            ? item.progressionIndex
            : null,
          drums: item.drums || null,
          seed: item.seed,
        },
      }));
    } catch (error) {
      // 收藏失败不该打断主要流程：拉不到就当空的，页面照常用。
      app.favorites = [];
    }
    renderFavorites();
    syncFavButton();
  }

  function renderFavorites() {
    const list = $("fav-list");
    if (!list) return;
    const items = app.favorites;
    $("fav-empty").hidden = items.length > 0;
    $("fav-note").textContent = items.length ? `${items.length} 段` : "";
    list.innerHTML = items
      .map((item) => {
        const recipe = item.__recipe;
        // 鼓点写**实际会听到的档位**（没钉住就取音景的推荐值）：收藏列表里
        // 用户认的是「那首能跟着走的」，写「自动」等于没说。
        const drums = recipe.drums || recipe.soundscape.groove.drumLevel || "none";
        const parts = [
          `${recipe.bpm} BPM`,
          `节奏量 ${Math.round(recipe.density * 100)}%`,
          recipe.key ? `${recipe.key} 调` : "调性自动",
          drums === "none" ? "无鼓点" : `鼓点${drumName(drums)}`,
        ];
        return `
        <li class="fav-item" data-fav="${esc(item.id)}">
          <div class="fav-main">
            <span class="fav-name">${esc(item.name || recipe.soundscape.name)}</span>
            <span class="fav-meta">${esc(recipe.soundscape.name)} · ${esc(parts.join(" · "))}</span>
          </div>
          <div class="fav-actions">
            <button type="button" class="ghost-btn" data-fav-play="${esc(item.id)}">播放</button>
            <button type="button" class="ghost-btn" data-fav-del="${esc(item.id)}" aria-label="删除这条收藏">
              <svg class="icon" aria-hidden="true"><use href="#i-trash" /></svg>
            </button>
          </div>
        </li>`;
      })
      .join("");
  }

  async function addFavorite() {
    const recipe = engine && engine.recipe;
    if (!recipe) return;
    if (isFavorite(recipe)) {
      toast("这一段已经在收藏里了");
      return;
    }
    try {
      const saved = await api("/api/playlist", {
        method: "POST",
        body: {
          styleId: recipe.soundscape.id,
          bpm: recipe.bpm,
          density: recipe.density,
          key: recipe.key || null,
          progressionIndex: Number.isInteger(recipe.progressionIndex)
            ? recipe.progressionIndex
            : null,
          drums: recipe.drums || null,
          seed: recipe.seed,
          name: `${recipe.soundscape.name} · ${recipe.bpm} BPM`,
        },
      });
      await loadFavorites();
      toast(saved && saved.duplicate ? "这一段已经在收藏里了" : "已存进收藏");
    } catch (error) {
      toast(`收藏失败：${error.message || error}`);
    }
  }

  async function removeFavorite(id) {
    const item = app.favorites.find((entry) => entry.id === id);
    if (!item) return;
    try {
      await api(`/api/playlist/${encodeURIComponent(id)}`, { method: "DELETE" });
      await loadFavorites();
    } catch (error) {
      toast(`删除失败：${error.message || error}`);
    }
  }

  // ── 事件绑定 ──────────────────────────────────────────────

  // ── 对话（agent 入口） ────────────────────────────────────
  // 用户随便写一段话，后端 music_companion/agent.py 把它读成日程与音乐。
  // 这一页只做三件事：把话收进去、把理解结果摊开、给两个去处——写进今天，
  // 或者去休息页自己调。对话存在本地存储里，刷新不丢。

  const CHAT_TURNS = 12; // 本地留存的条数（发给模型的上下文更短，见 ai_client.understand）
  const chat = { turns: [], busy: false };

  const CHAT_GREETING =
    '<li class="bubble from-agent" id="chat-greeting">' +
    "<p>我在。把今天的事说给我听，我排好时间，再把音乐配上。</p></li>";

  function loadChat() {
    if (Array.isArray(prefs.chat)) chat.turns = prefs.chat.slice(-CHAT_TURNS);
  }

  function saveChat() {
    prefs.chat = chat.turns.slice(-CHAT_TURNS);
    savePrefs();
  }

  function chatClock(iso) {
    return clockOf(iso);
  }

  /** 一条 agent 回话：说的话 + 它排出来的东西。 */
  function bubbleHtml(turn, index) {
    if (turn.role === "user") {
      return `<li class="bubble from-me"><p>${esc(turn.text)}</p></li>`;
    }
    return `<li class="bubble from-agent"><p>${esc(turn.text)}</p>${planCardHtml(turn.plan, index)}</li>`;
  }

  /**
   * 排出来的东西：几件事 + 一段音乐 + 两个去处。
   *
   * 刻意不显示「置信度」「模型」「tokens」这类东西——用户要判断的是「排得对不对」，
   * 不是「AI 有多聪明」。他自己看一眼时间就知道对不对。
   */
  function planCardHtml(plan, index) {
    if (!plan) return "";
    const rows = (plan.events || [])
      .map(
        (item) => `
        <li>
          <span class="plan-time">${esc(chatClock(item.start))}–${esc(chatClock(item.end))}</span>
          <span class="plan-title">${esc(item.title)}</span>
        </li>`
      )
      .join("");
    const music = plan.music || {};
    const scape = SCAPES && typeof SCAPES.get === "function" ? SCAPES.get(music.soundscape) : null;
    const musicLine = scape
      ? `听「${esc(scape.name)}」，${esc(String(music.bpm || ""))} BPM${
          music.drums ? ` · 鼓点${esc(drumName(music.drums))}` : ""
        }`
      : "";
    if (!scape && !rows) return "";
    // 用户顺口提到的喜好单独说一句「记下了」——不然他说了「别太吵」却没看到
    // 任何反应，会以为白说了。（不进 prefs.preferred：那个字段存的是音景 id。）
    const prefs_ = plan.preferences || {};
    const remembered = [
      ...(prefs_.likes || []).map((item) => `喜欢${esc(item)}`),
      ...(prefs_.avoids || []).map((item) => `不要${esc(item)}`),
    ];
    const note = plan.written ? '<p class="plan-done">已经写进今天了</p>' : "";
    const prefsLine = remembered.length
      ? `<p class="plan-prefs">记下了：${remembered.join("、")}</p>`
      : "";
    // 「听这段」写进去之后也要留着：刚排完的那一段，过一会儿还想听是常事。
    const actions = `
      <div class="plan-actions">
        ${
          plan.written
            ? '<button type="button" class="ghost-btn" data-goto="today">去看看今天</button>'
            : `<button type="button" class="solid-btn" data-plan-write="${index}">写进今天</button>`
        }
        <button type="button" class="ghost-btn" data-plan-listen="${index}">听这段</button>
      </div>`;
    return `<div class="plan-card">
      ${rows ? `<ol class="plan-list">${rows}</ol>` : ""}
      ${musicLine ? `<p class="plan-music">${musicLine}</p>` : ""}
      ${prefsLine}
      ${note}
      ${actions}
    </div>`;
  }

  function renderChat() {
    const log = $("chat-log");
    if (!log) return;
    const busy = chat.busy ? '<li class="bubble from-agent is-busy"><p>正在读…</p></li>' : "";
    log.innerHTML = `${CHAT_GREETING}${chat.turns.map(bubbleHtml).join("")}${busy}`;
    $("chat-try").hidden = chat.turns.length > 0;
    $("chat-reset").hidden = chat.turns.length === 0;
    const last = log.lastElementChild;
    if (last && typeof last.scrollIntoView === "function") last.scrollIntoView({ block: "nearest" });
  }

  async function sayToAgent(text) {
    const message = String(text || "").trim();
    if (!message || chat.busy) return;
    chat.turns.push({ role: "user", text: message });
    chat.busy = true;
    saveChat();
    renderChat();
    $("chat-send").disabled = true;
    try {
      const result = await api("/api/agent", {
        method: "POST",
        body: {
          text: message,
          now: toLocalIso(new Date()),
          history: chat.turns.slice(-6).map((turn) => ({ role: turn.role, text: turn.text })),
        },
      });
      chat.turns.push({
        role: "agent",
        text: result.reply || "读完了。",
        plan: {
          events: result.events || [],
          music: result.music || {},
          state: result.state || {},
          preferences: result.preferences || {},
        },
      });
      showChatNote(result);
    } catch (error) {
      chat.turns.push({ role: "agent", text: `没读上：${error.message}` });
    } finally {
      chat.busy = false;
      $("chat-send").disabled = false;
      saveChat();
      renderChat();
      $("chat-input").value = "";
    }
  }

  /**
   * 输入框下面那行小字：这次是模型读的，还是本地规则读的。
   *
   * 和「休息」页的远端通路提示同一个道理——不配密钥也能完整演示，但得让人
   * 知道这一句是谁读的，否则「怎么有时聪明有时笨」永远是个谜。
   */
  function showChatNote(result) {
    const note = $("chat-note");
    if (!note) return;
    if (result.source === "ai") {
      note.hidden = true;
      note.textContent = "";
      return;
    }
    note.hidden = false;
    // 具体原因（HTTP 402、超时……）放进 title：查问题时用得上，摆在脸上只是
    // 一行英文报错。这里只说清「这次是谁读的」——说清这一句，用户就明白了。
    note.textContent = "这次用的是本地规则";
    note.title = result.fallback_reason
      ? `模型通道没接上：${result.fallback_reason}`
      : "没配模型密钥也照样能用，见 .env.example";
  }

  /** 把 agent 排好的东西真正落到后端：日程、状态、记下来的偏好。 */
  async function writePlan(index) {
    const turn = chat.turns[index];
    const plan = turn && turn.plan;
    if (!plan || plan.written) return;
    const button = document.querySelector(`[data-plan-write="${index}"]`);
    if (button) button.disabled = true;
    try {
      for (const item of plan.events || []) {
        await api("/api/calendar/events", { method: "POST", body: item });
      }
      // 状态只在用户真的说了的时候才写——否则会拿默认值把「了解你」里
      // 已经填好的拨盘覆盖掉。
      const energy = plan.state && plan.state.energy;
      const stress = plan.state && plan.state.stress;
      if (Number.isFinite(Number(energy)) || Number.isFinite(Number(stress))) {
        const merged = {
          ...prefs.state,
          captured_at: toLocalIso(new Date()),
          ...(Number.isFinite(Number(energy)) ? { energy: Number(energy) } : {}),
          ...(Number.isFinite(Number(stress)) ? { stress: Number(stress) } : {}),
        };
        await api("/api/state/manual", { method: "POST", body: merged });
        Object.assign(prefs.state, merged);
        savePrefs();
        syncStateInputs();
      }
      plan.written = true;
      saveChat();
      renderChat();
      await loadEvents();
      await loadDay();
      toast("写进今天了");
      go("today");
    } catch (error) {
      if (button) button.disabled = false;
      toast(error.message);
    }
  }

  async function listenToPlan(index) {
    const turn = chat.turns[index];
    const music = (turn && turn.plan && turn.plan.music) || {};
    if (!music.soundscape) return;
    go("rest");
    try {
      await playScape(music.soundscape, {
        bpm: music.bpm || undefined,
        drums: music.drums || null,
        context: "说给它听之后配的",
      });
    } catch (error) {
      toast(error.message);
    }
  }

  function bind() {
    for (const trigger of document.querySelectorAll("[data-goto]")) {
      trigger.addEventListener("click", () => go(trigger.dataset.goto));
    }

    // 对话
    $("chat-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      await sayToAgent($("chat-input").value);
    });

    // 回车就发，Shift+回车换行——聊天框该有的手感，少一步鼠标。
    $("chat-input").addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        $("chat-form").requestSubmit();
      }
    });

    $("chat-try").addEventListener("click", (event) => {
      const chip = event.target.closest("[data-say]");
      if (chip) sayToAgent(chip.dataset.say);
    });

    $("chat-reset").addEventListener("click", () => {
      chat.turns = [];
      saveChat();
      renderChat();
      $("chat-note").hidden = true;
      $("chat-input").value = "";
    });

    $("chat-log").addEventListener("click", async (event) => {
      // 卡片是发完消息才生成的，boot 时那轮 [data-goto] 绑定扫不到它，
      // 这里补一次——「去看看今天」就是卡片里的按钮。
      const jump = event.target.closest("[data-goto]");
      if (jump) {
        go(jump.dataset.goto);
        return;
      }
      const write = event.target.closest("[data-plan-write]");
      if (write) {
        await writePlan(Number(write.dataset.planWrite));
        return;
      }
      const listen = event.target.closest("[data-plan-listen]");
      if (listen) await listenToPlan(Number(listen.dataset.planListen));
    });

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

    $("route-start").addEventListener("click", () => {
      const queue = app.routeQueue;
      const player = routePlayer();
      if (!queue || !queue.segments.length || !player) return;
      go("rest");
      app.breakContext = null; // 路线不是某个休息点，别让休息点的种子串进来
      player.start(queue.segments);
    });

    $("timeline").addEventListener("click", async (event) => {
      const melody = event.target.closest("[data-melody-play]");
      if (melody) {
        const item = (app.plan && app.plan.timeline ? app.plan.timeline : []).find(
          (entry) => entry.id === melody.dataset.melodyPlay
        );
        if (item) await playMelodyOf(item, `为「${item.title}」准备的`);
        return;
      }
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
          // 用 currentRequest() 而不是直接复用 last：暂停前可能刚拖过节拍、
          // 换过调性/进行，复用旧请求会把那些改动悄悄回滚。
          // 路线在走时恢复播放不是「换台」——接着听当前这一段，路线继续走。
          app.routeStaging = Boolean(dayPlayer && dayPlayer.isActive());
          try {
            await playScape(last.soundscapeId, { ...currentRequest(), context: last.context });
          } finally {
            app.routeStaging = false;
          }
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

    // 路线连播条：上一段 / 下一段 / 结束。段内「换一段」（shuffle）不算
    // 退出——还是这一段，只是换了调性和进行。
    $("route-bar").addEventListener("click", (event) => {
      const hit = event.target.closest("[data-route]");
      if (!hit || !dayPlayer) return;
      const action = hit.dataset.route;
      if (action === "prev") dayPlayer.prev();
      else if (action === "next") dayPlayer.next();
      else dayPlayer.stop("user");
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
      // 调的是 density 不是 intensity：拖「节奏量」只该改音符多少，
      // 不该顺带把响度和亮度也动了。
      engine.setDensity(value);
      $("out-density").textContent = `${Math.round(value * 100)}%`;
    });

    $("player-volume").addEventListener("input", () => {
      if (!engine) return;
      const value = Number($("player-volume").value) / 100;
      engine.setVolume(value);
      $("out-volume").textContent = String(Math.round(value * 100));
    });

    // 鼓点音量独立于音乐音量。没有音景时也允许先调（和音量滑杆同理：
    // 一按播放就该是这个配比），所以不做「先选音景」的拦截。
    $("drum-volume").addEventListener("input", () => {
      if (!engine) return;
      const value = Number($("drum-volume").value) / 100;
      engine.setDrumVolume(value);
      $("out-drum-volume").textContent = String(Math.round(value * 100));
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
      if (!$("view-rest").hidden) {
        drawDisc(0);
        // 粒子画布铺满整张卡片，尺寸变了必须重新算 dpr 和边界。
        if (viz.particles) viz.particles.resize();
      }
    });

    // 自己调：调性 / 和声进行 / 粒子
    $("key-chips").addEventListener("click", (event) => {
      const chip = event.target.closest("[data-key]");
      if (!chip) return;
      if (!engine || !engine.recipe) {
        toast("先选一个音景");
        return;
      }
      // data-key="" 是「自动」：engine.setKey(null) 会解除钉住。
      engine.setKey(chip.dataset.key || null);
      renderTuning();
      syncTransport();
    });

    $("progression-chips").addEventListener("click", (event) => {
      const chip = event.target.closest("[data-progression]");
      if (!chip) return;
      if (!engine || !engine.recipe) {
        toast("先选一个音景");
        return;
      }
      // data-progression="" 是「自动」；setProgression 认得 null。
      const raw = chip.dataset.progression;
      engine.setProgression(raw === "" ? null : Number(raw));
      renderTuning();
      syncTransport();
    });

    $("drum-chips").addEventListener("click", (event) => {
      const chip = event.target.closest("[data-drums]");
      if (!chip) return;
      if (!engine || !engine.recipe) {
        toast("先选一个音景");
        return;
      }
      // data-drums="" 是「自动」：setDrums(null) 交回音景的推荐档位。
      engine.setDrums(chip.dataset.drums || null);
      renderTuning();
      syncTransport();
    });

    $("driver-chips").addEventListener("click", (event) => {
      const chip = event.target.closest("[data-driver]");
      if (!chip) return;
      app.driver = chip.dataset.driver;
      const particles = ensureParticles();
      if (particles) particles.setDriver(app.driver);
      renderTuning();
    });

    $("fav-btn").addEventListener("click", async () => {
      if (!engine || !engine.recipe) {
        toast("先放一段再收藏");
        return;
      }
      await addFavorite();
    });

    // 提醒卡：关掉 / 直接开播。两条路都要把卡片收起来——留着会挡住
    // 右下角的内容，而且「已经处理过了」这件事用户已经从动作里得到了确认。
    $("reminder-close").addEventListener("click", () => {
      $("reminder").hidden = true;
    });

    $("reminder-play").addEventListener("click", async () => {
      const item = app.reminderItem;
      $("reminder").hidden = true;
      if (!item) return;
      await playMelodyOf(item, `为「${item.title || item.label || ""}」提前听一段`);
    });

    $("fav-list").addEventListener("click", async (event) => {
      const play = event.target.closest("[data-fav-play]");
      if (play) {
        const item = app.favorites.find((entry) => entry.id === play.dataset.favPlay);
        if (!item) return;
        try {
          await playScape(item.__recipe.soundscape.id, {
            bpm: item.__recipe.bpm,
            density: item.__recipe.density,
            key: item.__recipe.key,
            progressionIndex: item.__recipe.progressionIndex,
            drums: item.__recipe.drums,
            seed: item.__recipe.seed,
            context: `收藏 · ${item.name || item.__recipe.soundscape.name}`,
          });
        } catch (error) {
          toast(error.message);
        }
        return;
      }
      const del = event.target.closest("[data-fav-del]");
      if (del) await removeFavorite(del.dataset.favDel);
    });
  }

  // ── 启动 ──────────────────────────────────────────────────

  /**
   * 告诉用户远端音乐通路到底通没通。
   *
   * 这条提示存在的理由很实在：不配 .env 也能完整演示（本地合成器是主线），
   * 所以「按下生成、结果出的是本地循环」这件事不该让人猜——直接写清楚是
   * 「没配」还是「配了但失败了」，用户才知道要不要去动 .env。
   */
  async function renderRemoteMusicHint() {
    const box = $("ai-fallback");
    if (!box) return;
    let health = null;
    try {
      health = await api("/api/health");
    } catch {
      return; // 健康接口都读不到，这条提示就没必要硬塞给用户
    }
    const remote = (health && health.remote_music) || {};
    box.hidden = false;
    box.textContent = remote.configured
      ? `远端通路已配置（${remote.provider || "未知"}）：按下生成会调用它，失败时自动回落本地。`
      : "远端通路还没配（可选）：现在按下生成会直接用本地合成器现场生成一段，不联网。" +
        "想换成真·AI 生成的音频，见项目根目录的 .env.example。";
  }

  async function boot() {
    loadPrefs();
    loadChat();
    renderChat();
    syncStateInputs();
    renderPreferChips();
    renderScapes();
    bind();
    syncTransport();
    renderRemoteMusicHint();

    // 收藏不挡首屏：后端 /api/playlist 慢一点或没起来，页面也照常用。
    // loadFavorites 自己吞异常，这里不用 await。
    loadFavorites();

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
