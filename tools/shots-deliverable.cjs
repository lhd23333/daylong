/* 生成交付用的 9 张界面截图，默认写进 <仓库>/dist/screenshots/。
 *
 * 为什么单独一个脚本而不是复用 shot-views.cjs：
 *   - 交付图要拍「休息页的两种状态」（音景库 / 播放中），后者需要先播起来；
 *   - 「播放中」那张要有「已收藏」状态才看得出功能，所以脚本自己造一条收藏，
 *     拍完再清空——交付包里的 data/playlist.json 必须是空的（新用户第一次打开）；
 *   - 对话页要拍「排完之后」，那需要真发一条消息（只读接口，不写日历）；
 *     「今天」那张则先点一次「写进今天」，让时间轴上真的出现 agent 排的那几件事，
 *     拍完再删干净——交付数据里不该留探针的痕迹。
 *
 * 用法：node tools/shots-deliverable.cjs [端口] [输出目录]
 */
const path = require("path");
const {
  chromium,
} = require(process.env.APPDATA +
  "\\npm\\node_modules\\@playwright\\cli\\node_modules\\playwright");

const PORT = Number(process.argv[2] || 8123);
const BASE = `http://127.0.0.1:${PORT}`;
const fs = require("fs");
const outDir = process.argv[3]
  ? path.resolve(process.argv[3])
  : path.resolve(__dirname, "..", "dist", "screenshots");
fs.mkdirSync(outDir, { recursive: true });

const SAY = "上午数学课，下午四点半去操场跑半小时，晚上写作业到十点。今天有点累。";

/** 清空收藏：交付包里不能留测试残留。 */
async function clearFavorites(page) {
  const list = await page.evaluate(async () => {
    const res = await fetch("/api/playlist");
    const data = await res.json();
    return (data.items || []).map((item) => item.id);
  });
  for (const id of list) {
    await page.evaluate(async (target) => {
      await fetch(`/api/playlist/${encodeURIComponent(target)}`, { method: "DELETE" });
    }, id);
  }
  return list.length;
}

/** 删掉 agent 写进去的日程，保留原本那几条演示日程。 */
async function clearAgentEvents(page) {
  return page.evaluate(async () => {
    const data = await (await fetch("/api/calendar/events")).json();
    const mine = data.events.filter((item) => (item.metadata || {}).via === "agent");
    for (const item of mine) {
      await fetch(`/api/calendar/events/${encodeURIComponent(item.id)}`, { method: "DELETE" });
    }
    return mine.length;
  });
}

/** 备份并删光现有日程。交付图里的「今天」只该有 agent 刚排的那一天——
 *  本机遗留的演示课表会把时间轴塞满、还与 agent 的日程互相重叠（拍出来
 *  带一条「有 N 处日程时间重叠」的警告，像 bug）。拍完由 restoreEvents 还原。 */
async function stashEvents(page) {
  return page.evaluate(async () => {
    const data = await (await fetch("/api/calendar/events")).json();
    const all = data.events;
    for (const item of all) {
      await fetch(`/api/calendar/events/${encodeURIComponent(item.id)}`, { method: "DELETE" });
    }
    return all;
  });
}

async function restoreEvents(page, items) {
  return page.evaluate(async (backup) => {
    for (const item of backup) {
      await fetch("/api/calendar/events", {
        method: "POST",
        headers: { "Content-Type": "application/json; charset=utf-8" },
        body: JSON.stringify(item),
      });
    }
    return backup.length;
  }, items);
}

/** 等黑色 toast 自己退场再按快门，别让它糊在交付图上。
 *  写日程是网络操作，toast 在写完之后才弹——固定时长的 sleep 等不准。 */
async function waitToastGone(page) {
  await page
    .waitForFunction(() => document.getElementById("toast").hidden, null, { timeout: 8000 })
    .catch(() => {});
  await page.waitForTimeout(250);
}

(async () => {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const written = [];
  const errs = [];

  async function shoot(page, name) {
    await page.screenshot({ path: path.join(outDir, name) });
    written.push(name);
  }

  /** 发一句给对话页，等它读完。 */
  async function say(page, text = SAY) {
    await page.fill("#chat-input", text);
    await page.click("#chat-send");
    await page.waitForFunction(() => !document.querySelector("#chat-log .is-busy"), null, {
      timeout: 40000,
    });
    await page.waitForTimeout(500);
  }

  const cleanup = {};

  // ── 桌面 ────────────────────────────────────────────────
  const desktop = await browser.newContext({
    viewport: { width: 1280, height: 1000 },
    deviceScaleFactor: 1,
  });
  const d = await desktop.newPage();
  d.on("pageerror", (e) => errs.push(`desktop: ${e.message}`));
  await d.goto(BASE, { waitUntil: "networkidle" });
  await d.waitForTimeout(1800);
  cleanup.favorites = await clearFavorites(d);
  cleanup.agentEvents = await clearAgentEvents(d);
  // 暂存本机遗留日程（跑完还原）：交付图只留 agent 刚排的这一天
  const stashedEvents = await stashEvents(d);
  cleanup.stashed = stashedEvents.length;

  // 对话页：空态 → 排完之后
  await d.evaluate(() => window.scrollTo(0, 0));
  await shoot(d, "界面-对话-桌面.png");
  await say(d);
  await d.evaluate(() => window.scrollTo(0, 0));
  await shoot(d, "界面-对话-排好-桌面.png");

  // 写进今天，再拍今天页——时间轴上就有 agent 排的那几件事和它插的休息点。
  // 「写进今天」不是立即跳转：先写日程/状态，再拉一遍 /api/day（AI 通道下
  // 可能好几秒），最后才 go("today")。必须**等视图真的到了今天页**再继续——
  // 固定睡眠会让这个迟到的跳转落在后面任何一次点击头上（曾把「休息」页的
  // 照片拍成今天页、把「听这段」的点击落在错误视图上）。
  await d.click("[data-plan-write]");
  await d.waitForFunction(() => !document.getElementById("view-today").hidden, null, {
    timeout: 30000,
  });
  await waitToastGone(d);
  await d.evaluate(() => window.scrollTo(0, 0));
  await d.waitForTimeout(300);
  await shoot(d, "界面-今天-桌面.png");

  await d.click('[data-goto="you"]');
  await d.waitForTimeout(700);
  await d.evaluate(() => window.scrollTo(0, 0));
  await shoot(d, "界面-了解你-桌面.png");

  // 休息页 · 音景库（还没选音景的状态）。音景网格在页面下半部分、1 屏
  // 装不下：临时加高视口并滚到网格前，让 18 张卡片整个进画面——这张图的
  // 主题就是「音景库」，停在页顶拍到的只有播放器。
  await d.click('[data-goto="rest"]');
  await d.waitForTimeout(500);
  await d.setViewportSize({ width: 1280, height: 1600 });
  await d.evaluate(() => {
    const grid = document.getElementById("scape-grid");
    window.scrollTo(0, grid.getBoundingClientRect().top + window.scrollY - 130);
  });
  await d.waitForTimeout(300);
  await shoot(d, "界面-休息-音景库-桌面.png");
  await d.setViewportSize({ width: 1280, height: 1000 });

  // 播起来（走对话页那颗「听这段」——交付图里的「播放中」最好就是它配的那段），
  // 收藏一条，再拍「播放中」
  await d.click('[data-goto="talk"]');
  await d.waitForTimeout(300);
  await d.click("[data-plan-listen]");
  await d.waitForTimeout(4500);
  await d.click("#fav-btn");
  // 等到 toast（2.6 秒）自己退场再按快门——「已收藏」的状态会留在按钮上，
  // 但那个黑色气泡不该出现在交付图里。
  await d.waitForTimeout(3300);
  await d.evaluate(() => window.scrollTo(0, 0));
  await d.waitForTimeout(300);
  await shoot(d, "界面-休息-播放中-桌面.png");

  const favLabel = await d.evaluate(() => document.getElementById("fav-label").textContent);
  const desktopState = await d.evaluate(() => ({
    playing: !!(window.MCEngine && window.MCEngine.isPlaying()),
    title: document.getElementById("player-title").textContent,
    meta: document.getElementById("player-meta").textContent,
    music: document.getElementById("player-volume").value,
    drums: document.getElementById("drum-volume").value,
  }));

  // ── 手机 ────────────────────────────────────────────────
  const mobile = await browser.newContext({
    viewport: { width: 414, height: 900 },
    deviceScaleFactor: 2,
  });
  const m = await mobile.newPage();
  m.on("pageerror", (e) => errs.push(`mobile: ${e.message}`));
  await m.goto(BASE, { waitUntil: "networkidle" });
  await m.waitForTimeout(1800);
  await m.evaluate(() => window.scrollTo(0, 0));
  await shoot(m, "界面-对话-手机.png");

  await say(m);
  await m.evaluate(() => window.scrollTo(0, 0));
  await shoot(m, "界面-对话-排好-手机.png");

  await m.click("[data-plan-write]");
  await m.waitForFunction(() => !document.getElementById("view-today").hidden, null, {
    timeout: 30000,
  });
  await waitToastGone(m);
  await m.evaluate(() => window.scrollTo(0, 0));
  await m.waitForTimeout(300);
  await shoot(m, "界面-今天-手机.png");

  await m.click('[data-goto="you"]');
  await m.waitForTimeout(700);
  await m.evaluate(() => window.scrollTo(0, 0));
  await shoot(m, "界面-了解你-手机.png");

  // ── 收尾：清空收藏与探针日程、还原暂存的原有日程，交付包不留测试数据 ──
  const removed = await clearFavorites(m);
  cleanup.agentEventsAfter = await clearAgentEvents(m);
  cleanup.restored = await restoreEvents(m, stashedEvents);
  const left = await m.evaluate(async () => {
    const favorites = await (await fetch("/api/playlist")).json();
    const events = await (await fetch("/api/calendar/events")).json();
    return {
      favorites: (favorites.items || []).length,
      events: events.events.length,
      agentEvents: events.events.filter((item) => (item.metadata || {}).via === "agent").length,
    };
  });

  await browser.close();
  console.log(
    JSON.stringify(
      {
        outDir,
        written,
        cleanup,
        favLabelDuringShoot: favLabel,
        desktopState,
        favoritesRemoved: removed,
        left,
        errs,
      },
      null,
      1
    )
  );
})().catch((error) => {
  console.error("FAILED:", error);
  process.exit(1);
});
