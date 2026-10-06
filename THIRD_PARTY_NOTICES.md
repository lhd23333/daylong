# Third-party notices

本项目的**服务端只用 Python 标准库**，没有任何 pip 依赖。浏览器端为了离线可用，
把用到的库固定版本放在 `static/vendor/` 下，不依赖运行时 CDN。

## Tone.js（运行时使用）

- Repository: https://github.com/Tonejs/Tone.js
- Version: 15.1.22（下载于 2026-10-05）
- License: MIT
- Local file: `static/vendor/tone/Tone.min.js`
- 用途：`static/js/engine.js` 用它搭建 Web Audio 图（PolySynth / FMSynth / AMSynth /
  MonoSynth / MembraneSynth / NoiseSynth + Filter / Chorus / Reverb / FeedbackDelay /
  Limiter / Meter / Analyser），在休息点实时生成音乐。

## CPython 3.12.15（随各免安装版分发，不在源码仓库与标准包里）

- 来源：python-build-standalone（astral-sh），release tag `20261003`，变体
  `install_only_stripped`：macOS 的 aarch64 / x86_64 各一份，Windows 的 x86_64 一份
- Repository: https://github.com/astral-sh/python-build-standalone
  下载地址形如
  `https://github.com/astral-sh/python-build-standalone/releases/download/20261003/cpython-3.12.15_20261003-<triple>-install_only_stripped.tar.gz`
  （`<triple>` 取 `aarch64-apple-darwin` / `x86_64-apple-darwin` / `x86_64-pc-windows-msvc`）
- License: PSF License（包内 `lib/python3.12/LICENSE.txt` 原样保留）
- 校验：下载后核对 SHA256 与官方发布页一致（macOS aarch64
  `ad8d0c637c0a36b967b310e2c07254f4d2ca8cabaa7699e55ed6290aceb481a2`，macOS x86_64
  `562c30864ece2cb1d3e0ad66a1acd498611a47e5a10ce81b99158bef1ccbd355`，Windows x86_64
  `6fba7f2ae506facf41d457ea8293c7497910a675c69a4e954875169410a50402`）
- 用途：在各免安装版 zip 的 `python-runtime/` 下（`macos-arm64` / `macos-x86_64` /
  `windows-x86_64`），给没装 Python 的机器免安装运行。打包时按两个 `prune.txt` 清单
  裁掉与本项目无关的组件（Tcl/Tk、pip、idlelib 等），并逐个校验解释器二进制的文件头与
  架构（`bin/python3.12` 是 64 位 Mach-O 且架构与目录名一致；`python.exe` 是 x86_64 PE）。

  **这三个 tar 与两份 `prune.txt` 不随本仓库分发**（合计约 68 MB，且是官方原始发布物、
  可随时按上面的地址重新下载）。`tools/pack-submit.ps1` 通过 `-RuntimeDir` 参数指向它们
  所在的目录；默认值见该脚本头部注释。

## 音频可视化的两层：只借鉴架构思路，未使用第三方代码

`static/js/features.js` 与 `static/js/particles.js` 采用一层「音频分析 → 标量 → 渲染」的
分层：前者把 engine 的 256 点 FFT 归纳成 bass / mid / treble / rms / 谱心 / 拍点几个
标量，后者只读这些标量、完全不碰原始 FFT。**这个分层思路借鉴自 Audio Shader Studio
（MIT）一类项目**——同一套分析可以驱动任意多套视觉，换视觉不用改分析。

- 参考对象：Audio Shader Studio，License: MIT（架构思路来源，非代码来源）。
- `static/js/particles.js` 里 orbit 驱动的「节拍加速」另有一处思路来源：
  resonance-visualizer 的粒子隧道。该项信息不全，这里只记为思路来源，不列仓库地址
  与许可证。

**这两个文件里的代码全部是自己写的，没有从上述项目复制任何代码。** 借的是分层方式
（把分析结果压成标量再交给渲染层），不是实现——这一点在 `features.js` 与
`particles.js` 的文件头注释里也写明了。因为属于思路层面的参考而非代码引用，此处
不附版本号与下载日期。

## 曾经用过、现已移除的两个库

下面两个库在早期版本里用过，改版后已**从仓库中删除**，此处仅留记录，便于对照
旧版本的来源。当前 `static/vendor/` 下只有 `tone/`。

| 库 | 版本 | 许可证 | 当初用途 | 为什么删 |
| --- | --- | --- | --- | --- |
| [FullCalendar](https://github.com/fullcalendar/fullcalendar) | 6.1.19 | MIT | 用月历控件展示日程 | 新版改成自己画的时间轴（日程 + 休息点 +「现在」标尺一体呈现），页面不再加载它 |
| [ical.js](https://github.com/kewisch/ical.js) | 1.5.0 | MPL-2.0 | 浏览器侧解析 `.ics`（预留） | `.ics` 导入一直走服务端 `music_companion/ics_parser.py`（标准库实现，结果可复现），浏览器侧这份从未被引用 |

两个文件都删除于 2026-10-05，共约 373 KB。删之前已确认：全项目搜索
`fullcalendar` / `ical` 只在这份声明里出现，被页面加载的任何文件都不引用它们。
