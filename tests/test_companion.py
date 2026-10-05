# -*- coding: utf-8 -*-
"""companion 模块的单元测试：关怀语模板渲染、分支优先级、时长措辞、AI 润色兜底。

被测文件：music_companion/companion.py
覆盖重点：
* ``compose_care`` 渲染结果不残留 ``{...}`` 占位符、长度不超限、无禁用词（遍历各分支）；
* 措辞确定性：同一天同一状态稳定，且跨进程稳定（不依赖内置 ``hash()``）；
* 状态分支：睡眠不足 / 压力高 / 精力低 / 深夜 / 空日程 / 挨着 / 空档多；
* ``_minutes_phrase`` 及用它的 ``free_phrase`` / ``longest_gap_phrase``（本文件直接测这两个
  私有函数，因为它们承载的是用户可见文案，且没有其它公开入口能稳定触发它们）；
* ``compose_care_with_ai`` 的任何失败都必须原样返回本地结果。
"""

import os
import re
import subprocess
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from music_companion.calendar_model import CalendarEvent
from music_companion.companion import (
    DETAIL_MAX,
    FORBIDDEN_PHRASES,
    GREETING_MAX,
    SUGGESTION_MAX,
    TONES,
    CareCard,
    _build_facts,
    _hours_phrase,
    _minutes_phrase,
    _normalize_phase,
    _select_branch,
    compose_care,
    compose_care_with_ai,
)
from music_companion.state import StatusSnapshot

ROOT = Path(__file__).resolve().parents[1]
DAY = "2026-10-04"
PLACEHOLDER = re.compile(r"\{[a-z_][a-z0-9_]*\}")


def event(eid, start, end, *, title="安排", **kw):
    """用 2026-10-04 当天的 HH:MM 构造日程。"""
    return CalendarEvent(eid, title, f"{DAY}T{start}:00+08:00", f"{DAY}T{end}:00+08:00", **kw)


def snapshot(*, energy=50.0, stress=20.0, sleep_hours=7.5, sedentary_minutes=0.0):
    """默认是一份「不好也不坏」的状态（不会触发任何状态分支）。"""
    return StatusSnapshot(
        "2026-10-03T21:00:00+08:00",
        energy=energy,
        stress=stress,
        focus=50.0,
        sleep_hours=sleep_hours,
        heart_rate=60.0,
        steps=1000.0,
        sedentary_minutes=sedentary_minutes,
    )


ONE_EVENT = [event("e1", "09:00", "10:30", category="study")]
TIGHT_EVENTS = [
    event("e1", "09:00", "10:00"),
    event("e2", "10:05", "11:00"),
    event("e3", "11:05", "12:00"),
]
LOOSE_EVENTS = [event("e1", "09:00", "10:00"), event("e2", "14:30", "15:30")]

# 覆盖全部分支的输入：(用例名, compose_care 关键字参数, 期望 tone)
BRANCH_CASES = (
    ("deep_night", dict(now=f"{DAY}T23:30:00+08:00", events=ONE_EVENT), "quiet"),
    ("deep_night_no_events", dict(now=f"{DAY}T04:00:00+08:00", events=[]), "quiet"),
    ("no_events", dict(now=f"{DAY}T13:00:00+08:00", events=[]), "bright"),
    ("no_events_bad_state", dict(now=f"{DAY}T13:00:00+08:00", events=[], state=snapshot(sleep_hours=5)), "steady"),
    ("sleep_debt", dict(now=f"{DAY}T13:00:00+08:00", events=ONE_EVENT, state=snapshot(sleep_hours=5)), "gentle"),
    ("high_stress", dict(now=f"{DAY}T13:00:00+08:00", events=ONE_EVENT, state=snapshot(stress=80)), "quiet"),
    ("low_energy", dict(now=f"{DAY}T13:00:00+08:00", events=ONE_EVENT, state=snapshot(energy=20)), "gentle"),
    ("first_soon", dict(now=f"{DAY}T08:30:00+08:00", events=ONE_EVENT), "steady"),
    ("morning", dict(now=f"{DAY}T07:00:00+08:00", events=ONE_EVENT), "steady"),
    ("afternoon", dict(now=f"{DAY}T15:00:00+08:00", events=ONE_EVENT), "steady"),
    ("evening", dict(now=f"{DAY}T19:00:00+08:00", events=ONE_EVENT), "steady"),
    ("tight", dict(now=f"{DAY}T08:00:00+08:00", events=TIGHT_EVENTS, day_phase="早晨"), "steady"),
    ("loose", dict(now=f"{DAY}T13:00:00+08:00", events=LOOSE_EVENTS, stats={"free_minutes": 539, "break_count": 3}), "bright"),
    (
        "all_facts",
        dict(
            now=f"{DAY}T07:00:00+08:00",
            events=ONE_EVENT,
            state=snapshot(sedentary_minutes=90),
            profile={"wake": "06:30", "sleep": "23:00", "chronotype": "night_owl"},
            stats={"free_minutes": 300, "break_count": 2},
        ),
        "steady",
    ),
)


class ComposeCareTests(unittest.TestCase):
    """compose_care 的渲染与分支行为。"""

    def test_各种输入下不残留占位符且长度语气合规(self):
        self.assertTrue(BRANCH_CASES, "分支用例表不能为空")
        for label, kwargs, expected_tone in BRANCH_CASES:
            with self.subTest(case=label):
                card = compose_care(**kwargs)
                for field, limit in (
                    ("greeting", GREETING_MAX),
                    ("detail", DETAIL_MAX),
                    ("suggestion", SUGGESTION_MAX),
                ):
                    text = getattr(card, field)
                    self.assertTrue(text, f"{field} 不能为空")
                    self.assertIsNone(
                        PLACEHOLDER.search(text), f"{field} 残留占位符：{text}"
                    )
                    self.assertNotIn("{", text, f"{field} 残留花括号：{text}")
                    self.assertNotIn("}", text, f"{field} 残留花括号：{text}")
                    self.assertLessEqual(len(text), limit, f"{field} 超长：{text}")
                    for phrase in FORBIDDEN_PHRASES:
                        self.assertNotIn(phrase, text, f"{field} 出现禁用词：{text}")
                self.assertIn(card.tone, TONES)
                self.assertEqual(card.tone, expected_tone)
                self.assertEqual(card.source, "local")
                self.assertEqual(
                    set(card.to_dict()),
                    {"greeting", "detail", "suggestion", "tone", "source"},
                )

    def test_同一输入措辞稳定(self):
        kwargs = dict(now=f"{DAY}T13:00:00+08:00", events=ONE_EVENT, state=snapshot(energy=20))
        self.assertEqual(compose_care(**kwargs), compose_care(**kwargs))

    def test_措辞跨进程稳定不依赖PYTHONHASHSEED(self):
        # 模板下标用 SHA-1 取模而非内置 hash()：换 PYTHONHASHSEED 也必须得到同一句话
        kwargs = dict(now=f"{DAY}T13:00:00+08:00", events=[])
        expected = compose_care(**kwargs).greeting
        code = (
            "import sys; sys.path.insert(0, {root!r});"
            "from music_companion.companion import compose_care;"
            "print(compose_care(now='{now}', events=[]).greeting)"
        ).format(root=str(ROOT), now=f"{DAY}T13:00:00+08:00")
        for seed in ("1", "2"):
            with self.subTest(seed=seed):
                env = dict(os.environ, PYTHONHASHSEED=seed, PYTHONIOENCODING="utf-8")
                result = subprocess.run(
                    [sys.executable, "-c", code],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    env=env,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), expected)

    def test_深夜里文案提到夜色或此刻时间(self):
        card = compose_care(now=f"{DAY}T23:30:00+08:00", events=ONE_EVENT)
        self.assertEqual(card.tone, "quiet")
        self.assertTrue(
            "23:30" in card.greeting or "夜" in card.greeting or "这个点" in card.greeting,
            card.greeting,
        )

    def test_睡眠不足走gentle分支且文案提到睡眠(self):
        card = compose_care(
            now=f"{DAY}T13:00:00+08:00", events=ONE_EVENT, state=snapshot(sleep_hours=5)
        )
        self.assertEqual(card.tone, "gentle")
        self.assertIn("睡", card.greeting)

    def test_压力高走quiet分支且文案提到压力(self):
        card = compose_care(
            now=f"{DAY}T13:00:00+08:00", events=ONE_EVENT, state=snapshot(stress=80)
        )
        self.assertEqual(card.tone, "quiet")
        self.assertIn("压力", card.greeting)

    def test_精力低走gentle分支且文案提到精力(self):
        card = compose_care(
            now=f"{DAY}T13:00:00+08:00", events=ONE_EVENT, state=snapshot(energy=20)
        )
        self.assertEqual(card.tone, "gentle")
        self.assertTrue(
            "精力" in card.greeting or "电量" in card.greeting, card.greeting
        )

    def test_睡眠不足优先于高压力(self):
        # 分支顺序：睡眠不足 > 高压力；两者同时命中时应该走 gentle 的睡眠措辞
        card = compose_care(
            now=f"{DAY}T13:00:00+08:00",
            events=ONE_EVENT,
            state=snapshot(sleep_hours=5, stress=80, energy=20),
        )
        self.assertEqual(card.tone, "gentle")
        self.assertIn("睡", card.greeting)
        self.assertNotIn("压力", card.greeting)

    def test_日程挨着时文案提到连着(self):
        card = compose_care(now=f"{DAY}T08:00:00+08:00", events=TIGHT_EVENTS, day_phase="早晨")
        self.assertTrue(
            "连" in card.greeting or "挨着" in card.greeting, card.greeting
        )

    def test_空档充足时语气轻快(self):
        card = compose_care(
            now=f"{DAY}T13:00:00+08:00",
            events=LOOSE_EVENTS,
            stats={"free_minutes": 539, "break_count": 3},
        )
        self.assertEqual(card.tone, "bright")
        # 539 分钟必须读成小时，不能出现「539 分钟」这种不像人话的措辞
        self.assertNotIn("539 分钟", card.greeting + card.detail + card.suggestion)

    def test_输入日程乱序不影响结果(self):
        forward = compose_care(now=f"{DAY}T08:00:00+08:00", events=TIGHT_EVENTS, day_phase="早晨")
        backward = compose_care(
            now=f"{DAY}T08:00:00+08:00", events=list(reversed(TIGHT_EVENTS)), day_phase="早晨"
        )
        self.assertEqual(forward, backward)

    def test_空日程无状态无profile也能生成关怀卡(self):
        card = compose_care(now=f"{DAY}T12:00:00+08:00", events=[], state=None, profile=None)
        self.assertTrue(card.greeting)
        self.assertTrue(card.detail)
        self.assertTrue(card.suggestion)
        self.assertEqual(card.source, "local")
        # 空日程 + 好状态 → 轻快；与 BRANCH_CASES 里的 no_events 一致
        self.assertEqual(card.tone, "bright")
        self.assertEqual(compose_care(now=f"{DAY}T12:00:00+08:00", events=None).tone, "bright")

    def test_时间段可以显式指定(self):
        # 同一时刻（13:00）显式指定夜间 → 走 deep_night 的 quiet 语气
        self.assertEqual(
            compose_care(now=f"{DAY}T13:00:00+08:00", events=[], day_phase="night").tone, "quiet"
        )
        self.assertEqual(
            compose_care(now=f"{DAY}T13:00:00+08:00", events=[], day_phase="夜间").tone, "quiet"
        )

    def test_上午不能被归成午后(self):
        # 「上午」含「午」，早先的单字规则会先命中，把 9–12 点归一成午后——
        # 正好是相反的一半。归到「早晨」是对的：两者同属音景目录的 morning 段。
        self.assertEqual(_normalize_phase("上午"), "早晨")
        self.assertEqual(_normalize_phase("下午"), "午后")
        self.assertEqual(_normalize_phase("中午"), "午后")
        self.assertEqual(_normalize_phase("早餐后"), "早晨")

    def test_时间段词表(self):
        cases = (
            ("早晨", "早晨"),
            ("上午", "早晨"),
            ("午后", "午后"),
            ("晚间", "晚间"),
            ("夜间", "夜间"),
            ("night", "夜间"),
            ("evening", "晚间"),
            ("morning", "早晨"),
            ("afternoon", "午后"),
            ("", ""),
            (None, ""),
            ("随便什么", ""),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(_normalize_phase(value), expected)

    def test_now必须是带时区的合法时间(self):
        for bad in ("", "2026-10-04T13:00:00", "not-a-time", None, 12345):
            with self.subTest(now=bad):
                with self.assertRaises(ValueError):
                    compose_care(now=bad, events=[])


class DurationPhraseTests(unittest.TestCase):
    """时长措辞：满 60 分钟改用小时，不足仍用分钟。"""

    def _facts(self, *, events=None, stats=None, phase="午后", state=None, profile=None, now=None):
        return _build_facts(
            now_dt=datetime.fromisoformat(now or f"{DAY}T13:00:00+08:00"),
            event_list=list(events or []),
            state=state,
            profile=profile,
            stats=stats,
            phase=phase,
        )

    def _two_events_with_gap(self, gap_minutes):
        """第一件 09:00-10:00，第二件与它相隔 gap_minutes 分钟。"""
        start = datetime.fromisoformat(f"{DAY}T10:00:00+08:00") + timedelta(minutes=gap_minutes)
        end = start + timedelta(minutes=30)
        return [
            event("e1", "09:00", "10:00"),
            CalendarEvent("e2", "第二件", start.isoformat(), end.isoformat()),
        ]

    def test_满六十分钟改用小时(self):
        cases = ((539, "9 小时"), (150, "2.5 小时"), (45, "45 分钟"), (59, "59 分钟"), (60, "1 小时"))
        for minutes, expected in cases:
            with self.subTest(minutes=minutes):
                self.assertEqual(_minutes_phrase(minutes), expected)

    def test_小时短语的边界(self):
        self.assertEqual(_hours_phrase(59), "不到 1 小时")
        self.assertEqual(_hours_phrase(60), "1 小时")
        self.assertEqual(_hours_phrase(90), "1.5 小时")
        self.assertEqual(_hours_phrase(150), "2.5 小时")
        self.assertEqual(_hours_phrase(600), "10 小时")

    def test_自由时间短语用分钟短语(self):
        # stats.free_minutes 来自 day_plan，539 分钟 → 「9 小时」
        facts = self._facts(stats={"free_minutes": 539, "break_count": 3})
        self.assertEqual(facts["free_minutes"], "539")
        self.assertEqual(facts["free_phrase"], "9 小时")
        self.assertEqual(facts["break_phrase"], "排了 3 个休息点")

        # 没有休息点时是「还没有休息点」
        self.assertEqual(self._facts(stats={"free_minutes": 90, "break_count": 0})["break_phrase"], "还没有休息点")

    def test_最长空档短语带分钟短语(self):
        cases = ((150, "2.5 小时"), (45, "45 分钟"), (20, "20 分钟"), (19, ""))
        for gap, expected in cases:
            with self.subTest(gap=gap):
                facts = self._facts(events=self._two_events_with_gap(gap))
                self.assertEqual(facts["longest_gap_phrase"], expected)
                self.assertEqual(facts["longest_gap_minutes"], str(gap) if gap >= 20 else "")

    def test_没有空档时不写空档事实(self):
        facts = self._facts(events=ONE_EVENT)
        self.assertEqual(facts["free_phrase"], "")
        self.assertEqual(facts["free_minutes"], "")
        self.assertEqual(facts["longest_gap_phrase"], "")

    def test_状态数值进入事实(self):
        facts = self._facts(
            events=ONE_EVENT, state=snapshot(energy=20, stress=80, sleep_hours=5)
        )
        self.assertEqual(facts["sleep_hours"], "5")
        self.assertEqual(facts["stress"], "80")
        self.assertEqual(facts["energy"], "20")


class BranchSelectionTests(unittest.TestCase):
    """分支优先级：夜间 > 空日程 > 睡眠不足 > 高压力 > 低精力 > 马上开始 > 挨着 > 空档 > 时段。"""

    def _facts(self, *, events=None, stats=None, phase="午后", state=None, now=None):
        return _build_facts(
            now_dt=datetime.fromisoformat(now or f"{DAY}T13:00:00+08:00"),
            event_list=list(events or []),
            state=state,
            profile=None,
            stats=stats,
            phase=phase,
        )

    def test_分支优先级(self):
        self.assertEqual(_select_branch(self._facts(events=[], phase="夜间"), 0), "deep_night")
        self.assertEqual(
            _select_branch(self._facts(events=[], phase="午后", state=snapshot(sleep_hours=5)), 0),
            "no_events",
        )
        self.assertEqual(
            _select_branch(self._facts(events=ONE_EVENT, state=snapshot(sleep_hours=5)), 1),
            "sleep_debt",
        )
        self.assertEqual(
            _select_branch(
                self._facts(events=ONE_EVENT, state=snapshot(stress=80, sleep_hours=7.5)), 1
            ),
            "high_stress",
        )
        self.assertEqual(
            _select_branch(
                self._facts(events=ONE_EVENT, state=snapshot(energy=20, stress=20)), 1
            ),
            "low_energy",
        )
        self.assertEqual(
            _select_branch(
                self._facts(events=ONE_EVENT, now=f"{DAY}T08:30:00+08:00", phase="早晨"), 1
            ),
            "first_soon",
        )
        self.assertEqual(
            _select_branch(
                self._facts(events=TIGHT_EVENTS, now=f"{DAY}T08:00:00+08:00", phase="早晨"), 3
            ),
            "tight",
        )
        self.assertEqual(
            _select_branch(
                self._facts(events=LOOSE_EVENTS, stats={"free_minutes": 539}), 2
            ),
            "loose",
        )
        self.assertEqual(_select_branch(self._facts(events=ONE_EVENT), 1), "afternoon")

    def test_日程时间事实按日历顺序计算(self):
        facts = self._facts(events=list(reversed(LOOSE_EVENTS)))
        self.assertEqual(facts["event_count"], "2")
        self.assertEqual(facts["first_start"], "09:00")
        self.assertEqual(facts["last_start"], "14:30")
        self.assertEqual(facts["last_end"], "15:30")
        self.assertEqual(facts["tight_count"], "")  # 两件相隔 270 分钟，不算挨着


class ProfileFactsTests(unittest.TestCase):
    """profile 只影响措辞，支持映射与属性对象两种形态。"""

    class _Profile:
        wake = "06:30"
        sleep = "23:00"
        chronotype = "night_owl"

    def _facts(self, profile):
        return _build_facts(
            now_dt=datetime.fromisoformat(f"{DAY}T07:00:00+08:00"),
            event_list=list(ONE_EVENT),
            state=None,
            profile=profile,
            stats=None,
            phase="早晨",
        )

    def test_profile支持映射与属性对象(self):
        for profile in (
            {"wake": "06:30", "sleep": "23:00", "chronotype": "night_owl"},
            self._Profile(),
        ):
            with self.subTest(profile=type(profile).__name__):
                facts = self._facts(profile)
                self.assertEqual(facts["wake_hm"], "06:30")
                self.assertEqual(facts["sleep_hm"], "23:00")
                self.assertEqual(facts["chronotype_phrase"], "你偏晚睡")

    def test_profile时间格式不合法时不写入事实(self):
        facts = self._facts({"wake": "6:30", "sleep": "25:00", "chronotype": "unknown"})
        self.assertEqual(facts["wake_hm"], "")
        self.assertEqual(facts["sleep_hm"], "")
        self.assertEqual(facts["chronotype_phrase"], "")


class _NotConfigured:
    def is_configured(self):
        return False

    def care(self, context, card):
        raise AssertionError("未配置时不应调用润色接口")


class _Exploding:
    def is_configured(self):
        return True

    def care(self, context, card):
        raise RuntimeError("模拟 AI 服务异常")


class _NoPolishMethod:
    def is_configured(self):
        return True


class _GoodClient:
    def is_configured(self):
        return True

    def care(self, context, card):
        return {
            "greeting": "今天慢一点",
            "detail": "AI 润色过的依据",
            "suggestion": "先喝水",
            "tone": "gentle",
        }


class _ForbiddenClient:
    def is_configured(self):
        return True

    def care(self, context, card):
        return {"greeting": "亲爱的用户", "detail": "依据", "suggestion": "建议", "tone": "gentle"}


class _LongClient:
    def is_configured(self):
        return True

    def care(self, context, card):
        return {
            "greeting": "这句话特别长" * 20,
            "detail": "依据也很长" * 40,
            "suggestion": "建议也很长" * 20,
            "tone": "gentle",
        }


class ComposeCareWithAiTests(unittest.TestCase):
    """AI 润色是旁路功能：任何失败都要退回本地结果，不能抛异常。"""

    def setUp(self):
        self.base = compose_care(now=f"{DAY}T13:00:00+08:00", events=ONE_EVENT)

    def test_无客户端或未配置时原样返回(self):
        self.assertIs(compose_care_with_ai(self.base, {}, None), self.base)
        self.assertIs(compose_care_with_ai(self.base, {}, _NotConfigured()), self.base)
        self.assertIs(compose_care_with_ai(self.base, {}, _NoPolishMethod()), self.base)

    def test_客户端抛异常时返回本地结果(self):
        self.assertIs(compose_care_with_ai(self.base, {}, _Exploding()), self.base)

    def test_结果含禁用词或空字段时丢弃(self):
        self.assertIs(compose_care_with_ai(self.base, {}, _ForbiddenClient()), self.base)
        self.assertIs(
            compose_care_with_ai(self.base, {}, type("Empty", (), {
                "is_configured": lambda self: True,
                "care": lambda self, context, card: {"greeting": "", "detail": "依据", "suggestion": "建议"},
            })()),
            self.base,
        )

    def test_合规结果被采纳并标记来源(self):
        polished = compose_care_with_ai(self.base, {"now": "13:00"}, _GoodClient())
        self.assertEqual(polished.source, "ai")
        self.assertEqual(polished.tone, "gentle")
        self.assertEqual(polished.greeting, "今天慢一点")
        # 本地结果不能被就地修改
        self.assertEqual(self.base.source, "local")

    def test_超长结果被压缩到上限内(self):
        polished = compose_care_with_ai(self.base, {}, _LongClient())
        self.assertEqual(polished.source, "ai")
        self.assertLessEqual(len(polished.greeting), GREETING_MAX)
        self.assertLessEqual(len(polished.detail), DETAIL_MAX)
        self.assertLessEqual(len(polished.suggestion), SUGGESTION_MAX)


class CareCardTests(unittest.TestCase):
    """关怀卡自身的字段约束。"""

    def test_字段校验(self):
        card = CareCard("主句", "依据", "建议", "steady")
        self.assertEqual(
            card.to_dict(),
            {"greeting": "主句", "detail": "依据", "suggestion": "建议", "tone": "steady", "source": "local"},
        )
        for kwargs in (
            dict(greeting="", detail="依据", suggestion="建议", tone="steady"),
            dict(greeting="主" * (GREETING_MAX + 1), detail="依据", suggestion="建议", tone="steady"),
            dict(greeting="主句", detail="", suggestion="建议", tone="steady"),
            dict(greeting="主句", detail="依据", suggestion="", tone="steady"),
            dict(greeting="主句", detail="依据", suggestion="建议", tone="unknown"),
            dict(greeting="主句", detail="依据", suggestion="建议", tone="steady", source="robot"),
        ):
            with self.subTest(**{k: (v if not isinstance(v, str) or len(v) < 12 else "...") for k, v in kwargs.items()}):
                with self.assertRaises(ValueError):
                    CareCard(**kwargs)


if __name__ == "__main__":
    unittest.main()
