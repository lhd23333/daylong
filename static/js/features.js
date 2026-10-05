/*
 * 音频特征提取：把 engine 的 256 段 dB 频谱归纳成几个标量。
 *
 * 为什么要有这一层？
 * 频谱是 256 个裸数字，里面既有音乐也有噪声底，而且每帧都在抖。谁直接读它，
 * 谁就要自己再写一遍「滤噪声底 / 分频段 / 判节拍 / 做平滑」这四件事；写四遍
 * 就会长出四套手感不一样、还各自随音量崩掉的实现。集中做一次之后，渲染层
 * 就再也不碰 FFT 了——它只订阅 bass / mid / treble / rms / centroid / beat，
 * 于是同一套音频分析可以驱动任意多套视觉。
 * （这个分层借鉴自 Audio Shader Studio（MIT）：它同样把 FFT 压成几个标量再
 * 交给渲染层。只借思路，实现是自己写的。）
 *
 * 除了 centroid 是「谱心 ÷ 奈奎斯特」的比值（额外多给一个 centroidHz 便于
 * 调试），其余输出全部归一化到 0..1。
 */
(() => {
  "use strict";

  // ── 归一化参考值 ──────────────────────────────────────────
  //
  // 频谱是 dB，动态范围极大：静音段实测约 -150 dB，最响的 bin 到过 -35 dB 左右
  // （见 app.js 里 drawViz 的注释）。直接当线性值用会得到一堆 0；按 -150..-35
  // 全域归一化，又把所有反应都挤在最后 5% 里。
  //
  // 所以：-100 dB 以下一律按噪声底处理（这一段没有可听信息），-100..-30 映射到
  // 0..1。三个频段共用同一组参考值，于是「bass = 0.8」和「treble = 0.8」说的是
  // 同一件事——该频段的平均功率就是这么大。若每个频段各自归一化，三个数字就
  // 不可比了，也就没法用它做「音色偏暗还是偏亮」的判断。
  const FLOOR_DB = -100;
  const CEIL_DB = -30;
  const SPAN_DB = CEIL_DB - FLOOR_DB;

  // 频段切分点（Hz）。注意不能按 bin 数平均分——bin 是线性分频的，平均分出来的
  // 「低频段」会一路盖到 5 kHz 去，那不是低频。
  const BASS_HZ = 250;
  const MID_HZ = 2000;

  // 一帧至少要有这么多个 bin 才认为频谱有效（与 app.js 里 drawViz 的判据一致）。
  const MIN_BINS = 8;

  // 低于这个响度就当作没在放音乐。真实的静音经过 -100 dB 的门限后正好落在 0，
  // 所以这个阈值只要不是 0 就能把「停播」和「很轻的一段」分开。
  const ACTIVE_FLOOR = 0.03;

  // 平滑系数：上冲快、回落慢。
  // 上游的 AnalyserNode 自带一道 smoothingTimeConstant（Tone 默认 0.8），所以
  // 原始数据已经有一点惯性；这里的回落刻意比 drawViz 的 0.86 更慢，是为了让
  // 特征值喂给粒子时是一段连着的曲线，而不是一串峰。
  const ATTACK = 0.55;
  const RELEASE = 0.11;
  const CENTROID_ATTACK = 0.3;
  const CENTROID_RELEASE = 0.1;

  // 拍点检测。用「低频能量的上升沿」近似鼓点：这不是真正的 onset detection
  // （那需要相位/谱通量分析），只是拿当前值去比一个慢速滑动平均。对底鼓这种
  // 陡起陡落的包络足够用，对弦乐渐强会漏——这里如实说明，不假装它是 onset 检测。
  //
  // 阈值必须是自适应的，这一点是拿真实音源量出来的：铺底一直在响时低频常年
  // 停在 0.76 上下，若按「高于均值 X%」来判，X=18% 就意味着要再高出 9.6 dB，
  // 实测 16 秒里只触发 1 次（同一段音乐有 9 个底鼓）。改成按信号自身的起伏
  // （mean absolute deviation）定阈值之后，阈值跟着素材走：垫音平稳时降下来，
  // 素材本身忽大忽小时抬上去。
  const BEAT_AVG_RISE = 0.25; // 滑动平均的上升速率：快，让一次上冲只触发一次
  const BEAT_AVG_FALL = 0.05; // 下降速率：慢，避免长音期间不断误判
  const BEAT_SPREAD_RATE = 0.05; // 起伏量的跟踪速率
  const BEAT_MARGIN = 0.012; // 绝对余量（约 0.8 dB），压住稳态抖动
  const BEAT_SPREAD_K = 1; // 相对余量：至少高出「通常起伏」的同样多
  const BEAT_SPREAD_CAP = 0.05; // 起伏量的上限，防止素材本身很闹时阈值被抬飞
  const BEAT_MIN_LEVEL = 0.1; // 整体太轻时不判拍，避免安静段落乱闪
  const BEAT_BASE = 0.75; // 触发时的基础强度（保证「接近 1」）
  const BEAT_GAIN = 1.2; // 上冲越猛，脉冲越强，上限仍为 1
  const BEAT_DECAY = 0.86; // 每帧衰减，约 10 帧（0.17 s）落到 0.2 以下
  const BEAT_REFRACTORY = 9; // 两次拍之间的最小帧数（60fps 下约 150 ms）

  /**
   * 频段的平均功率，映射到 0..1。
   *
   * 平均在功率域做（dB → 能量 → 平均 → 再回 dB），不是把 dB 直接平均：dB 是对
   * 数量，直接平均会让一个 -40 dB 的强 bin 被一堆 -140 dB 的底噪 bin 平掉，
   * 物理上说不通。先转能量再平均，才是「这一段到底有多响」。
   *
   * 注意宽频段会被底噪稀释：高频段有两百多个 bin，只有少数几个真有能量时，
   * 平均值自然被拉低。这是真实情况，这里不做任何补偿——补偿就是在造数据。
   */
  function bandLevel(spectrum, from, to) {
    const count = to - from;
    if (count <= 0) return 0;
    let sum = 0;
    for (let index = from; index < to; index += 1) {
      const db = spectrum[index];
      if (!Number.isFinite(db)) continue;
      sum += Math.pow(10, Math.max(db, FLOOR_DB) / 10);
    }
    if (!(sum > 0)) return 0;
    const meanDb = 10 * Math.log10(sum / count);
    return clamp01((meanDb - FLOOR_DB) / SPAN_DB);
  }

  /**
   * 把两个 Hz 切分点落到 bin 上，得到三段首尾相接、互不重叠的区间。
   * 用 round 而不是 floor/ceil：256 段线性 FFT 的 bin 宽约 86 Hz，切分点本来就
   * 落不到 bin 边界上，取最近的 bin 边界偏差最小，也不会漏掉或重复算某个 bin。
   */
  function splitBins(binHz, binCount) {
    const clampBin = (value, low, high) => Math.max(low, Math.min(high, value));
    const bassTo = clampBin(Math.round(BASS_HZ / binHz), 1, binCount - 2);
    const midTo = clampBin(Math.round(MID_HZ / binHz), bassTo + 1, binCount - 1);
    return { bass: [0, bassTo], mid: [bassTo, midTo], treble: [midTo, binCount] };
  }

  /** 数一数有多少个能用的 bin，够 MIN_BINS 就提前收工（正常频谱几个 bin 就够）。 */
  function countFinite(spectrum) {
    let finite = 0;
    for (let index = 0; index < spectrum.length; index += 1) {
      if (Number.isFinite(spectrum[index])) {
        finite += 1;
        if (finite >= MIN_BINS) return finite;
      }
    }
    return finite;
  }

  function clamp01(value) {
    if (!Number.isFinite(value)) return 0;
    return value < 0 ? 0 : value > 1 ? 1 : value;
  }

  /** 上冲快、回落慢。同样的一阶平滑，方向不同系数就不同。 */
  function smooth(current, target, attack, release) {
    const rate = target > current ? attack : release;
    return current + (target - current) * rate;
  }

  /**
   * 建一个特征提取器。内部保存平滑与拍点检测的状态，所以每次播放都要新建一个；
   * 暂停或切曲时调 reset()，否则上一个状态会渗进来（表现为「刚点播放粒子就已经
   * 在使劲」）。
   *
   * @param {{sampleRate?:number, binCount?:number}} [opts]
   */
  function createExtractor(opts) {
    const options = opts || {};
    // 采样率只影响 bin→Hz 的换算。默认值优先向 Tone 现实取，取不到才用 44100：
    // 44100 与 48000 之间有 9% 的偏差，会让频段边界整体偏掉。
    const tone = typeof window !== "undefined" && window.Tone ? window.Tone : null;
    const contextRate =
      tone && typeof tone.getContext === "function" ? tone.getContext().sampleRate : 0;
    const sampleRate = Number(options.sampleRate) > 0 ? Number(options.sampleRate) : contextRate || 44100;
    const binCount = Number(options.binCount) > 0 ? Math.floor(Number(options.binCount)) : 256;

    const binHz = sampleRate / 2 / binCount;
    const bands = splitBins(binHz, binCount);

    // 平滑状态。centroid 单独一组系数：它不是瞬态量，跟着 bass 一起「上冲快」
    // 只会让音色忽明忽暗。
    let bass = 0;
    let mid = 0;
    let treble = 0;
    let rms = 0;
    let centroid = 0;
    let centroidHz = 0;

    // 拍点状态。
    let average = 0; // 低频的慢速滑动平均
    let spread = 0; // 低频通常的起伏幅度（平均绝对偏差）
    let pulse = 0; // 正在回落的脉冲
    let cooldown = 0;
    let primed = false; // 第一帧只用来给滑动平均定初值

    function reset() {
      bass = 0;
      mid = 0;
      treble = 0;
      rms = 0;
      centroid = 0;
      centroidHz = 0;
      average = 0;
      spread = 0;
      pulse = 0;
      cooldown = 0;
      primed = false;
    }

    /** 频谱不可用时的降级路径：让状态向 0 收敛，而不是「卡」在上一帧。 */
    function fade() {
      bass = smooth(bass, 0, ATTACK, RELEASE);
      mid = smooth(mid, 0, ATTACK, RELEASE);
      treble = smooth(treble, 0, ATTACK, RELEASE);
      rms = smooth(rms, 0, ATTACK, RELEASE);
      centroid = smooth(centroid, 0, CENTROID_ATTACK, CENTROID_RELEASE);
      centroidHz = smooth(centroidHz, 0, CENTROID_ATTACK, CENTROID_RELEASE);
      pulse *= BEAT_DECAY;
      primed = false;
      return result(false);
    }

    // 每帧返回一个新对象，而不是复用同一个：调用方可以把这一帧的结果存下来
    // 慢慢用，不必担心「下一帧就被覆写」这种隐蔽约定。每秒 60 个小对象的 GC
    // 代价可以忽略，不值得为它换来一类难查的 bug。
    function result(active) {
      return {
        bass: bass,
        mid: mid,
        treble: treble,
        rms: rms,
        centroid: centroid,
        // 额外给一个 Hz 版本：除以奈奎斯特之后典型值只有 0.005–0.02，窄得没法
        // 直接驱动参数，调用方需要时可以用这个自己换一条映射。
        centroidHz: centroidHz,
        beat: pulse,
        // level 取 rms 而不是 max(bass, mid, treble)：后者在配器一变（比如只剩
        // 贝斯在响）就会跳到接近 1，粒子跟着猛地放大；rms 是整条谱的平均，作为
        // 「这一刻整体有多响」更稳，也更诚实。
        level: rms,
        active: active,
      };
    }

    /**
     * 喂一帧频谱，返回这一帧的特征。
     * @param {number[]|null} spectrum engine.getSpectrum() 的返回值（256 个 dB）
     */
    function update(spectrum) {
      if (!spectrum || spectrum.length < MIN_BINS) return fade();
      if (countFinite(spectrum) < MIN_BINS) return fade();

      const usable = Math.min(spectrum.length, binCount);

      // 三个频段的能量。注意这几个是「未平滑」的原始值——拍点检测必须看原始值，
      // 拿平滑后的缓坡去比滑动平均，上冲早被抹平了。
      const rawBass = bandLevel(spectrum, bands.bass[0], Math.min(bands.bass[1], usable));
      const rawMid = bandLevel(spectrum, bands.mid[0], Math.min(bands.mid[1], usable));
      const rawTreble = bandLevel(spectrum, bands.treble[0], Math.min(bands.treble[1], usable));
      // 整体响度：跳过 bin 0（直流），它对「有多响」没有贡献。
      const rawRms = bandLevel(spectrum, 1, usable);

      // 谱心：能量加权的重心频率。bin 0 同样跳过，直流会把重心往 0 拽。
      // 用 bin 中心频率（index + 0.5）而不是下沿，少半个 bin 的系统偏差。
      //
      // 只统计门限以上的 bin。全都算进去的话，静音时所有 bin 一样低，重心就
      // 会落在谱的正中间（约 11 kHz）——一个「没有声音」的输入给出一个「音色
      // 很亮」的答案，那是彻头彻尾的假数据。
      let weighted = 0;
      let total = 0;
      for (let index = 1; index < usable; index += 1) {
        const db = spectrum[index];
        if (!Number.isFinite(db) || db <= FLOOR_DB) continue;
        const power = Math.pow(10, db / 10);
        weighted += power * (index + 0.5) * binHz;
        total += power;
      }
      const rawCentroidHz = total > 0 ? weighted / total : 0;

      // ── 拍点：低频的上升沿 ──
      // 先用「上一帧的均值与起伏量」定阈值，再更新这两个参考量——否则当前这一
      // 帧的上冲会同时抬高阈值，把自己判掉。
      const excess = rawBass - average;
      const threshold = BEAT_MARGIN + Math.min(spread, BEAT_SPREAD_CAP) * BEAT_SPREAD_K;
      let triggered = false;
      if (!primed) {
        // 第一帧只定初值。否则滑动平均从 0 起步，开播那一帧必定被误判成一次重拍。
        average = rawBass;
        spread = 0;
        primed = true;
      } else if (cooldown > 0) {
        cooldown -= 1;
      } else if (rawBass > BEAT_MIN_LEVEL && excess > threshold) {
        pulse = Math.min(1, BEAT_BASE + excess * BEAT_GAIN);
        cooldown = BEAT_REFRACTORY;
        triggered = true;
      }
      // 均值上快下慢：一次上冲只该触发一次，但长音期间不能一直举着。
      average += excess * (excess > 0 ? BEAT_AVG_RISE : BEAT_AVG_FALL);
      spread += (Math.abs(excess) - spread) * BEAT_SPREAD_RATE;
      if (!triggered) pulse *= BEAT_DECAY;

      // ── 平滑 ──
      bass = smooth(bass, rawBass, ATTACK, RELEASE);
      mid = smooth(mid, rawMid, ATTACK, RELEASE);
      treble = smooth(treble, rawTreble, ATTACK, RELEASE);
      rms = smooth(rms, rawRms, ATTACK, RELEASE);
      centroidHz = smooth(centroidHz, rawCentroidHz, CENTROID_ATTACK, CENTROID_RELEASE);
      centroid = clamp01(centroidHz / (sampleRate / 2));

      // active 看的是「这一帧有没有声音」，用的是平滑后的值，所以不会在阈值附近
      // 逐帧翻转——上层拿它来开关粒子，翻转一次就是一次可见的闪。
      return result(rms > ACTIVE_FLOOR || bass > ACTIVE_FLOOR);
    }

    // 解算出来的频段落在哪几个 bin 上，暴露出来便于调试（例如换采样率之后确认
    // 高频段没有宽到把中频也吞进去）。
    const debugBands = {
      binHz: binHz,
      bass: [bands.bass[0], bands.bass[1]],
      mid: [bands.mid[0], bands.mid[1]],
      treble: [bands.treble[0], bands.treble[1]],
    };

    return { update: update, reset: reset, bands: debugBands };
  }

  window.MCFeatures = {
    createExtractor: createExtractor,

    /** 归一化用的常量，暴露出来便于上层调试与自行换算。 */
    RANGES: Object.freeze({
      floorDb: FLOOR_DB,
      ceilDb: CEIL_DB,
      /** 频段切分点（Hz）。treble 的上界是奈奎斯特，由采样率决定。 */
      bassHz: Object.freeze([0, BASS_HZ]),
      midHz: Object.freeze([BASS_HZ, MID_HZ]),
      trebleHz: Object.freeze([MID_HZ, Infinity]),
      /** 低频段从 bin 0 起：它覆盖 0..86 Hz，底鼓的基频正好在这里，
       *  丢掉它等于砍掉一半的低频反应。 */
      defaultSampleRate: 44100,
      defaultBinCount: 256,
      minBins: MIN_BINS,
      activeFloor: ACTIVE_FLOOR,
    }),
  };
})();
