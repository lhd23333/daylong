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
  const DRUM_PATTERNS = {
    lofi: { kick: [0, 6, 10], snare: [4, 12], hat: [2, 6, 10, 14], level: 0.5 },
    soft: { kick: [0, 8], snare: [12], hat: [4, 12], level: 0.36 },
    pop: { kick: [0, 4, 8, 12], snare: [4, 12], hat: [0, 2, 4, 6, 8, 10, 12, 14], level: 0.46 },
    citypop: { kick: [0, 3, 7, 8, 11], snare: [4, 12], hat: [2, 6, 10, 14], level: 0.44 },
  };

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
      this.nextBarAt = 0;
      this.key = null;
      this.barIndex = 0;
      this.progressionIndex = 0;
      this.passIndex = -1;
      this.rng = mulberry32(1);
      this.listeners = { bar: new Set(), tick: new Set() };
      this.fadeToken = 0;
      this.armed = false;
      this.muted = false;
      this.volume = 0.7;
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
     * @param {{soundscapeId?:string, bpm?:number, intensity?:number, seed?:number}} request
     * @param {{crossfade?:number}} [opts]
     */
    async play(request = {}, opts = {}) {
      await this.unlock();
      const Tone = window.Tone;
      const recipe = Library.resolve(request.soundscapeId, {
        bpm: request.bpm,
        intensity: request.intensity ?? 0.5,
        seed: request.seed ?? 0,
      });

      const sameSoundscape =
        this.playing && this.recipe && this.recipe.soundscape.id === recipe.soundscape.id;

      if (sameSoundscape) {
        // 同一个音景只是调速度或密度：不重建链路，避免可听见的断点。
        this.recipe = recipe;
        this.applyTempo();
        this.applyIntensity();
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

    /**
     * 自己排每一小节，不用 Tone.Loop。
     *
     * 起因：切几次音景之后 onBar 就不再被调用了（实测第 3 小节之后彻底静音，
     * 且不抛异常）。Tone.Loop 内部按「回调收到的音频时间 + 构造时算出的固定
     * 秒数」自我重排，而 ToneEvent.start() 把参数当 transport 秒数用——两者
     * 不同源，位置会逐渐落到过去，于是事件被调度器丢弃。
     *
     * 这里改成显式的：所有时间都只用一个基准（transport.seconds），推进量
     * 按当前 BPM 现算，掉队时吸附到前方，行为和原因都看得见。
     */
    ensureLoop() {
      const transport = window.Tone.getTransport();
      if (transport.state !== "started") {
        transport.position = 0;
        transport.start("+0.04");
        this.nextBarAt = transport.seconds + 0.25;
      }
      // 注意：transport 已在运行时不能回头重设 nextBarAt。Transport 的
      // 事件时间线是单调递增的（Timeline({increasing:true})），往回排会
      // 直接抛 "The time must be greater than or equal to the last
      // scheduled time"。切换音景时沿用原来的小节网格，新速度从下一小节
      // 起生效——这本来也符合变速的直觉。
      this.armed = true;
      this.scheduleNextBar();
    }

    scheduleNextBar() {
      if (!this.armed || !this.recipe) return;
      const transport = window.Tone.getTransport();
      const barSeconds = (60 / this.recipe.bpm) * 4;
      // 调度器有约 0.1 s 的 lookAhead，落在窗口内的事件会被丢掉；
      // 同时兜住任何可能的时间倒流。
      const floor = Math.max(transport.seconds + 0.12, (this.lastScheduledAt || 0) + 0.02);
      let at = this.nextBarAt;
      if (!(at > floor)) at = floor;
      this.pendingBarAt = at;
      this.nextBarAt = at + barSeconds;
      this.lastScheduledAt = at;
      this.barEventId = transport.schedule((time) => {
        if (!this.armed) return;
        this.onBar(time);
        this.scheduleNextBar();
      }, at);
    }

    cancelBar() {
      this.armed = false;
      if (this.barEventId !== undefined && this.barEventId !== null) {
        window.Tone.getTransport().clear(this.barEventId);
        this.barEventId = null;
        // 复用刚腾出来的那个槽位重排，避免白白空掉一小节。
        if (this.pendingBarAt != null) this.nextBarAt = this.pendingBarAt;
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

    /** 换一组随机选择（调性 + 和声进行），声音不断。 */
    shuffle() {
      if (!this.recipe) return;
      this.recipe.seed = (this.recipe.seed + 1) % 997;
      this.key = this.pickKey(this.recipe);
      this.rng = mulberry32(seedFrom(`${this.recipe.soundscape.id}:${this.recipe.seed}:${this.recipe.bpm}`));
      this.progressionIndex = Math.floor(this.rng() * this.recipe.soundscape.progressions.length);
      this.barIndex = 0;
      this.passIndex = -1;
      this.enter();
    }

    setIntensity(value) {
      if (!this.recipe) return;
      this.recipe.intensity = Math.max(0, Math.min(1, Number(value) || 0));
      this.applyIntensity();
    }

    setBpm(value) {
      if (!this.recipe) return;
      const [low, high] = this.recipe.soundscape.bpm;
      this.recipe.bpm = Math.max(low, Math.min(high, Math.round(Number(value) || this.recipe.bpm)));
      this.applyTempo();
    }

    setMuted(value) {
      this.muted = Boolean(value);
      if (this.nodes) this.nodes.chain.master.gain.rampTo(this.muted ? 0 : this.targetGain(), 0.35);
    }

    /** 总音量 0–1。和 setMuted 分开：静音不该丢掉用户调好的音量。 */
    setVolume(value) {
      this.volume = Math.max(0, Math.min(1, Number(value) || 0));
      if (this.nodes && !this.muted) {
        this.nodes.chain.master.gain.rampTo(this.targetGain(), 0.2);
      }
    }

    /** 播放速度与推荐的差距，用于界面上诚实地说明「已按你现在的状态调慢」。 */
    describeShift() {
      if (!this.recipe) return null;
      const [low, high] = this.recipe.soundscape.bpm;
      const middle = (low + high) / 2;
      const delta = this.recipe.bpm - middle;
      if (Math.abs(delta) < 2) return null;
      return { delta: Math.round(delta), direction: delta < 0 ? "slower" : "faster" };
    }

    async stop(fade = 1.1) {
      if (!this.nodes) {
        this.playing = false;
        this.recipe = null;
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
      this.playing = false;
      this.recipe = null;
      this.key = null;
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
      const bus = new Tone.Gain(0.9);
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
      const kick = new Tone.MembraneSynth({
        pitchDecay: 0.035,
        octaves: 5,
        oscillator: { type: "sine" },
        envelope: { attack: 0.002, decay: 0.34, sustain: 0.01, release: 0.5 },
      });
      const snare = new Tone.NoiseSynth({
        noise: { type: "brown" },
        envelope: { attack: 0.002, decay: 0.13, sustain: 0 },
      });
      const hat = new Tone.NoiseSynth({
        noise: { type: "white" },
        envelope: { attack: 0.001, decay: 0.035, sustain: 0 },
      });
      kick.volume.value = -12;
      snare.volume.value = -26;
      hat.volume.value = -32;
      // 鼓组统一过一档低通，抹掉数字味的毛刺。
      const glue = new Tone.Filter({ type: "lowpass", frequency: 7000, rolloff: -12 });
      [kick, snare, hat].forEach((node) => node.connect(glue));
      return { kick, snare, hat, glue };
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
      drums.glue.connect(chain.bus);
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
      return (0.5 + intensity * 0.24) * this.volume;
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
      const drums = this.nodes.drums;
      if (drums) {
        const lift = -6 * (intensity - 0.5);
        drums.kick.volume.rampTo(-12 + lift * 0.4, 0.5);
        drums.snare.volume.rampTo(-26 + lift, 0.5);
        drums.hat.volume.rampTo(-32 + lift * 1.4, 0.5);
      }
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
      const { soundscape, bpm, intensity } = this.recipe;
      const voicesSpec = soundscape.voices;
      const voices = this.nodes.voices;
      const rng = this.rng;
      const bar = this.barIndex;

      const barSeconds = (60 / bpm) * 4;
      const stepSeconds = barSeconds / 16;
      const humanize = () => (rng() - 0.5) * 0.014;

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
          const hits = Math.max(2, Math.min(spec.rate || 8, 8));
          const spacing = barSeconds / hits;
          for (let step = 0; step < hits; step += 1) {
            const at = time + step * spacing;
            const note = notes[order[step % order.length] % notes.length];
            const velocity = 0.34 + (step % 2 === 0 ? 0.14 : 0) + intensity * 0.14;
            voices.keys.triggerAttackRelease(note, spacing * 1.6, at + humanize(), velocity);
          }
        } else if (spec.mode === "sparse") {
          const hits = Math.max(1, Math.min(spec.rate || 3, 6));
          for (let index = 0; index < hits; index += 1) {
            if (rng() > 0.5 + intensity * 0.35) continue;
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
        const hits = Math.max(1, Math.min(spec.rate || 4, 8));
        const spacing = barSeconds / hits;
        for (let step = 0; step < hits; step += 1) {
          const gate = spec.mode === "arp" ? 0.5 + intensity * 0.4 : 0.32 + intensity * 0.24;
          if (rng() > gate) continue;
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
            if (beat % 2 === 1 && rng() > 0.4 + intensity * 0.4) continue;
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
      const pattern = DRUM_PATTERNS[soundscape.groove.drums];
      if (pattern) {
        const drums = this.nodes.drums;
        const scale = 0.5 + intensity * 0.85;
        pattern.kick.forEach((step) => {
          drums.kick.triggerAttackRelease("C1", "8n", time + step * stepSeconds, pattern.level * scale);
        });
        pattern.snare.forEach((step) => {
          drums.snare.triggerAttackRelease("16n", time + step * stepSeconds, pattern.level * scale * 0.8);
        });
        pattern.hat.forEach((step, index) => {
          // 反拍踩镲轻一点、正拍重一点，这是 lo-fi 的基础律动。
          const accent = index % 2 === 0 ? 1 : 0.62;
          drums.hat.triggerAttackRelease(
            "32n", time + step * stepSeconds, pattern.level * scale * accent * 0.7
          );
        });
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
