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
