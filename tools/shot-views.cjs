/* 按页面顶部对齐截图，用来看版面本身（冒烟脚本会因为 click 自动滚动）。 */
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

(async () => {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  for (const [name, width, height, scale] of [
    ["desktop", 1280, 1000, 1],
    ["mobile", 414, 900, 2],
  ]) {
    const context = await browser.newContext({
      viewport: { width, height },
      deviceScaleFactor: scale,
    });
    const page = await context.newPage();
    await page.goto(BASE, { waitUntil: "networkidle" });
    await page.waitForTimeout(1500);
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({ path: path.join(outDir, `view-today-${name}.png`) });

    await page.click('[data-goto="rest"]');
    await page.waitForTimeout(300);
    await page.click("#scape-grid .scape");
    await page.waitForTimeout(4000);
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.waitForTimeout(200);
    await page.screenshot({ path: path.join(outDir, `view-rest-${name}.png`) });
    await context.close();
  }
  await browser.close();
  console.log("ok");
})().catch((error) => {
  console.error("FAILED:", error);
  process.exit(1);
});
