# 日历与状态联动增强实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 在现有智能音乐伴侣上实现次日日程、ICS 导入、状态导入、休息/散步计划、提醒和音乐联动。

**Architecture:** Python 标准库后端新增日历模型、ICS 解析、状态模型和确定性计划算法；HTTP API 暴露规范化 JSON。前端保持原生 HTML/CSS/JS，新增日历列表、状态表单、计划卡片和浏览器提醒，数据保存在 localStorage，并与现有推荐/音频流程对接。

**Tech Stack:** Python 3.10+ stdlib, unittest, native HTML/CSS/JS, FullCalendar/ical.js-compatible data boundary with stdlib ICS fallback.

**Spec:** docs/superpowers/specs/2026-10-03-calendar-state-planning-design.md

## Global Constraints
- 不引入 Flask、Node 构建流程、数据库或品牌专属健康 SDK。
- AI 不可用时全部日历、计划、提醒和本地音频功能仍可运行。
- 手表数据只标记为导入数据，不声称浏览器直接读取设备。
- 推荐结果不构成医疗诊断或运动处方。
- 不写入真实 API Key、Cookie 或设备凭证。

## Review Focus
- 重叠日程必须保留并返回冲突，而不是静默覆盖；测试归入 calendar_model。
- 跨午夜和带时区的 ICS 事件必须正确解析；测试归入 ics_parser。
- 连续专注达到阈值但没有足够空闲时间时不得插入计划；测试归入 planner。
- 非法/超范围状态导入必须拒绝；测试归入 state。
- 计划项的 BPM 必须在本地规则范围内并能传给既有推荐器；测试归入 planner/API。

### Task 1: 日历与 ICS 后端模型
**Files:** Create `music_companion/calendar_model.py`, `music_companion/ics_parser.py`; Test `tests/test_calendar.py`.
- [ ] 写失败测试覆盖事件校验、重叠检测、ICS 单事件/多行折叠/UTC 时间解析。
- [ ] 运行测试确认因模块/函数缺失失败。
- [ ] 实现 `CalendarEvent`、`validate_events()`、`find_conflicts()`、`parse_ics()`。
- [ ] 运行测试确认通过。

### Task 2: 状态与计划算法
**Files:** Create `music_companion/state.py`, `music_companion/planner.py`, `music_companion/health_adapter.py`; Test `tests/test_planner.py`.
- [ ] 写失败测试覆盖状态范围、长专注安排、无空闲不插入、高压力 BPM 下调、JSON/CSV 导入。
- [ ] 运行测试确认失败。
- [ ] 实现 `StatusSnapshot`、`parse_status_import()`、`plan_breaks()`、`music_context_for_plan()` 和 `HealthAdapter` 协议。
- [ ] 运行测试确认通过。

### Task 3: HTTP API
**Files:** Modify `server.py`, `music_companion/__init__.py`; Test `tests/test_server_api.py`.
- [ ] 写失败测试覆盖日历增删查、ICS 导入、状态写入/读取、计划生成和错误 JSON。
- [ ] 运行测试确认失败。
- [ ] 实现内存会话存储、请求体限制和 API 路由。
- [ ] 运行测试确认通过。

### Task 4: 前端日历/状态/计划/提醒
**Files:** Modify `static/index.html`, `static/app.js`, `static/styles.css`.
- [ ] 先补静态检查脚本断言新 DOM/API/提醒入口存在。
- [ ] 实现原生离线日历列表和事件编辑、ICS 文件导入、状态录入/导入、计划卡片、Notification 提醒、计划项触发音乐推荐。
- [ ] 运行 Node 语法和静态检查。

### Task 5: 音乐联动与文档
**Files:** Modify `music_companion/recommender.py`, `README.md`, `项目及说明.md`; Create `THIRD_PARTY_NOTICES.md`, `ACCEPTANCE.md`.
- [ ] 写失败测试覆盖计划上下文进入推荐器和 AI 关闭时 fallback。
- [ ] 实现计划上下文映射与解释字段。
- [ ] 修订路径、能力边界、启动/验收说明和第三方选型记录。
- [ ] 运行全量测试、语法检查、API 实测和 WAV 检查。
