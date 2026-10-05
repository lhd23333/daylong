/**
 * 今天的声音路线：把一整天串成一条能连续听的节目单。
 *
 * 前两天做的三件事在这里合到一处：每条日程有自己的旋律（daymusic.js），
 * 每个休息点有自己的音景，引擎换段之间是无缝 crossfade。单独看是三个功能，
 * 串起来才是「陪你过完这一天」——从早到晚，一件事一段曲，一段接一段自动续上，
 * 用户只需要按一次「开始收听」。
 *
 * 两条设计原则：
 *
 * 1. **节目单，不是空白播放列表**。每段都带着来源：看见「10:40 的休息 ·
 *    呼吸」就知道接下来这两分钟是为哪一刻放的。剥掉这层，它就只是又一个
 *    随机播放器。
 *
 * 2. **段长克制在分钟级**。一天十几条日程全放完要好几个小时，没人这样听。
 *    每段放一两分钟——够听出「这是那件事的曲子」，也让整条路线大约半小时
 *    走得完。段长是常量，好改，不搞自适应那套玄学。
 *
 * 模块本身不碰 DOM、不碰引擎：build() 是纯函数，createPlayer() 用注入的
 * play 回调换段、订阅引擎的 bar 事件计时。这样它能被单独测，也能在任何
 * 页面上复现。
 */
(function () {
  "use strict";

  /** 日程段放多久（秒）。两分钟够听出「这是那件事的曲子」。 */
  const EVENT_SECONDS = 120;

  /** 休息点段放多久（秒）。休息点本身只有几分钟，听一小段就够。 */
  const BREAK_SECONDS = 90;

  function hash32(text) {
    // 与 daymusic.js / app.js 同一套 FNV-1a。休息点的种子必须和从时间轴
    // 直接点「开始休息」时完全一致——同一条目从哪进都是同一段音乐。
    let value = 0x811c9dc5;
    const source = String(text ?? "");
    for (let index = 0; index < source.length; index += 1) {
      value ^= source.charCodeAt(index);
      value = Math.imul(value, 0x01000193) >>> 0;
    }
    return value >>> 0;
  }

  function clockOf(iso) {
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return "--:--";
    const pad = (n) => String(n).padStart(2, "0");
    return `${pad(date.getHours())}:${pad(date.getMinutes())}`;
  }

  /**
   * 一条时间轴条目 → 一段节目。没有可播配方的条目返回 null，由 build 过滤。
   *
   * @param {object} item 时间轴条目（kind: "event" | "break"）
   * @param {object} [options] 透传给 daymusic 的 date / energy / stress
   */
  function segmentOf(item, options = {}) {
    if (!item || typeof item !== "object") return null;
    if (item.kind === "break") {
      if (!item.soundscape) return null;
      // 与时间轴「开始休息」同一套换算：同一个 id 当种子，低强度休息点更轻。
      const soft = typeof item.intensity === "string" && item.intensity.includes("低");
      return {
        id: item.id,
        kind: "break",
        clock: clockOf(item.start),
        label: item.label || item.title || "休息",
        seconds: BREAK_SECONDS,
        request: {
          soundscapeId: item.soundscape,
          bpm: item.bpm || undefined,
          intensity: soft ? 0.35 : 0.55,
          seed: hash32(item.id) % 997,
        },
      };
    }
    const music = window.MCDayMusic;
    if (!music || typeof music.recipeFor !== "function") return null;
    const recipe = music.recipeFor(item, options);
    if (!recipe) return null;
    return {
      id: item.id,
      kind: "event",
      clock: clockOf(item.start),
      label: item.title || item.label || "一件事",
      seconds: EVENT_SECONDS,
      request: {
        soundscapeId: recipe.soundscapeId,
        bpm: recipe.bpm,
        density: recipe.density,
        key: recipe.key,
        progressionIndex: recipe.progressionIndex,
        seed: recipe.seed,
      },
    };
  }

  /**
   * 整天时间轴 → 有序节目单。
   *
   * @returns {{segments: object[], totalSeconds: number, secondsPerSegment: object}}
   */
  function build(timeline, options = {}) {
    const items = (Array.isArray(timeline) ? timeline : [])
      .slice()
      .sort((a, b) => String(a.start || "").localeCompare(String(b.start || "")));
    const segments = [];
    for (const item of items) {
      const segment = segmentOf(item, options);
      if (segment) segments.push(segment);
    }
    return {
      segments,
      totalSeconds: segments.reduce((sum, segment) => sum + segment.seconds, 0),
      secondsPerSegment: { event: EVENT_SECONDS, break: BREAK_SECONDS },
    };
  }

  /**
   * 连播控制器。换段用注入的 play 回调，计时订阅引擎的 bar 事件——
   * 不自己开 setInterval：小节回调跟着音频时钟走，切去别的标签页也不会
   * 因为定时器节流而乱套（顶多晚半个小节换段，耳朵听不出来）。
   *
   * @param {object} deps
   * @param {object} deps.engine   MCEngine（要能 on("bar", fn)）
   * @param {(segment: object, meta: {index: number, count: number}) => any} deps.play
   *        换到某一段。返回 promise 时失败会被兜住并停下整条路线。
   * @param {(segment: object, index: number, count: number) => void} [deps.onSegment]
   * @param {(reason: string) => void} [deps.onEnd] reason: ended / manual / user / error
   */
  function createPlayer({ engine, play, onSegment, onEnd } = {}) {
    let segments = [];
    let index = 0;
    let elapsed = 0;
    let active = false;
    let unsubscribe = null;
    let token = 0;

    function state() {
      return {
        active,
        index,
        count: segments.length,
        segment: active ? segments[index] : null,
      };
    }

    function barSeconds(bpm) {
      // 一小节 = 4 拍。bpm 缺失时按 80 估——只影响换段早晚几秒，不影响正确性。
      const value = Number(bpm);
      return 240 / (Number.isFinite(value) && value > 0 ? value : 80);
    }

    async function stage(nextIndex) {
      index = nextIndex;
      elapsed = 0;
      const mine = ++token;
      const segment = segments[index];
      if (typeof onSegment === "function") onSegment(segment, index, segments.length);
      try {
        await play(segment, { index, count: segments.length });
      } catch (error) {
        // 段放不出来（引擎报错）就整条停下来，不要带着半截状态继续数拍子。
        if (mine === token && active) stop("error", error);
      }
    }

    function tick(payload) {
      if (!active || !segments.length) return;
      elapsed += barSeconds(payload && payload.bpm);
      if (elapsed >= segments[index].seconds) {
        if (index + 1 < segments.length) stage(index + 1);
        else stop("ended");
      }
    }

    function stop(reason = "user", detail) {
      if (!active) return;
      active = false;
      token += 1;
      if (unsubscribe) unsubscribe();
      unsubscribe = null;
      if (typeof onEnd === "function") onEnd(reason, detail);
    }

    return {
      /** 从第 startIndex 段开始走。重复调用会从头重排。 */
      start(list, startIndex = 0) {
        if (!Array.isArray(list) || !list.length) return null;
        stop("restart");
        segments = list;
        active = true;
        if (engine && typeof engine.on === "function") unsubscribe = engine.on("bar", tick);
        stage(Math.min(Math.max(0, startIndex), list.length - 1));
        return state();
      },
      next() {
        if (!active) return;
        if (index + 1 < segments.length) stage(index + 1);
        else stop("ended");
      },
      prev() {
        if (!active) return;
        stage(Math.max(0, index - 1));
      },
      stop,
      isActive: () => active,
      state,
      /** 测试与探针用：段总时长常量。 */
      secondsPerSegment: { event: EVENT_SECONDS, break: BREAK_SECONDS },
      _tick: tick,
    };
  }

  window.MCDayQueue = {
    build,
    segmentOf,
    createPlayer,
    EVENT_SECONDS,
    BREAK_SECONDS,
    hash32,
  };
})();
