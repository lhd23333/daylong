/*
 * 音景库 —— 程序化音乐的素材包。
 *
 * 一个「音景」不是一首固定的曲子，而是一套气质约束：调性、和声进行、
 * 音色、律动方式。引擎每次从这里抽取一组参数实时演奏，所以同一音景
 * 听一小时也不会重复，但始终在同一种气质里。
 *
 * 和声进行用级数记号写（1maj7 / 5dom9 / 6min7 …），配合 keys 可搬到
 * 任意调上，这是「8 个音景 → 上千种组合」的来源。
 *
 * ── 三条轴是解耦的 ────────────────────────────────────────────
 * 早先每个音景把速度区间写死成硬边界（`bpm: [58, 76]` 就意味着拖不出这个
 * 范围），音景和速度实际被绑在一起。现在拆成三条彼此独立的轴：
 *
 *     音景（本文件的每一条）  ×  节拍（LIMITS.bpm 内任意值）  ×  节奏量（0–1）
 *
 * 音景上写着的那两个区间**降级为推荐值**——后端按状态从中挑一个起点，
 * 界面上把它高亮出来，但用户可以拖到区间外，只要不越过 LIMITS 的全局
 * 硬边界。理由：伴侣应用的核心是完全的自定义，硬夹住用户的手是反的；
 * 但完全不给方向又会让人面对一堆滑杆不知道该往哪拖，所以留推荐、去强制。
 *
 * 于是下面 `bpm` / `density` 两个字段的语义是「推荐区间」而不是「合法
 * 区间」。真正的合法区间在 LIMITS。
 */
(() => {
  "use strict";

  /**
   * 全局硬边界。超出这里才算非法——推荐区间是软的，这两个是硬的。
   *
   * 节拍放到 40–200：原来各音景的区间大多落在 52–128 之间，合起来看还是
   * 窄。40 约等于一分钟 40 拍（很慢的铺底），200 是急板，再往外就不太能
   * 称之为「音乐」了，所以到这里为止。
   */
  const LIMITS = {
    bpm: [40, 200],
    density: [0, 1],
  };

  /**
   * 可选调性。**必须与 `theory.js` 的 PITCH_CLASSES 逐字一致**——那边只认
   * 升号写法（`C#` 而不是 `Db`），写错会在 keyRootMidi 里抛「未知调性」。
   * 两份清单分居两个文件是有风险的，但 theory.js 是纯乐理层、不该知道
   * 音景的存在，所以这里留一条注释守住同步。
   */
  const KEYS = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];

  // 复用的音色模板，避免每个音景重复一大段合成器参数。
  const TIMBRE = {
    // 电钢琴：FMSynth，泛音干净、衰减自然。
    electricPiano: {
      kind: "fm",
      harmonicity: 2.5,
      modulationIndex: 6,
      envelope: { attack: 0.012, decay: 1.6, sustain: 0.12, release: 2.6 },
      modulation: { type: "sine" },
      modulationEnvelope: { attack: 0.02, decay: 0.6, sustain: 0, release: 0.6 },
      oscillator: { type: "sine" },
    },
    // 钢片琴 / 八音盒：高 modulationIndex，音头清脆、尾巴长。
    bell: {
      kind: "fm",
      harmonicity: 3.01,
      modulationIndex: 14,
      envelope: { attack: 0.004, decay: 2.2, sustain: 0.02, release: 3.2 },
      modulation: { type: "sine" },
      modulationEnvelope: { attack: 0.002, decay: 0.4, sustain: 0, release: 0.4 },
      oscillator: { type: "sine" },
    },
    // 拨弦：三角波快衰减，接近尼龙弦吉他。
    pluck: {
      kind: "synth",
      oscillator: { type: "triangle" },
      envelope: { attack: 0.006, decay: 0.9, sustain: 0.04, release: 1.4 },
    },
    // 柔和衬底：慢起音、长释放。
    softPad: {
      kind: "am",
      harmonicity: 1.5,
      oscillator: { type: "sine" },
      envelope: { attack: 2.4, decay: 3, sustain: 0.6, release: 5 },
      modulation: { type: "triangle" },
      modulationEnvelope: { attack: 3.5, decay: 2, sustain: 0.4, release: 4 },
    },
    // 明亮合成器：锯齿波，带滤波扫动。
    brightSynth: {
      kind: "synth",
      oscillator: { type: "fatsawtooth", count: 3, spread: 24 },
      envelope: { attack: 0.06, decay: 0.7, sustain: 0.35, release: 1.6 },
    },
    // 弦乐群奏：锯齿轮廓 + 慢起音，靠滤波压暗。
    strings: {
      kind: "synth",
      oscillator: { type: "fatsawtooth", count: 5, spread: 36 },
      envelope: { attack: 1.8, decay: 1.5, sustain: 0.72, release: 3.4 },
    },
    bassRound: {
      kind: "mono",
      oscillator: { type: "sine" },
      filter: { Q: 1.2, type: "lowpass" },
      envelope: { attack: 0.02, decay: 0.5, sustain: 0.75, release: 0.9 },
      filterEnvelope: { attack: 0.01, decay: 0.4, sustain: 0.5, release: 0.8, baseFrequency: 120, octaves: 2.4 },
    },
    bassPick: {
      kind: "mono",
      oscillator: { type: "triangle" },
      filter: { Q: 2, type: "lowpass" },
      envelope: { attack: 0.008, decay: 0.36, sustain: 0.42, release: 0.5 },
      filterEnvelope: { attack: 0.005, decay: 0.22, sustain: 0.2, release: 0.5, baseFrequency: 180, octaves: 3.2 },
    },
  };

  /**
   * 每个音景的字段说明：
   *   id / name / en / blurb   身份与展示文案
   *   when / moods / scenes     匹配条件（与后端 soundscape.py 的清单保持一致）
   *   bpm: [下限, 上限]
   *   keys: 可用的调
   *   progressions: 和声进行（级数记号数组）
   *   barsPerChord: 每个和弦持续几小节
   *   voices: 各声部的音色与演奏法
   *   space: 空间感（混响 / 延迟 / 滤波）
   *   groove: 律动方式
   */
  const SOUNDSCAPES = [
    {
      id: "first-light",
      name: "晨光",
      en: "first light",
      blurb: "钢琴与弦乐慢慢铺开，像清晨第一缕光落在桌面上。",
      character: "温和、清醒、不催促",
      when: ["morning"],
      moods: ["平静", "专注", "开心"],
      scenes: ["sedentary", "any"],
      energy: ["低", "中低"],
      bpm: [58, 76],
      density: [0.15, 0.4],
      keys: ["C", "F", "G", "D", "A#"],
      progressions: [
        ["1maj7", "5add9", "6min7", "4maj7"],
        ["1maj9", "4maj7", "6min7", "5sus4"],
        ["6min7", "4maj7", "1maj7", "5sus4"],
        ["4maj7", "1maj7", "2min7", "5dom9"],
        ["1maj7", "3min7", "6min7", "4maj7"],
        ["4maj9", "5sus4", "3min7", "6min9"],
      ],
      barsPerChord: 2,
      voices: {
        keys: { timbre: "electricPiano", gain: -15, octave: 4, mode: "block", rate: 1 },
        pad: { timbre: "strings", gain: -27, octave: 3, mode: "sustain" },
        bell: { timbre: "bell", gain: -25, octave: 5, mode: "sparse", rate: 3 },
        bass: { timbre: "bassRound", gain: -18, octave: 1, mode: "root" },
      },
      groove: { drums: "none", swing: 0.06 },
      space: { reverb: 5.5, wet: 0.46, filter: 5200, chorus: 0.22, delay: 0.1 },
    },
    {
      id: "desk-hours",
      name: "书桌前",
      en: "desk hours",
      blurb: "电钢琴配一点松散的鼓，适合把注意力放在一件事上。",
      character: "稳定、专注、微微摇摆",
      when: ["morning", "afternoon"],
      moods: ["专注", "平静"],
      scenes: ["sedentary"],
      energy: ["中低", "中"],
      bpm: [68, 88],
      density: [0.4, 0.7],
      keys: ["F", "A#", "D#", "C", "G"],
      progressions: [
        ["2min9", "5dom9", "1maj9", "6min9"],
        ["1maj7", "3min7", "6min7", "4maj7"],
        ["4maj9", "3min7", "2min7", "5dom9"],
        ["1maj9", "6min9", "2min7", "5dom9"],
        ["6min7", "2min7", "5dom9", "1maj7"],
        ["4maj7", "5dom9", "3min7", "6min9"],
        ["1maj7", "4maj9", "2min7", "5sus4"],
      ],
      barsPerChord: 1,
      voices: {
        keys: { timbre: "electricPiano", gain: -14, octave: 4, mode: "block", rate: 2 },
        pad: { timbre: "softPad", gain: -30, octave: 3, mode: "sustain" },
        bell: { timbre: "bell", gain: -27, octave: 5, mode: "arp", rate: 8 },
        bass: { timbre: "bassRound", gain: -16, octave: 1, mode: "root" },
      },
      groove: { drums: "lofi", swing: 0.14 },
      space: { reverb: 3.2, wet: 0.3, filter: 4400, chorus: 0.3, delay: 0.14 },
    },
    {
      id: "after-rain",
      name: "雨后",
      en: "after rain",
      blurb: "木吉他的分解和弦，配上一点点潮湿的混响。",
      character: "清爽、透气、适合走动",
      when: ["morning", "afternoon", "evening"],
      moods: ["平静", "低落", "开心"],
      scenes: ["walking"],
      energy: ["中低", "中"],
      bpm: [92, 112],
      density: [0.45, 0.75],
      keys: ["G", "D", "C", "A", "E"],
      progressions: [
        ["1add9", "5sus4", "6min7", "4maj7"],
        ["6min7", "4maj7", "1maj7", "5add9"],
        ["1maj7", "4add9", "6min7", "5sus4"],
        ["4maj7", "1maj7", "5sus4", "6min7"],
        ["1add9", "3min7", "4maj9", "5sus4"],
        ["2min7", "5sus4", "1maj7", "4maj7"],
      ],
      barsPerChord: 1,
      voices: {
        keys: { timbre: "pluck", gain: -13, octave: 4, mode: "broken", rate: 8 },
        pad: { timbre: "softPad", gain: -29, octave: 3, mode: "sustain" },
        bell: { timbre: "bell", gain: -26, octave: 5, mode: "sparse", rate: 4 },
        bass: { timbre: "bassPick", gain: -17, octave: 1, mode: "root" },
      },
      groove: { drums: "soft", swing: 0.08 },
      space: { reverb: 4.2, wet: 0.38, filter: 6200, chorus: 0.16, delay: 0.12 },
    },
    {
      id: "breath",
      name: "呼吸",
      en: "breath",
      blurb: "没有鼓，只有两三个音在缓慢地明灭。适合焦虑或者很累的时候。",
      character: "极简、开阔、留白",
      when: ["any"],
      moods: ["焦虑", "疲惫", "低落"],
      scenes: ["any"],
      energy: ["低"],
      bpm: [52, 68],
      density: [0.05, 0.3],
      keys: ["C", "D", "F", "A#"],
      progressions: [
        ["1maj9", "4maj9"],
        ["6min9", "4maj9"],
        ["1sus2", "5sus2"],
        ["4maj9", "1maj9"],
        ["1add9", "6min9"],
      ],
      barsPerChord: 4,
      voices: {
        keys: { timbre: "softPad", gain: -18, octave: 3, mode: "block", rate: 1 },
        pad: { timbre: "strings", gain: -26, octave: 3, mode: "sustain" },
        bell: { timbre: "bell", gain: -28, octave: 5, mode: "sparse", rate: 2 },
        bass: { timbre: "bassRound", gain: -20, octave: 1, mode: "root" },
      },
      groove: { drums: "none", swing: 0 },
      space: { reverb: 7.5, wet: 0.58, filter: 3600, chorus: 0.24, delay: 0.08 },
    },
    {
      id: "high-noon",
      name: "午后",
      en: "high noon",
      blurb: "明亮的合成器与轻快的鼓组，给下午加一点阳光。",
      character: "明亮、轻快、有精神",
      when: ["afternoon"],
      moods: ["开心", "专注"],
      scenes: ["any"],
      energy: ["中", "中高"],
      bpm: [104, 126],
      density: [0.55, 0.85],
      keys: ["D", "A", "E", "G", "C"],
      progressions: [
        ["1maj7", "5dom7", "6min7", "4maj7"],
        ["4maj7", "5dom7", "3min7", "6min7"],
        ["1add9", "6min7", "4maj7", "5sus4"],
        ["6min7", "5dom7", "4maj7", "3min7"],
        ["4maj9", "5dom9", "1maj7", "6min7"],
        ["2min7", "5dom7", "1maj7", "4maj7"],
      ],
      barsPerChord: 1,
      voices: {
        keys: { timbre: "brightSynth", gain: -18, octave: 4, mode: "broken", rate: 8 },
        pad: { timbre: "softPad", gain: -30, octave: 3, mode: "sustain" },
        bell: { timbre: "bell", gain: -25, octave: 5, mode: "arp", rate: 8 },
        bass: { timbre: "bassPick", gain: -15, octave: 1, mode: "pulse" },
      },
      groove: { drums: "pop", swing: 0.05 },
      space: { reverb: 2.6, wet: 0.26, filter: 7600, chorus: 0.34, delay: 0.16 },
    },
    {
      id: "night-lamp",
      name: "夜灯",
      en: "night lamp",
      blurb: "暗一点的电钢琴，音量收在最舒服的位置，不打扰旁边的人。",
      character: "安静、内敛、有安全感",
      when: ["evening", "night"],
      moods: ["疲惫", "低落", "专注"],
      scenes: ["sedentary"],
      energy: ["低", "中低"],
      bpm: [62, 80],
      density: [0.15, 0.4],
      keys: ["A", "D", "F", "G"],
      progressions: [
        ["6min9", "4maj7", "1maj7", "5sus2"],
        ["1maj7", "6min7", "4maj7", "5sus4"],
        ["4maj9", "6min9", "2min7", "5sus4"],
        ["6min7", "3min7", "4maj7", "1maj7"],
        ["1maj9", "2min7", "4maj9", "5sus4"],
      ],
      barsPerChord: 2,
      voices: {
        keys: { timbre: "electricPiano", gain: -16, octave: 4, mode: "block", rate: 1 },
        pad: { timbre: "strings", gain: -29, octave: 3, mode: "sustain" },
        bell: { timbre: "bell", gain: -30, octave: 5, mode: "sparse", rate: 2 },
        bass: { timbre: "bassRound", gain: -19, octave: 1, mode: "root" },
      },
      groove: { drums: "none", swing: 0.1 },
      space: { reverb: 5, wet: 0.44, filter: 3400, chorus: 0.2, delay: 0.12 },
    },
    {
      id: "way-home",
      name: "归途",
      en: "way home",
      blurb: "有律动的贝斯和干净的和弦，适合走在路上不想说话的时候。",
      character: "轻快、都市、向前",
      when: ["afternoon", "evening"],
      moods: ["开心", "平静", "专注"],
      scenes: ["walking", "commute"],
      energy: ["中", "中高"],
      bpm: [108, 128],
      density: [0.6, 0.9],
      keys: ["A", "D", "E", "F", "G"],
      progressions: [
        ["4maj7", "5dom9", "3min7", "6min9"],
        ["2min7", "5dom9", "1maj7", "6min7"],
        ["6min9", "2min7", "5dom9", "1maj9"],
        ["1maj7", "4maj7", "2min7", "5dom9"],
        ["4maj9", "3min7", "2min7", "5dom9"],
        ["6min7", "4maj7", "5dom7", "1maj7"],
      ],
      barsPerChord: 1,
      voices: {
        keys: { timbre: "electricPiano", gain: -16, octave: 4, mode: "broken", rate: 8 },
        pad: { timbre: "brightSynth", gain: -30, octave: 3, mode: "sustain" },
        bell: { timbre: "bell", gain: -26, octave: 5, mode: "arp", rate: 8 },
        bass: { timbre: "bassPick", gain: -14, octave: 1, mode: "walk" },
      },
      groove: { drums: "citypop", swing: 0.08 },
      space: { reverb: 3, wet: 0.3, filter: 6800, chorus: 0.3, delay: 0.18 },
    },
    {
      id: "settling",
      name: "落地",
      en: "settling",
      blurb: "低音带着铃音慢慢下沉，一天的事到这里可以先放下。",
      character: "温暖、缓慢、收束",
      when: ["evening", "night"],
      moods: ["平静", "疲惫", "低落"],
      scenes: ["any"],
      energy: ["低"],
      bpm: [54, 70],
      density: [0.1, 0.35],
      keys: ["C", "F", "A#", "D"],
      progressions: [
        ["1maj7", "4maj7", "6min7", "5sus4"],
        ["4maj9", "1maj9", "2min7", "5sus4"],
        ["1maj9", "6min7", "4maj7", "5sus2"],
        ["4maj7", "3min7", "6min9", "1maj7"],
        ["6min9", "5sus4", "4maj9", "1maj9"],
      ],
      barsPerChord: 2,
      voices: {
        keys: { timbre: "bell", gain: -22, octave: 5, mode: "sparse", rate: 3 },
        pad: { timbre: "strings", gain: -22, octave: 3, mode: "sustain" },
        bell: { timbre: "electricPiano", gain: -20, octave: 4, mode: "block", rate: 1 },
        bass: { timbre: "bassRound", gain: -17, octave: 1, mode: "root" },
      },
      groove: { drums: "none", swing: 0.04 },
      space: { reverb: 6.5, wet: 0.5, filter: 4000, chorus: 0.18, delay: 0.12 },
    },
  ];

  const BY_ID = new Map(SOUNDSCAPES.map((item) => [item.id, item]));

  function clampTo(value, range) {
    return Math.min(range[1], Math.max(range[0], value));
  }

  /** 把任意数字夹进合法的节拍范围。非法输入（NaN/字符串）退回 null 交给调用方。 */
  function clampBpm(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return null;
    return clampTo(Math.round(number), LIMITS.bpm);
  }

  /** 同上，节奏量。 */
  function clampDensity(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return null;
    return clampTo(number, LIMITS.density);
  }

  /** 这个调名合法吗——挡住拼错的调性，免得在 keyRootMidi 里才炸。 */
  function isValidKey(key) {
    return KEYS.includes(String(key || "").toUpperCase());
  }

  /** 全局节拍档位数：40–200 共 161 档。 */
  function tempoSteps() {
    return LIMITS.bpm[1] - LIMITS.bpm[0] + 1;
  }

  /**
   * 一个音景能展开出多少种组合：调性 × 和声进行 × 节拍档位。
   *
   * 这里刻意用**全局**节拍档位（161）而不是该音景的推荐区间——解耦之后
   * 速度本来就能拖到推荐区间外，若还按推荐区间计数，等于在计数上偷偷把
   * 解耦又收回去，那个数字会小看自己。
   *
   * 节奏量是连续量，没有「档位」可言，所以不参与计数；界面上单独说明。
   */
  function combinationCount(soundscape) {
    return soundscape.keys.length * soundscape.progressions.length * tempoSteps();
  }

  /** 全库统计：音景数、和声进行总数、组合总数。用于界面上诚实地说清「程序库有多大」。 */
  function libraryStats() {
    const progressions = SOUNDSCAPES.reduce((sum, item) => sum + item.progressions.length, 0);
    const combinations = SOUNDSCAPES.reduce((sum, item) => sum + combinationCount(item), 0);
    return {
      soundscapes: SOUNDSCAPES.length,
      progressions,
      combinations,
      tempos: tempoSteps(),
      keys: KEYS.length,
      bpm: [LIMITS.bpm[0], LIMITS.bpm[1]],
    };
  }

  function get(id) {
    return BY_ID.get(String(id)) || null;
  }

  /** 某条轴的推荐区间，供界面高亮与「超出推荐」提示使用。 */
  function recommendFor(id) {
    const soundscape = get(id) || SOUNDSCAPES[0];
    return { bpm: soundscape.bpm.slice(), density: soundscape.density.slice() };
  }

  /** 某个值是否落在推荐区间内。 */
  function inRecommendation(id, axis, value) {
    const band = recommendFor(id)[axis];
    if (!band) return true;
    const number = Number(value);
    return Number.isFinite(number) && number >= band[0] && number <= band[1];
  }

  /**
   * 后端只给 id / BPM / 节奏量；这里补全成引擎能直接演奏的完整配方。
   *
   * 三个入参的处理原则一致：**给了就用（只要不越全局硬边界），没给就取
   * 推荐区间的中点**。`key` 与 `progressionIndex` 为 null 时交给引擎按
   * 种子随机挑，这样同一条配方在不同休息点会落到不同调性上。
   */
  function resolve(
    id,
    { bpm, density, seed = 0, intensity = 0.5, key = null, progressionIndex = null } = {}
  ) {
    const soundscape = get(id) || SOUNDSCAPES[0];
    const targetBpm =
      clampBpm(bpm) ?? Math.round((soundscape.bpm[0] + soundscape.bpm[1]) / 2);
    const targetDensity =
      clampDensity(density) ?? (soundscape.density[0] + soundscape.density[1]) / 2;
    const targetKey = isValidKey(key) ? String(key).toUpperCase() : null;
    const targetProgression =
      Number.isInteger(progressionIndex) && progressionIndex >= 0
        ? progressionIndex % soundscape.progressions.length
        : null;
    return {
      soundscape,
      bpm: targetBpm,
      density: targetDensity,
      seed,
      intensity,
      key: targetKey,
      progressionIndex: targetProgression,
    };
  }

  window.MCSoundscapes = {
    list: SOUNDSCAPES,
    get,
    resolve,
    recommendFor,
    inRecommendation,
    libraryStats,
    combinationCount,
    clampBpm,
    clampDensity,
    isValidKey,
    LIMITS,
    KEYS,
    timbres: TIMBRE,
  };
})();
