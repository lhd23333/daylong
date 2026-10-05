# 日历、状态与休息规划增强设计

## 目标

在现有“状态驱动的音乐推荐与本地音频合成 MVP”上增加可离线演示的次日日程管理、`.ics` 导入、休息/散步规划、手动与手表状态导入、浏览器提醒，并让每个计划项能解释性地生成音乐参数。

## 已确认范围

- 第一版状态来源：手动填写 + `.ics` 日历导入 + JSON/CSV 手表状态导入 + 预留真实设备适配器接口。
- 日历界面采用 GitHub `fullcalendar/fullcalendar`（MIT）；不使用其 Premium iCalendar 插件。
- `.ics` 解析采用 GitHub `kewisch/ical.js`（MPL-2.0）；固定版本并随项目提供第三方 License 说明，支持离线运行。
- 保留 Python 标准库后端，不引入 Flask、Node 构建流程或数据库。
- 浏览器本地数据优先存放在 `localStorage`；后端 API 接受规范化 JSON，便于未来替换存储层。
- 手表数据在第一版只允许用户导入或适配器注入，不声称已直接读取任何品牌设备。

## 明确边界

第一版可以在网页打开期间进行页面倒计时、浏览器 Notification 提醒、声音提示和“开始散步/稍后提醒/忽略”操作。网页关闭后不保证 JavaScript 定时器继续运行；可靠后台通知需要后续 PWA、Service Worker 或手机端实现。

推荐结果是生活节奏建议，不是医疗诊断、运动处方或心率安全判断。异常心率只触发“请自行确认状态/停止活动”的保守提示，不做健康结论。

## 数据模型

### CalendarEvent

```json
{
  "id": "evt-uuid",
  "title": "数学复习",
  "start": "2026-10-04T09:00:00+08:00",
  "end": "2026-10-04T10:30:00+08:00",
  "category": "study",
  "priority": "hard",
  "interruptible": false,
  "source": "manual"
}
```

`category` 枚举：`study`、`work`、`commute`、`exercise`、`break`、`other`。
`priority` 枚举：`hard`、`soft`。
`source` 枚举：`manual`、`ics`。

### StatusSnapshot

```json
{
  "captured_at": "2026-10-03T21:00:00+08:00",
  "energy": 42,
  "stress": 68,
  "focus": 76,
  "sleep_hours": 6.5,
  "heart_rate": 82,
  "steps": 4300,
  "sedentary_minutes": 72,
  "source": "manual"
}
```

`energy`、`stress`、`focus` 范围为 0–100；`sleep_hours` 范围为 0–24；`heart_rate`、`steps`、`sedentary_minutes` 必须为非负数并有合理上限。`source` 为 `manual` 或 `watch_import`。

### BreakPlanItem

```json
{
  "id": "plan-uuid",
  "start": "2026-10-04T10:35:00+08:00",
  "end": "2026-10-04T10:45:00+08:00",
  "type": "walk",
  "reason": "连续专注 90 分钟，且下一硬性日程尚有 35 分钟",
  "reminder_minutes_before": 3,
  "recommended_bpm": 108,
  "status": "planned"
}
```

`type` 枚举：`walk`、`stretch`、`water`、`rest`；`status` 枚举：`planned`、`started`、`done`、`snoozed`、`dismissed`。

## 计划算法

1. 校验并按开始时间排序日程；重叠事件保留并报告冲突，不静默覆盖。
2. `hard` 且 `interruptible=false` 的事件不可插入计划；已有 `exercise`、`commute`、`break` 事件不重复安排同类活动。
3. 在相邻日程和工作日边界中计算空闲区间，只考虑长度至少 10 分钟且距离下一硬性日程至少 10 分钟的窗口。
4. 连续学习/工作达到 50 分钟时优先安排 5 分钟 `stretch` 或 `water`；达到 90 分钟且窗口至少 10 分钟时安排 10–15 分钟 `walk`。
5. 压力高、精力低、睡眠不足或久坐时间长会提高休息优先级；当天步数较高时不额外强制散步。
6. 每个计划项记录触发规则、状态快照摘要和推荐 BPM，保证前端能解释结果。

负荷分数定义为：

$$L = 0.35F + 0.25S + 0.20D + 0.20H$$

其中 `F` 是连续专注负荷、`S` 是压力值、`D` 是久坐时长归一化值、`H` 是睡眠不足程度，均归一化到 0–100。实现可以使用整数分钟和固定阈值，但必须输出触发原因；不把分数当作医疗指标。

## 音乐联动规则

计划项生成音乐上下文：当前日程、计划类型、连续专注分钟、状态快照、负荷分数、目标和限制。推荐器使用该上下文覆盖普通自由输入的活动与 BPM，但仍复用现有风格、配器和 fallback 逻辑。

- `walk`：基础 100–115 BPM；高压力或低精力时向 80–105 BPM 下调。
- `stretch` / `rest`：70–100 BPM，优先钢琴、pad、轻打击。
- `water`：不强制生成音乐，可生成 80–105 BPM 的短提示音上下文。
- 用户手动状态优先于从日程推断的状态；最新快照优先于过期快照。
- AI 只增强文本和风格字段，BPM 区间、目标 BPM、计划类型和安全边界由本地规则校验。

## API

```text
GET  /api/calendar/events?date=YYYY-MM-DD
POST /api/calendar/events
POST /api/calendar/import-ics
DELETE /api/calendar/events/{id}

POST /api/planner/plan
POST /api/state/manual
POST /api/state/import
GET  /api/state/latest
```

接口均使用 JSON；导入接口限制请求体、事件数、字段长度和状态数，错误返回不包含凭证或完整远端错误。

## 前端结构

- 日历页：FullCalendar 日视图 + 事件编辑表单 + `.ics` 文件选择。
- 状态页：手动状态表单 + JSON/CSV 文件导入 + 数据来源徽标。
- 计划页：建议休息卡片、触发原因、BPM、开始/稍后提醒/忽略按钮。
- 结果页：沿用现有音乐推荐和音频生成，但增加“来自日程计划”的上下文摘要。
- 所有本地数据使用版本化 `localStorage` 键，导入新数据前校验，避免覆盖不可恢复。

## 第三方资源与复现

不依赖运行时 CDN。将 FullCalendar 的 MIT 发行包和 ical.js 的固定版本文件放到 `static/vendor/`，保留版本号、来源 URL、License 文件和下载日期。若当前环境无法完成 vendor 固定，则先实现无第三方依赖的标准化 `.ics` fallback，并在状态文档中标记待补齐。

## 验收标准

- 可手动创建、编辑、删除次日日程；重叠事件可见且有冲突提示。
- 可导入常见 `.ics` 文件并生成规范化事件。
- 长时间学习/工作后生成至少一个可解释的休息或散步计划。
- 手动状态与导入状态能改变计划优先级及音乐参数。
- 计划项可触发页面提醒，并支持开始、稍后提醒和忽略。
- AI 未配置时，日程、计划、提醒和本地音乐仍可离线运行。
- Python 单元/API 测试、JavaScript 语法检查和 WAV 结构检查全部通过。
- 文档中的路径、命令、第三方版本和实际提交文件一致。
