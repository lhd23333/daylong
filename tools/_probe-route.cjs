/* 「今天的声音路线」端到端探针：
 * 1) 今天页出现路线卡（现有 5 条课表 → 应有 ≥5 段）
 * 2) 开始收听 → 跳休息页、连播条出现、引擎在放第一段
 * 3) 用引擎 bar 监听器模拟时间流逝 → 自动切到第二段（crossfade 完再断言）
 * 4) 下一段 / 上一段按钮
 * 5) 手动点音景 → 路线退出（连播条消失），不与用户抢方向盘
 *
 * 用法：node tools/_probe-route.cjs [端口]
 */
const {
  chromium,
} = require(process.env.APPDATA +
  "\\npm\\node_modules\\@playwright\\cli\\node_modules\\playwright");

const PORT = Number(process.argv[2] || 8131);

(async () => {
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("console", (m) => {
    if (m.type() === "error") errors.push(`console.error: ${m.text()}`);
  });

  await page.goto(`http://127.0.0.1:${PORT}`, { waitUntil: "networkidle" });
  await page.waitForSelector("#timeline .tl-item", { state: "attached", timeout: 15000 });
  await page.click('[data-goto="today"]');
  await page.waitForTimeout(300);

  const card = await page.evaluate(() => {
    const node = document.getElementById("day-route");
    const rows = [...document.querySelectorAll("#route-preview li")];
    return {
      可见: node && !node.hidden,
      说明: document.getElementById("route-note").textContent,
      预览: rows.map((li) => li.textContent.trim()),
    };
  });
  console.log("== 1. 今天页路线卡 ==");
  console.log(JSON.stringify(card, null, 2));

  // 2) 开始收听
  await page.click("#route-start");
  await page.waitForTimeout(2200); // crossfade 1.6s + 余量
  const start = await page.evaluate(() => {
    const bar = document.getElementById("route-bar");
    return {
      休息页在前: !document.getElementById("view-rest").hidden,
      连播条可见: bar && !bar.hidden,
      位置: document.getElementById("route-pos").textContent,
      段落: document.getElementById("route-seg").textContent,
      上下文: document.getElementById("player-context").textContent,
      在播: window.MCEngine.isPlaying(),
      音景: window.MCEngine.currentId(),
      监听器数: window.MCEngine.listeners.bar.size,
    };
  });
  console.log("== 2. 开始收听 ==");
  console.log(JSON.stringify(start, null, 2));

  // 3) 模拟时间流逝（120bpm 每小节 2 秒；一段 90~120 秒 → 62 个小节足够溢出）
  const advanced = await page.evaluate(async () => {
    const eng = window.MCEngine;
    const handlers = [...eng.listeners.bar];
    for (let i = 0; i < 62; i += 1) handlers.forEach((h) => h({ bpm: 120, bar: i }));
    await new Promise((r) => setTimeout(r, 2600)); // 等 crossfade
    return {
      位置: document.getElementById("route-pos").textContent,
      段落: document.getElementById("route-seg").textContent,
      上下文: document.getElementById("player-context").textContent,
      音景: eng.currentId(),
    };
  });
  console.log("== 3. 自动切下一段 ==");
  console.log(JSON.stringify(advanced, null, 2));

  // 4) 下一段 / 上一段
  await page.click('[data-route="next"]');
  await page.waitForTimeout(2100);
  const afterNext = await page.evaluate(() => ({
    位置: document.getElementById("route-pos").textContent,
    段落: document.getElementById("route-seg").textContent,
  }));
  await page.click('[data-route="prev"]');
  await page.waitForTimeout(2100);
  const afterPrev = await page.evaluate(() => ({
    位置: document.getElementById("route-pos").textContent,
    段落: document.getElementById("route-seg").textContent,
  }));
  console.log("== 4. 下一段 / 上一段 ==");
  console.log(`next → ${JSON.stringify(afterNext)}`);
  console.log(`prev → ${JSON.stringify(afterPrev)}`);

  // 4.5 暂停 → 恢复：路线保持（暂停不是换台，回来接着听这一段）
  await page.click("#play-btn");
  await page.waitForTimeout(1300);
  const paused = await page.evaluate(() => ({
    在播: window.MCEngine.isPlaying(),
    连播条可见: !document.getElementById("route-bar").hidden,
  }));
  await page.click("#play-btn");
  await page.waitForTimeout(2200);
  const resumed = await page.evaluate(() => ({
    在播: window.MCEngine.isPlaying(),
    连播条可见: !document.getElementById("route-bar").hidden,
    位置: document.getElementById("route-pos").textContent,
    上下文: document.getElementById("player-context").textContent,
  }));
  console.log("== 4.5 暂停 / 恢复 ==");
  console.log(`暂停后 → ${JSON.stringify(paused)}`);
  console.log(`恢复后 → ${JSON.stringify(resumed)}`);

  // 5) 手动干预退出
  await page.evaluate(() => document.querySelector('[data-scape="breath"]').click());
  await page.waitForTimeout(2100);
  const manual = await page.evaluate(() => ({
    连播条可见: !document.getElementById("route-bar").hidden,
    上下文: document.getElementById("player-context").textContent,
    音景: window.MCEngine.currentId(),
  }));
  console.log("== 5. 手动点音景后 ==");
  console.log(JSON.stringify(manual, null, 2));

  // 6) 走完整条：重新开始后一路 next 到最后一段，再点一次 next 应结束
  //    （连播条消失 + 提示「走完了」，音乐继续放——结束的是节目单不是音乐）
  await page.click('[data-goto="today"]');
  await page.waitForTimeout(200);
  await page.click("#route-start");
  await page.waitForTimeout(2200);
  for (let i = 0; i < 9; i += 1) {
    await page.click('[data-route="next"]');
    await page.waitForTimeout(i === 8 ? 2200 : 120);
  }
  const ended = await page.evaluate(() => ({
    连播条可见: !document.getElementById("route-bar").hidden,
    在播: window.MCEngine.isPlaying(),
    提示: document.getElementById("toast").hidden
      ? null
      : document.getElementById("toast").textContent,
  }));
  console.log("== 6. 走到最后一段再点下一段 ==");
  console.log(JSON.stringify(ended, null, 2));

  await browser.close();
  console.log(`页面错误: ${JSON.stringify(errors)}`);
})().catch((e) => {
  console.error("FAILED:", e);
  process.exit(1);
});
