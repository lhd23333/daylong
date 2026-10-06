"""临时自验脚本：build_day / companion / soundscape 的关键分支实测。

跑法（项目根目录）：python tools/verify_day_plan.py
只读，不写任何项目文件；跑完可直接删除。
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from music_companion.calendar_model import CalendarEvent  # noqa: E402
from music_companion.companion import (  # noqa: E402
    DETAIL_MAX,
    FORBIDDEN_PHRASES,
    GREETING_MAX,
    SUGGESTION_MAX,
    CareCard,
    _TEMPLATES,
    compose_care,
    compose_care_with_ai,
)
from music_companion.day_plan import build_day, day_soundscape_hint  # noqa: E402
from music_companion.soundscape import (  # noqa: E402
    SOUNDSCAPES,
    bpm_for,
    energy_label,
    select_soundscape,
    soundscape_catalog,
)
from music_companion.state import StatusSnapshot  # noqa: E402

FAILURES: list[str] = []


def check(condition: bool, label: str) -> None:
    print(f"  [{'OK' if condition else 'FAIL'}] {label}")
    if not condition:
        FAILURES.append(label)


def event(eid: str, title: str, start: str, end: str, **kwargs) -> CalendarEvent:
    return CalendarEvent.from_mapping({"id": eid, "title": title, "start": start, "end": end, **kwargs})


def state(**kwargs) -> StatusSnapshot:
    payload = {"captured_at": "2026-10-05T14:00:00+08:00", "energy": 60, "stress": 40, "focus": 55}
    payload.update(kwargs)
    return StatusSnapshot.from_mapping(payload)


def show(plan) -> None:
    print(f"  日期={plan.date} 统计={json.dumps(plan.stats, ensure_ascii=False)}")
    print(f"  关怀[{plan.care.tone}] {plan.care.greeting} / {plan.care.detail} / {plan.care.suggestion}")
    print(f"  冲突={plan.conflicts} 音景提示={day_soundscape_hint(plan)}")
    for item in plan.timeline:
        if item.kind == "break":
            print(f"    break  {item.start[11:16]}-{item.end[11:16]} {item.break_type}/{item.label}"
                  f" {item.duration_minutes}min bpm={item.bpm} soundscape={item.soundscape} 理由={item.reason}")
        else:
            print(f"    event  {item.start[11:16]}-{item.end[11:16]} {item.title} [{item.category}/{item.priority}]")


DAY_START = "2026-10-05T08:00:00+08:00"
DAY_END = "2026-10-05T22:00:00+08:00"

print("=== 1. 空 events（整天没有安排）===")
empty = build_day(events=[], day_start=DAY_START, day_end=DAY_END, now="2026-10-05T09:30:00+08:00")
show(empty)
check(empty.timeline == (), "空 events → 时间轴为空")
check(empty.stats["break_count"] == 0 and empty.stats["first_event"] is None, "空 events → 无休息点、first_event 为空")
check(empty.care.tone == "bright", "空日程 + 默认状态 → bright")

print("=== 2. 90 分钟连续学习（硬日程，不可打断）===")
study = build_day(
    events=[event("s1", "数学复习", "2026-10-05T09:00:00+08:00", "2026-10-05T10:30:00+08:00", category="study")],
    state=state(),
    day_start=DAY_START,
    day_end=DAY_END,
    now="2026-10-05T08:30:00+08:00",
)
show(study)
breaks = [item for item in study.timeline if item.kind == "break"]
check(len(breaks) == 1 and breaks[0].break_type == "walk", "90 分钟连续学习 → 安排一次 walk")
check(breaks[0].start[11:16] == "10:30", "walk 紧跟在学习块结束后")

print("=== 3. 5 分钟空档（插 eyes）与 4 分钟空档（不插）===")
five = build_day(
    events=[
        event("s1", "语文", "2026-10-05T09:00:00+08:00", "2026-10-05T10:00:00+08:00", category="study"),
        event("s2", "英语", "2026-10-05T10:05:00+08:00", "2026-10-05T11:00:00+08:00", category="study"),
    ],
    state=state(),
    day_start=DAY_START,
    day_end=DAY_END,
    now="2026-10-05T08:30:00+08:00",
)
show(five)
middle = [item for item in five.timeline if item.kind == "break" and item.start[11:16] == "10:00"]
check(len(middle) == 1 and middle[0].break_type == "eyes", "5 分钟空档 → eyes")
check(middle[0].duration_minutes <= 5, "eyes 时长不超过空档")

four = build_day(
    events=[
        event("s1", "语文", "2026-10-05T09:00:00+08:00", "2026-10-05T10:00:00+08:00", category="study"),
        event("s2", "英语", "2026-10-05T10:04:00+08:00", "2026-10-05T11:00:00+08:00", category="study"),
    ],
    state=state(),
    day_start=DAY_START,
    day_end=DAY_END,
    now="2026-10-05T08:30:00+08:00",
)
show(four)
check(all(item.start[11:16] != "10:00" for item in four.timeline if item.kind == "break"), "4 分钟空档 → 不插 break")

print("=== 4. 高压力（stress=78，精力低）===")
high_stress = build_day(
    events=[event("w1", "小组会", "2026-10-05T09:00:00+08:00", "2026-10-05T11:30:00+08:00", category="work")],
    state=state(stress=78, energy=28, sleep_hours=5.5),
    day_start=DAY_START,
    day_end=DAY_END,
    now="2026-10-05T08:40:00+08:00",
)
show(high_stress)
pressure_breaks = [item for item in high_stress.timeline if item.kind == "break"]
check(high_stress.care.tone in {"quiet", "gentle"}, "高压力/睡眠不足 → 关怀语气 quiet 或 gentle")
check(all(item.bpm == SOUNDSCAPES[item.soundscape].bpm_range[0] for item in pressure_breaks), "压力高 → 每个休息点 BPM 取下沿")
check(not any(word in high_stress.care.greeting + high_stress.care.detail + high_stress.care.suggestion for word in FORBIDDEN_PHRASES), "关怀语无禁用词")

print("=== 5. 深夜（跨零点：20:00 → 次日 02:00）===")
night = build_day(
    events=[event("n1", "晚自习", "2026-10-05T20:30:00+08:00", "2026-10-05T22:30:00+08:00", category="study")],
    state=state(energy=45, stress=30),
    day_start="2026-10-05T20:00:00+08:00",
    day_end="2026-10-06T02:00:00+08:00",
    now="2026-10-05T23:40:00+08:00",
)
show(night)
check(night.date == "2026-10-05", "跨零点时 date 取 day_start 的日期")
check(night.care.tone == "quiet", "深夜 → 关怀语气 quiet")
check(all("2026-10-05" <= item.start[:10] <= "2026-10-06" for item in night.timeline), "跨零点时间轴仍在窗口内")

print("=== 6. 只有 soft 日程（可在日程内部安排休息点）===")
soft = build_day(
    events=[event("sf", "自己看网课", "2026-10-05T09:00:00+08:00", "2026-10-05T11:10:00+08:00", category="study", priority="soft")],
    state=state(),
    day_start=DAY_START,
    day_end=DAY_END,
    now="2026-10-05T08:30:00+08:00",
)
show(soft)
soft_breaks = [item for item in soft.timeline if item.kind == "break"]
check(len(soft_breaks) == 1 and soft_breaks[0].break_type == "walk", "soft 长块内部 → walk")
check(soft_breaks[0].start[11:16] == "10:30", "soft 块内部 walk 落在第 90 分钟")

print("=== 7. 全天无空档（硬日程铺满整窗）===")
packed = build_day(
    events=[event("p", "集训", DAY_START, "2026-10-05T20:00:00+08:00", category="work")],
    state=state(),
    day_start=DAY_START,
    day_end="2026-10-05T20:00:00+08:00",
    now="2026-10-05T09:00:00+08:00",
)
show(packed)
check(packed.stats["free_minutes"] == 0 and packed.stats["break_count"] == 0, "无空档 → 不插休息点")

print("=== 8. 非法输入 ===")
for label, kwargs in (
    ("结束早于开始", {"day_start": DAY_END, "day_end": DAY_START}),
    ("时间缺时区", {"day_start": "2026-10-05T08:00:00", "day_end": DAY_END}),
):
    try:
        build_day(events=[], now=None, **kwargs)
        check(False, f"{label} → ValueError")
    except ValueError as exc:
        check(True, f"{label} → ValueError：{exc}")

print("=== 9. 休息点 id 稳定性 & bpm 区间 ===")
again = build_day(
    events=[event("s1", "数学复习", "2026-10-05T09:00:00+08:00", "2026-10-05T10:30:00+08:00", category="study")],
    state=state(),
    day_start=DAY_START,
    day_end=DAY_END,
    now="2026-10-05T08:30:00+08:00",
)
check([item.id for item in study.timeline] == [item.id for item in again.timeline], "同样输入 → 同样 id")
check(all(
    SOUNDSCAPES[item.soundscape].bpm_range[0] <= item.bpm <= SOUNDSCAPES[item.soundscape].bpm_range[1]
    for item in study.timeline + five.timeline + night.timeline + soft.timeline
    if item.kind == "break"
), "所有休息点 BPM 落在所属音景区间内")

print("=== 10. 音景选择与打分 ===")
for args in (
    ("早晨", "平静", "久坐", "低"),
    ("上午", "专注", "久坐", "中"),
    ("午后", "开心", "行走", "高"),
    ("晚间", "疲惫", "久坐", "低"),
    ("夜间", "平静", "通用", "低"),
    ("", "", "", ""),
):
    print(f"  select_soundscape{args} = {select_soundscape(time_context=args[0], mood=args[1], scene=args[2], energy=args[3])}")
check(select_soundscape(time_context="上午", mood="专注", scene="久坐", energy="中") == "desk-hours", "上午/专注/久坐/中 → desk-hours")
check(select_soundscape(time_context="午后", mood="开心", scene="行走", energy="高") == "way-home", "午后/开心/行走/高 → way-home")
check(select_soundscape(time_context="", mood="", scene="", energy="", explicit="night-lamp") == "night-lamp", "explicit 优先")
check(select_soundscape(time_context="晚上", mood="专注", scene="久坐", energy="中", explicit="不存在的id") == select_soundscape(time_context="晚上", mood="专注", scene="久坐", energy="中"), "未知 explicit 被忽略")
check(energy_label(80) == "高" and energy_label(30) == "低" and energy_label("中") == "中", "energy_label 数值/标签归一")
check(bpm_for("first-light", energy="低") == 58 and bpm_for("first-light", energy="高") == 76, "bpm_for 取下沿/上沿")
check(all(bpm_for(key, energy=value) in range(SOUNDSCAPES[key].bpm_range[0], SOUNDSCAPES[key].bpm_range[1] + 1)
          for key in SOUNDSCAPES for value in ("低", "中", "中高", "高")), "所有音景 × 精力档的 BPM 都在区间内")
try:
    bpm_for("no-such-scape", energy="中")
    check(False, "未知音景 → ValueError")
except ValueError as exc:
    check(True, f"未知音景 → ValueError：{exc}")
check(len(soundscape_catalog()) == 8 and sorted(item["id"] for item in soundscape_catalog()) == sorted(SOUNDSCAPES), "音景目录 8 条、id 与常量一致")

print("=== 11. 文案模板全量体检（长度 / 禁用词 / 占位符）===")
max_facts = {
    "now_hm": "23:40", "phase_label": "早晨", "day_phase": "夜间",
    "tight_count": "12", "tight_start": "09:00", "tight_end": "21:30",
    "longest_gap_minutes": "120", "free_minutes": "180", "break_count": "3",
    "break_phrase": "排了 3 个休息点", "remaining_count": "12",
    "minutes_to_first": "59", "hours_to_first": "1.5 小时", "minutes_to_next": "55",
    "next_start": "14:00", "next_end": "15:30", "since_last": "不到半小时",
    "first_start": "09:00", "first_end": "10:40", "last_start": "18:00", "last_end": "19:30",
    "event_count": "12", "sleep_hours": "5.5", "stress": "78", "energy": "30",
    "wake_hm": "07:00", "sleep_hm": "23:30", "chronotype_phrase": "你偏晚睡", "focus_hours": "3.3",
}
limits = {"greeting": GREETING_MAX, "detail": DETAIL_MAX, "suggestion": SUGGESTION_MAX}
template_count = 0
for branch, pools in _TEMPLATES.items():
    for pool_name in ("greeting", "detail", "suggestion"):
        options = pools[pool_name]
        check_count = len(options) >= 3
        if not check_count:
            FAILURES.append(f"{branch}.{pool_name} 少于 3 条")
        for template in options:
            template_count += 1
            text = template.format_map(max_facts)
            if len(text) > limits[pool_name]:
                FAILURES.append(f"{branch}.{pool_name} 超长({len(text)}): {text}")
            if any(word in text for word in FORBIDDEN_PHRASES):
                FAILURES.append(f"{branch}.{pool_name} 含禁用词: {text}")
print(f"  共体检 {template_count} 条模板（12 个分支 × 3 个措辞池）")
check(not FAILURES, "所有模板都在长度上限内且不含禁用词")

print("=== 12. 关怀语 determinism（同进程 / 跨进程哈希）===")
first = compose_care(now="2026-10-05T14:23:00+08:00", events=[event("s1", "数学", "2026-10-05T09:00:00+08:00", "2026-10-05T10:30:00+08:00", category="study")],
                     state=state(), stats={"break_count": 1, "free_minutes": 90})
second = compose_care(now="2026-10-05T14:23:00+08:00", events=[event("s1", "数学", "2026-10-05T09:00:00+08:00", "2026-10-05T10:30:00+08:00", category="study")],
                      state=state(), stats={"break_count": 1, "free_minutes": 90})
print(f"  {first.greeting} / {first.detail} / {first.suggestion} [{first.tone}]")
check(first == second, "同一输入 → 同一条文案")
print(f"  DIGEST={first.greeting}|{first.detail}|{first.suggestion}|{first.tone}")

print("=== 13. AI 润色（失败回退 + 禁用词丢弃）===")


class OkClient:
    def is_configured(self) -> bool:
        return True

    def care(self, summary, card):
        return {"greeting": "上午两节课连着，中间没有缝。", "detail": "09:00 到 11:30 连着两件事，中间没有空档。",
                "suggestion": "两节之间站起来两分钟。", "tone": "steady"}


class ForbiddenClient(OkClient):
    def care(self, summary, card):
        return {"greeting": "亲爱的用户您好，让我们一起加油！", "detail": "x", "suggestion": "y", "tone": "steady"}


class BrokenClient(OkClient):
    def care(self, summary, card):
        raise RuntimeError("网络炸了")


class LongClient(OkClient):
    def care(self, summary, card):
        return {"greeting": "这是一句非常非常长的关怀语" * 5, "detail": "依据" * 40, "suggestion": "建议" * 30, "tone": "steady"}


local_card = CareCard(greeting="现在 14:23，下午还有两件。", detail="今天 3 件事，最长空档 90 分钟。", suggestion="喝水，然后继续。", tone="steady")
polished = compose_care_with_ai(local_card, {"now": "14:23"}, OkClient())
print(f"  AI 通过 → [{polished.source}/{polished.tone}] {polished.greeting}")
check(polished.source == "ai", "AI 结果被采纳时 source=ai")
print(f"  AI 禁用词 → {compose_care_with_ai(local_card, {}, ForbiddenClient()).source}")
check(compose_care_with_ai(local_card, {}, ForbiddenClient()) == local_card, "AI 含禁用词 → 丢弃并返回本地")
check(compose_care_with_ai(local_card, {}, BrokenClient()) == local_card, "AI 抛异常 → 返回本地")
long_card = compose_care_with_ai(local_card, {}, LongClient())
check(len(long_card.greeting) <= GREETING_MAX and len(long_card.detail) <= DETAIL_MAX and len(long_card.suggestion) <= SUGGESTION_MAX, "AI 超长 → 裁剪到上限")
check(compose_care_with_ai(local_card, {}, None) == local_card, "无 AI 客户端 → 返回本地")
check(compose_care_with_ai(local_card, {}, type("Off", (), {"is_configured": lambda self: False})()) == local_card, "未配置 AI → 返回本地")
print(f"  超长裁剪 → {long_card.greeting} | {long_card.detail} | {long_card.suggestion}")

print("=== 14. 各时段/状态分支抽样 ===")
cases = [
    ("早晨+第一件很近", "2026-10-05T08:40:00+08:00", [event("a", "早读", "2026-10-05T09:00:00+08:00", "2026-10-05T09:40:00+08:00", category="study")], state(), None),
    ("早晨+空档多", "2026-10-05T07:30:00+08:00", [event("a", "早读", "2026-10-05T09:00:00+08:00", "2026-10-05T09:40:00+08:00", category="study")], state(), None),
    ("午后+连排无空档", "2026-10-05T13:30:00+08:00", [
        event("a", "物理", "2026-10-05T13:40:00+08:00", "2026-10-05T14:30:00+08:00", category="study"),
        event("b", "化学", "2026-10-05T14:35:00+08:00", "2026-10-05T15:30:00+08:00", category="study"),
    ], state(), None),
    ("晚间+睡眠不足", "2026-10-05T19:30:00+08:00", [event("a", "作业", "2026-10-05T19:00:00+08:00", "2026-10-05T21:00:00+08:00", category="study")], state(sleep_hours=5.2), None),
    ("夜间", "2026-10-05T23:20:00+08:00", [event("a", "作业", "2026-10-05T19:00:00+08:00", "2026-10-05T21:00:00+08:00", category="study")], state(), None),
    ("精力低", "2026-10-05T10:00:00+08:00", [event("a", "作业", "2026-10-05T10:30:00+08:00", "2026-10-05T12:00:00+08:00", category="study")], state(energy=30), None),
    ("无状态", "2026-10-05T15:00:00+08:00", [event("a", "作业", "2026-10-05T15:30:00+08:00", "2026-10-05T17:00:00+08:00", category="study")], None, None),
    ("显式 day_phase", "2026-10-05T15:00:00+08:00", [], state(), "晚间"),
]
for label, now, events, st, phase in cases:
    card = compose_care(now=now, events=events, state=st, day_phase=phase,
                        stats={"break_count": 1, "free_minutes": 120})
    print(f"  {label}: [{card.tone}] {card.greeting} / {card.detail} / {card.suggestion}")
    check(all(len(text) <= limit for text, limit in ((card.greeting, GREETING_MAX), (card.detail, DETAIL_MAX), (card.suggestion, SUGGESTION_MAX))), f"{label} 长度合规")
    check(not any(word in card.greeting + card.detail + card.suggestion for word in FORBIDDEN_PHRASES), f"{label} 无禁用词")

print("=== 15. HTTP 端点（/api/day、/api/day/break-status、/api/soundscapes）===")
import tempfile  # noqa: E402
import threading  # noqa: E402
from http.client import HTTPConnection  # noqa: E402

from server import MusicCompanionServer  # noqa: E402

data_dir = tempfile.TemporaryDirectory()
server = MusicCompanionServer(("127.0.0.1", 0), data_dir=data_dir.name)
server.timeout = 0.1
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
port = server.server_address[1]


def request(method: str, path: str, body=None):
    connection = HTTPConnection("127.0.0.1", port, timeout=5)
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    connection.request(method, path, data, {"Content-Type": "application/json"} if data else {})
    response = connection.getresponse()
    raw = response.read()
    connection.close()
    return response.status, json.loads(raw.decode()) if raw else None


try:
    status, catalog = request("GET", "/api/soundscapes")
    print(f"  GET /api/soundscapes → {status}，{len(catalog)} 条，第一条={json.dumps(catalog[0], ensure_ascii=False)}")
    check(status == 200 and isinstance(catalog, list) and len(catalog) == 8, "音景目录返回数组且 8 条")
    check(set(catalog[0]) == {"id", "name", "when", "moods", "bpm_range", "character"}, "目录字段与前端约定一致")

    body = {
        "day_start": "2026-10-05T08:00:00+08:00",
        "day_end": "2026-10-05T22:00:00+08:00",
        "now": "2026-10-05T14:23:00+08:00",
        "profile": {"wake": "07:00", "sleep": "23:30", "chronotype": "neutral", "preferred_styles": ["钢琴"], "vocal": "none"},
        "state": {"captured_at": "2026-10-05T14:00:00+08:00", "energy": 60, "stress": 40, "focus": 55,
                  "sleep_hours": 7, "sedentary_minutes": 0, "steps": 0, "heart_rate": 0},
        "events": [
            {"id": "e1", "title": "数学", "start": "2026-10-05T09:00:00+08:00", "end": "2026-10-05T10:30:00+08:00", "category": "study"},
            {"id": "e2", "title": "英语", "start": "2026-10-05T14:40:00+08:00", "end": "2026-10-05T16:00:00+08:00", "category": "study"},
        ],
    }
    status, day = request("POST", "/api/day", body)
    print(f"  POST /api/day → {status}")
    print(f"    {json.dumps({k: v for k, v in day.items() if k != 'timeline'}, ensure_ascii=False)}")
    for item in day["timeline"]:
        print(f"    {json.dumps(item, ensure_ascii=False)}")
    check(status == 200 and set(day) == {"date", "care", "timeline", "stats", "conflicts", "soundscape_hint"}, "响应结构完整")
    check(day["date"] == "2026-10-05" and day["care"]["source"] == "local", "date 与 care.source 正确")
    starts = [item["start"] for item in day["timeline"]]
    check(starts == sorted(starts), "时间轴按 start 排序")
    break_ids = [item["id"] for item in day["timeline"] if item["kind"] == "break"]

    status, again_day = request("POST", "/api/day", {**body, "events": None})
    check(status in (200, 400), "events 省略/为 null 的分支可用")

    status, stored = request("POST", "/api/day", {**body, "events": None}) if False else (0, None)
    # events 省略 → 用服务器已存日程（此时服务器还没有日程，时间轴为空）
    body_no_events = {k: v for k, v in body.items() if k != "events"}
    status, stored = request("POST", "/api/day", body_no_events)
    print(f"  POST /api/day（省略 events）→ {status}，timeline={len(stored['timeline'])} 条")
    check(status == 200 and stored["timeline"] == [], "省略 events 时用服务器已存日程")

    if break_ids:
        break_id = break_ids[0]
        status, result = request("POST", "/api/day/break-status", {"id": break_id, "status": "done"})
        print(f"  POST /api/day/break-status → {status} {result}")
        check(status == 200 and result == {"id": break_id, "status": "done"}, "break-status 返回 id/status")
        status, day_again = request("POST", "/api/day", body)
        hydrated = {item["id"]: item["status"] for item in day_again["timeline"] if item["kind"] == "break"}
        print(f"    重新取 /api/day 的休息点状态：{hydrated}")
        check(hydrated.get(break_id) == "done", "状态回填到 /api/day 时间轴")

        server.shutdown()
        server.server_close()
        server = MusicCompanionServer(("127.0.0.1", 0), data_dir=data_dir.name)
        server.timeout = 0.1
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        status, day_restart = request("POST", "/api/day", body)
        hydrated = {item["id"]: item["status"] for item in day_restart["timeline"] if item["kind"] == "break"}
        print(f"    重启后状态：{hydrated}")
        check(hydrated.get(break_id) == "done", "break-status 跨重启保留")

    status, body_400 = request("POST", "/api/day", {"day_start": "2026-10-05T22:00:00+08:00", "day_end": "2026-10-05T08:00:00+08:00"})
    print(f"  非法区间 → {status} {body_400}")
    check(status == 400 and "error" in body_400, "非法输入 → 400 + 中文错误")
    status, body_400 = request("POST", "/api/day", {"day_start": "2026-10-05T08:00:00", "day_end": "2026-10-05T22:00:00"})
    check(status == 400, "缺时区 → 400")
    status, body_400 = request("POST", "/api/day/break-status", {"id": "x", "status": "nope"})
    check(status == 400, "非法休息点状态 → 400")
    status, body_400 = request("POST", "/api/day/break-status", {"id": "", "status": "done"})
    check(status == 400, "空 id → 400")
    status, health = request("GET", "/api/health")
    check(status == 200 and health["ok"] is True, "既有 /api/health 未受影响")
    status, old = request("POST", "/api/planner/plan", {"day_start": "2026-10-05T08:00:00+08:00", "day_end": "2026-10-05T22:00:00+08:00"})
    check(status == 200 and "plans" in old and "conflicts" in old, "既有 /api/planner/plan 响应结构未变")
finally:
    server.shutdown()
    server.server_close()
    data_dir.cleanup()

print()
if FAILURES:
    print(f"### 失败 {len(FAILURES)} 项：")
    for item in FAILURES:
        print(f"  - {item}")
    sys.exit(1)
print("### 全部自验通过")
