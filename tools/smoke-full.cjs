/* 全站冒烟：走一遍三个页面 + 播放 + 收藏 + 时间轴配乐，收集所有控制台错误。
 * 用法：node tools/smoke-full.cjs [端口]   （服务器需先起好）
 */
const path = require("path");
const {
  chromium,
} = require(process.env.APPDATA +
  "\\npm\\node_modules\\@playwright\\cli\\node_modules\\playwright");

const PORT = Number(process.argv[2] || 8123);
const BASE = `http://127.0.0.1:${PORT}`;
const fs = require("fs");
const outDir = path.resolve(__dirname, "..", "dist", "screenshots");
fs.mkdirSync(outDir, { recursive: true });
const problems = [];

(async () => {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, deviceScaleFactor: 1 });
  const page = await context.newPage();

  page.on("console", (m) => {
    if (m.type() === "error" || m.type() === "warning") problems.push(`console.${m.type()}: ${m.text()}`);
  });
  page.on("pageerror", (e) => problems.push(`pageerror: ${e.message}`));
  page.on("requestfailed", (r) => problems.push(`requestfailed: ${r.url()} ${r.failure() && r.failure().errorText}`));
  page.on("response", (r) => {
    if (r.status() >= 400) problems.push(`http ${r.status()}: ${r.url()}`);
  });

  await page.goto(BASE, { waitUntil: "networkidle" });
  await page.waitForTimeout(1200);

  const globals = await page.evaluate(() => ({
    scapes: !!(window.MCSoundscapes && window.MCSoundscapes.list || []).length,
    features: !!window.MCFeatures,
    particles: !!(window.MCParticles && window.MCParticles.DRIVERS || []).length,
    daymusic: !!window.MCDayMusic,
    drivers: (window.MCParticles && window.MCParticles.DRIVERS || []).map((d) => d.id),
  }));

  // 今天页：时间轴 + 配乐入口
  const today = await page.evaluate(() => ({
    timelineRows: document.querySelectorAll("#timeline .tl-item").length,
    melodyButtons: document.querySelectorAll("#timeline [data-melody-play]").length,
    careLine: (document.getElementById("care-line") || {}).textContent || "",
  }));
  await page.screenshot({ path: path.join(outDir, "smoke-today.png"), fullPage: false });

  // 休息页：挑音景 → 播放 → 频谱/光碟/粒子跑一会儿
  await page.click('[data-goto="rest"]');
  await page.waitForTimeout(400);
  await page.click("#scape-grid .scape");
  await page.waitForTimeout(1200);
  // 点一下 #play-btn 会**暂停**（选音景就已经开始放了）。这里顺便把
  // 「暂停 → 再按恢复」这条路径也走一遍：暂停期间 recipe 要留住，
  // 三个滑杆和读数不能空，恢复后要接回同一段。
  await page.click("#play-btn");
  await page.waitForTimeout(1600);
  const paused = await page.evaluate(() => ({
    playerClass: document.getElementById("player").className,
    bpm: document.getElementById("disc-bpm").textContent,
    key: document.getElementById("disc-key").textContent,
    tempoDisabled: document.getElementById("player-tempo").disabled,
  }));
  await page.click("#play-btn");
  await page.waitForTimeout(5000);

  const playing = await page.evaluate(() => ({
    playerClass: document.getElementById("player").className,
    title: document.getElementById("player-title").textContent,
    bpm: document.getElementById("disc-bpm").textContent,
    key: document.getElementById("disc-key").textContent,
    tempoOut: document.getElementById("out-tempo").textContent,
    densityOut: document.getElementById("out-density").textContent,
    bandHint: document.getElementById("band-hint").textContent,
    keyChips: document.querySelectorAll("#key-chips .chip").length,
    progChips: document.querySelectorAll("#progression-chips .chip").length,
    driverChips: document.querySelectorAll("#driver-chips .chip").length,
    favLabel: document.getElementById("fav-label").textContent,
    // 数一下频谱里有多少个 bin 明显高于噪声底（-120 dB）。
    // 没有这条，光碟外圈一根柱子都没画出来也照样会「通过」。
    spectrumLiveBins: (() => {
      const eng = window.MCEngine;
      const s = eng && typeof eng.getSpectrum === "function" ? eng.getSpectrum() : null;
      if (!s) return null;
      return s.filter((v) => Number.isFinite(v) && v > -120).length;
    })(),
  }));
  await page.screenshot({ path: path.join(outDir, "smoke-rest-playing.png") });

  // 自定义：换调性 + 换粒子驱动 + 拖节奏量
  await page.click('#key-chips .chip[data-key="F"]');
  await page.waitForTimeout(300);
  await page.click('#driver-chips .chip:nth-child(3)');
  await page.waitForTimeout(300);
  await page.$eval("#player-density", (el) => {
    el.value = "80";
    el.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await page.$eval("#player-tempo", (el) => {
    el.value = "150";
    el.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await page.waitForTimeout(2500);
  const custom = await page.evaluate(() => ({
    key: document.getElementById("disc-key").textContent,
    bpm: document.getElementById("disc-bpm").textContent,
    densityOut: document.getElementById("out-density").textContent,
    bandHint: document.getElementById("band-hint").textContent.trim(),
    driverOn: (document.querySelector("#driver-chips .chip.is-on") || {}).textContent,
  }));
  await page.screenshot({ path: path.join(outDir, "smoke-rest-custom.png") });

  // 收藏 → 歌单
  await page.click("#fav-btn");
  await page.waitForTimeout(900);
  const fav = await page.evaluate(() => ({
    label: document.getElementById("fav-label").textContent,
    items: document.querySelectorAll("#fav-list .fav-item").length,
    note: document.getElementById("fav-note").textContent,
  }));
  await page.screenshot({ path: path.join(outDir, "smoke-rest-fav.png") });

  // 了解你页
  await page.click('[data-goto="you"]');
  await page.waitForTimeout(600);
  const you = await page.evaluate(() => ({
    energy: document.getElementById("out-energy").textContent,
    scapeChips: document.querySelectorAll("#prefer-chips .chip").length,
  }));
  await page.screenshot({ path: path.join(outDir, "smoke-you.png") });

  // 回今天，点一条配好的旋律
  await page.click('[data-goto="today"]');
  await page.waitForTimeout(400);
  const hasMelody = await page.$("#timeline [data-melody-play]");
  let melody = null;
  if (hasMelody) {
    await hasMelody.click();
    await page.waitForTimeout(3000);
    melody = await page.evaluate(() => ({
      view: document.querySelector(".view:not([hidden])").id,
      context: document.getElementById("player-context").textContent,
      title: document.getElementById("player-title").textContent,
    }));
  }

  await browser.close();
  console.log(
    JSON.stringify({ globals, today, paused, playing, custom, fav, you, melody, problems }, null, 1)
  );
})().catch((error) => {
  console.error("SMOKE FAILED:", error);
  process.exit(1);
});
