/**
 * 日程 → 旋律，以及「开始前提醒」。
 *
 * 这是「陪伴」两个字落到实处的那个模块：用户填完明天的日程，每一条日程都会
 * 自动得到一首属于它的曲子——不是随机配一首，而是**从这条日程本身推出来的**。
 *
 * 三条设计原则，按优先级排：
 *
 * 1. **确定性**。同一个标题 + 同一天，永远推出同一首。用户明天再看这条日程，
 *    听到的还是昨天那段旋律。随机配乐会让人觉得是台机器在放背景音；稳定的
 *    对应关系才会让人产生「这是我这门课的曲子」的归属感。所以整条推导链路上
 *    没有任何 Math.random()，随机部分全部走 FNV-1a 哈希 + 种子。
 *
 * 2. **可说理**。每条推导都附一句 ``why``，在界面上直接显示。用户看到的
 *    不是「AI 配了一首歌」，而是「因为你 21:00 要睡觉，所以给你一首 62 BPM、
 *    音符很少的夜灯」——能被追问、能被反驳的规则，比黑箱更像设计而不是玄学。
 *
 * 3. **不越权**。推出来的 bpm / density 一律夹回该音景的**推荐区间**，而不是
 *    全局硬边界。音景之所以是音景，靠的就是它自己那一段；为了「贴合日程」把它
 *    推到区间外，推出来的东西就不再是那个音景了。要越界是用户在面板上手动拖的
 *    事，程序不替他做决定。
 *
 * 只依赖 window.MCSoundscapes。没有 MCSoundscapes 时 recipeFor 返回 null，
 * 调用方照常渲染日程，只是没有旋律入口。
 */
(function () {
  "use strict";

  /** 日程开始前多久提醒。用户要的是 5 分钟，休息点用自己的更紧的值。 */
  const LEAD_MINUTES = 5;

  /** 提醒扫描间隔。20 秒够准（误差不影响「提前 5 分钟」的体感），也不费电。 */
  const TICK_MS = 20_000;

  // ── 日程 → 音景 ────────────────────────────────────────────
  //
  // 日历的 category 只有六类（study/work/commute/exercise/break/other），粒度
  // 太粗：「自习」和「开会」被归成一类，但这两件事该配的音乐差得很远。所以
  // 标题关键词有一票否决权——先看标题，标题没线索才回落到 category。
  const CATEGORY_SCAPES = {
    study: "desk-hours",
    work: "desk-hours",
    commute: "way-home",
    exercise: "high-noon",
    break: "breath",
    other: "after-rain",
  };

  // 顺序即优先级：越靠前越先匹配。「睡」要排在「读」前面，否则「睡前阅读」
  // 会被后面的阅读规则抢走，配成一首清醒的曲子。
  const KEYWORD_SCAPES = [
    [/睡|躺下|晚安|入睡|sleep/i, "night-lamp"],
    [/考|测验|exam|面试|答辩/i, "breath"],
    [/呼吸|冥想|闭眼|午休|小憩|放空/, "breath"],
    // 跑和走各自单独成条，且都排在「运动」和「通勤」前面：这两个是唯一
    // **音乐直接决定步频**的场景，配错等于把人的步子带偏。球/游泳/健身这些
    // 动作跟拍子没关系，继续走原来那条。
    // 末尾那个光杆「跑」是给对话入口留的：agent 常把标题收成「操场跑」这种
    // 说法，只认「跑步」的话，它排的跑步会被配成一首 121 拍的午后。
    [/跑步|慢跑|长跑|晨跑|夜跑|马拉松|体测|跑步机|跑|jogging|jog|running/i, "run"],
    [/散步|走路|走一|遛弯|遛狗|饭后走|漫步/, "brisk"],
    [/球|游泳|健身|锻炼|体育|运动/, "high-noon"],
    // 风格关键词（第二轮扩充的音景接进对话/日程入口）：用户把「练吉他」「钢琴课」
    // 「游戏时间」写进标题时，配的应该是对应风格，而不是落进最后那条「课」的兜底。
    // 排在行走/跑步之后——「操场跑」这类标题优先按步子配乐，风格词只是补充。
    [/摇滚|金属|吉他|贝斯|乐队|排练|rock|metal/i, "volt"],
    [/电音|蹦迪|夜店|dj|remix/i, "neon"],
    [/爵士|jazz|摇摆|小酒馆/i, "midnight"],
    [/钢琴|古典|交响|小提琴|轻音乐|classical|piano/i, "velvet"],
    [/游戏|像素|电玩|8-?bit|chiptune/i, "pixel"],
    [/通勤|路上|公交|地铁|骑车|回家/, "way-home"],
    [/晚[饭餐]|午餐|午饭|吃饭|食堂|加餐/, "after-rain"],
    [/早[饭餐]|起床|晨读|morning/i, "first-light"],
    [/洗澡|收拾|整理|日记|总结|复盘|静一静|发呆/, "settling"],
    [/写|作业|复习|预习|背|读|看书|自习|上机|练习|study|work|课/i, "desk-hours"],
  ];

  function hash32(text) {
    // FNV-1a 32 位。选它是因为短、无依赖、雪崩够用——改一个字，整首曲子就换。
    let value = 0x811c9dc5;
    const source = String(text ?? "");
    for (let index = 0; index < source.length; index += 1) {
      value ^= source.charCodeAt(index);
      value = Math.imul(value, 0x01000193) >>> 0;
    }
    return value >>> 0;
  }

  function pickSoundscape(item) {
    const library = window.MCSoundscapes;
    if (!library) return null;
    const title = String(item.title || item.label || "");
    for (const [pattern, id] of KEYWORD_SCAPES) {
      if (pattern.test(title)) return library.get(id) || null;
    }
    const byCategory = CATEGORY_SCAPES[item.category];
    if (byCategory) return library.get(byCategory) || null;
    return library.get("after-rain") || library.list[0] || null;
  }

  function clampInto(value, range) {
    return Math.min(range[1], Math.max(range[0], value));
  }

  function durationMinutes(item) {
    const start = Date.parse(item.start);
    const end = Date.parse(item.end);
    if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) return 0;
    return Math.round((end - start) / 60000);
  }

  /**
   * 一条日程 → 一份配方（可直接喂给 engine.play 的 request）。
   *
   * @param {object} item 时间轴条目（要有 title / start / end，category 可选）
   * @param {{date?: string, energy?: number, stress?: number}} [options]
   *        date 只影响种子（同一天恒定）；energy/stress 是当天的状态快照，
   *        有就把曲子往「还有力气」或「该松一点」的方向推一把。
   * @returns {object|null} {soundscapeId, bpm, density, key, progressionIndex, seed, why}
   *
   * 注意配方里**不带 `drums`**：鼓点交给音景自己的推荐档位（`groove.drumLevel`），
   * 用户想改就到休息页的鼓点行改。在这里钉死会把「自动」变成一句假话——推导出来
   * 的曲子应该保持可调，而不是替用户把四个档位选完。
   */
  function recipeFor(item, options = {}) {
    const library = window.MCSoundscapes;
    const soundscape = pickSoundscape(item);
    if (!library || !soundscape) return null;

    const [bpmLow, bpmHigh] = soundscape.bpm;
    const [densityLow, densityHigh] = soundscape.density;
    const midBpm = (bpmLow + bpmHigh) / 2;
    const midDensity = (densityLow + densityHigh) / 2;

    const start = new Date(item.start);
    const hour = Number.isFinite(start.getTime()) ? start.getHours() : 9;
    const minutes = durationMinutes(item);
    const day = options.date || String(item.start || "").slice(0, 10);
    const title = String(item.title || item.label || "");
    const seed = hash32(`${title}|${day}`) % 997;

    const notes = [];

    // ── 节拍：时间段是主因，任务长短是次因 ──
    // 人对速度的体感跟着生物钟走：清早和深夜要慢，午后和傍晚能快一点。
    let bpm = midBpm;
    if (hour < 7) {
      bpm -= 8;
      notes.push("这个点还没完全醒");
    } else if (hour < 9) {
      bpm -= 4;
      notes.push("早晨起拍慢一点");
    } else if (hour < 12) {
      bpm += 2;
      notes.push("上午精力在线");
    } else if (hour < 14) {
      bpm += 4;
      notes.push("午后提一点劲");
    } else if (hour < 18) {
      bpm += 6;
      notes.push("下午最能推得动");
    } else if (hour < 22) {
      bpm -= 2;
      notes.push("入夜了，速度收回来");
    } else {
      bpm -= 8;
      notes.push("深夜留出余地");
    }

    // 长任务配慢拍：40 分钟以上还突突突地跑，人会被推着走，反而坐不住。
    if (minutes >= 45) {
      bpm -= 5;
      notes.push(`${minutes} 分钟的长段落，拍子压慢`);
    } else if (minutes > 0 && minutes <= 15) {
      // 短任务不需要「稳」，需要「利落」——上去一点，别拖。
      bpm += 3;
      notes.push("短任务，干脆一点");
    }

    const stress = Number(options.stress);
    if (Number.isFinite(stress) && stress >= 60) {
      bpm -= 4;
      notes.push("你今天压力偏高，再慢一点");
    }

    // ── 节奏量：安静的程度，跟着任务性质走 ──
    let density = midDensity;
    if (item.category === "study" || item.category === "work") {
      density -= 0.1;
      notes.push("要专注，音符少一些");
    }
    if (item.category === "exercise") {
      density += 0.12;
      notes.push("动着的时候可以热闹些");
    }
    if (minutes >= 45) {
      density -= 0.08;
    }
    const energy = Number(options.energy);
    if (Number.isFinite(energy) && energy <= 40) {
      density -= 0.08;
      notes.push("今天电量不高，留白多一点");
    } else if (Number.isFinite(energy) && energy >= 75 && minutes > 0 && minutes <= 30) {
      density += 0.06;
    }

    // 鼓点不在这里挑——它跟着音景的推荐档位走（soundscapes.js 的 groove.drumLevel）。
    // 但「跟着鼓点走」正是走 / 跑两个音景存在的理由，说明句里得点出来，否则用户
    // 只看到「配了一首曲子」，看不出节奏是特意加上去的。只有最重的一档才说这句：
    // 轻档打不出「踩得住」的效果，说了就是骗人。
    if (soundscape.groove.drumLevel === "strong") {
      notes.push("鼓点打满，步子能跟着踩");
    }

    // ── 调性与和声：哈希决定，但锁在音景自己的推荐里 ──
    // 不放进上面的 why：这两个是「同一标题永远同一首」的实现细节，说给用户听
    // 只会变成一串没意义的字母，不如留白。
    const key = soundscape.keys[hash32(`${title}|key`) % soundscape.keys.length];
    const progressionIndex = hash32(`${title}|prog`) % soundscape.progressions.length;

    return {
      soundscapeId: soundscape.id,
      // 夹回**推荐区间**：音景的辨识度来自它自己那一段，程序推导不该把它推出去。
      bpm: Math.round(clampInto(bpm, [bpmLow, bpmHigh])),
      density: Number(clampInto(density, [densityLow, densityHigh]).toFixed(3)),
      key,
      progressionIndex,
      seed,
      // 说理句。取前三条，再多就成了论文摘要，没人读。
      why: `${notes.slice(0, 3).join("，")}。`,
      soundscapeName: soundscape.name,
    };
  }

  /**
   * 给一整天的条目各配一首。返回 [{item, recipe}]，recipe 为 null 的原样保留，
   * 调用方按需过滤——不要在这里悄悄丢掉条目，否则「少了一条」很难查。
   */
  function plan(items, options = {}) {
    return (Array.isArray(items) ? items : []).map((item) => ({
      item,
      recipe: recipeFor(item, options),
    }));
  }

  // ── 提前提醒 ────────────────────────────────────────────────
  //
  // 只在页面开着的时候有效（浏览器关掉就没有后台能力，这是网页形态的边界，
  // 已在文档里写明）。所以除了「刚好到点」的触发，还要处理「打开页面时已经在
  // 窗口里」的补触发——用户 13:58 才打开页面，13:57 该响的那条不能被吞掉。
  function createReminder({ items = [], lead = LEAD_MINUTES, onDue, now = () => Date.now() } = {}) {
    const fired = new Set();
    let timer = null;

    function leadFor(item) {
      // 休息点自带更紧的提醒值（1–3 分钟）：那件事本身就是「起来动一下」，
      // 提前 5 分钟提醒反而会打断正在进行的事。0 才回落到默认 5 分钟。
      const own = Number(item.reminder_minutes_before);
      return Number.isFinite(own) && own > 0 ? own : lead;
    }

    /**
     * 现在到点的条目。返回 [{item, minutesLeft}]。
     *
     * 判定窗口是 [start - lead, start)：已经开始的条目不再提醒——「还有
     * -2 分钟开始」是句错话。
     */
    function due(moment = now()) {
      const result = [];
      for (const item of items) {
        if (!item || fired.has(item.id)) continue;
        const start = Date.parse(item.start);
        if (!Number.isFinite(start)) continue;
        const minutesLeft = (start - moment) / 60000;
        if (minutesLeft <= 0 || minutesLeft > leadFor(item)) continue;
        result.push({ item, minutesLeft });
      }
      return result;
    }

    function check() {
      for (const hit of due()) {
        fired.add(hit.item.id);
        if (typeof onDue === "function") onDue(hit.item, hit.minutesLeft);
      }
    }

    return {
      /** 立刻查一遍，然后每 20 秒查一遍。重复调用不会叠加定时器。 */
      start() {
        check();
        if (timer === null) timer = window.setInterval(check, TICK_MS);
        return this;
      },
      stop() {
        if (timer !== null) window.clearInterval(timer);
        timer = null;
        return this;
      },
      /**
       * 换一批条目（时间轴重渲染时会走到这里）。
       *
       * **刻意不清空已触发集合**：重渲染是家常便饭（标记一个休息点完成就会
       * 触发），清空会让已经提醒过、甚至已经被用户关掉的那条再弹一次。
       * 真的换了新的一天，条目 id 本来就不同，自然能重新触发。
       */
      setItems(nextItems) {
        if (Array.isArray(nextItems)) items = nextItems;
        check();
        return this;
      },
      /** 清空已触发记录。跨天或用户手动「重新提醒」时才需要。 */
      forget() {
        fired.clear();
        return this;
      },
      due,
      markFired(id) {
        fired.add(id);
      },
      isFired(id) {
        return fired.has(id);
      },
    };
  }

  window.MCDayMusic = {
    recipeFor,
    plan,
    createReminder,
    pickSoundscape,
    durationMinutes,
    hash32,
    LEAD_MINUTES,
    TICK_MS,
  };
})();
