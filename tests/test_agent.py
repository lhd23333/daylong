# -*- coding: utf-8 -*-
"""agent 模块的单元测试：一段自由文本 → 一天的日程 + 音乐。

被测文件：music_companion/agent.py
覆盖重点：
* 本地解析的确定性（同一输入两次结果完全一致），以及「心情句不是日程」这条底线；
* 时间写法的几个真实坑：结束时刻（写到十点）、区间（9 点到 11 点）、
  「一个半小时」不能被读成「半小时」、下午三点 = 15:00；
* 没写时间的条目接在上一件之后，一件都没有时从此刻之后的整半点起步；
* 事件对象能原样回传给 ``CalendarEvent``（前端就是这么用的）；
* 跑步才钉鼓点，累了不钉；情绪关键词能落到 state / music 上；
* AI 通道：合法结果覆盖本地、单个字段不合法只退那一个字段、请求失败留 fallback_reason。
"""

import unittest
from datetime import datetime

from music_companion.agent import MAX_TEXT_CHARS, plan_day_from_text, plan_locally
from music_companion.ai_client import AIRecommenderError
from music_companion.calendar_model import CalendarEvent
from music_companion.soundscape import SOUNDSCAPES

NOW = "2026-10-05T14:07:00+08:00"


def plan(text, **kw):
    return plan_locally(text, now=kw.pop("now", NOW), **kw)


def clocks(result):
    return [(item["start"][11:16], item["end"][11:16], item["title"]) for item in result["events"]]


class PlanLocallyTests(unittest.TestCase):
    def test_一句话排成三件事(self):
        result = plan("上午数学课，下午四点半去操场跑半小时，晚上写作业到十点。今天有点累。")
        self.assertEqual(
            clocks(result),
            [("09:00", "09:45", "数学课"), ("16:30", "17:00", "操场跑"), ("19:30", "22:00", "写作业")],
        )

    def test_心情与喜好不是日程(self):
        # 这三句都没有具体安排，把任何一句排成一条日程都是在编。
        result = plan("今天好累，压力有点大，想听点安静的钢琴")
        self.assertEqual(result["events"], [])
        self.assertEqual(result["state"]["energy"], 28)
        self.assertEqual(result["state"]["stress"], 72)
        self.assertEqual(result["preferences"]["likes"], ["安静的钢琴"])
        self.assertIn("听「呼吸」", result["reply"])

    def test_结束时刻不当作开始时刻(self):
        result = plan("晚上写作业到十点")
        self.assertEqual(clocks(result), [("19:30", "22:00", "写作业")])

    def test_区间写法(self):
        result = plan("9 点到 11 点数学")
        self.assertEqual(clocks(result), [("09:00", "11:00", "数学")])

    def test_跨中午的区间(self):
        result = plan("上午 11 点到 1 点吃饭")
        self.assertEqual(clocks(result), [("11:00", "13:00", "吃饭")])

    def test_一个半小时不是半小时(self):
        result = plan("下午两点跑步，一个半小时")
        self.assertEqual(clocks(result), [("14:00", "15:30", "跑步")])

    def test_点半与中文数字(self):
        result = plan("下午三点半英语课")
        self.assertEqual(clocks(result), [("15:30", "16:15", "英语课")])

    def test_没写时间就接在上一件之后(self):
        result = plan("9:00 数学课，10 点英语课，然后写作业")
        self.assertEqual(clocks(result)[-1], ("10:45", "11:30", "写作业"))

    def test_一件都没写时间时从现在之后起步(self):
        # 一件都没有却说了事情，就按「接下来干嘛」排在下一个整半点之后。
        result = plan("跑步、写作业")
        self.assertEqual([item["start"][11:16] for item in result["events"]], ["14:30", "15:10"])

    def test_整天全是自习排成一整块(self):
        result = plan("今天全是自习，有点累")
        self.assertEqual(clocks(result), [("14:30", "21:30", "自习")])
        self.assertTrue(result["events"][0]["interruptible"])

    def test_课不可打断自习可打断(self):
        # 上课被休息点插进去课就听不成了；自习里安插休息点正是要的效果。
        result = plan("上午数学课，晚上自习")
        self.assertFalse(result["events"][0]["interruptible"])
        self.assertTrue(result["events"][1]["interruptible"])

    def test_明天整体顺延一天(self):
        result = plan("明天上午 9 点数学月考")
        self.assertTrue(result["events"][0]["start"].startswith("2026-10-06"))

    def test_事件能原样回传给日历模型(self):
        # 前端就是把 agent 返回的 JSON 直接 POST 回 /api/calendar/events 的。
        result = plan("上午数学课，下午四点半去操场跑半小时")
        for item in result["events"]:
            rebuilt = CalendarEvent.from_mapping(item)
            self.assertEqual(rebuilt.to_dict()["start"], item["start"])
            self.assertEqual(item["metadata"]["via"], "agent")

    def test_同一句话两次结果一致(self):
        text = "7:30 起床，8 点早读，12:00 午饭，19:30 晚自习"
        self.assertEqual(plan(text), plan(text))

    def test_同一件事再说一遍是同一个_id(self):
        first = plan("上午 9 点数学课")["events"][0]
        second = plan("上午 9 点数学课")["events"][0]
        self.assertEqual(first["id"], second["id"])

    def test_时间撞上时不静默丢件(self):
        # 撞了就是撞了：两件都留着，让冲突提示去说，而不是悄悄吃掉一条。
        result = plan("9:00 交作业，9:05 数学课")
        self.assertEqual(len(result["events"]), 2)
        self.assertTrue(result["events"][0]["end"] > result["events"][1]["start"])

    def test_空文本与超长文本报错(self):
        with self.assertRaises(ValueError):
            plan("   ")
        with self.assertRaises(ValueError):
            plan("课" * (MAX_TEXT_CHARS + 1))

    def test_now_必须带时区(self):
        with self.assertRaises(ValueError):
            plan_locally("上午数学课", now="2026-10-05T14:07:00")


class MusicTests(unittest.TestCase):
    def test_跑步配奔跑且鼓点打满(self):
        result = plan("下午四点去操场跑半小时")
        self.assertEqual(result["music"]["soundscape"], "run")
        self.assertEqual(result["music"]["drums"], "strong")
        self.assertIn("奔跑", result["reply"])

    def test_累了就不钉奔跑(self):
        # 累到不想动的时候，被一首 165 拍的曲子推着跑只会更烦——和
        # day_plan._walk_soundscape 是同一条分寸。
        result = plan("下午四点跑步，今天特别累")
        self.assertNotEqual(result["music"]["soundscape"], "run")
        self.assertIsNone(result["music"]["drums"])

    def test_走路配疾走(self):
        result = plan("晚饭后散步半小时")
        self.assertEqual(result["music"]["soundscape"], "brisk")

    def test_自习日配书桌前(self):
        result = plan("上午语文，下午数学，晚上写作业")
        self.assertEqual(result["music"]["soundscape"], "desk-hours")

    def test_bpm_落在所选音景区间内(self):
        for text in ("下午跑步", "上午自习", "晚上写作业", "今天有点累", "放学走路回家"):
            music = plan(text)["music"]
            low, high = SOUNDSCAPES[music["soundscape"]].bpm_range
            with self.subTest(text=text):
                self.assertLessEqual(low, music["bpm"])
                self.assertLessEqual(music["bpm"], high)

    def test_累字不重复两遍(self):
        # 理由里已经说过一次「累」，同一件事换个说法讲两遍是最像机器的写法。
        reply = plan("下午跑步，今天有点累")["reply"]
        self.assertEqual(reply.count("累"), 1)


class _FakeAI:
    """按需返回一份 AI 结果，或抛错——测试只关心合并逻辑。"""

    def __init__(self, payload=None, error=None, configured=True):
        self.payload = payload
        self.error = error
        self.configured = configured
        self.calls = []

    def is_configured(self):
        return self.configured

    def understand(self, text, *, now_iso, local, catalog, history=()):
        self.calls.append({"text": text, "now_iso": now_iso, "history": list(history), "catalog": catalog})
        if self.error is not None:
            raise self.error
        return self.payload


class MergeAITests(unittest.TestCase):
    def test_没配模型时就是本地结果(self):
        result = plan_day_from_text("上午数学课", now=NOW, ai=_FakeAI(configured=False))
        self.assertEqual(result["source"], "local")
        self.assertIsNone(result["fallback_reason"])

    def test_模型结果覆盖本地(self):
        ai = _FakeAI({
            "reply": "上午那节课我按专注给你配。",
            "events": [
                {"title": "数学课", "start": "09:00", "end": "09:45", "category": "study", "priority": "hard"},
                {"title": "课间操", "start": "10:00", "end": "10:20", "category": "exercise", "priority": "soft"},
            ],
            "music": {"soundscape": "desk-hours", "drums": "light", "bpm": 72, "why": "大半天在桌前"},
            "state": {"energy": 40},
            "preferences": {"likes": ["钢琴"], "avoids": ["太吵"]},
        })
        result = plan_day_from_text("上午数学课，还有课间操", now=NOW, ai=ai)
        self.assertEqual(result["source"], "ai")
        self.assertEqual(result["reply"], "上午那节课我按专注给你配。")
        self.assertEqual([item["title"] for item in result["events"]], ["数学课", "课间操"])
        self.assertEqual(result["music"]["bpm"], 72)
        self.assertEqual(result["state"]["energy"], 40)
        self.assertEqual(result["preferences"]["likes"], ["钢琴"])
        self.assertEqual(ai.calls[0]["now_iso"], datetime.fromisoformat(NOW).isoformat())
        self.assertIn("desk-hours", ai.calls[0]["catalog"])
        self.assertIn("数学课", ai.calls[0]["text"])

    def test_单个字段不合法只退那一个(self):
        ai = _FakeAI({
            "reply": "好。",
            "events": [{"title": "数学课", "start": "上午九点", "end": "09:45"}],
            "music": {"soundscape": "不存在的音景", "drums": "爆炸", "bpm": 9999, "why": "?"},
            "state": {"energy": 300},
        })
        result = plan_day_from_text("上午数学课", now=NOW, ai=ai)
        self.assertEqual(result["source"], "ai")
        self.assertEqual(result["reply"], "好。")
        # 事件时间解析不出来 → 整条丢掉并改回本地结果。
        self.assertEqual(clocks(result), [("09:00", "09:45", "数学课")])
        # 音景不存在 → 退回本地选的那个（上午自习是「书桌前」）；drums 非法 → 退回本地的 None。
        self.assertEqual(result["music"]["soundscape"], "desk-hours")
        self.assertIsNone(result["music"]["drums"])
        self.assertLessEqual(result["music"]["bpm"], SOUNDSCAPES[result["music"]["soundscape"]].bpm_range[1])
        # 精力 300 超出 0–100 → 不采纳。
        self.assertIsNone(result["state"]["energy"])

    def test_模型给的时间照本地时区补齐(self):
        ai = _FakeAI({"events": [{"title": "跑步", "start": "18:00", "end": "18:40", "category": "exercise"}]})
        result = plan_day_from_text("下午六点跑步", now=NOW, ai=ai)
        self.assertEqual(result["events"][0]["start"], "2026-10-05T18:00:00+08:00")

    def test_请求失败退回本地并留原因(self):
        ai = _FakeAI(error=AIRecommenderError("AI network error: timed out"))
        result = plan_day_from_text("上午数学课", now=NOW, ai=ai)
        self.assertEqual(result["source"], "local")
        self.assertIn("AI network error", result["fallback_reason"])
        self.assertEqual(clocks(result), [("09:00", "09:45", "数学课")])

    def test_模型没排出行程时用本地结果并说明(self):
        ai = _FakeAI({"reply": "今天先歇着。", "events": []})
        result = plan_day_from_text("上午数学课", now=NOW, ai=ai)
        self.assertEqual(result["reply"], "今天先歇着。")
        self.assertEqual(clocks(result), [("09:00", "09:45", "数学课")])
        self.assertIn("本地规则", result["fallback_reason"])


if __name__ == "__main__":
    unittest.main()
