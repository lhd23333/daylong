/*
 * 音乐理论小工具：把「级数 + 和弦类型」的抽象记号翻译成实际音高。
 *
 * 音景库里的和声进行写成 `["1maj7", "5add9", "6min7", "4maj7"]` 这种形式，
 * 好处是同一套进行可以搬到任意调上，于是「10 个音景」能展开成上万种组合。
 * 这里只做最必要的事：级数 → 音名 → 交给 Tone.js 发声。
 */
(() => {
  "use strict";

  const PITCH_CLASSES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];
  const MAJOR_SCALE = [0, 2, 4, 5, 7, 9, 11];

  // 和弦类型 → 相对根音的半音音程。
  const CHORD_TYPES = {
    maj: [0, 4, 7],
    maj6: [0, 4, 7, 9],
    maj7: [0, 4, 7, 11],
    maj9: [0, 4, 7, 11, 14],
    add9: [0, 4, 7, 14],
    min: [0, 3, 7],
    min6: [0, 3, 7, 9],
    min7: [0, 3, 7, 10],
    min9: [0, 3, 7, 10, 14],
    dom7: [0, 4, 7, 10],
    dom9: [0, 4, 7, 10, 14],
    sus2: [0, 2, 7, 12],
    sus4: [0, 5, 7, 12],
    dim: [0, 3, 6],
    min7b5: [0, 3, 6, 10],
  };

  // 转位：把和弦最低音往上挪，避免所有和弦都从根音起、听起来笨重。
  const INVERSIONS = {
    maj: [0, 1, 2], maj6: [0, 1, 2], maj7: [0, 1, 2], maj9: [0, 1, 2, 3], add9: [0, 1, 2],
    min: [0, 1, 2], min6: [0, 1, 2], min7: [0, 1, 2], min9: [0, 1, 2, 3],
    dom7: [0, 1, 2], dom9: [0, 1, 2, 3],
    sus2: [0, 1, 2], sus4: [0, 1, 2], dim: [0, 1, 2], min7b5: [0, 1, 2],
  };

  function midiToName(midi) {
    const rounded = Math.round(midi);
    const pitch = PITCH_CLASSES[((rounded % 12) + 12) % 12];
    return `${pitch}${Math.floor(rounded / 12) - 1}`;
  }

  function keyRootMidi(key, octave) {
    const index = PITCH_CLASSES.indexOf(String(key).toUpperCase());
    if (index < 0) throw new Error(`未知调性：${key}`);
    return (octave + 1) * 12 + index;
  }

  /** 解析 "5dom9" → { degree: 5, type: "dom9" } */
  function parseChord(symbol) {
    const match = /^([1-7])([a-zA-Z0-9]+)$/.exec(String(symbol).trim());
    if (!match) throw new Error(`无法解析和弦记号：${symbol}`);
    const type = match[2].toLowerCase();
    if (!CHORD_TYPES[type]) throw new Error(`未知和弦类型：${type}`);
    return { degree: Number(match[1]), type };
  }

  /**
   * 把级数和弦展开成音名数组，交给 Tone.js。
   *
   * @param {string} symbol  级数记号，如 "5dom9"
   * @param {string} key     调性，如 "C" / "F#" / "Bb"
   * @param {object} [opts]
   * @param {number} [opts.octave=3]      根音所在八度（Tone 记法，C4 = 中央 C）
   * @param {number} [opts.voicing=0]     转位序号，0 表示原位
   * @param {number} [opts.span=1]        音域跨度上限，超出就整体下移一个八度
   * @param {string[]} [opts.omit]        需要省略的音名（避免与贝斯打架）
   * @returns {string[]} 音名数组
   */
  function chordNotes(symbol, key, opts = {}) {
    const { degree, type } = parseChord(symbol);
    const octave = opts.octave ?? 3;
    const voicing = opts.voicing ?? 0;
    const span = opts.span ?? 14;
    const root = keyRootMidi(key, octave) + MAJOR_SCALE[(degree - 1) % 7] +
      (degree > 7 ? 12 * Math.floor((degree - 1) / 7) : 0);

    const intervals = CHORD_TYPES[type];
    const allowed = INVERSIONS[type] || [0];
    const inversion = allowed[((voicing % allowed.length) + allowed.length) % allowed.length];

    let notes = intervals.map((interval, index) =>
      root + interval + (index < inversion ? 12 : 0)
    );

    // 音域过宽会让 pad 糊成一团，整体下移而不是压缩。
    const lowest = notes[0];
    notes = notes.map((note) => (note - lowest > span ? note - 12 : note));
    notes.sort((a, b) => a - b);

    const omitted = new Set((opts.omit || []).map((item) => String(item).toUpperCase()));
    const names = notes.map(midiToName).filter((name) => {
      const pitch = name.replace(/\d/g, "");
      return !omitted.has(pitch);
    });
    return names.length ? names : notes.map(midiToName);
  }

  /** 只取和弦根音音名，用于贝斯声部。 */
  function rootNote(symbol, key, octave = 1) {
    const { degree } = parseChord(symbol);
    return midiToName(keyRootMidi(key, octave) + MAJOR_SCALE[(degree - 1) % 7]);
  }

  /**
   * 从和弦音里挑出一串琶音，用于拨弦类声部。
   * 方向可上可下，长度固定，落在和弦内音上——所以怎么排都不会难听。
   */
  function arpeggio(symbol, key, { octave = 4, steps = 8, direction = "up", spread = 0, rng = Math.random } = {}) {
    const notes = chordNotes(symbol, key, { octave, voicing: 0, span: 18 });
    const pool = [];
    // 向上铺 2 个八度，让琶音有起伏空间。
    for (let octaveShift = 0; octaveShift < 2; octaveShift += 1) {
      notes.forEach((note) => {
        const midi = midiFromName(note) + 12 * octaveShift;
        pool.push(midiToName(midi));
      });
    }
    const ordered = direction === "down" ? pool.slice().reverse() : pool;
    const result = [];
    for (let index = 0; index < steps; index += 1) {
      const base = ordered[index % ordered.length];
      if (spread > 0 && rng() < spread) continue; // 留白，避免琶音变节拍器
      result.push(base);
    }
    return result;
  }

  function midiFromName(name) {
    const match = /^([A-G][#]?)(-?\d+)$/.exec(String(name).trim());
    if (!match) throw new Error(`无法解析音名：${name}`);
    const pitch = PITCH_CLASSES.indexOf(match[1]);
    return (Number(match[2]) + 1) * 12 + pitch;
  }

  /** 统计一个音景能组合出多少种不同的和声进行组合，用于「程序库」计数展示。 */
  function countCombinations(soundscape) {
    const keys = soundscape.keys?.length || 0;
    const progressions = soundscape.progressions?.length || 0;
    const tempos = soundscape.bpm ? Math.max(1, Math.round(soundscape.bpm[1] - soundscape.bpm[0] + 1)) : 0;
    return keys * progressions * tempos;
  }

  window.MCTheory = {
    PITCH_CLASSES,
    CHORD_TYPES,
    chordNotes,
    rootNote,
    arpeggio,
    midiToName,
    midiFromName,
    parseChord,
    countCombinations,
  };
})();
