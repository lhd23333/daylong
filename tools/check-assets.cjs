/* 资源完整性检查：页面引用的每个 js/css/字体/图片是不是都真的取得到。
 * 少一个文件不会报错，只会静默缺样式或缺功能，所以要在交付前逐个数。
 * 用法：node tools/check-assets.cjs [端口]
 */
const {
  chromium,
} = require(process.env.APPDATA +
  "\\npm\\node_modules\\@playwright\\cli\\node_modules\\playwright");

const PORT = Number(process.argv[2] || 8123);
const BASE = `http://127.0.0.1:${PORT}`;

(async () => {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const page = await (
    await browser.newContext({ viewport: { width: 1440, height: 1000 } })
  ).newPage();

  const bad = [];
  const seen = new Map(); // url -> status
  page.on("response", (r) => {
    const url = r.url();
    if (!url.startsWith(BASE)) return;
    seen.set(url.slice(BASE.length), r.status());
    if (r.status() >= 400) bad.push(`${r.status()} ${url.slice(BASE.length)}`);
  });
  page.on("requestfailed", (r) => {
    if (r.url().startsWith(BASE)) bad.push(`FAILED ${r.url().slice(BASE.length)}`);
  });

  await page.goto(BASE, { waitUntil: "networkidle" });
  await page.waitForTimeout(2500);

  // 页面里声明了但浏览器根本没去取的（比如路径写错到别的域）
  const declared = await page.evaluate(() =>
    [...document.querySelectorAll("script[src], link[href], img[src]")].map((el) => ({
      tag: el.tagName.toLowerCase(),
      url: el.src || el.href,
      ok:
        el.tagName === "IMG"
          ? el.complete && el.naturalWidth > 0
          : true,
    }))
  );

  // 逐个自己再取一遍，绕开浏览器缓存造成的假通过
  const probes = [];
  for (const item of declared) {
    if (!item.url.startsWith(BASE)) continue;
    const path = item.url.slice(BASE.length);
    const res = await page.request.get(item.url);
    probes.push({ path, status: res.status(), ok: res.ok() });
  }

  // 字体是不是真的用上了（回退到系统字体也算「取到了」，但要能看出来）
  const font = await page.evaluate(() => {
    const el = document.querySelector("h1, .hero h1, body");
    const cs = getComputedStyle(el);
    return { family: cs.fontFamily, ready: document.fonts ? document.fonts.status : "n/a" };
  });

  // 破图
  const brokenImgs = await page.evaluate(() =>
    [...document.querySelectorAll("img")].filter((i) => i.complete && i.naturalWidth === 0).map((i) => i.getAttribute("src"))
  );

  await browser.close();
  console.log(
    JSON.stringify(
      {
        totalRequests: seen.size,
        bad,
        probes: probes.filter((p) => !p.ok),
        declaredCount: declared.length,
        brokenImgs,
        font,
      },
      null,
      1
    )
  );
})().catch((e) => {
  console.error("FAILED:", e);
  process.exit(1);
});
