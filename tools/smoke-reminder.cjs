/* 验证「开始前 5 分钟」提醒：造一条 3 分钟后开始的日程，看提醒卡会不会自己弹出来。
 * 用法：node tools/smoke-reminder.cjs [端口]
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

/** 本地时间的 "YYYY-MM-DDTHH:mm:ss+08:00"，不能用 toISOString（那是 UTC）。 */
function localIso(date) {
  const pad = (n) => String(n).padStart(2, "0");
  const offset = -date.getTimezoneOffset();
  const sign = offset >= 0 ? "+" : "-";
  const abs = Math.abs(offset);
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    `T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}` +
    `${sign}${pad(Math.floor(abs / 60))}:${pad(abs % 60)}`
  );
}

(async () => {
  const problems = [];
  const start = new Date(Date.now() + 3 * 60 * 1000);
  const end = new Date(start.getTime() + 40 * 60 * 1000);
  const id = `reminder-probe-${Date.now()}`;

  // 先造日程（用 Node 的 fetch，不经浏览器）
  const makeRes = await fetch(`${BASE}/api/calendar/events`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      id,
      title: "英语听力练习",
      start: localIso(start),
      end: localIso(end),
      category: "study",
      priority: "soft",
      source: "manual",
    }),
  });
  if (!makeRes.ok) throw new Error(`造日程失败 ${makeRes.status} ${await makeRes.text()}`);

  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await context.newPage();
  page.on("pageerror", (e) => problems.push(`pageerror: ${e.message}`));
  page.on("console", (m) => {
    if (m.type() === "error") problems.push(`console.error: ${m.text()}`);
  });

  await page.goto(BASE, { waitUntil: "networkidle" });
  await page.waitForTimeout(2500);

  const reminder = await page.evaluate(() => {
    const box = document.getElementById("reminder");
    return {
      hidden: box.hidden,
      when: document.getElementById("reminder-when").textContent,
      title: document.getElementById("reminder-title").textContent,
      why: document.getElementById("reminder-why").textContent,
      playVisible: !document.getElementById("reminder-play").hidden,
    };
  });

  // 行程里这条日程的配乐行
  const row = await page.evaluate((wanted) => {
    const items = [...document.querySelectorAll("#timeline .tl-item")];
    const hit = items.find((el) => el.textContent.includes(wanted));
    return hit ? hit.textContent.replace(/\s+/g, " ").trim() : null;
  }, "英语听力练习");

  await page.screenshot({ path: path.join(outDir, "smoke-reminder.png") });

  // 点「先听一段」，应该跳到休息页并开始放
  let afterPlay = null;
  if (!reminder.hidden && reminder.playVisible) {
    await page.click("#reminder-play");
    await page.waitForTimeout(2500);
    afterPlay = await page.evaluate(() => ({
      reminderHidden: document.getElementById("reminder").hidden,
      view: document.querySelector(".view:not([hidden])").id,
      context: document.getElementById("player-context").textContent,
      title: document.getElementById("player-title").textContent,
      playing: document.getElementById("player").classList.contains("is-playing"),
    }));
    await page.screenshot({ path: path.join(outDir, "smoke-reminder-played.png") });
  }

  // 清理：删掉这条探针日程
  await fetch(`${BASE}/api/calendar/events/${id}`, { method: "DELETE" });

  await browser.close();
  console.log(JSON.stringify({ startAt: localIso(start), reminder, row, afterPlay, problems }, null, 1));
})().catch((error) => {
  console.error("FAILED:", error);
  process.exit(1);
});
