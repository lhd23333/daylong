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

## 曾经用过、现已移除的两个库

下面两个库在早期版本里用过，改版后已**从仓库中删除**，此处仅留记录，便于对照
旧版本的来源。当前 `static/vendor/` 下只有 `tone/`。

| 库 | 版本 | 许可证 | 当初用途 | 为什么删 |
| --- | --- | --- | --- | --- |
| [FullCalendar](https://github.com/fullcalendar/fullcalendar) | 6.1.19 | MIT | 用月历控件展示日程 | 新版改成自己画的时间轴（日程 + 休息点 +「现在」标尺一体呈现），页面不再加载它 |
| [ical.js](https://github.com/kewisch/ical.js) | 1.5.0 | MPL-2.0 | 浏览器侧解析 `.ics`（预留） | `.ics` 导入一直走服务端 `music_companion/ics_parser.py`（标准库实现，结果可复现），浏览器侧这份从未被引用 |

两个文件都删除于 2026-10-05，共约 373 KB。删之前已确认：全项目搜索
`fullcalendar` / `ical` 只在这份声明里出现，被页面加载的任何文件都不引用它们。
