/* tick 排期修复的验收矩阵：真实使用路径逐项过一遍。
 *
 * 相位：breath 稳定 → 播放中 setBpm(100) → 切 first-light → 播放中
 * setBpm(180) → 切 run → 播放中 setDrums(strong)+setKey(G)+setDensity(0.7)。
 *
 * 判据：
 *   - 每相位内相邻小节回调间隔 ≈ 240/bpm 秒（±30%，换挡那一拍豁免）；
 *   - 真静默（RMS < p95×0.06 且 ≥0.4 s）为 0；浅坑（<p95×0.15 且 ≥0.3 s）
 *     单独列出（first-light 的换和弦呼吸允许出现）；
 *   - stale（音频钟 − 回调时间）≈ -0.1s 量级，无正值。
 *
 * 用法：node tools/_probe-accept.cjs [端口]
 */
const {
  chromium,
} = require(process.env.APPDATA +
  "\\npm\\node_modules\\@playwright\\cli\\node_modules\\playwright");

const PORT = Number(process.argv[2] || 8131);

(async () => {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const logs = [];
  page.on("pageerror", (e) => logs.push(`pageerror: ${e.message}`));
  page.on("console", (m) => {
    if (m.type() === "error") logs.push(`console.error: ${m.text()}`);
  });
  await page.goto(`http://127.0.0.1:${PORT}`, { waitUntil: "networkidle" });
  await page.waitForTimeout(1200);
  await page.click('[data-goto="rest"]');
  await page.waitForTimeout(400);

  const out = await page.evaluate(async () => {
    const Tone = window.Tone;
    const eng = window.MCEngine;
    // 先起播，等音频链路建好（nodes 在 play 前是 null）
    document.querySelector('[data-scape="breath"]').click();
    await new Promise((r) => setTimeout(r, 1500));

    // 相位定义：setup 是切换动作，steady 是观测秒数
    const PHASES = [
      { name: "breath 稳定", setup: () => document.querySelector('[data-scape="breath"]').click(), steady: 10 },
      { name: "播放中 setBpm(100)", setup: () => eng.setBpm(100), steady: 10 },
      { name: "切 first-light", setup: () => document.querySelector('[data-scape="first-light"]').click(), steady: 10 },
      { name: "播放中 setBpm(180)", setup: () => eng.setBpm(180), steady: 10 },
      { name: "切 run(165)", setup: () => document.querySelector('[data-scape="run"]').click(), steady: 10 },
      {
        name: "播放中 鼓/调/密度",
        setup: () => {
          eng.setDrums("strong");
          eng.setKey("G");
          eng.setDensity(0.7);
        },
        steady: 8,
      },
    ];

    const bars = []; // {wall, phase, gapMs, stale, bar}
    let phaseIndex = -1;
    const original = eng.onBar.bind(eng);
    eng.onBar = (time) => {
      const wall = performance.now();
      const prev = bars[bars.length - 1];
      bars.push({
        wall,
        phase: phaseIndex,
        bar: eng.barIndex,
        gapMs: prev ? Math.round(wall - prev.wall) : null,
        samePhase: prev ? prev.phase === phaseIndex : false,
        stale: Number((Tone.getContext().currentTime - time).toFixed(3)),
      });
      return original(time);
    };

    const probe = new Tone.Analyser("waveform", 1024);
    eng.nodes.chain.master.connect(probe);
    const bucketsAll = [];
    let bucket = { sum: 0, n: 0, phase: -1, wall0: 0 };
    const flush = () => {
      if (!bucket.n) return;
      bucketsAll.push({
        phase: bucket.phase,
        at: bucket.wall0,
        rms: Math.sqrt(bucket.sum / bucket.n),
      });
      bucket = { sum: 0, n: 0, phase: bucket.phase, wall0: 0 };
    };

    const results = [];
    for (let i = 0; i < PHASES.length; i += 1) {
      phaseIndex = i;
      PHASES[i].setup();
      const t0 = performance.now();
      const end = t0 + (PHASES[i].steady + 1) * 1000; // 多跑 1s，分析时裁掉开头 1s
      bucket = { sum: 0, n: 0, phase: i, wall0: performance.now() };
      let lastFlush = performance.now();
      while (performance.now() < end) {
        const v = probe.getValue();
        let s = 0;
        for (let k = 0; k < v.length; k += 1) s += v[k] * v[k];
        if (!bucket.wall0) bucket.wall0 = performance.now();
        bucket.sum += s / v.length;
        bucket.n += 1;
        if (performance.now() - lastFlush >= 100) {
          flush();
          bucket = { sum: 0, n: 0, phase: i, wall0: performance.now() };
          lastFlush = performance.now();
        }
        await new Promise((r) => requestAnimationFrame(r));
      }
      flush();
      results.push({
        name: PHASES[i].name,
        bpm: eng.recipe ? Math.round(eng.recipe.bpm * 10) / 10 : null,
        t0,
        err: eng.lastError || null,
      });
    }
    probe.dispose();
    eng.onBar = original;

    // 逐相位分析：裁掉过渡的前 1.2 s
    const analyze = (phase) => {
      const t0 = results[phase].t0 + 1200;
      const cbs = bars.filter((b) => b.phase === phase && b.wall >= t0);
      const gaps = cbs.slice(1).map((b, k) => (cbs[k].wall >= t0 ? b.gapMs : null)).filter((g) => g !== null);
      const stale = cbs.map((b) => b.stale);
      const buckets = bucketsAll.filter((b) => b.phase === phase && b.at >= t0);
      const sorted = buckets.map((b) => b.rms).sort((a, b) => a - b);
      const p95 = sorted[Math.floor(sorted.length * 0.95)] || 0;
      const spans = (frac, minBuckets) => {
        const list = [];
        let span = null;
        buckets.forEach((b, idx) => {
          if (b.rms < p95 * frac) {
            if (span && idx === span.end + 1) span.end = idx;
            else {
              if (span && span.end - span.start + 1 >= minBuckets) list.push(span);
              span = { start: idx, end: idx };
            }
          }
        });
        if (span && span.end - span.start + 1 >= minBuckets) list.push(span);
        return list.map((s) => ({
          起s: Number(((buckets[s.start].at - t0) / 1000).toFixed(2)),
          长s: Number((buckets[s.end].at - buckets[s.start].at + 0.1).toFixed(2)),
        }));
      };
      return {
        name: results[phase].name,
        bpm: results[phase].bpm,
        err: results[phase].err,
        回调数: cbs.length,
        gapMs: gaps.length
          ? { min: Math.min(...gaps), max: Math.max(...gaps), 全部: gaps }
          : null,
        期望小节ms: results[phase].bpm ? Math.round((240 / results[phase].bpm) * 1000) : null,
        stale: stale.length
          ? { min: Math.min(...stale), max: Math.max(...stale) }
          : null,
        真静默: spans(0.06, 4),
        浅坑: spans(0.15, 3),
      };
    };

    return results.map((_, i) => analyze(i));
  });

  await browser.close();
  out.forEach((r) => {
    console.log(`\n=== ${r.name}  bpm=${r.bpm}  期望小节=${r.期望小节ms}ms  回调=${r.回调数}`);
    if (r.gapMs) console.log(`  间隔ms: min=${r.gapMs.min} max=${r.gapMs.max}`);
    if (r.gapMs) console.log(`  全部间隔: ${JSON.stringify(r.gapMs.全部)}`);
    if (r.stale) console.log(`  stale: ${JSON.stringify(r.stale)}`);
    console.log(`  真静默(<6%p95,≥0.4s): ${JSON.stringify(r.真静默)}`);
    console.log(`  浅坑(<15%p95,≥0.3s): ${JSON.stringify(r.浅坑)}`);
    if (r.err) console.log(`  err: ${r.err}`);
  });
  console.log(`\nlogs: ${JSON.stringify(logs)}`);
})().catch((e) => {
  console.error("FAILED:", e);
  process.exit(1);
});
