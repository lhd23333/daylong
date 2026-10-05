# -*- coding: utf-8 -*-
"""day_plan 模块的单元测试：一天的时间轴编排。

被测文件：music_companion/day_plan.py
覆盖重点：
* 确定性：同样输入 → 完全相同的输出（含 uuid5 生成的休息点 id）；
* 休息点不越过 [day_start, day_end]、不压到 hard & 不可打断的日程；
* 休息类型随连续学习时长与空档大小变化，且**形状相近的多个空档不会被排成同一种**；
* stats 计数与实际条目一致；空日程 / 单件 / 重叠 / 贴边 / 跨界裁剪等边界；
* 缺时区、窗口非法等输入抛 ValueError；hydrate_timeline 不修改原计划。
"""

import unittest
from dataclasses import replace
from datetime import datetime

from music_companion.calendar_model import CalendarEvent
from music_companion.day_plan import (
    BREAK_TYPES,
    TimelineItem,
    build_day,
    day_soundscape_hint,
    hydrate_timeline,
)
from music_companion.soundscape import DEFAULT_SOUNDSCAPE, SOUNDSCAPES
from music_companion.state import StatusSnapshot

DAY = "2026-10-04"
DAY_START = f"{DAY}T08:00:00+08:00"
DAY_END = f"{DAY}T22:00:00+08:00"


def event(eid, start, end, **kw):
    """用当天 HH:MM 构造日程（标题默认等于 id）。"""
    return CalendarEvent(eid, eid, f"{DAY}T{start}:00+08:00", f"{DAY}T{end}:00+08:00", **kw)


def build(events, *, day_start="08:00", day_end="22:00", **kw):
    return build_day(
        events=events,
        day_start=f"{DAY}T{day_start}:00+08:00",
        day_end=f"{DAY}T{day_end}:00+08:00",
        **kw,
    )


def breaks(plan):
    return [item for item in plan.timeline if item.kind == "break"]


def event_items(plan):
    return [item for item in plan.timeline if item.kind == "event"]


def overlaps(first, second):
    return first.start_dt < second.end_dt and second.start_dt < first.end_dt


# 四段 2 小时学习，中间四个形状完全相同的 1 小时空档
ALTERNATING = [
    event("s1", "08:00", "10:00", category="study"),
    event("s2", "11:00", "13:00", category="study"),
    event("s3", "14:00", "16:00", category="study"),
    event("s4", "17:00", "19:00", category="study"),
]
LONG_BLOCK = [event("s1", "09:00", "12:00", category="study")]
TINY_GAP = [
    event("s1", "08:00", "10:00", category="study"),
    event("s2", "10:06", "11:00", category="study"),
]
FOUR_MINUTE_GAP = [
    event("s1", "08:00", "10:00", category="study"),
    event("s2", "10:04", "11:00", category="study"),
]
OVERLAP = [event("x", "09:00", "11:00"), event("y", "10:00", "12:00")]
TOUCHING = [
    event("a", "09:00", "10:00", category="study"),
    event("b", "10:00", "10:40", category="study"),
]
SOFT_BLOCK = [
    event("h", "08:00", "10:00", category="study"),
    event("o", "10:00", "11:00", category="other", priority="soft"),
]
INTERRUPTIBLE = [
    event("k", "09:00", "11:00", category="study", priority="soft", interruptible=True)
]
CLIPPED = [event("big", "06:00", "11:00", category="study")]
STRESSED_STATE = StatusSnapshot(
    "2026-10-03T21:00:00+08:00",
    energy=28,
    stress=80,
    focus=50,
    sleep_hours=5.5,
)


class DayPlanDeterminismTests(unittest.TestCase):
    def test_同输入得到完全相同的输出与休息点id(self):
        first = build(ALTERNATING, day_end="20:00")
        second = build(ALTERNATING, day_end="20:00")
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(
            [item.id for item in first.timeline], [item.id for item in second.timeline]
        )
        # 休息点 id 由 uuid5 生成：同输入跨进程稳定，前端可拿它持久化状态
        selected = breaks(first)
        self.assertTrue(selected)
        for item in selected:
            with self.subTest(item=item.start):
                self.assertEqual(len(item.id), 36)
                self.assertEqual(item.id[8], "-")
                self.assertEqual(item.id[14], "5")
        self.assertEqual(
            [item.id for item in selected],
            [item.id for item in breaks(second)],
        )


class DayPlanBreakPlacementTests(unittest.TestCase):
    """休息点的位置与类型。"""

    def test_休息点不压硬日程且不越过当天窗口(self):
        plan = build(ALTERNATING, day_end="20:00")
        window_start = datetime.fromisoformat(f"{DAY}T08:00:00+08:00")
        window_end = datetime.fromisoformat(f"{DAY}T20:00:00+08:00")
        blocked = [e for e in ALTERNATING if e.priority == "hard" and not e.interruptible]
        selected = breaks(plan)
        self.assertTrue(selected)
        for item in selected:
            with self.subTest(item=item.start):
                self.assertGreaterEqual(item.start_dt, window_start)
                self.assertLessEqual(item.end_dt, window_end)
                for hard_event in blocked:
                    self.assertFalse(
                        overlaps(item, hard_event),
                        f"休息点 {item.start} 压到了硬日程 {hard_event.id}",
                    )
        # 休息点之间也不能互相重叠
        for earlier, later in zip(selected, selected[1:]):
            self.assertLessEqual(earlier.end_dt, later.start_dt)

    def test_形状相近的空档不会都排成同一种休息点(self):
        # 这是源码里刻意实现的去重优先级：四个空档形状完全一样，
        # 也应该排出 walk / stretch / breathe / water 四种，而不是四个「出去走一圈」。
        plan = build(ALTERNATING, day_end="20:00")
        selected = breaks(plan)
        types = [item.break_type for item in selected]
        self.assertEqual(len(selected), 4)
        self.assertGreater(
            len(set(types)), 1, f"多个相近空档排出了同一种休息点：{types}"
        )
        self.assertEqual(types, ["walk", "stretch", "breathe", "water"])
        # 理由里带上连续学习时长与空档大小，四个休息点措辞各不相同
        self.assertEqual(len({item.reason for item in selected}), 4)
        for item in selected:
            self.assertIn("120", item.reason)

    def test_长学习块之后安排walk(self):
        plan = build(LONG_BLOCK, day_end="18:00")
        selected = breaks(plan)
        self.assertEqual(len(selected), 1)
        item = selected[0]
        self.assertEqual(item.break_type, "walk")
        self.assertEqual(item.start[11:16], "12:00")
        self.assertEqual(item.end[11:16], "12:15")
        self.assertEqual(item.duration_minutes, 15)
        self.assertIn("180", item.reason)
        self.assertEqual(item.label, BREAK_TYPES["walk"]["label"])

    def test_窗口很小时改用eyes而不是walk(self):
        # 6 分钟空档：walk 要 20 分钟、stretch 要 10 分钟，都装不下 → eyes
        plan = build(TINY_GAP, day_end="11:00")
        selected = breaks(plan)
        self.assertEqual(len(selected), 1)
        item = selected[0]
        self.assertEqual(item.break_type, "eyes")
        self.assertEqual(item.start[11:16], "10:00")
        self.assertEqual(item.duration_minutes, 2)
        self.assertIn("空档只有 6 分钟", item.reason)

    def test_按连续学习时长与空档大小选型(self):
        # 连续学习 25 分钟：够不上 breathe（30 分钟线），只能补水 → water
        water = build(
            [event("s1", "09:00", "09:25", category="study")], day_start="08:00", day_end="10:00"
        )
        selected = breaks(water)
        self.assertEqual([item.break_type for item in selected], ["water"])
        self.assertEqual(selected[0].start[11:16], "09:25")
        self.assertIn("25", selected[0].reason)

        # 连续学习 35 分钟 + 25 分钟空档：够 breathe，但空档不到 water 的 30 分钟线
        breathe = build(
            [event("s1", "09:00", "09:35", category="study")], day_start="08:00", day_end="10:00"
        )
        selected = breaks(breathe)
        self.assertEqual([item.break_type for item in selected], ["breathe"])
        self.assertEqual(selected[0].start[11:16], "09:35")

    def test_四分钟空档不插休息点(self):
        # MIN_WINDOW_MINUTES = 5：小于 5 分钟的缝一律跳过（这条压过 eyes 的 3 分钟下限）
        plan = build(FOUR_MINUTE_GAP, day_end="11:00")
        self.assertEqual(breaks(plan), [])
        self.assertEqual(plan.stats["break_count"], 0)
        self.assertEqual(plan.stats["free_minutes"], 4)

    def test_可打断长块内部按第90分钟安排walk(self):
        plan = build(INTERRUPTIBLE, day_end="12:00")
        selected = breaks(plan)
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0].break_type, "walk")
        self.assertEqual(selected[0].start[11:16], "10:30")
        self.assertIn("90", selected[0].reason)

    def test_可打断一小时块内安排stretch而不足门槛时不插(self):
        plan = build(
            [event("k", "09:00", "10:00", category="study", priority="soft", interruptible=True)],
            day_end="12:00",
        )
        selected = breaks(plan)
        self.assertEqual([item.break_type for item in selected], ["stretch"])
        self.assertEqual(selected[0].start[11:16], "09:50")
        # 40 分钟的可打断块连 stretch 门槛（50 + 5 + 5）都不够
        short = build(
            [event("k", "09:00", "09:40", category="study", priority="soft", interruptible=True)],
            day_end="12:00",
        )
        self.assertEqual(breaks(short), [])

    def test_软日程内部可以插休息点且同刻休息点排在日程前(self):
        plan = build(SOFT_BLOCK, day_end="12:00")
        self.assertEqual([item.kind for item in plan.timeline], ["event", "break", "event"])
        self.assertEqual(plan.timeline[1].break_type, "walk")
        self.assertEqual(plan.timeline[1].start[11:16], "10:00")
        # 同一时刻 break 排在 event 前面（时间轴排序规则）
        self.assertEqual(plan.timeline[1].start_dt, plan.timeline[2].start_dt)
        self.assertEqual(plan.timeline[2].id, "o")


class DayPlanStatsTests(unittest.TestCase):
    """stats 与时间轴条目必须对得上。"""

    def test_统计计数与条目一致(self):
        plan = build(ALTERNATING, day_end="20:00")
        selected = breaks(plan)
        items = event_items(plan)
        self.assertEqual(plan.stats["break_count"], len(selected))
        self.assertEqual(
            plan.stats["break_minutes"], sum(item.duration_minutes for item in selected)
        )
        self.assertEqual(plan.stats["event_count"], len(items))
        self.assertEqual(
            plan.stats["focus_minutes"], sum(item.duration_minutes for item in items)
        )
        self.assertEqual(
            plan.stats["protected_minutes"],
            sum(
                item.duration_minutes
                for item in items
                if item.priority == "hard" and not item.interruptible
            ),
        )
        self.assertEqual(
            plan.stats,
            {
                "focus_minutes": 480,
                "protected_minutes": 480,
                "free_minutes": 240,
                "break_count": 4,
                "break_minutes": 25,
                "event_count": 4,
                "first_event": "08:00",
                "last_event": "17:00",
                "soundscape": "desk-hours",
            },
        )

    def test_每个条目的时长与起止时间一致且时间轴有序(self):
        # 注意：跨界出窗口的日程保留原始起止时间、duration_minutes 按窗口裁剪，
        # 所以「时长 == 起止之差」只对窗口内的场景成立（跨界的例外见
        # test_跨界日程保留原始起止但统计只算窗口内）。
        for plan in (build(ALTERNATING, day_end="20:00"), build(SOFT_BLOCK, day_end="12:00")):
            for item in plan.timeline:
                with self.subTest(item=item.id):
                    span = int((item.end_dt - item.start_dt).total_seconds() // 60)
                    self.assertEqual(item.duration_minutes, span)
                    self.assertGreater(item.duration_minutes, 0)
        starts = [item.start_dt for item in build(ALTERNATING, day_end="20:00").timeline]
        self.assertEqual(starts, sorted(starts))
        self.assertEqual(build(ALTERNATING, day_end="20:00").date, DAY)

    def test_休息点字段完整(self):
        plan = build(ALTERNATING, day_end="20:00")
        for item in breaks(plan):
            with self.subTest(item=item.start):
                spec = BREAK_TYPES[item.break_type]
                self.assertEqual(item.title, spec["label"])
                self.assertEqual(item.label, spec["label"])
                self.assertEqual(item.intensity, spec["intensity"])
                self.assertEqual(item.reminder_minutes_before, spec["reminder_minutes_before"])
                self.assertLessEqual(item.duration_minutes, spec["duration_minutes"])
                self.assertTrue(item.reason)
                self.assertIn(item.soundscape, SOUNDSCAPES)
                self.assertEqual(item.status, "planned")
                self.assertEqual(item.to_dict()["kind"], "break")

    def test_空日程不插休息点且统计归零(self):
        plan = build([])
        self.assertEqual(plan.timeline, ())
        self.assertEqual(plan.stats["event_count"], 0)
        self.assertEqual(plan.stats["break_count"], 0)
        self.assertEqual(plan.stats["break_minutes"], 0)
        self.assertEqual(plan.stats["focus_minutes"], 0)
        self.assertEqual(plan.stats["protected_minutes"], 0)
        self.assertEqual(plan.stats["free_minutes"], 840)
        self.assertIsNone(plan.stats["first_event"])
        self.assertIsNone(plan.stats["last_event"])
        self.assertEqual(plan.conflicts, [])
        self.assertTrue(plan.care.greeting)

    def test_单件日程(self):
        plan = build([event("one", "09:00", "10:30", category="study")])
        self.assertEqual(plan.stats["event_count"], 1)
        self.assertEqual(plan.stats["focus_minutes"], 90)
        self.assertEqual(plan.stats["first_event"], "09:00")
        self.assertEqual(plan.stats["last_event"], "09:00")
        selected = breaks(plan)
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0].break_type, "walk")
        self.assertEqual(selected[0].start[11:16], "10:30")


class DayPlanEdgeCaseTests(unittest.TestCase):
    """重叠、贴边、裁剪、跨零点。"""

    def test_重叠日程进入conflicts且都保留在时间轴(self):
        plan = build(OVERLAP)
        self.assertEqual(plan.conflicts, [["x", "y"]])
        self.assertEqual([item.id for item in event_items(plan)], ["x", "y"])
        # 重叠部分只算一次
        self.assertEqual(plan.stats["protected_minutes"], 180)
        self.assertEqual(plan.stats["free_minutes"], 660)
        self.assertEqual(plan.stats["break_count"], 0)

    def test_贴边日程合并成一段连续学习(self):
        plan = build(TOUCHING)
        self.assertEqual(plan.conflicts, [])
        self.assertEqual(plan.stats["focus_minutes"], 100)
        self.assertEqual(plan.stats["protected_minutes"], 100)
        selected = breaks(plan)
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0].start[11:16], "10:40")
        self.assertIn("100", selected[0].reason)

    def test_跨界日程保留原始起止但统计只算窗口内(self):
        plan = build(CLIPPED, day_start="08:00", day_end="12:00")
        item = event_items(plan)[0]
        self.assertEqual(item.start, f"{DAY}T06:00:00+08:00")
        self.assertEqual(item.end, f"{DAY}T11:00:00+08:00")
        self.assertEqual(item.duration_minutes, 180)  # 06:00-11:00 被裁成 08:00-11:00
        self.assertEqual(plan.stats["focus_minutes"], 180)
        self.assertEqual(plan.stats["protected_minutes"], 180)
        self.assertEqual(plan.stats["free_minutes"], 60)
        # 唯一空档在 11:00-12:00，连续学习 180 分钟 → walk
        self.assertEqual([item.break_type for item in breaks(plan)], ["walk"])

    def test_跨零点窗口(self):
        plan = build_day(
            events=[
                CalendarEvent(
                    "n1", "晚自习", "2026-10-05T20:30:00+08:00", "2026-10-05T22:30:00+08:00",
                    category="study",
                )
            ],
            day_start="2026-10-05T20:00:00+08:00",
            day_end="2026-10-06T02:00:00+08:00",
            now="2026-10-05T23:40:00+08:00",
        )
        self.assertEqual(plan.date, "2026-10-05")
        window_end = datetime.fromisoformat("2026-10-06T02:00:00+08:00")
        selected = breaks(plan)
        self.assertTrue(selected)
        for item in selected:
            self.assertLessEqual(item.end_dt, window_end)
        self.assertEqual(plan.care.tone, "quiet")  # 深夜语气

    def test_非法时间与窗口抛ValueError(self):
        with self.assertRaises(ValueError) as ctx:
            build_day(events=[], day_start=f"{DAY}T08:00:00", day_end=DAY_END)
        self.assertIn("时区", str(ctx.exception))

        with self.assertRaises(ValueError) as ctx:
            build_day(events=[], day_start=DAY_START, day_end=DAY_END, now=f"{DAY}T09:00:00")
        self.assertIn("时区", str(ctx.exception))

        with self.assertRaises(ValueError) as ctx:
            build_day(events=[], day_start="", day_end=DAY_END)
        self.assertIn("不能为空", str(ctx.exception))

        for start, end in ((DAY_END, DAY_START), (DAY_START, DAY_START)):
            with self.subTest(start=start, end=end):
                with self.assertRaises(ValueError):
                    build_day(events=[], day_start=start, day_end=end)

    def test_时间轴条目自身校验(self):
        for kwargs in (
            dict(kind="meeting", id="x", start=f"{DAY}T09:00:00+08:00", end=f"{DAY}T10:00:00+08:00"),
            dict(kind="break", id="", start=f"{DAY}T09:00:00+08:00", end=f"{DAY}T10:00:00+08:00"),
            dict(kind="break", id="x", start=f"{DAY}T10:00:00+08:00", end=f"{DAY}T09:00:00+08:00"),
            dict(kind="break", id="x", start=f"{DAY}T10:00:00+08:00", end=f"{DAY}T10:00:00+08:00"),
            dict(kind="break", id="x", start=f"{DAY}T10:00:00", end=f"{DAY}T11:00:00"),
        ):
            with self.subTest(**kwargs):
                with self.assertRaises(ValueError):
                    TimelineItem(**kwargs)


class DayPlanSoundscapeTests(unittest.TestCase):
    """音景与 BPM 的接入。"""

    def test_休息点bpm落在音景区间内且压力高时取下沿(self):
        plan = build(LONG_BLOCK, day_end="18:00", state=STRESSED_STATE)
        selected = breaks(plan)
        self.assertTrue(selected)
        for item in selected:
            with self.subTest(item=item.start):
                scape = SOUNDSCAPES[item.soundscape]
                self.assertGreaterEqual(item.bpm, scape.bpm_range[0])
                self.assertLessEqual(item.bpm, scape.bpm_range[1])
                # 压力 80 + 精力 28（低）→ 一律取区间下沿，慢一点
                self.assertEqual(item.bpm, scape.bpm_range[0])

    def test_久坐时长写进休息理由(self):
        state = StatusSnapshot(
            "2026-10-03T21:00:00+08:00",
            energy=50,
            stress=20,
            focus=50,
            sleep_hours=7.5,
            sedentary_minutes=80,
        )
        plan = build(LONG_BLOCK, day_end="18:00", state=state)
        selected = breaks(plan)
        self.assertTrue(selected)
        for item in selected:
            self.assertIn("已连续久坐 80 分钟", item.reason)

    def test_音景提示应用到全部休息点(self):
        plan = build(LONG_BLOCK, day_end="18:00", soundscape_hint="high-noon")
        selected = breaks(plan)
        self.assertTrue(selected)
        low, high = SOUNDSCAPES["high-noon"].bpm_range
        for item in selected:
            with self.subTest(item=item.start):
                self.assertEqual(item.soundscape, "high-noon")
                self.assertGreaterEqual(item.bpm, low)
                self.assertLessEqual(item.bpm, high)
        self.assertEqual(plan.stats["soundscape"], "high-noon")
        self.assertEqual(day_soundscape_hint(plan), "high-noon")

    def test_主导音景与当天提示(self):
        plan = build(ALTERNATING, day_end="20:00")
        # desk-hours 出现两次，是当天的主导音景
        self.assertEqual(plan.stats["soundscape"], "desk-hours")
        self.assertEqual(day_soundscape_hint(plan), "desk-hours")
        self.assertIn(plan.stats["soundscape"], {item.soundscape for item in breaks(plan)})
        # 没有音景信息时回落到默认 id
        without_scapes = replace(plan, stats={"soundscape": ""})
        self.assertEqual(day_soundscape_hint(without_scapes), DEFAULT_SOUNDSCAPE)


class DayPlanHydrateTests(unittest.TestCase):
    def test_合并状态且不修改原计划(self):
        plan = build(ALTERNATING, day_end="20:00")
        statuses = {item.id: "done" for item in breaks(plan)}
        hydrated = hydrate_timeline(plan, statuses)
        self.assertEqual(len(hydrated), len(plan.timeline))
        for original, item in zip(plan.timeline, hydrated):
            if item.kind == "break":
                self.assertEqual(item.status, "done")
            else:
                self.assertEqual(item.status, original.status)
        # 原计划是 frozen dataclass，不能被就地改写
        self.assertTrue(all(item.status == "planned" for item in plan.timeline))

    def test_日程状态不会被状态表顶掉且未登记时保持planned(self):
        plan = build(ALTERNATING, day_end="20:00")
        hijack = {plan.timeline[0].id: "done"}
        hydrated = hydrate_timeline(plan, hijack)
        self.assertEqual(hydrated[0].status, "planned")
        self.assertEqual(hydrate_timeline(plan, {})[1].status, "planned")


class DayPlanInvariantTests(unittest.TestCase):
    """把所有场景扫一遍，确认共同不变量都成立。"""

    def test_所有场景都满足统一不变量(self):
        scenarios = (
            ("alternating", ALTERNATING, dict(day_end="20:00")),
            ("long_block", LONG_BLOCK, dict(day_end="18:00")),
            ("tiny_gap", TINY_GAP, dict(day_end="11:00")),
            ("four_minute_gap", FOUR_MINUTE_GAP, dict(day_end="11:00")),
            ("soft_block", SOFT_BLOCK, dict(day_end="12:00")),
            ("interruptible", INTERRUPTIBLE, dict(day_end="12:00")),
            ("clipped", CLIPPED, dict(day_start="08:00", day_end="12:00")),
            ("overlap", OVERLAP, {}),
            ("touching", TOUCHING, {}),
            ("empty", [], {}),
            ("stressed", LONG_BLOCK, dict(day_end="18:00", state=STRESSED_STATE)),
            ("hinted", ALTERNATING, dict(day_end="20:00", soundscape_hint="breath")),
        )
        for label, events, kwargs in scenarios:
            with self.subTest(scenario=label):
                plan = build(events, **kwargs)
                start_bound = datetime.fromisoformat(
                    f"{DAY}T{kwargs.get('day_start', '08:00')}:00+08:00"
                )
                end_bound = datetime.fromisoformat(
                    f"{DAY}T{kwargs.get('day_end', '22:00')}:00+08:00"
                )
                hard = [
                    item for item in events if item.priority == "hard" and not item.interruptible
                ]
                selected = breaks(plan)

                starts = [item.start_dt for item in plan.timeline]
                self.assertEqual(starts, sorted(starts), "时间轴必须按开始时间排序")
                self.assertEqual(plan.stats["break_count"], len(selected))
                self.assertEqual(
                    plan.stats["break_minutes"],
                    sum(item.duration_minutes for item in selected),
                )
                self.assertTrue(plan.care.greeting)
                for item in selected:
                    self.assertGreaterEqual(item.start_dt, start_bound)
                    self.assertLessEqual(item.end_dt, end_bound)
                    self.assertGreater(item.duration_minutes, 0)
                    self.assertIn(item.break_type, BREAK_TYPES)
                    self.assertIn(item.soundscape, SOUNDSCAPES)
                    scape = SOUNDSCAPES[item.soundscape]
                    self.assertGreaterEqual(item.bpm, scape.bpm_range[0])
                    self.assertLessEqual(item.bpm, scape.bpm_range[1])
                    for hard_event in hard:
                        self.assertFalse(
                            overlaps(item, hard_event),
                            f"{label}: 休息点 {item.start} 压到了硬日程 {hard_event.id}",
                        )
                for earlier, later in zip(selected, selected[1:]):
                    self.assertLessEqual(
                        earlier.end_dt, later.start_dt, f"{label}: 休息点互相重叠"
                    )


if __name__ == "__main__":
    unittest.main()
