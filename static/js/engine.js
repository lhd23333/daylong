/*
 * 演奏引擎：用 Tone.js 把音景库实时演奏出来。
 *
 * 为什么不预先生成一个音频文件？
 * 预渲染的循环听三遍就腻，而且改速度、改情绪都得重新渲染。实时演奏换来
 * 三件事：可以无限听下去不重复、可以随时调速度和密度、可以按用户当下的
 * 疲惫程度实时把音乐收软一点。
 *
 * 所有随机选择都走同一个带种子的伪随机数（mulberry32），所以「同一音景 +
 * 同一速度 + 同一种子」永远得到同一段音乐，方便复现与写测试。
 */
(() => {
  "use strict";

  const Theory = window.MCTheory;
  const Library = window.MCSoundscapes;
  const TIMBRES = Library.timbres;

  // 每演奏这么多小节换一条和声进行，避免长时间聆听绕回同一段。
  const BARS_PER_PROGRESSION_PASS = 16;

  // 鼓组以「一小节 = 16 个十六分音符」为网格，直接写命中位置，比字符串好数。
  // 这里只写**律动性格**（重音落在哪几格），打几层由档位决定，见 DRUM_LAYERS。
  const DRUM_PATTERNS = {
    lofi: { kick: [0, 6, 10], snare: [4, 12], hat: [2, 6, 10, 14], level: 0.5 },
    soft: { kick: [0, 8], snare: [12], hat: [4, 12], level: 0.36 },
    pop: { kick: [0, 4, 8, 12], snare: [4, 12], hat: [0, 2, 4, 6, 8, 10, 12, 14], level: 0.46 },
    citypop: { kick: [0, 3, 7, 8, 11], snare: [4, 12], hat: [2, 6, 10, 14], level: 0.44 },
    // 走路：正拍四平八稳地踩，踩镲全部落在反拍（每拍的「和」）上。抬脚落在
    // 底鼓、落脚落在踩镲，这是最简单也最经得住长时间循环的「脚步网格」。
    stride: { kick: [0, 4, 8, 12], snare: [4, 12], hat: [2, 6, 10, 14], level: 0.5 },
    // 跑步：踩镲铺满全部十六分格（一秒十几次），密度本身就推着步频往上走；
    // 重音落在偶数格（八分位）上，奇数格减到 0.62，形成「咚-嗒-咚-嗒」的
    // 前后脚交替。底鼓保持四拍全踩，不然跑起来会飘。
    run: {
      kick: [0, 4, 8, 12],
      snare: [4, 12],
      hat: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15],
      level: 0.56,
    },
  };

  /**
   * 鼓点档位 → 允许发声的层。这是和调性、和声进行平级的第四条自由轴：
   * 音景决定律动性格，用户决定打几层。
   *
   * 同一条节奏型，「轻」和「强」是完全不同的推动力——前者像秒针，后者
   * 能踩着走路。这是「加不加鼓」之外更细的一层，也是用户抱怨「节奏感不够」
   * 时最直接的解法。
   */
  const DRUM_LAYERS = {
    none: { kick: false, snare: false, hat: false },
    light: { kick: false, snare: false, hat: true },
    standard: { kick: true, snare: false, hat: true },
    strong: { kick: true, snare: true, hat: true },
  };

  // 三层鼓的基础音量（dB）。写成常量而不是散在两处：buildDrums 建节点时设一次，
  // applyDrumMix 按强度和档位再算一次，早先两边各写各的数字，改了一处会被另一处
  // 悄悄覆盖回去（表现为「改了没生效」）。
  //
  // 踩镲从 -32 提到 -28：它是走路/跑步场景里真正标出「脚步格子」的那一层，
  // 埋在铺底下面等于没有；而且 brown 噪声的军鼓本来就比白噪声的镲片更暗，
  // 不可能靠拉低镲来给军鼓让路。
  const DRUM_BASE_VOLUME = { kick: -12, snare: -26, hat: -28 };

  /**
   * 档位带来的整体音量差（dB）。**层数不等于响度**：只加层不加音量的话，
   * 「强」和「标准」在整段音乐里差不到 1 dB，名字叫强却听不出来
   * （实测：三层全上时整段音乐的宽带能量只比无鼓高 0.62 dB）。
   *
   * 所以「强」在这里额外抬一档——走路/跑步要的就是这个能被听见的推力；
   * 「轻」保持克制，它的角色是秒针，不是鼓。
   */
  const DRUM_LEVEL_GAIN_DB = { none: 0, light: 0, standard: 0, strong: 7 };

  /** 鼓点滑杆（0–1）→ dB。70% 是 0 dB，往上到 +3.1，往下到静音。 */
  function drumVolumeToDb(value) {
    const level = Math.max(0, Math.min(1, Number(value) || 0));
    if (level <= 0.02) return -80;
    return 20 * Math.log10(level / 0.7);
  }

  /** 确定性伪随机数：同种子 → 同序列。 */
  function mulberry32(seed) {
    let state = seed >>> 0;
    return function next() {
      state = (state + 0x6d2b79f5) >>> 0;
      let t = Math.imul(state ^ (state >>> 15), 1 | state);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  function seedFrom(text) {
    let hash = 2166136261;
    const value = String(text);
    for (let index = 0; index < value.length; index += 1) {
      hash ^= value.charCodeAt(index);
      hash = Math.imul(hash, 16777619);
    }
    return hash >>> 0;
  }

  class CompanionEngine {
    constructor() {
      this.playing = false;
      this.recipe = null;
      this.nodes = null;
      this.barEventId = null;
      // 小节排期一律用 tick（音乐时间）刻度，理由见 scheduleNextBar 的说明。
      this.nextBarTick = 0;
      this.lastScheduledTick = 0;
      this.key = null;
      this.barIndex = 0;
      this.progressionIndex = 0;
      this.passIndex = -1;
      this.rng = mulberry32(1);
      this.listeners = { bar: new Set(), tick: new Set() };
      this.fadeToken = 0;
      this.armed = false;
      this.muted = false;
      this.volume = 0.62; // 音乐音量。比原来低一档：实测音乐一直压着鼓点
      this.drumVolume = 0.85; // 鼓点音量。默认略高于音乐，让拍子站到前面
    }

    // ── 对外接口 ──────────────────────────────────────────────

    /** 浏览器要求音频上下文必须由用户手势唤醒，这里统一封装。 */
    async unlock() {
      if (typeof window.Tone === "undefined") {
        throw new Error("音乐引擎未加载，请刷新页面重试。");
      }
      if (window.Tone.getContext().state !== "running") {
        await window.Tone.start();
      }
      return true;
    }

    isPlaying() {
      return this.playing;
    }

    currentId() {
      return this.recipe ? this.recipe.soundscape.id : null;
    }

    currentBpm() {
      return this.recipe ? this.recipe.bpm : null;
    }

    on(event, handler) {
      const bucket = this.listeners[event];
      if (!bucket) return () => {};
      bucket.add(handler);
      return () => bucket.delete(handler);
    }

    _emit(event, payload) {
      const bucket = this.listeners[event];
      if (!bucket) return;
      bucket.forEach((handler) => {
        try {
          handler(payload);
        } catch (error) {
          console.warn("[engine] 监听器出错", error);
        }
      });
    }

    /**
     * 开始演奏，或在不中断声音的前提下切到另一个音景。
     * @param {{soundscapeId?:string, bpm?:number, density?:number, intensity?:number,
     *   seed?:number, key?:string, progressionIndex?:number, drums?:string}} request
     * @param {{crossfade?:number}} [opts]
     */
    async play(request = {}, opts = {}) {
      await this.unlock();
      const Tone = window.Tone;
      const recipe = Library.resolve(request.soundscapeId, {
        bpm: request.bpm,
        density: request.density,
        intensity: request.intensity ?? 0.5,
        seed: request.seed ?? 0,
        key: request.key,
        progressionIndex: request.progressionIndex,
        drums: request.drums,
      });

      const sameSoundscape =
        this.playing && this.recipe && this.recipe.soundscape.id === recipe.soundscape.id;

      if (sameSoundscape) {
        // 同一个音景只是调速度/密度/调性：不重建链路，避免可听见的断点。
        const keyChanged = Boolean(recipe.key) && recipe.key !== this.key;
        this.recipe = recipe;
        if (keyChanged) this.key = recipe.key;
        if (Number.isInteger(recipe.progressionIndex)) {
          this.progressionIndex = recipe.progressionIndex;
        }
        this.applyTempo();
        this.applyIntensity();
        // 换调性必须立刻出声：音高只在 renderBar 里现算，不补这一下的话
        // 用户点完新调性要等满一个和弦时长（慢速音景是 8 秒）才听到变化，
        // 中途那段会像是「点了没反应」。
        if (keyChanged) this.enter();
        return;
      }

      const previous = this.nodes;
      const fade = opts.crossfade ?? 1.8;

      this.recipe = recipe;
      this.barIndex = 0;
      this.progressionIndex = 0;
      this.passIndex = -1;
      this.key = this.pickKey(recipe);
      this.rng = mulberry32(seedFrom(`${recipe.soundscape.id}:${recipe.seed}:${recipe.bpm}`));
      this.nodes = this.assemble(recipe);
      this.nodes.chain.master.gain.value = 0;
      // buildDrums 只给了基础音量，档位那一档增益得在这里补上——play() 起新音景
      // 不会走 applyIntensity()，漏了这句的话「强」只有多出来的两层，没有音量。
      this.applyDrumMix();

      // 速度一步到位，不做渐变：Tone 在速度渐变期间秒↔tick 的换算不再是
      // 线性的，此时去排事件会把时间点算错。
      this.applyTempo(true);

      // 丢掉旧的排期，按新音景的速度重新起拍。
      this.cancelBar();
      this.ensureLoop();
      this.playing = true;

      const token = ++this.fadeToken;
      this.nodes.chain.master.gain.rampTo(this.targetGain(), fade);
      if (previous) this.retire(previous, fade, token);

      // 切音景时旧音景最多还会响到本小节结束，这里补一个「入场和弦」，
      // 让新音景立刻出声，不必等下一个整小节（慢速音景要等 4 秒）。
      this.enter();
    }

    /** 当前 BPM 下每秒多少 tick。只用来把「秒」换算成保险量，小节长度恒为 4×PPQ。 */
    ticksPerSecond() {
      const transport = window.Tone.getTransport();
      return (transport.bpm.value / 60) * transport.PPQ;
    }

    /**
     * 自己排每一小节，不用 Tone.Loop。
     *
     * 起因一：切几次音景之后 onBar 就不再被调用了（实测第 3 小节之后彻底静音，
     * 且不抛异常）。Tone.Loop 内部按「回调收到的音频时间 + 构造时算出的固定
     * 秒数」自我重排，而 ToneEvent.start() 把参数当 transport 秒数用——两者
     * 不同源，位置会逐渐落到过去，于是事件被调度器丢弃。
     *
     * 起因二（2026-10-05 定位）：改用「绝对秒数」当基准后仍有洞。schedule()
     * 收到秒数时，Tone 用**当前 BPM** 把整段秒数一次性换算成 tick
     * （at × bpm/60 × PPQ）；而真正决定事件何时触发的 tick 计数器，在每次
     * BPM 被写入（换音景、拖速度都会写）时会重算原点。两个刻度从此固定错开，
     * 事件落到错误的 tick 上——听感就是演奏中随机静默 1~5 秒再自愈。
     *
     * 所以现在锚点只用 tick（音乐时间）：一小节 = 4 拍 × PPQ = 768 tick，
     * 与 BPM 无关；每次回调现取 transport.ticks 重新锚定，掉队自动吸附。
     */
    ensureLoop() {
      const transport = window.Tone.getTransport();
      if (transport.state !== "started") {
        transport.position = 0;
        transport.start("+0.04");
      }
      // 每次起链都重新对表，而且必须显式归零：写入 BPM 之后计数器原点会被
      // Tone 内部重算，紧接着读到的 tick 还是旧刻度的值（实测换音景瞬间读到
      // 的是上一个音景累计的 tick 数），拿它当锚会凭空多等十几秒。
      // 归零由本引擎定义新原点，网格永远从「现在」重新起。
      transport.ticks = 0;
      this.lastScheduledTick = 0;
      this.nextBarTick = this.ticksPerSecond() * 0.35;
      this.armed = true;
      this.scheduleNextBar();
    }

    scheduleNextBar() {
      if (!this.armed || !this.recipe) return;
      const Tone = window.Tone;
      const transport = Tone.getTransport();
      const rate = this.ticksPerSecond();
      // 调度器有约 0.1 s 的 lookAhead，落在窗口内的事件会被丢掉；
      // 同时兜住任何可能的时间倒流。
      const floor = Math.max(
        transport.ticks + rate * 0.12,
        (this.lastScheduledTick || 0) + rate * 0.02
      );
      const at = Math.round(Math.max(this.nextBarTick, floor));
      this.nextBarTick = at + transport.PPQ * 4;
      this.lastScheduledTick = at;
      const eventId = transport.schedule((time) => {
        // 放完立刻摘掉：schedule() 的事件不是一次性的，默认留在时间线里；
        // tick 计数器换过原点后同一个 tick 值会被再走一遍，陈旧事件会被
        // 二次触发（实测过，时序会乱）。
        transport.clear(eventId);
        if (!this.armed) return;
        this.onBar(time);
        this.scheduleNextBar();
      }, new Tone.Ticks(at));
      this.barEventId = eventId;
    }

    cancelBar() {
      this.armed = false;
      if (this.barEventId !== undefined && this.barEventId !== null) {
        window.Tone.getTransport().clear(this.barEventId);
        this.barEventId = null;
      }
    }

    /** 入场和弦：切音景的瞬间先把这个音景的第一个和弦放出来。 */
    enter() {
      if (!this.nodes || !this.recipe) return;
      const now = window.Tone.now() + 0.05;
      const { soundscape } = this.recipe;
      const symbol = soundscape.progressions[this.progressionIndex % soundscape.progressions.length][0];
      const chordSeconds = (60 / this.recipe.bpm) * 4 * soundscape.barsPerChord;
      const hear = 0.34 + this.recipe.intensity * 0.18;

      if (this.nodes.voices.keys && soundscape.voices.keys && soundscape.voices.keys.mode !== "sparse") {
        const notes = Theory.chordNotes(symbol, this.key, {
          octave: soundscape.voices.keys.octave,
          voicing: 0,
          omit: ["B"],
        });
        const gates = [];
        const step = chordSeconds / 4;
        notes.forEach((note, index) => {
          this.nodes.voices.keys.triggerAttackRelease(note, chordSeconds * 0.9, now + index * step * 0.5, hear);
          gates.push(note);
        });
      }
      if (this.nodes.voices.pad && soundscape.voices.pad) {
        const notes = Theory.chordNotes(symbol, this.key, {
          octave: soundscape.voices.pad.octave,
          voicing: 0,
        });
        this.nodes.voices.pad.triggerAttackRelease(notes, chordSeconds * 1.05, now, hear * 0.6);
      }
      if (this.nodes.voices.bass && soundscape.voices.bass) {
        const root = Theory.rootNote(symbol, this.key, soundscape.voices.bass.octave);
        this.nodes.voices.bass.triggerAttackRelease(root, chordSeconds * 0.85, now, 0.5);
      }
    }

    /**
     * 换一组随机选择（调性 + 和声进行），声音不断。
     *
     * 用户手动钉住的轴不参与抽签：钉了调性就只换进行，钉了进行就只换调性，
     * 两个都钉住时只换装饰音的种子（琶音与留白会变，和声骨架不动）。
     * 不做这个区分的话，「换一段」会把用户刚挑好的东西冲掉——那和
     * 「完全的自定义」是相反的。
     */
    shuffle() {
      if (!this.recipe) return;
      const pinnedKey = Boolean(this.recipe.key);
      const pinnedProgression = Number.isInteger(this.recipe.progressionIndex);

      this.recipe.seed = (this.recipe.seed + 1) % 997;
      this.rng = mulberry32(seedFrom(`${this.recipe.soundscape.id}:${this.recipe.seed}:${this.recipe.bpm}`));
      if (!pinnedKey) this.key = this.pickKey(this.recipe);
      if (!pinnedProgression) {
        this.progressionIndex = Math.floor(this.rng() * this.recipe.soundscape.progressions.length);
      }
      this.barIndex = 0;
      this.passIndex = -1;
      this.enter();
    }

    setIntensity(value) {
      if (!this.recipe) return;
      this.recipe.intensity = Math.max(0, Math.min(1, Number(value) || 0));
      this.applyIntensity();
    }

    /**
     * 节奏量 0–1：每小节发多少个音。
     *
     * **和 intensity 是两件事**，早先它们被同一个滑杆写坏过：界面上的「疏密」
     * 调的是 intensity，而 intensity 同时管着音符数量、滤波亮度和主音量，
     * 于是用户拖「疏密」会顺带把响度也改了——他以为自己只调了音符。
     * 现在密度只管音符，亮度与响度归 intensity。
     */
    setDensity(value) {
      if (!this.recipe) return;
      const next = Library.clampDensity(value);
      if (next === null) return;
      this.recipe.density = next;
    }

    currentDensity() {
      return this.recipe ? this.recipe.density : null;
    }

    setBpm(value) {
      if (!this.recipe) return;
      // 夹到全局硬边界（40–200），**不再**夹到音景自己的推荐区间——
      // 推荐是给方向用的，不是给手铐用的。
      const next = Library.clampBpm(value);
      if (next === null) return;
      this.recipe.bpm = next;
      this.applyTempo();
    }

    /**
     * 换调性。音高在 renderBar 里现算，不用重建链路，但要立刻让人听见。
     * 传 null / 空串 = 解除钉住，交回种子在音景推荐调里挑。
     */
    setKey(value) {
      if (!this.recipe) return;
      this.recipe.key = Library.isValidKey(value) ? String(value).toUpperCase() : null;
      this.key = this.pickKey(this.recipe);
      this.enter();
    }

    /**
     * 换鼓点档位。传 null / 非法值 = 解除钉住，回落到音景自己的推荐档位。
     *
     * 不用重建链路，也不用 enter()：鼓点是整小节一次性排期的，本小节已经
     * 排出去的鼓改不了，下一小节自然读到新档位。所以点完最迟一小节内听到
     * 变化——这也符合「打点」这件事的直觉，总不会有人指望鼓点从半拍中间
     * 换掉。
     */
    setDrums(value) {
      if (!this.recipe) return;
      this.recipe.drums = Library.isDrumLevel(value) ? String(value) : null;
      // 音量立刻渐变过去，打几层等下一小节——听感上是「鼓变响了」而不是「啪一下
      // 换了套鼓」，中间不会出现半小节的空档。
      this.applyDrumMix();
    }

    currentDrums() {
      if (!this.recipe) return null;
      return this.recipe.drums || this.recipe.soundscape.groove.drumLevel || "none";
    }

    /** 指定和声进行。传 null 表示交回种子随机。 */
    setProgression(index) {
      if (!this.recipe) return;
      const count = this.recipe.soundscape.progressions.length;
      const next = Number.isInteger(index) && index >= 0 ? index % count : null;
      this.recipe.progressionIndex = next;
      if (next !== null) this.progressionIndex = next;
      this.passIndex = -1;
      this.enter();
    }

    setMuted(value) {
      this.muted = Boolean(value);
      if (this.nodes) this.nodes.chain.master.gain.rampTo(this.muted ? 0 : this.targetGain(), 0.35);
    }

    /** 音乐音量 0–1（只作用于旋律声部，鼓点有自己的滑杆）。 */
    setVolume(value) {
      this.volume = Math.max(0, Math.min(1, Number(value) || 0));
      if (this.nodes) this.nodes.chain.bus.gain.rampTo(this.volume, 0.2);
    }

    /**
     * 鼓点音量 0–1，70% 即 0 dB 基准。和音乐音量分开是刻意的：用户抱怨
     * 「音乐盖过鼓点」时，能立刻自己拧回来，而不是只能整体调小。
     */
    setDrumVolume(value) {
      this.drumVolume = Math.max(0, Math.min(1, Number(value) || 0));
      this.applyDrumMix();
    }

    /**
     * 当前速度偏离该音景推荐区间多远，用于界面上诚实地说明「已按你现在的
     * 状态调慢」。落在推荐区间内返回 null——没偏离就别说自己调过。
     *
     * 语义和早先不同：以前比的是区间中点（区间内也算「偏」），解耦之后
     * 区间内是正常的，「偏」只应该指跑到区间外面去了。
     */
    describeShift() {
      if (!this.recipe) return null;
      const band = Library.recommendFor(this.recipe.soundscape.id).bpm;
      if (this.recipe.bpm >= band[0] && this.recipe.bpm <= band[1]) return null;
      const middle = (band[0] + band[1]) / 2;
      const delta = this.recipe.bpm - middle;
      return { delta: Math.round(delta), direction: delta < 0 ? "slower" : "faster" };
    }

    async stop(fade = 1.1) {
      if (!this.nodes) {
        this.playing = false;
        return;
      }
      const token = ++this.fadeToken;
      // 解除武装并清掉已排进时间线的回调节点。
      this.cancelBar();
      const nodes = this.nodes;
      try {
        nodes.chain.master.gain.rampTo(0, fade);
      } catch (error) {
        console.warn("[engine] 淡出失败", error);
      }
      // 暂停不等于「卸下这段曲子」：recipe / key 照留。界面上的节拍与调性
      // 读数、三个滑杆、音景卡片的选中态都要继续指向刚才在放的那一段，
      // 恢复播放也才能从 currentRequest() 拿回**用户当下看到的**参数。
      this.playing = false;
      await new Promise((resolve) => window.setTimeout(resolve, fade * 1000 + 120));
      if (token !== this.fadeToken) return; // 等待期间又启动了新的一段
      // transport 停在这里不复位，下次 play 时若已停止会自己复位到 0。
      if (window.Tone.getTransport().state === "started") window.Tone.getTransport().stop();
      this.nodes = null;
      this.disposeNodes(nodes);
    }

    /** 主输出的电平（0–1），供可视化用。 */
    getLevel() {
      if (!this.nodes || !this.nodes.chain.meter) return 0;
      const value = this.nodes.chain.meter.getValue();
      const db = Array.isArray(value) ? Math.max(...value) : value;
      if (!Number.isFinite(db)) return 0;
      return Math.max(0, Math.min(1, (db + 60) / 60));
    }

    /**
     * 256 段频谱（dB 数组，大致落在 -150…-30 之间），供可视化用。
     * 是线性分频的原始 FFT，对数分频是前端呈现的事。
     */
    getSpectrum() {
      if (!this.nodes || !this.nodes.chain.analyser) return null;
      const values = this.nodes.chain.analyser.getValue();
      return values ? Array.from(values) : null;
    }

    // ── 内部：构建 ────────────────────────────────────────────

    pickKey(recipe) {
      // 用户明确指定了调性就用它；否则按种子从该音景的推荐调里挑。
      // 推荐列表是精选过的（比如「晨光」给的是 C/F/G/D/A#），不是十二个
      // 全上——某些调配某些音色会浑浊，这一层筛选是音景存在的意义之一。
      if (recipe.key && Library.isValidKey(recipe.key)) return recipe.key;
      const rng = mulberry32(seedFrom(`${recipe.soundscape.id}:key:${recipe.seed}`));
      return recipe.soundscape.keys[Math.floor(rng() * recipe.soundscape.keys.length)];
    }

    buildChain(space) {
      const Tone = window.Tone;
      const limiter = new Tone.Limiter(-2);
      const master = new Tone.Gain(0);
      const reverb = new Tone.Reverb({ decay: space.reverb, preDelay: 0.02, wet: space.wet });
      const chorus = new Tone.Chorus({
        frequency: 0.35,
        delayTime: 3.5,
        depth: space.chorus,
        wet: 0.5,
      }).start();
      const filter = new Tone.Filter({
        type: "lowpass",
        frequency: space.filter,
        rolloff: -12,
        Q: 0.5,
      });
      // 延迟走一条独立支路再汇入混响，避免干声被重复叠加。
      const delay = new Tone.FeedbackDelay({ delayTime: "8n.", feedback: 0.24, wet: space.delay });
      // 音乐总线：**音乐音量滑杆就挂在这个节点上**。它和鼓组总线是并列的
      // 两条路，各自被自己的滑杆控制——「音乐太大盖过鼓点」这件事，用户自己
      // 就能拧回来，不必等我们改代码。
      const bus = new Tone.Gain(this.volume);
      const meter = new Tone.Meter({ smoothing: 0.85 });
      // 256 段而不是 64：低频那几个 bin 挤在一起，64 段画出来的频谱
      // 左边一坨、右边空荡。分辨率高一点，前端才有余量做对数分频。
      const analyser = new Tone.Analyser("fft", 256);

      filter.connect(chorus);
      chorus.connect(reverb);
      filter.connect(delay);
      delay.connect(reverb);
      reverb.connect(limiter);
      limiter.connect(master);
      master.connect(meter);
      master.connect(analyser);
      master.toDestination();

      return { limiter, master, reverb, chorus, filter, delay, bus, meter, analyser };
    }

    buildVoice(timbre, gain) {
      const Tone = window.Tone;
      let voice;
      if (timbre.kind === "fm") {
        voice = new Tone.PolySynth(Tone.FMSynth, {
          harmonicity: timbre.harmonicity,
          modulationIndex: timbre.modulationIndex,
          oscillator: timbre.oscillator,
          envelope: timbre.envelope,
          modulation: timbre.modulation,
          modulationEnvelope: timbre.modulationEnvelope,
        });
      } else if (timbre.kind === "am") {
        voice = new Tone.PolySynth(Tone.AMSynth, {
          harmonicity: timbre.harmonicity,
          oscillator: timbre.oscillator,
          envelope: timbre.envelope,
          modulation: timbre.modulation,
          modulationEnvelope: timbre.modulationEnvelope,
        });
      } else if (timbre.kind === "mono") {
        voice = new Tone.MonoSynth({
          oscillator: timbre.oscillator,
          filter: timbre.filter,
          envelope: timbre.envelope,
          filterEnvelope: timbre.filterEnvelope,
        });
      } else {
        voice = new Tone.PolySynth(Tone.Synth, {
          oscillator: timbre.oscillator,
          envelope: timbre.envelope,
        });
      }
      voice.volume.value = gain;
      // 密集的琶音容易堆满音符，给和弦类声部一个明确上限。
      if (voice instanceof Tone.PolySynth) voice.maxPolyphony = 20;
      return voice;
    }

    buildDrums() {
      const Tone = window.Tone;
      // 底鼓：低、短、有芯。decay 0.34→0.26、octaves 5→4 是往「积极」调：
      // 同样音量下，拖长尾巴听起来是「咚——」（闷），收短了才是「咚！」（推人）。
      const kick = new Tone.MembraneSynth({
        pitchDecay: 0.03,
        octaves: 4,
        oscillator: { type: "sine" },
        envelope: { attack: 0.001, decay: 0.26, sustain: 0.01, release: 0.3 },
      });
      // 军鼓：原先用 brown 噪声——那是噪声里最暗的一种，再叠低通就是一团闷响。
      // 换白噪声 + 300 Hz 高通，留下的是「啪」而不是「噗」。
      const snare = new Tone.NoiseSynth({
        noise: { type: "white" },
        envelope: { attack: 0.001, decay: 0.09, sustain: 0 },
      });
      // 踩镲：咔哒声全在 8 kHz 以上。原来它过的是 7 kHz **低通**，等于把唯一
      // 能「咔」的那一段先砍掉，只剩噪声的低频尾巴——这是「闷」的根。现在反
      // 过来：6 kHz 高通，衰减收到 20 ms，短才脆。
      const hat = new Tone.NoiseSynth({
        noise: { type: "white" },
        envelope: { attack: 0.0005, decay: 0.02, sustain: 0 },
      });

      // 三层各自整形：底鼓只要低频、军鼓砍掉胸腔以下、踩镲只留最上面一截。
      // 原先三层共用一个 7 kHz 低通（"glue"），对底鼓无所谓，对踩镲是致命的。
      const kickTone = new Tone.Filter({ type: "lowpass", frequency: 3800, rolloff: -12 });
      const snareTone = new Tone.Filter({ type: "highpass", frequency: 280, rolloff: -12 });
      const hatTone = new Tone.Filter({ type: "highpass", frequency: 6000, rolloff: -12 });
      kick.volume.value = DRUM_BASE_VOLUME.kick;
      snare.volume.value = DRUM_BASE_VOLUME.snare;
      hat.volume.value = DRUM_BASE_VOLUME.hat;
      kick.connect(kickTone);
      snare.connect(snareTone);
      hat.connect(hatTone);

      // 鼓组总线。它（而不是每一层）承担「档位增益 + 用户滑杆」，于是「鼓点多响」
      // 只有一个旋钮，不会出现三处各调一点、加起来听不出谁说了算。
      const drumGain = new Tone.Volume(0);
      [kickTone, snareTone, hatTone].forEach((node) => node.connect(drumGain));
      return { kick, snare, hat, kickTone, snareTone, hatTone, drumGain };
    }

    assemble(recipe) {
      const chain = this.buildChain(recipe.soundscape.space);
      const voices = {};
      Object.entries(recipe.soundscape.voices).forEach(([name, spec]) => {
        const timbre = TIMBRES[spec.timbre];
        if (!timbre) return;
        const voice = this.buildVoice(timbre, spec.gain);
        voice.connect(chain.bus);
        voices[name] = voice;
      });
      const drums = this.buildDrums();
      // 鼓组直接进 master，**绕开音乐那条链**。两个理由：
      //   1) 那条链上的低通（space.filter，多数音景 7 kHz 上下）会把刚做出来的
      //      高频咔哒声又砍回去，等于白改；
      //   2) 合唱（wet 0.5）加在鼓上是把瞬态抹开，正是「鼓点不干脆」的另一半原因。
      // 代价是鼓不带混响——对节拍来说这恰恰是好事：干才紧。
      drums.drumGain.connect(chain.master);
      chain.bus.connect(chain.filter);
      return { chain, voices, drums };
    }

    retire(nodes, fade, token) {
      nodes.chain.master.gain.rampTo(0, fade * 0.7);
      window.setTimeout(() => {
        if (token !== this.fadeToken) return;
        this.disposeNodes(nodes);
      }, fade * 1000 + 220);
    }

    disposeNodes(nodes) {
      if (!nodes) return;
      try {
        Object.values(nodes.voices || {}).forEach((node) => node.dispose && node.dispose());
        Object.values(nodes.drums || {}).forEach((node) => node.dispose && node.dispose());
        Object.values(nodes.chain || {}).forEach((node) => node.dispose && node.dispose());
      } catch (error) {
        console.warn("[engine] 释放节点失败", error);
      }
    }

    // ── 内部：参数 ────────────────────────────────────────────

    targetGain() {
      const intensity = this.recipe ? this.recipe.intensity : 0.5;
      // 0.74 是长时间聆听的安全上限；强度只在 ±20% 内浮动。
      // 用户音量不在这里乘——音乐音量在 chain.bus、鼓点在 drums.drumGain，
      // 两边各自独立（混在一起就分不开了，那正是这次要修的问题）。
      return 0.5 + intensity * 0.24;
    }

    /**
     * @param {boolean} [immediate] 立即生效而不是渐变。开新音景时必须立即，
     *   否则渐变期间排循环会失准（见 play() 里的说明）。
     */
    applyTempo(immediate = false) {
      if (!this.recipe) return;
      const transport = window.Tone.getTransport();
      if (immediate) {
        transport.bpm.cancelScheduledValues(0);
        transport.bpm.value = this.recipe.bpm;
      } else {
        transport.bpm.rampTo(this.recipe.bpm, 0.4);
      }
      transport.swing = this.recipe.soundscape.groove.swing || 0;
      transport.swingSubdivision = "8n";
    }

    applyIntensity() {
      if (!this.nodes || !this.recipe) return;
      const intensity = this.recipe.intensity;
      const space = this.recipe.soundscape.space;
      // 强度映射到「亮度」和「响度」：累了就关一点高频、收一点音量。
      this.nodes.chain.filter.frequency.rampTo(space.filter * (0.72 + intensity * 0.5), 0.8);
      this.nodes.chain.master.gain.rampTo(this.muted ? 0 : this.targetGain(), 0.8);
      this.applyDrumMix();
    }

    /**
     * 鼓的音量分两处，各管各的：
     *   - 三层各自的 volume 只管**三层之间的配比**（底鼓厚、踩镲脆），加上
     *     强度偏移——累了就把鼓整体收一点；
     *   - 总线 drumGain 管**鼓组相对音乐有多响**，由「档位增益 + 用户滑杆」决定。
     * 写成一处是因为它有四个触发源：拖节奏量、点鼓点芯片、拖鼓点滑杆、换音景。
     */
    applyDrumMix() {
      if (!this.nodes || !this.nodes.drums) return;
      const drums = this.nodes.drums;
      const intensity = this.recipe ? this.recipe.intensity : 0.5;
      const lift = -6 * (intensity - 0.5);
      drums.kick.volume.rampTo(DRUM_BASE_VOLUME.kick + lift * 0.4, 0.5);
      drums.snare.volume.rampTo(DRUM_BASE_VOLUME.snare + lift, 0.5);
      drums.hat.volume.rampTo(DRUM_BASE_VOLUME.hat + lift * 1.4, 0.5);
      const levelGain = DRUM_LEVEL_GAIN_DB[this.currentDrums()] || 0;
      drums.drumGain.volume.rampTo(levelGain + drumVolumeToDb(this.drumVolume), 0.5);
    }

    // ── 内部：演奏 ────────────────────────────────────────────

    /** 演奏一小节：把这一小节要发的所有音符排好队交给 Tone.js 调度。 */
    onBar(time) {
      try {
        this.renderBar(time);
      } catch (error) {
        // 调度器在音频线程里跑，抛出去的异常会被静默吞掉，表现为
        // 「这个音景没声音」而完全看不出原因。这里兜住并留证。
        this.lastError = `${error && error.message ? error.message : error}`;
        console.error("[engine] 演奏出错", error);
      }
    }

    renderBar(time) {
      if (!this.nodes || !this.recipe) return;
      const Tone = window.Tone;
      const { soundscape, bpm, intensity, density } = this.recipe;
      const voicesSpec = soundscape.voices;
      const voices = this.nodes.voices;
      const rng = this.rng;
      const bar = this.barIndex;

      const barSeconds = (60 / bpm) * 4;
      const stepSeconds = barSeconds / 16;
      const humanize = () => (rng() - 0.5) * 0.014;

      // 节奏量 → 音符数量与放行门槛。
      //
      // `0.6 + density * 0.8` 让 density = 0.5 时倍率恰好是 1.0，也就是
      // 「按乐谱原样演奏」；两端分别约 0.6 倍（明显更空）和 1.4 倍（明显
      // 更密）。不直接用 density 当倍率，是因为那样 0.5 只能得到一半的音符，
      // 推荐区间的中点会听起来比改版前稀薄一大截。
      const densityScale = 0.6 + density * 0.8;
      // 门槛：density = 0.5 时等于 base，向两端各放开 span / 2。
      const gate = (base, span) => base + (density - 0.5) * span;
      // 按倍率缩放原谱上的 rate，再夹进 [floor, ceil]。
      const hitsFor = (rate, floor, ceil) =>
        Math.max(floor, Math.min(ceil, Math.round((rate || floor) * densityScale)));

      // 每若干小节换一条和声进行。
      const passIndex = Math.floor(bar / (BARS_PER_PROGRESSION_PASS * soundscape.barsPerChord));
      if (passIndex !== this.passIndex) {
        this.passIndex = passIndex;
        if (passIndex > 0) {
          this.progressionIndex = Math.floor(rng() * soundscape.progressions.length);
        }
      }
      const progression = soundscape.progressions[this.progressionIndex % soundscape.progressions.length];
      const chordIndex = Math.floor(bar / soundscape.barsPerChord) % progression.length;
      const symbol = progression[chordIndex];
      const isChordChange = bar % soundscape.barsPerChord === 0;
      const chordSeconds = barSeconds * soundscape.barsPerChord;

      // —— 主奏 / 键盘声部 ——
      if (voices.keys && voicesSpec.keys) {
        const spec = voicesSpec.keys;
        const notes = Theory.chordNotes(symbol, this.key, {
          octave: spec.octave,
          voicing: chordIndex,
          omit: ["B"],
        });
        if (spec.mode === "block" || spec.mode === "sustain") {
          if (isChordChange) {
            voices.keys.triggerAttackRelease(
              notes, chordSeconds * 0.92, time + humanize(), 0.5 + intensity * 0.2
            );
          }
        } else if (spec.mode === "broken") {
          // 分解和弦：低—高—中—高的次序比均匀琶音更有呼吸。
          const order = [0, 2, 1, 3, 0, 2, 3, 1];
          // 上限从 8 提到 12：原谱最快的分解和弦写的就是 rate 8，若仍夹在 8，
          // 节奏量拉到最右一个音都不多，滑杆的右半边会是死的。
          const hits = hitsFor(spec.rate, 2, 12);
          const spacing = barSeconds / hits;
          for (let step = 0; step < hits; step += 1) {
            const at = time + step * spacing;
            const note = notes[order[step % order.length] % notes.length];
            const velocity = 0.34 + (step % 2 === 0 ? 0.14 : 0) + intensity * 0.14;
            voices.keys.triggerAttackRelease(note, spacing * 1.6, at + humanize(), velocity);
          }
        } else if (spec.mode === "sparse") {
          const hits = hitsFor(spec.rate, 1, 8);
          for (let index = 0; index < hits; index += 1) {
            if (rng() > gate(0.55, 0.7)) continue;
            const at = time + Math.floor(rng() * 16) * stepSeconds;
            const note = notes[Math.floor(rng() * notes.length)];
            voices.keys.triggerAttackRelease(
              note, barSeconds * 0.8, at + humanize(), 0.24 + intensity * 0.16
            );
          }
        }
      }

      // —— 衬底 pad：铺满整个和弦时长 ——
      if (voices.pad && voicesSpec.pad && isChordChange) {
        const spec = voicesSpec.pad;
        const notes = Theory.chordNotes(symbol, this.key, { octave: spec.octave, voicing: 0 });
        voices.pad.triggerAttackRelease(notes, chordSeconds * 1.05, time, 0.24 + intensity * 0.12);
      }

      // —— 铃音 / 高音琶音 ——
      if (voices.bell && voicesSpec.bell) {
        const spec = voicesSpec.bell;
        const notes = Theory.chordNotes(symbol, this.key, {
          octave: spec.octave,
          voicing: 0,
          span: 20,
        });
        const hits = hitsFor(spec.rate, 1, 10);
        const spacing = barSeconds / hits;
        for (let step = 0; step < hits; step += 1) {
          // 变量名不能叫 gate——外层那个密度门槛 helper 已经占了。
          const keep = spec.mode === "arp" ? gate(0.6, 0.7) : gate(0.42, 0.6);
          if (rng() > keep) continue;
          const at = time + step * spacing + humanize();
          const note = notes[(step + chordIndex) % notes.length];
          voices.bell.triggerAttackRelease(note, barSeconds * 0.5, at, 0.16 + intensity * 0.12);
        }
      }

      // —— 贝斯 ——
      if (voices.bass && voicesSpec.bass) {
        const spec = voicesSpec.bass;
        const root = Theory.rootNote(symbol, this.key, spec.octave);
        if (spec.mode === "root") {
          if (isChordChange) {
            voices.bass.triggerAttackRelease(root, chordSeconds * 0.9, time, 0.5);
          }
        } else if (spec.mode === "pulse") {
          const beatSeconds = barSeconds / 4;
          for (let beat = 0; beat < 4; beat += 1) {
            if (beat % 2 === 1 && rng() > gate(0.5, 0.6)) continue;
            voices.bass.triggerAttackRelease(root, beatSeconds * 0.8, time + beat * beatSeconds, 0.44);
          }
        } else if (spec.mode === "walk") {
          // 走动感来自「和弦音 + 级进过渡音」交替，而不是硬走音阶。
          const scale = [0, 2, 4, 5, 7, 9, 11];
          const beatSeconds = barSeconds / 4;
          const rootMidi = Theory.midiFromName(root);
          for (let beat = 0; beat < 4; beat += 1) {
            const at = time + beat * beatSeconds;
            const useChordTone = beat === 0 || beat === 2 || rng() < 0.45;
            const note = useChordTone
              ? root
              : Theory.midiToName(rootMidi + scale[Math.floor(rng() * scale.length)]);
            voices.bass.triggerAttackRelease(note, beatSeconds * 0.72, at + humanize(), 0.42);
          }
        }
      }

      // —— 鼓组 ——
      // 性格来自音景（groove.drums，哪几格有重音），层数来自用户选的档位
      // （recipe.drums）。用户没选过就是 null，回落到音景自己的推荐档位。
      // 两层拆开才能同时有「同一条节奏型只留秒针」和「同一条节奏型全组上」。
      const pattern = DRUM_PATTERNS[soundscape.groove.drums];
      const drumLevel = this.recipe.drums || soundscape.groove.drumLevel || "none";
      const layers = DRUM_LAYERS[drumLevel] || DRUM_LAYERS.none;
      if (pattern && (layers.kick || layers.snare || layers.hat)) {
        const drums = this.nodes.drums;
        const scale = 0.5 + intensity * 0.85;
        if (layers.kick) {
          pattern.kick.forEach((step) => {
            drums.kick.triggerAttackRelease("C1", "8n", time + step * stepSeconds, pattern.level * scale);
          });
        }
        if (layers.snare) {
          pattern.snare.forEach((step) => {
            drums.snare.triggerAttackRelease("16n", time + step * stepSeconds, pattern.level * scale * 0.8);
          });
        }
        if (layers.hat) {
          pattern.hat.forEach((step, index) => {
            // 反拍踩镲轻一点、正拍重一点，这是 lo-fi 的基础律动。跑步音景的
            // 踩镲占满十六格，这条规则正好变成「八分位重、十六分位轻」。
            const accent = index % 2 === 0 ? 1 : 0.62;
            // 低节奏量时先砍反拍踩镲：它是鼓组里最不伤骨架、又最能听出「疏」
            // 的一层。按比例抽掉底鼓会让律动直接塌掉，砍踩镲不会。
            if (accent < 1 && density < 0.35) return;
            drums.hat.triggerAttackRelease(
              "32n", time + step * stepSeconds, pattern.level * scale * accent * 0.7
            );
          });
        }
      }

      this.barIndex += 1;
      const payload = { bar, chord: symbol, key: this.key, bpm };
      this._emit("bar", payload);
      // 可视化必须在音频线程的准确时刻触发，交给 Tone 的 Draw 调度。
      Tone.getDraw().schedule(() => this._emit("tick", payload), time);
    }
  }

  window.MCEngine = new CompanionEngine();
  window.MCEngineClass = CompanionEngine;
})();
