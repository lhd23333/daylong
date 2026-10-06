/* 「连接 AI」面板端到端探针：初始状态 → 填假 Key 保存 → health 翻转 →
 * 刷新持久 → 坏值拒绝 → 清除回退。
 * 输出里所有 …xxxx 提示一律替换成 …****，不把任何 key 片段带进日志。
 * 用法：node tools/_probe-ai-settings.cjs [端口]
 */
const {
  chromium,
} = require(process.env.APPDATA +
  "\\npm\\node_modules\\@playwright\\cli\\node_modules\\playwright");

const PORT = Number(process.argv[2] || 8132);
const FAKE = "sk-probe-abcd9999";
const mask = (s) => (s || "").replace(/…[^\s，。）]*/g, "…****");

(async () => {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 1000 } });
  const errs = [];
  page.on("pageerror", (e) => errs.push(e.message));
  page.on("dialog", (d) => d.accept());
  await page.goto(`http://127.0.0.1:${PORT}`, { waitUntil: "networkidle" });
  await page.waitForTimeout(1200);

  const steps = {};
  const stateText = async () => mask(await page.textContent("#ai-panel-state"));

  await page.click('[data-goto="you"]');
  await page.click("#ai-panel > summary");
  await page.waitForTimeout(300);
  steps["1_初始状态"] = await stateText();

  // 填假 Key 保存
  await page.fill("#ai-chat-key", FAKE);
  await page.fill("#ai-chat-base", "https://api.example.com/v1");
  await page.fill("#ai-chat-model", "probe-model");
  await page.click("#ai-save-btn");
  await page.waitForFunction(
    () => document.getElementById("ai-save-hint").textContent.includes("已保存"),
    null,
    { timeout: 5000 }
  );
  steps["2_保存后状态"] = await stateText();
  steps["2_key框已清空"] = (await page.inputValue("#ai-chat-key")) === "";
  steps["2_base占位"] = await page.getAttribute("#ai-chat-base", "placeholder");
  steps["2_页面里没有假Key残留"] = !(await page.content()).includes(FAKE);

  steps["3_health"] = await page.evaluate(async () => {
    const data = await (await fetch("/api/health")).json();
    return { mode: data.mode, remote: data.remote_music };
  });

  // 刷新：设置真的落了盘
  await page.reload({ waitUntil: "networkidle" });
  await page.waitForTimeout(1200);
  await page.click('[data-goto="you"]');
  await page.waitForTimeout(300);
  await page.click("#ai-panel > summary"); // details 不记住展开状态，要重新打开
  await page.waitForTimeout(300);
  steps["4_刷新后状态"] = await stateText();

  // 坏值拒绝：ftp:// 不是地址
  await page.fill("#ai-chat-base", "ftp://nope");
  await page.click("#ai-save-btn");
  await page.waitForTimeout(600);
  steps["5_坏值toast"] = await page.textContent("#toast");

  // 清除：回到初始
  await page.click("#ai-clear-btn");
  await page.waitForTimeout(700);
  steps["6_清除后状态"] = await stateText();

  await browser.close();
  console.log(JSON.stringify({ steps, errs }, null, 1));
})().catch((e) => {
  console.error("FAILED:", e);
  process.exit(1);
});
