/* 「清除全部数据」新路径补测：填了 Key 之后一键清除，设置文件应被删掉。
 * 用法：node tools/_probe-clear-all.cjs [端口]
 */
const {
  chromium,
} = require(process.env.APPDATA +
  "\\npm\\node_modules\\@playwright\\cli\\node_modules\\playwright");

const PORT = Number(process.argv[2] || 8132);

(async () => {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 1000 } });
  const errs = [];
  const dialogs = [];
  page.on("pageerror", (e) => errs.push(e.message));
  page.on("dialog", (d) => {
    dialogs.push(d.message());
    d.accept();
  });
  await page.goto(`http://127.0.0.1:${PORT}`, { waitUntil: "networkidle" });
  await page.waitForTimeout(1000);

  // 先填一个假 Key 保存
  await page.click('[data-goto="you"]');
  await page.click("#ai-panel > summary");
  await page.waitForTimeout(200);
  await page.fill("#ai-chat-key", "sk-clearall-0001");
  await page.click("#ai-save-btn");
  await page.waitForTimeout(600);

  const steps = {};
  steps["1_保存后source"] = await page.evaluate(async () => {
    const data = await (await fetch("/api/ai-settings")).json();
    return data.chat.source;
  });

  // 一键清除（footer）
  await page.click("#clear-all-btn");
  await page.waitForLoadState("networkidle");
  await page.waitForTimeout(800);

  steps["2_清除后source"] = await page.evaluate(async () => {
    const data = await (await fetch("/api/ai-settings")).json();
    return data.chat.source;
  });
  steps["2_确认弹窗文案"] = dialogs;

  await browser.close();
  console.log(JSON.stringify({ steps, errs }, null, 1));
})().catch((e) => {
  console.error("FAILED:", e);
  process.exit(1);
});
