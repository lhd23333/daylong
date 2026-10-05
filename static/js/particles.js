/*
 * 音频反应粒子系统。
 *
 * 核心设计：风格 = 驱动规则 × 参数。
 * 「一种观感写死一套代码」最省事，也最没前途——加一种效果就要加一个文件，用户
 * 永远只能在你写好的那几套里挑。这里把「力怎么算」（驱动规则）和「力有多大、
 * 拖多长、铺多开」（参数）拆成两组正交的东西：四条规则 × 六个连续参数，能扫出
 * 的观感是连续的，而且调参不用改代码。这正是这个产品「完全自定义」卖点的落点。
 * （驱动规则里 orbit 的「节拍加速」参考了 resonance-visualizer（MIT）的粒子隧道
 * 那一档，只借思路，实现是自己写的。）
 *
 * 四条规则统一用直角坐标：规则只负责写 p.ax / p.ay / p.drag，积分与渲染共用。
 * 统一之后「换规则」就退化成两组力的加权混合，所以能做到平滑过渡而不是清屏重来。
 *
 * 粒子活动范围由一块「领地」（遮罩）圈定，默认对齐光碟、避开文字，见 DEFAULT_MASK。
 * 四套观感共用同一个游戏场——如果每条规则各自决定铺多大，换规则时画面会跳。
 *
 * 所有随机都走带种子的 mulberry32：同一个种子得到同一套初始分布，方便复现和写
 * 测试。注意时间积分用的是真实帧间隔（见 frame()），所以「同一帧序列」才可复现，
 * 而不是「同一次点击」——真实时间不能拿固定步长糊弄，否则帧率一变速度就变。
 */
(() => {
  "use strict";

  const TAU = Math.PI * 2;

  // 硬上限。用户把 count 拉满时先兜住性能，而不是等掉帧了再补。
  const MAX_PARTICLES = 600;

  const TRANSITION_SECONDS = 0.7; // 换驱动规则的过渡时长
  const COUNT_RAMP = 260; // 数量每秒最多增减多少个（拖滑杆不会瞬间跳变）
  const FADE_IN = 2.4; // alpha 每秒上升速率
  const FADE_OUT = 3.6; // alpha 每秒下降速率

  // 值噪声的空间尺度：数值越大，涡旋越小、越密。按 0.018 算，一个噪声格约
  // 55 px，640×180 的条幅上横向有十来个涡旋——这个密度下才看得到「流」，而不是
  // 一条被整体推着走的平行雨幕（0.004 时整块画布落在同一格里，实测所有粒子
  // 朝同一个方向跑，那就不是湍流了）。
  const NOISE_SCALE = 0.018;

  // ── 领地（遮罩）─────────────────────────────────────────
  //
  // 粒子铺满整张卡片时，第一眼是「这屏幕脏了」而不是「这是个特效」：细斜线
  // 正好是划痕的视觉特征，压在标题和滑杆上尤其糟。所以给粒子划一块领地——
  // 以光碟圆心为中心的椭圆，出了边界 alpha 快速归零，卡片边缘和文字区保持干净。
  //
  // 顺便换来一个更重要的东西：整幅画面有了明确的焦点。均匀撒灰没有主次，
  // 一圈围着光碟的尘埃有。
  //
  // 默认值是按真实布局量出来的（1280 视口下播放器卡片 658.8×720.2，光碟圆心
  // 在卡片高度的 45% 处，碟面半径 121、外圈频谱到 158，文字块底边在圆心上方
  // 192、播放控件顶边在下方 187）：椭圆取 0.42w × 0.23h，正好包住外圈频谱又
  // 碰不到文字。
  const DEFAULT_MASK = Object.freeze({
    cxRatio: 0.5, // 圆心 x（画布宽度的比例）
    cyRatio: 0.45, // 圆心 y（画布高度的比例）
    rxRatio: 0.42, // 横向半径（画布宽度的比例）
    ryRatio: 0.23, // 纵向半径（画布高度的比例）
    feather: 0.35, // 0 = 硬边；0.35 = 从 65% 半径处开始淡出
    glow: 0.05, // 圆心处极淡的暖色辉光，把粒子和光碟绑成一组（0 = 不画）
  });

  // ── 单颗粒子的画法 ──────────────────────────────────────
  //
  // 硬边的圆点在 2 倍屏上就是一颗颗小圆珠，凑近看像撒在纸上的胡椒——「屏幕脏了」
  // 有一半是这么来的。所以每颗粒子不是 arc + fill，而是把一颗预先烘焙好的柔边
  // 光点贴上去：中心接近实心、边缘一圈极淡的光晕，读起来是尘埃而不是色块。
  //
  // 这笔观感是花钱买的，如实记账：headless 软件光栅下 600 颗粒子、658×720 画布，
  // 每帧 arc+fill 约 0.1 ms，改贴图后 1.0–1.6 ms（贴图要过一次重采样）。默认 300
  // 颗粒子约 0.5–0.8 ms，占 60 fps 预算的 3%–5%，换来的是凑近看不露怯——
  // 这里选观感。真要在低端机上省这一笔，把本文件的 draw() 换回 arc 即可。
  //
  // 贴图边长取「粒子直径 × 这个倍数」，光晕因此比实心部分大一圈；倍数太小就
  // 退化成硬边圆点，太大整片会糊成雾。边长本身对成本不敏感（6..32 px 实测
  // 都在同一档），所以取中间值兼顾缩放的平滑度。
  const SPRITE_SIZE = 24;
  const SPRITE_SCALE = 2.8;

  // 谱心（Hz）→ 0..1「亮暗」的对数映射区间。用对数是因为音高的听感是对数的。
  const BRIGHT_LOW = 120;
  const BRIGHT_HIGH = 1620;
  const BRIGHT_SPAN_LOG = Math.log2(BRIGHT_HIGH / BRIGHT_LOW);

  // ── 参数目录 ────────────────────────────────────────────
  //
  // 这里是渲染选择器的唯一数据源：上层照着 PARAMS 生成滑杆，照着 DRIVERS 生成
  // 规则选择器，不需要在 UI 里再抄一份参数表。
  const PARAMS = Object.freeze(
    [
      {
        id: "count",
        name: "粒子数量",
        min: 30,
        max: MAX_PARTICLES,
        step: 10,
        // 默认值是按「粒子只在光碟一圈活动」定的：领地比整张卡片小得多，同样
        // 的数量落在里面会显得更密，所以比铺满全屏时给得多一些。
        default: 300,
        hint: "调大画面更厚实，调小更透气；粒子不会跑到文字上去，放心拉",
      },
      {
        id: "size",
        name: "粒子大小",
        min: 0.4,
        max: 4,
        step: 0.1,
        // 默认偏「有重量」而不是「极细」：1 px 级的点在浅色底上接近临界可见度，
        // 看起来像屏幕上的灰点而不是设计出来的光。宁可少而清楚。
        default: 2.1,
        hint: "调大从「尘埃」变成「光点」，再大就丢掉了细腻感",
      },
      {
        id: "trail",
        name: "拖尾",
        min: 0,
        max: 1,
        step: 0.01,
        // 默认比「最长拖尾」短一截：细长的斜线正是划痕的视觉特征，短尾或圆点
        // 才像星尘。想要星轨的可以把 星轨 规则自带的推荐值套上来。
        default: 0.45,
        hint: "调大留下余晖与轨迹，调到 0 就是每帧清屏、干净利落",
      },
      {
        id: "speed",
        name: "整体速度",
        min: 0,
        max: 3,
        step: 0.05,
        default: 1,
        hint: "调大更活跃，调小更沉；调到 0 只剩音频本身在推",
      },
      {
        id: "spread",
        name: "扩散范围",
        min: 0.2,
        max: 2,
        step: 0.05,
        default: 0.9,
        hint: "调大铺得更开，超出光碟一圈的部分会自动淡出",
      },
      {
        id: "react",
        name: "反应强度",
        min: 0,
        max: 2,
        step: 0.05,
        default: 1,
        hint: "对音乐的反应幅度；调到 0 就成了一段缓慢的静物",
      },
    ].map(Object.freeze)
  );

  const PARAM_INDEX = {};
  const DEFAULT_PARAMS = {};
  PARAMS.forEach((def) => {
    PARAM_INDEX[def.id] = def;
    DEFAULT_PARAMS[def.id] = def.default;
  });

  // ── 值噪声 ──────────────────────────────────────────────
  //
  // 湍流规则要的是一个平滑的随机场。simplex / perlin 的频谱和旋度特性在这里用
  // 不上，值噪声 + 五次平滑已经足够，还省掉一个第三方依赖（本项目的铁律）。
  // 全程整数哈希，结果只由坐标决定，所以同一帧的同一位置永远得到同一个值。

  function hash3(x, y, z) {
    let h = Math.imul(x | 0, 374761393) ^ Math.imul(y | 0, 668265263) ^ Math.imul(z | 0, 1274126177);
    h = Math.imul(h ^ (h >>> 13), 1274126177);
    return ((h ^ (h >>> 16)) >>> 0) / 4294967296;
  }

  /** 五次平滑：一阶、二阶导数在格点上都连续，插值出来看不出方格。 */
  function fade(t) {
    return t * t * t * (t * (t * 6 - 15) + 10);
  }

  function valueNoise(x, y, z) {
    const xi = Math.floor(x);
    const yi = Math.floor(y);
    const zi = Math.floor(z);
    const xf = fade(x - xi);
    const yf = fade(y - yi);
    const zf = fade(z - zi);

    const c000 = hash3(xi, yi, zi);
    const c100 = hash3(xi + 1, yi, zi);
    const c010 = hash3(xi, yi + 1, zi);
    const c110 = hash3(xi + 1, yi + 1, zi);
    const c001 = hash3(xi, yi, zi + 1);
    const c101 = hash3(xi + 1, yi, zi + 1);
    const c011 = hash3(xi, yi + 1, zi + 1);
    const c111 = hash3(xi + 1, yi + 1, zi + 1);

    const y00 = c000 + (c100 - c000) * xf;
    const y10 = c010 + (c110 - c010) * xf;
    const y01 = c001 + (c101 - c001) * xf;
    const y11 = c011 + (c111 - c011) * xf;
    const z0 = y00 + (y10 - y00) * yf;
    const z1 = y01 + (y11 - y01) * yf;
    return z0 + (z1 - z0) * zf;
  }

  // ── 确定性伪随机 ────────────────────────────────────────
  // 与 engine.js 里同一份实现：同种子 → 同序列。
  function mulberry32(seed) {
    let state = seed >>> 0;
    return function next() {
      state = (state + 0x6d2b79f5) >>> 0;
      let t = Math.imul(state ^ (state >>> 15), 1 | state);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  // ── 颜色 ────────────────────────────────────────────────

  /** 解析 #rgb / #rrggbb / rgb() / rgba()。认不出来就退回米白底，不抛错。 */
  function parseColor(css, fallback) {
    const text = typeof css === "string" ? css.trim() : "";
    const hex = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(text);
    if (hex) {
      const body = hex[1];
      if (body.length === 3) {
        return {
          r: parseInt(body[0] + body[0], 16),
          g: parseInt(body[1] + body[1], 16),
          b: parseInt(body[2] + body[2], 16),
        };
      }
      return {
        r: parseInt(body.slice(0, 2), 16),
        g: parseInt(body.slice(2, 4), 16),
        b: parseInt(body.slice(4, 6), 16),
      };
    }
    const rgb = /^rgba?\(\s*([\d.]+)[\s,]+([\d.]+)[\s,]+([\d.]+)/i.exec(text);
    if (rgb) {
      return { r: Number(rgb[1]) | 0, g: Number(rgb[2]) | 0, b: Number(rgb[3]) | 0 };
    }
    return fallback;
  }

  function clamp01(value) {
    if (!Number.isFinite(value)) return 0;
    return value < 0 ? 0 : value > 1 ? 1 : value;
  }

  function clamp(value, low, high) {
    if (!Number.isFinite(value)) return low;
    return value < low ? low : value > high ? high : value;
  }

  /**
   * 把用户传进来的 mask 补成一份完整的、所有字段都可用的配置。
   * 逐字段夹紧而不是整体接受：一个 0.9 的 ryRatio 会让领地大到盖住文字，
   * 一个 NaN 会让整块画布消失，这两种「配置错误」都不该由用户来调试。
   */
  function normalizeMask(input) {
    const source = input && typeof input === "object" ? input : {};
    const pick = (key, fallback) => {
      const value = Number(source[key]);
      return Number.isFinite(value) ? value : fallback;
    };
    return {
      cxRatio: clamp(pick("cxRatio", DEFAULT_MASK.cxRatio), -1, 2),
      cyRatio: clamp(pick("cyRatio", DEFAULT_MASK.cyRatio), -1, 2),
      rxRatio: clamp(pick("rxRatio", DEFAULT_MASK.rxRatio), 0.02, 2),
      ryRatio: clamp(pick("ryRatio", DEFAULT_MASK.ryRatio), 0.02, 2),
      feather: clamp(pick("feather", DEFAULT_MASK.feather), 0, 1),
      glow: clamp(pick("glow", DEFAULT_MASK.glow), 0, 1),
    };
  }

  // ── 驱动规则 ────────────────────────────────────────────
  //
  // 每条规则是 { name, blurb, rec, spawn, force }：
  //   spawn(p, env)  —— 把这颗粒子放到这条规则下「应该出现的地方」
  //   force(p, env)  —— 只写 p.ax / p.ay / p.drag；需要重生时置 p.dead = true
  // 生命周期（什么时候重生）由规则自己决定，所以重力落地、爆发飞出边界这些
  // 差别都能自然表达；混合过渡时只让新规则管生命周期，见 frame()。
  //
  // env 每帧只构造一次并复用（见 System 构造器），规则里不要再 new。

  const DRIVER_DEFS = {
    gravity: {
      name: "重力场",
      blurb: "粒子被低频往下拽，落得慢、飘得远",
      rec: { count: [140, 380], size: [1.2, 3], trail: [0, 0.45], speed: [0.6, 1.4], spread: [0.6, 1.1], react: [0.8, 1.8] },
      spawn(p, env) {
        const rnd = env.rng;
        p.x = env.cx + (rnd() - 0.5) * env.w;
        p.y = env.top + env.h * 0.9 * rnd(); // 从半空起步，不用等第一批落到底
        p.vx = (rnd() - 0.5) * env.unit * 0.08;
        p.vy = rnd() * env.unit * 0.06;
        p.drift = (rnd() - 0.5) * 2;
        p.rate = 0.5 + rnd() * 0.9;
        p.phase = rnd() * TAU;
      },
      force(p, env) {
        // 低频直接加重力：bass 越大落得越急。乘 0.25 的底是为了让安静时也还在
        // 缓慢下沉——完全静止的粒子看起来像卡住了。
        p.ay = env.unit * 1.7 * (0.25 + env.bass * 1.9) * env.react;
        // 高频给一点向上的抬升，音色亮的时候雨会飘起来
        p.ay -= env.unit * env.treble * 1.2 * env.react;
        // 横向：一个固定漂移加一点正弦摆，避免所有粒子走成一条竖直线
        p.ax = p.drift * env.unit * 0.16 + Math.sin(env.t * p.rate + p.phase) * env.unit * 0.22;
        p.drag = 0.25;
        // 越接近底部越淡，淡完再回顶部重生——「消散」比「撞到地板消失」安静得多。
        // 这里用领地的上下边界而不是画布边界：粒子只在光碟一圈活动，让它一路
        // 落到卡片底部毫无意义（那段早就被遮罩淡成 0 了，等于白算几百帧）。
        p.fade = clamp01((env.bottom - p.y) / (env.h * 0.16));
        if (p.y > env.bottom) p.dead = true;
      },
    },

    burst: {
      name: "径向爆发",
      blurb: "粒子从中心炸开，鼓点落下的那一刻最亮",
      rec: { count: [110, 320], size: [1.2, 3], trail: [0.35, 0.75], speed: [0.8, 1.8], spread: [0.5, 1.1], react: [1, 2] },
      spawn(p, env) {
        const rnd = env.rng;
        p.angle = rnd() * TAU;
        const radius = (0.02 + rnd() * 0.1) * env.maxR;
        p.x = env.cx + Math.cos(p.angle) * radius;
        p.y = env.cy + Math.sin(p.angle) * radius;
        const kick = (0.1 + rnd() * 0.16) * env.unit;
        p.vx = Math.cos(p.angle) * kick;
        p.vy = Math.sin(p.angle) * kick;
        p.rate = 0.7 + rnd() * 0.6;
        p.phase = rnd() * TAU;
      },
      force(p, env) {
        const dx = p.x - env.cx;
        const dy = p.y - env.cy;
        const dist = Math.sqrt(dx * dx + dy * dy) || 0.0001;
        const ux = dx / dist;
        const uy = dy / dist;
        // 常态外推跟着响度走，安静时几乎不推；beat 是一记短促的额外冲量。
        // 把 beat 当加速度而不是「一次性速度」是因为帧率会变，一次性速度会
        // 让同一次鼓点在 120 Hz 屏上比 60 Hz 屏踢得远一倍。
        const push = env.unit * (0.35 + env.level * 0.9 + env.beat * 4.5) * env.react;
        p.ax = ux * push;
        p.ay = uy * push;
        // 没有阻尼的话粒子会一路加速飞出屏幕，看起来是「漏了」而不是「炸开」
        p.drag = 1.6;
        const outer = env.maxR * env.spread;
        p.fade = clamp01((outer - dist) / (outer * 0.3));
        if (dist > outer) p.dead = true;
      },
    },

    turbulence: {
      name: "湍流",
      blurb: "气流推着粒子翻涌，声音越密转得越快",
      rec: { count: [180, 460], size: [1.2, 3.2], trail: [0.6, 0.95], speed: [0.4, 1.2], spread: [0.6, 1.1], react: [0.5, 1.4] },
      spawn(p, env) {
        const rnd = env.rng;
        p.x = env.cx + (rnd() - 0.5) * env.w * 0.9;
        p.y = env.cy + (rnd() - 0.5) * env.h * 0.9;
        p.vx = 0;
        p.vy = 0;
        p.seed = rnd() * 1000;
      },
      force(p, env) {
        const scale = NOISE_SCALE;
        // 两个分量取自同一噪声场的不同位置：同一个采样点会让所有粒子朝对角线
        // 方向跑（两个分量相关），错开偏移量就能得到真正的二维流场。
        const nx = valueNoise(p.x * scale, p.y * scale, env.noiseT + p.seed);
        const ny = valueNoise(p.x * scale + 43.7, p.y * scale + 91.3, env.noiseT + p.seed + 17.1);
        const amp = env.unit * 2.4 * env.react;
        p.ax = (nx - 0.5) * 2 * amp;
        p.ay = (ny - 0.5) * 2 * amp;
        // 向心的回复力：噪声场是发散的，没有它粒子迟早会被推到角落堆成一团。
        // 刚度必须随尺度走——「每像素多少加速度」这种量纲不能写死常数，否则
        // 同一组参数在大卡片上把云吹散、在小卡片上又缩成一团。按 unit/maxR
        // 归一之后，云的相对形状与画布尺寸无关；spread 再反比平方地缩放它，
        // 于是 spread 直接就是「云有多大」。
        const pull = 0.5 * (env.unit / env.maxR) / (env.spread * env.spread);
        p.ax += (env.cx - p.x) * pull;
        p.ay += (env.cy - p.y) * pull;
        p.drag = 1.1;
      },
    },

    orbit: {
      name: "星轨",
      blurb: "粒子绕着中心转，音色越亮转得越快",
      rec: { count: [90, 300], size: [1, 2.4], trail: [0.85, 0.99], speed: [0.7, 1.6], spread: [0.6, 1.1], react: [0.8, 1.6] },
      spawn(p, env) {
        const rnd = env.rng;
        p.angle = rnd() * TAU;
        // 每颗粒子有自己的轨道半径，否则所有粒子会挤成一根线
        p.radius = (0.3 + rnd() * 0.65) * env.maxR * env.spread;
        p.x = env.cx + Math.cos(p.angle) * p.radius;
        p.y = env.cy + Math.sin(p.angle) * p.radius;
        const spin = env.spin;
        p.vx = -Math.sin(p.angle) * p.radius * spin;
        p.vy = Math.cos(p.angle) * p.radius * spin;
        p.rate = 0.5 + rnd() * 0.8;
        p.phase = rnd() * TAU;
      },
      force(p, env) {
        const dx = p.x - env.cx;
        const dy = p.y - env.cy;
        const dist = Math.sqrt(dx * dx + dy * dy) || 0.0001;
        const ux = dx / dist;
        const uy = dy / dist;
        // 转速由谱心决定：音色越亮转得越快。beat 再叠一记加速。
        const spin = env.spin * (1 + env.beat * 1.6 * env.react);
        // 用「朝目标切向速度靠拢」而不是硬积一个切向加速度：硬加速度会让轨道
        // 越转越大地螺旋出去，靠拢式则天然收敛到稳定圆轨道。
        const targetX = -uy * dist * spin;
        const targetY = ux * dist * spin;
        p.ax = (targetX - p.vx) * 3.2;
        p.ay = (targetY - p.vy) * 3.2;
        // 半径抖动来自高频；回复力负责把粒子按回自己的轨道上
        const wobble = 1 + env.treble * 0.9 * env.react * Math.sin(env.t * p.rate + p.phase);
        const pull = (p.radius * wobble - dist) * 6;
        p.ax += ux * pull;
        p.ay += uy * pull;
        p.drag = 1.4;
      },
    },
  };

  const DRIVER_IDS = Object.keys(DRIVER_DEFS);

  /** 驱动规则目录，供上层渲染成选择器。 */
  const DRIVERS = Object.freeze(
    DRIVER_IDS.map((id) =>
      Object.freeze({
        id: id,
        name: DRIVER_DEFS[id].name,
        blurb: DRIVER_DEFS[id].blurb,
      })
    )
  );

  /** 粒子。字段一次性列全，保持隐藏类稳定（别在运行期删字段）。 */
  function Particle() {
    this.x = 0;
    this.y = 0;
    this.vx = 0;
    this.vy = 0;
    this.ax = 0;
    this.ay = 0;
    this.drag = 0;
    this.alpha = 0; // 数量增减时的淡入淡出
    this.fade = 1; // 驱动规则自己的消散（落地、飞出边界）
    this.color = 0;
    this.seed = 0;
    this.rate = 1;
    this.phase = 0;
    this.drift = 0;
    this.angle = 0;
    this.radius = 0;
    this.live = false;
    this.dead = false;
  }

  class System {
    constructor(canvas, opts) {
      const options = opts || {};
      this.canvas = canvas;
      this.ctx = canvas && typeof canvas.getContext === "function" ? canvas.getContext("2d") : null;
      this.destroyed = false;

      this.params = Object.assign({}, DEFAULT_PARAMS);
      this.setParams(options.params);

      // 粒子池一次性分配到位，之后运行期只改字段、不再 new。
      this.pool = new Array(MAX_PARTICLES);
      for (let index = 0; index < MAX_PARTICLES; index += 1) this.pool[index] = new Particle();

      this.background = typeof options.background === "string" ? options.background : "#f5f2ea";
      this.bgFallback = { r: 245, g: 242, b: 234 };
      this.bg = parseColor(this.background, this.bgFallback);

      // 领地。options.mask === false 就退回「铺满整张画布」的老行为，
      // 其余情况一律按 DEFAULT_MASK 补齐缺失字段——调用方只传一个
      // { mask: { ryRatio: 0.3 } } 也能用。
      this.mask = options.mask === false ? null : normalizeMask(options.mask);
      this.maskOn = false;
      this.maskCx = 0;
      this.maskCy = 0;
      this.maskRx = 1;
      this.maskRy = 1;
      this.maskFeather = DEFAULT_MASK.feather;
      this.maskGlow = DEFAULT_MASK.glow;
      this.glowGradient = null;
      this.glowRadius = 0;
      this.worldUnit = 1;
      // 默认配色：赤陶（也就是频谱环用的那个强调色）+ 浅陶土 + 暖灰。
      //
      // 刻意不含两样东西。一是近黑的深墨 #4c463d——那是正文的颜色，抽到它的
      // 粒子会在浅底上变成几个扎眼的黑点，整片尘埃立刻读成「屏幕上的脏东西」。
      // 二是高饱和的苔绿：暖色底上撒一把冷绿，视觉上就是「撒糖霜」，一眼假。
      // 三档全部落在「纸 + 陶土」这一个色系里，随机抽到哪一档都是同一片光尘，
      // 而且和频谱环同色，粒子和光碟看起来才是一件东西。
      this.palette = [];
      this.sprites = [];
      this.setPalette(options.palette || ["#a8552f", "#c08a63", "#8d8579"]);

      const wanted = typeof options.driver === "string" ? options.driver : "";
      this.driver = DRIVER_DEFS[wanted] ? wanted : "gravity";
      this.prevDriver = this.driver;
      this.blend = 1; // 1 = 完全由 this.driver 驱动

      this.rng = mulberry32(Number.isFinite(options.seed) ? options.seed >>> 0 : 1);

      this.width = 0;
      this.height = 0;
      this.unit = 1;
      this.count = 0; // 当前活跃数量，向 params.count 平滑靠拢
      this.lastTime = 0;
      this.time = 0;
      this.noiseT = 0;
      this.colorKey = "";
      this.trailFill = "";

      // 每帧复用的环境对象。规则函数从这里读，不再各自 new。
      this.env = {
        dt: 0,
        w: 0, // 领地的宽 / 高（不是画布的）
        h: 0,
        top: 0, // 领地的上下边界，坐标仍是画布坐标
        bottom: 0,
        cx: 0,
        cy: 0,
        unit: 1,
        maxR: 1,
        t: 0,
        noiseT: 0,
        bass: 0,
        mid: 0,
        treble: 0,
        rms: 0,
        level: 0,
        centroid: 0,
        bright: 0,
        beat: 0,
        react: 1,
        speed: 1,
        spread: 1,
        spin: 0,
        rng: this.rng,
      };

      // 容器尺寸变化由观察器兜住，上层只在容器不是靠布局变化时才需要手动 resize()。
      this.observer = null;
      if (typeof ResizeObserver !== "undefined" && canvas) {
        this.observer = new ResizeObserver(() => this.resize());
        this.observer.observe(canvas);
      }

      this.resize();
    }

    // ── 对外接口 ──────────────────────────────────────────────

    /** 喂一帧特征并渲染。features 来自 MCFeatures 的 update()。 */
    frame(features) {
      if (this.destroyed) return;
      const ctx = this.ctx;
      if (!ctx || this.width < 4 || this.height < 4) return;

      // 用真实帧间隔积分。固定步长会让速度随帧率漂移（120 Hz 屏上快一倍），
      // 而上下限是为了兜住切标签页回来的那种长间隔——那一帧不该把粒子甩飞。
      const now = performance.now();
      let dt = this.lastTime > 0 ? (now - this.lastTime) / 1000 : 1 / 60;
      this.lastTime = now;
      dt = clamp(dt, 1 / 240, 1 / 30);

      const env = this.env;
      const source = features || EMPTY_FEATURES;
      const react = this.params.react;

      this.time += dt;
      env.dt = dt;
      env.t = this.time;
      env.react = react;
      env.speed = this.params.speed;
      env.spread = this.params.spread;
      env.bass = clamp01(source.bass);
      env.mid = clamp01(source.mid);
      env.treble = clamp01(source.treble);
      env.rms = clamp01(source.rms);
      env.level = clamp01(Number.isFinite(source.level) ? source.level : source.rms);
      env.centroid = clamp01(source.centroid);
      env.beat = clamp01(source.beat);
      // 谱心的 Hz 值优先；拿不到就用比值乘奈奎斯特粗算。映射取对数区间，
      // 因为音色的听感是对数的：120 Hz 以下算「暗」，1.6 kHz 以上算「亮」。
      const centroidHz = Number.isFinite(source.centroidHz) && source.centroidHz > 0
        ? source.centroidHz
        : env.centroid * 22050;
      env.bright = centroidHz > BRIGHT_LOW
        ? clamp01(Math.log2(centroidHz / BRIGHT_LOW) / BRIGHT_SPAN_LOG)
        : 0;
      // 湍流场的时间演化速度由中频决定：声音越密，翻涌越快。
      this.noiseT += (0.12 + env.mid * 0.9 * react) * dt * this.params.speed;
      env.noiseT = this.noiseT;
      env.spin = (0.5 + env.bright * 2.5) * this.params.speed;

      // 数量平滑增减：拖滑杆时不该整片闪一下
      const target = this.params.count;
      const step = COUNT_RAMP * dt;
      this.count = this.count < target
        ? Math.min(target, this.count + step)
        : Math.max(target, this.count - step);
      const active = Math.round(this.count);

      if (this.blend < 1) this.blend = Math.min(1, this.blend + dt / TRANSITION_SECONDS);
      const weight = this.blend * this.blend * (3 - 2 * this.blend); // smoothstep

      this.updateParticles(env, active, weight);
      this.draw(active);
    }

    /** 换驱动规则。不重排、不清屏，两组力在 0.7 s 内交叉淡入淡出。 */
    setDriver(id) {
      if (!DRIVER_DEFS[id] || id === this.driver) return;
      this.prevDriver = this.driver;
      this.driver = id;
      // 上一次过渡还没走完就又换：直接以当前规则为起点重新开始，不叠三层力。
      this.blend = 0;
    }

    /** 换配色（CSS 颜色字符串数组）。只重新分配颜色下标，不重置粒子。 */
    setPalette(colors) {
      if (!Array.isArray(colors)) return;
      const clean = colors.filter((item) => typeof item === "string" && item.trim());
      if (!clean.length) return;
      this.palette = clean;
      // 取模重映射而不是重新抽签：换配色不该让画面重排，也不该消耗随机序列。
      for (let index = 0; index < this.pool.length; index += 1) {
        this.pool[index].color = this.pool[index].color % clean.length;
      }
      this.buildSprites();
      this.buildGlow(); // 辉光取的是主色，配色换了它也得跟着换
    }

    /**
     * 把每种配色烘焙成一颗柔边光点。离屏画布只在换配色时重建一次，运行期
     * 每颗粒子只是一次 drawImage：边缘的柔和是烘进去的，不花运行时的钱，
     * 但贴图本身要过一次重采样，比 arc+fill 贵（见 SPRITE_SIZE 处的实测）。
     */
    buildSprites() {
      this.sprites = [];
      if (typeof document === "undefined" || !document.createElement) return;
      for (let index = 0; index < this.palette.length; index += 1) {
        const color = parseColor(this.palette[index], { r: 168, g: 85, b: 47 });
        const sprite = document.createElement("canvas");
        sprite.width = SPRITE_SIZE;
        sprite.height = SPRITE_SIZE;
        const sctx = sprite.getContext("2d");
        if (!sctx) {
          this.sprites = [];
          return;
        }
        const half = SPRITE_SIZE / 2;
        const head = color.r + "," + color.g + "," + color.b + ",";
        const gradient = sctx.createRadialGradient(half, half, 0, half, half, half);
        // 落点：0.34 处还剩 0.82 的实心感，0.62 之后迅速转淡，到边缘归零。
        // 这样「实心部分」大约是贴图边长的三分之一，也就是粒子直径那么大，
        // 外面那圈是白送的光晕。
        gradient.addColorStop(0, "rgba(" + head + "1)");
        gradient.addColorStop(0.34, "rgba(" + head + "0.82)");
        gradient.addColorStop(0.62, "rgba(" + head + "0.26)");
        gradient.addColorStop(1, "rgba(" + head + "0)");
        sctx.fillStyle = gradient;
        sctx.fillRect(0, 0, SPRITE_SIZE, SPRITE_SIZE);
        this.sprites.push(sprite);
      }
    }

    /** 调参数，只传要改的字段。 */
    setParams(partial) {
      if (!partial) return;
      PARAMS.forEach((def) => {
        if (!Object.prototype.hasOwnProperty.call(partial, def.id)) return;
        const value = Number(partial[def.id]);
        if (!Number.isFinite(value)) return;
        this.params[def.id] = clamp(value, def.min, def.max);
      });
    }

    /** 读当前参数（返回副本，外部改它不会影响渲染）。 */
    getParams() {
      return Object.assign({}, this.params);
    }

    /** 容器尺寸变了。 */
    resize() {
      const canvas = this.canvas;
      if (!canvas) return;
      const width = canvas.clientWidth;
      const height = canvas.clientHeight;
      if (width < 4 || height < 4) return; // 还没布局出来（隐藏的 view），等下次

      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const pixelWidth = Math.round(width * dpr);
      const pixelHeight = Math.round(height * dpr);
      // 只在真的变了才写 width/height——赋值会清空画布，而这个系统的拖尾全靠
      // 不清屏，每帧无脑赋值等于每帧自己把尾巴擦掉。
      if (canvas.width !== pixelWidth || canvas.height !== pixelHeight) {
        canvas.width = pixelWidth;
        canvas.height = pixelHeight;
      }
      if (this.ctx) this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

      // 先把新尺寸与领地算出来，再搬运粒子——搬运要用的是新领地的尺度，
      // 顺序反了就会按旧尺度缩放（表现为窗口一变大，轨道半径就慢半拍地漂）。
      const oldWidth = this.width;
      const oldHeight = this.height;
      const oldWorldUnit = this.worldUnit;
      const hadSize = oldWidth >= 4 && oldHeight >= 4;

      this.width = width;
      this.height = height;
      this.unit = Math.min(width, height);
      this.applyWorld();
      this.buildGlow();

      if (hadSize) {
        // 按比例搬运，构图不会因为窗口变化而重排。轨道半径跟着领地尺度缩放
        // （而不是画布的短边）：半径是相对领地定的，跟画布走的话，窗口一拉宽
        // 轨道就会胀出领地、被遮罩吃掉。
        const scaleX = width / oldWidth;
        const scaleY = height / oldHeight;
        const scaleUnit = oldWorldUnit > 1 ? this.worldUnit / oldWorldUnit : 1;
        for (let index = 0; index < this.pool.length; index += 1) {
          const p = this.pool[index];
          p.x *= scaleX;
          p.y *= scaleY;
          p.radius *= scaleUnit;
        }
      } else {
        // 从「没有尺寸」到「有尺寸」：全部重来，让粒子按新领地摊开
        for (let index = 0; index < this.pool.length; index += 1) this.pool[index].live = false;
        this.count = 0;
      }

      this.lastTime = 0; // 尺寸变化往往伴随卡顿，别把那一帧的长间隔算进去
    }

    /**
     * 把领地写进 env。所有驱动规则都只读 env，于是「粒子活在光碟一圈里」
     * 这件事只需要在这里说一次——四条规则不用各自知道遮罩的存在，
     * 重力自然会落在领地底边、爆发半径自然收进领地半径。
     */
    applyWorld() {
      const env = this.env;
      const width = this.width;
      const height = this.height;
      const mask = this.mask;
      if (mask) {
        // 按椭圆算：横向半径取画布宽的比例，纵向取高的比例。不做「短边统一」，
        // 因为这里的约束来自文字（上方标题、下方控件），是纵向的。
        const cx = width * mask.cxRatio;
        const cy = height * mask.cyRatio;
        const rx = Math.max(2, width * mask.rxRatio);
        const ry = Math.max(2, height * mask.ryRatio);
        this.maskOn = true;
        this.maskCx = cx;
        this.maskCy = cy;
        this.maskRx = rx;
        this.maskRy = ry;
        this.maskFeather = mask.feather;
        this.maskGlow = mask.glow;
        // 内切圆半径（而不是短半轴）作为「单位长度」：四条规则的尺度都按它定，
        // 用短半轴会让横向的力在扁椭圆里显得比纵向猛。
        const half = Math.min(rx, ry);
        env.cx = cx;
        env.cy = cy;
        env.w = rx * 2;
        env.h = ry * 2;
        env.top = cy - ry;
        env.bottom = cy + ry;
        env.unit = half * 2;
        env.maxR = half;
        this.worldUnit = half;
      } else {
        this.maskOn = false;
        this.maskCx = width / 2;
        this.maskCy = height / 2;
        this.maskRx = width / 2;
        this.maskRy = height / 2;
        this.maskGlow = 0;
        env.cx = width / 2;
        env.cy = height / 2;
        env.w = width;
        env.h = height;
        env.top = 0;
        env.bottom = height;
        env.unit = this.unit;
        env.maxR = this.unit * 0.46;
        this.worldUnit = this.unit;
      }
    }

    /**
     * 预建辉光渐变。渐变对象是「相对某次变换」的，创建一次就能一直用，没必要
     * 每帧 new 一个（那正是本文件反复避免的逐帧分配）。尺寸或配色变了才重建。
     */
    buildGlow() {
      const ctx = this.ctx;
      this.glowGradient = null;
      this.glowRadius = 0;
      if (!ctx || !this.maskOn || this.maskGlow <= 0 || !this.palette.length) return;
      const radius = Math.max(this.maskRx, this.maskRy);
      if (!(radius > 1)) return;
      const color = parseColor(this.palette[0], { r: 168, g: 85, b: 47 });
      const rgb = color.r + "," + color.g + "," + color.b;
      const gradient = ctx.createRadialGradient(this.maskCx, this.maskCy, 0, this.maskCx, this.maskCy, radius);
      gradient.addColorStop(0, "rgba(" + rgb + ",1)");
      gradient.addColorStop(0.55, "rgba(" + rgb + ",0.4)");
      gradient.addColorStop(1, "rgba(" + rgb + ",0)");
      this.glowGradient = gradient;
      this.glowRadius = radius;
    }

    /**
     * 暂停：清空画面并把时间基准归零。
     *
     * 刻意不设「已暂停」的锁。真正省 CPU 的开关是上层别再调 frame()（app.js 的
     * stopViz 取消 requestAnimationFrame 就是这么做的）；这里要是把 frame() 锁
     * 死，上层恢复播放时只会看到一块永远不动的画布，还得额外找一个 unlock 接口。
     * 归零 lastTime 是为了恢复的第一帧不会因为中间隔了几分钟而让粒子瞬移。
     */
    pause() {
      if (this.destroyed) return;
      this.lastTime = 0;
      this.clear();
    }

    /** 彻底销毁，解绑所有东西。 */
    destroy() {
      if (this.destroyed) return;
      this.destroyed = true;
      if (this.observer) {
        this.observer.disconnect();
        this.observer = null;
      }
      this.clear();
      this.ctx = null;
      this.canvas = null;
      this.palette = [];
      this.sprites = [];
      this.glowGradient = null;
    }

    // ── 内部 ──────────────────────────────────────────────────

    /** 整块涂成背景色。暂停/销毁时用，避免残留的拖尾留在画布上。 */
    clear() {
      const ctx = this.ctx;
      if (!ctx || this.width < 4) return;
      ctx.globalAlpha = 1;
      ctx.fillStyle = this.background;
      ctx.fillRect(0, 0, this.width, this.height);
    }

    /** 把一颗粒子放到「当前规则应该出现的位置」。 */
    place(p) {
      p.alpha = 0;
      p.fade = 1;
      p.dead = false;
      p.live = true;
      p.color = this.palette.length > 1 ? Math.floor(this.rng() * this.palette.length) : 0;
      DRIVER_DEFS[this.driver].spawn(p, this.env);
    }

    updateParticles(env, active, weight) {
      const pool = this.pool;
      const mainDef = DRIVER_DEFS[this.driver];
      const prevDef = DRIVER_DEFS[this.prevDriver];
      const blending = this.blend < 1 && this.prevDriver !== this.driver;

      for (let index = 0; index < pool.length; index += 1) {
        const p = pool[index];
        const retire = index >= active;

        if (!retire && !p.live) this.place(p);
        if (!p.live) continue; // 已经淡出完的槽位：一次比较就跳过

        // 生命周期（重生）只让权重过半的一方说了算。两套规则同时喊「重生」的话
        // 粒子会在两个位置之间反复横跳。
        mainDef.force(p, env);
        const nextAx = p.ax;
        const nextAy = p.ay;
        const nextDrag = p.drag;
        const nextFade = p.fade;
        const nextDead = p.dead && !retire;
        if (blending) {
          // 过渡期：旧规则只参与受力与消散，不参与生命周期
          p.dead = false;
          prevDef.force(p, env);
          p.ax = nextAx * weight + p.ax * (1 - weight);
          p.ay = nextAy * weight + p.ay * (1 - weight);
          p.drag = nextDrag * weight + p.drag * (1 - weight);
          p.fade = nextFade * weight + p.fade * (1 - weight);
          p.dead = weight >= 0.5 ? nextDead : p.dead;
        } else {
          p.fade = nextFade;
          p.dead = nextDead;
        }

        // 积分：先按阻尼衰减速度，再加速度。指数阻尼与帧率无关，换成
        // v *= (1 - drag*dt) 在 dt 变大时会过冲甚至反向。
        const damp = Math.exp(-p.drag * env.dt);
        p.vx = (p.vx + p.ax * env.dt) * damp;
        p.vy = (p.vy + p.ay * env.dt) * damp;
        p.x += p.vx * env.dt;
        p.y += p.vy * env.dt;

        if (p.dead && !retire) this.place(p);

        // alpha 只管「数量增减」，fade 只管「规则要求的消散」，两者相乘。
        // 分开记是因为它们的时间常数完全不同：一个是滑杆拖出来的，一个是物理的。
        const rate = retire ? -FADE_OUT : FADE_IN;
        p.alpha = clamp01(p.alpha + rate * env.dt);
        if (retire && p.alpha <= 0) p.live = false;
      }
    }

    draw(active) {
      const ctx = this.ctx;
      const env = this.env;
      const params = this.params;

      // 拖尾：半透明地盖一层背景色，而不是 clearRect。alpha 由 trail 决定，
      // trail = 0 时 alpha = 1（等价于清屏），trail = 1 时留下几乎不散的长尾。
      // 字符串缓存起来复用：每帧拼一个新串会白白让 GC 忙起来。
      const trailAlpha = Math.max(0.015, 1 - params.trail * 0.985);
      const alpha = trailAlpha.toFixed(3);
      const key = alpha + "|" + this.background;
      if (key !== this.colorKey) {
        this.colorKey = key;
        this.trailFill = "rgba(" + this.bg.r + "," + this.bg.g + "," + this.bg.b + "," + alpha + ")";
      }
      ctx.globalAlpha = 1;
      ctx.fillStyle = this.trailFill;
      ctx.fillRect(0, 0, this.width, this.height);

      // 光碟背后的暖色辉光。乘 trailAlpha 而不是直接画：拖尾层本身是
      // 「每帧盖掉一部分」，辉光也是每帧画一次，稳态亮度会是 glow / trailAlpha，
      // 拖尾拉长时就会越积越亮直到糊成一片。乘上去之后稳态恰好收敛到 glow，
      // 不论 trail 调到哪里，亮度都是同一个数。
      if (this.glowGradient && this.maskGlow > 0) {
        ctx.globalAlpha = this.maskGlow * trailAlpha;
        ctx.fillStyle = this.glowGradient;
        ctx.beginPath();
        ctx.arc(this.maskCx, this.maskCy, this.glowRadius, 0, TAU);
        ctx.fill();
        ctx.globalAlpha = 1;
      }

      // 尺寸随响度微涨，再用 beat 打一个整体的脉冲——这是四条规则共用的
      // 「听得到拍子」的最低保证，不用每条规则各写一遍。
      const size = Math.max(
        0.4,
        params.size * (1 + env.level * 0.55 * env.react) * (1 + env.beat * 0.45 * env.react)
      );

      // 按颜色分组绘制：外层遍历配色，内层遍历粒子。每颗粒子贴一张自己的
      // 配色贴图，所以同一配色的粒子能连着画完，不用来回换图。
      // （sprites 与 palette 同长，逐位对应；贴图为空时退回 arc 画法。）
      const sprite = size * SPRITE_SCALE;
      const spriteHalf = sprite / 2;
      const sprites = this.sprites;
      const palette = this.palette;

      // 领地的落差。归一化成「椭圆上的相对距离」之后判断：d = 1 是边界，d 越小
      // 越靠圆心。feather 段内用 smoothstep 而不是线性——线性淡出的两端会各留
      // 一道能看出来的硬边，而「看不出边界在哪」正是这块遮罩存在的理由。
      const masked = this.maskOn;
      const invRx = 1 / this.maskRx;
      const invRy = 1 / this.maskRy;
      const cx = this.maskCx;
      const cy = this.maskCy;
      const inner = 1 - this.maskFeather;
      const inner2 = inner * inner;
      const invFeather = this.maskFeather > 0 ? 1 / this.maskFeather : 0;

      for (let color = 0; color < palette.length; color += 1) {
        const image = sprites[color] || null;
        if (!image) ctx.fillStyle = palette[color]; // 退化路径才需要每颗粒子设色
        for (let index = 0; index < active; index += 1) {
          const p = this.pool[index];
          if (p.color !== color) continue;
          let opacity = p.alpha * p.fade;
          if (opacity <= 0.01) continue;

          if (masked) {
            const dx = (p.x - cx) * invRx;
            const dy = (p.y - cy) * invRy;
            const d2 = dx * dx + dy * dy;
            if (d2 >= 1) continue; // 出了领地：不画，也不留拖尾
            if (d2 > inner2) {
              // 只在淡出段开方：领地中心那一大片粒子省掉一次 sqrt。
              let mask = (1 - Math.sqrt(d2)) * invFeather;
              mask = mask * mask * (3 - 2 * mask);
              opacity *= mask;
              if (opacity <= 0.01) continue;
            }
          }

          ctx.globalAlpha = opacity;
          if (image) {
            ctx.drawImage(image, p.x - spriteHalf, p.y - spriteHalf, sprite, sprite);
          } else {
            // 贴图建不出来（离屏画布不可用）时的退路：硬边圆点。观感差一档，
            // 但不至于整层空白——「宁可比设计的丑一点，也别什么都不显示」。
            ctx.beginPath();
            ctx.arc(p.x, p.y, size / 2, 0, TAU);
            ctx.fill();
          }
        }
      }
      ctx.globalAlpha = 1;
    }
  }

  // features 缺失时的替身：粒子照常缓慢浮动，但不对音乐做反应。
  const EMPTY_FEATURES = Object.freeze({
    bass: 0,
    mid: 0,
    treble: 0,
    rms: 0,
    centroid: 0,
    centroidHz: 0,
    beat: 0,
    level: 0,
    active: false,
  });

  /** 拿不到 canvas / context 时的空壳：接口一样，全部安静地什么都不做。 */
  function noopHandle(params) {
    const safe = Object.assign({}, DEFAULT_PARAMS, params || {});
    return {
      frame() {},
      setDriver() {},
      setPalette() {},
      setParams() {},
      getParams() {
        return Object.assign({}, safe);
      },
      resize() {},
      pause() {},
      destroy() {},
    };
  }

  /**
   * @param {HTMLCanvasElement} canvas
   * @param {{driver?:string, palette?:string[], params?:object, seed?:number,
   *          background?:string,
   *          mask?:false|{cxRatio?:number, cyRatio?:number, rxRatio?:number,
   *                       ryRatio?:number, feather?:number, glow?:number}}} [opts]
   *   mask 缺省即启用（默认值见 DEFAULT_MASK，对齐光碟圆心，不碰文字）；
   *   传 false 退回「铺满整张画布」。
   */
  function create(canvas, opts) {
    if (!canvas || typeof canvas.getContext !== "function") return noopHandle(opts && opts.params);
    let handle;
    try {
      handle = new System(canvas, opts);
    } catch (error) {
      // 画布初始化失败（例如 context 被浏览器回收）不该让整个页面挂掉。
      console.warn("[particles] 初始化失败，已降级为空实现", error);
      return noopHandle(opts && opts.params);
    }
    if (!handle.ctx) return noopHandle(opts && opts.params);
    return handle;
  }

  /**
   * 每条驱动规则各自的推荐参数区间，供界面上给出建议（产品明确要求：
   * 参数完全开放，但要给一组「这样调不会难看」的起点）。
   * 返回的是新对象，调用方改它不会污染内部表。
   */
  function recommend(driverId) {
    const def = DRIVER_DEFS[driverId] || DRIVER_DEFS.gravity;
    const out = {};
    PARAMS.forEach((param) => {
      const range = def.rec[param.id];
      out[param.id] = range ? [range[0], range[1]] : [param.min, param.max];
    });
    return out;
  }

  /**
   * 推荐区间的中点，直接可以喂给 setParams。上层做「换规则时套一组合适的参数」
   * 时不用自己算中点，也就不会算出界。
   */
  function defaultsFor(driverId) {
    const ranges = recommend(driverId);
    const out = {};
    Object.keys(ranges).forEach((id) => {
      const def = PARAM_INDEX[id];
      const mid = (ranges[id][0] + ranges[id][1]) / 2;
      out[id] = def ? clamp(mid, def.min, def.max) : mid;
    });
    return out;
  }

  window.MCParticles = {
    DRIVERS: DRIVERS,
    PARAMS: PARAMS,
    create: create,
    recommend: recommend,
    defaultsFor: defaultsFor,
    /** 领地的默认几何，暴露出来便于上层照着实际布局微调（见文件里 DEFAULT_MASK 的注释）。 */
    DEFAULT_MASK: DEFAULT_MASK,
  };
})();
