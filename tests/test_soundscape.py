# -*- coding: utf-8 -*-
"""soundscape 模块的单元测试：音景目录、打分挑选、BPM 决策、精力分档。

被测文件：music_companion/soundscape.py
覆盖重点（对应模块 docstring 里承诺的性质）：
* 目录完整性与 API 输出契约；
* ``select_soundscape`` 的确定性与同分时的字典序 tie-break；
* ``explicit`` 命中已知 id 直接返回、未知 id 忽略；
* ``bpm_for`` 的返回值必须落在所属音景的 bpm_range 内（参数化遍历全部 18 个音景）；
* 压力高 / 精力低取下沿，精力高取上沿；
* ``energy_label`` 对数值、中文标签、英文别名与脏输入的处理。
"""

import unittest

from music_companion.soundscape import (
    DEFAULT_SOUNDSCAPE,
    ENERGY_TAGS,
    SOUNDSCAPES,
    STRESS_HIGH,
    bpm_for,
    energy_label,
    select_soundscape,
    soundscape_catalog,
)


class SoundscapeCatalogTests(unittest.TestCase):
    """音景目录本身的结构约束。"""

    def test_目录含十八个音景且元信息完整(self):
        # 10 个初版 + 第二轮扩充 8 个（摇滚/金属、轻音乐/古典、电子/合成器、爵士/摇摆）
        self.assertEqual(len(SOUNDSCAPES), 18)
        for sid, scape in SOUNDSCAPES.items():
            with self.subTest(sid=sid):
                self.assertEqual(sid, scape.id)
                self.assertTrue(scape.name.strip(), "name 不能为空")
                self.assertTrue(scape.character.strip(), "character 不能为空")
                low, high = scape.bpm_range
                self.assertEqual(len(scape.bpm_range), 2)
                self.assertLess(low, high, "bpm_range 必须是 (low, high) 且 low < high")
                self.assertGreaterEqual(low, 30)
                self.assertLessEqual(high, 220)
                # 三个标签维度都要参与打分，不能是空元组
                self.assertTrue(scape.when)
                self.assertTrue(scape.moods)
                self.assertTrue(scape.scenes)
                # energies 由 bpm_range 推导，必须落在合法四档内
                self.assertTrue(scape.energies)
                self.assertTrue(set(scape.energies) <= set(ENERGY_TAGS))

    def test_兜底音景是字典序最小的id(self):
        # 信息不足时返回 DEFAULT_SOUNDSCAPE，且它必须与同分 tie-break 规则一致
        self.assertEqual(DEFAULT_SOUNDSCAPE, sorted(SOUNDSCAPES)[0])
        self.assertIn(DEFAULT_SOUNDSCAPE, SOUNDSCAPES)

    def test_目录输出按id排序且字段符合接口约定(self):
        catalog = soundscape_catalog()
        self.assertEqual([item["id"] for item in catalog], sorted(SOUNDSCAPES))
        for item in catalog:
            with self.subTest(sid=item["id"]):
                self.assertEqual(
                    set(item),
                    {"id", "name", "when", "moods", "bpm_range", "character"},
                )
                self.assertIsInstance(item["when"], list)
                self.assertIsInstance(item["moods"], list)
                self.assertEqual(item["bpm_range"], list(SOUNDSCAPES[item["id"]].bpm_range))


class SelectSoundscapeTests(unittest.TestCase):
    """打分制挑选音景。"""

    def test_按打分权重挑出对应音景(self):
        # 权重 时段 3 / 心情 2 / 场景 2 / 精力 1：下面几组都是唯一最高分，能锁住选择结果
        self.assertEqual(
            select_soundscape(time_context="早晨", mood="专注", scene="久坐", energy="低"),
            "first-light",
        )
        # 午后 + 开心 + 行走 + 高精力：归途与疾走同为 8 分，字典序取 brisk。
        # 这正是「跟着鼓点走」想要的结果——走路场景优先给走路写的音景。
        self.assertEqual(
            select_soundscape(time_context="午后", mood="开心", scene="行走", energy="高"),
            "brisk",
        )
        # 归途仍然凭「通勤」标签在自己的场景里唯一最高分（疾走没有这个标签）
        self.assertEqual(
            select_soundscape(time_context="晚间", mood="开心", scene="通勤", energy="中"),
            "way-home",
        )
        self.assertEqual(
            select_soundscape(time_context="晚间", mood="疲惫", scene="久坐", energy="低"),
            "night-lamp",
        )
        self.assertEqual(
            select_soundscape(time_context="上午", mood="专注", scene="久坐", energy="中"),
            "desk-hours",
        )

    def test_跑步场景只由跑步音景命中(self):
        # 跑步标签只挂在 run 上：它是「出去跑一段」专用的，不该被别的场景借走
        self.assertEqual(
            select_soundscape(time_context="早晨", mood="兴奋", scene="跑步", energy="高"),
            "run",
        )
        self.assertIn("跑步", SOUNDSCAPES["run"].scenes)
        self.assertNotIn("跑步", SOUNDSCAPES["brisk"].scenes)

    def test_同分时按id字典序取最小(self):
        # 晚间 + 低落 + 通用 + 中精力：breath / night-lamp / settling 三家同分（均为 6 分），
        # 按 id 字典序取最小的 breath —— 这是「同输入同输出」的一部分。
        self.assertEqual(
            select_soundscape(time_context="晚间", mood="低落", scene="通用", energy="中"),
            "breath",
        )

    def test_同输入重复调用结果一致(self):
        cases = (
            dict(time_context="早晨", mood="专注", scene="久坐", energy="低"),
            dict(time_context="午后", mood="低落", scene="通用", energy=20),
            dict(time_context="夜间", mood="疲惫", scene="通勤", energy="中高"),
        )
        for kwargs in cases:
            with self.subTest(**kwargs):
                first = select_soundscape(**kwargs)
                self.assertEqual(first, select_soundscape(**kwargs))
                self.assertIn(first, SOUNDSCAPES)

    def test_显式音景优先且大小写空白容错(self):
        for raw in ("night-lamp", " Night-Lamp ", "NIGHT-LAMP"):
            with self.subTest(raw=raw):
                self.assertEqual(
                    select_soundscape(
                        time_context="早晨", mood="专注", scene="久坐", energy="低", explicit=raw
                    ),
                    "night-lamp",
                )

    def test_未知显式音景被忽略而不是报错(self):
        base = select_soundscape(time_context="早晨", mood="专注", scene="久坐", energy="低")
        self.assertEqual(base, "first-light")
        for raw in ("no-such-scape", "", "   ", None):
            with self.subTest(raw=raw):
                self.assertEqual(
                    select_soundscape(
                        time_context="早晨", mood="专注", scene="久坐", energy="低", explicit=raw
                    ),
                    base,
                )

    def test_英文别名与中文标签归一化后等价(self):
        # 晚间 + 疲惫 + 行走 + 低精力：breath（全天 3 分 + 疲惫 2 分 + 低精力 1 分）胜出
        baseline = select_soundscape(time_context="晚间", mood="疲惫", scene="行走", energy="低")
        self.assertEqual(baseline, "breath")
        self.assertEqual(
            select_soundscape(time_context="evening", mood="tired", scene="walk", energy="low"),
            baseline,
        )
        # 别名表支持关键词包含（「有点累」→ 疲惫、「散步」→ 行走）
        self.assertEqual(
            select_soundscape(time_context="晚上", mood="有点累", scene="散步", energy=20),
            baseline,
        )


class BpmForTests(unittest.TestCase):
    """BPM 决策。"""

    def test_返回值总是落在音景区间内(self):
        # 这是 bpm_for docstring 的硬承诺：全音景 × 多维脏输入遍历
        energies = ["低", "中", "中高", "高", 0, 34.9, 35, 60, 75, 100, "", None]
        stresses = [None, 0, 40, 64.9, 65, 80, 200, "70", "abc"]
        moods = ["", "平静", "焦虑", "疲惫", "低落", "开心", "兴奋", "专注", "乱码"]
        for sid, scape in SOUNDSCAPES.items():
            low, high = scape.bpm_range
            for energy in energies:
                for stress in stresses:
                    for mood in moods:
                        bpm = bpm_for(sid, energy=energy, stress=stress, mood=mood)
                        with self.subTest(sid=sid, energy=energy, stress=stress, mood=mood):
                            self.assertGreaterEqual(bpm, low)
                            self.assertLessEqual(bpm, high)

    def test_压力高或精力低时取区间下沿(self):
        for sid, scape in SOUNDSCAPES.items():
            low = scape.bpm_range[0]
            with self.subTest(sid=sid):
                self.assertEqual(bpm_for(sid, energy="中", stress=STRESS_HIGH), low)  # 正好到线
                self.assertEqual(bpm_for(sid, energy="中", stress=90), low)
                self.assertEqual(bpm_for(sid, energy="中", stress="80"), low)  # 手表导出的字符串
                self.assertEqual(bpm_for(sid, energy="低"), low)
                self.assertEqual(bpm_for(sid, energy=10), low)
                # 压力脏输入按 0 处理，与不传等价
                self.assertEqual(
                    bpm_for(sid, energy="中", stress=None),
                    bpm_for(sid, energy="中", stress=0),
                )
                # 压力高时心情不能把速度拉上去
                self.assertEqual(bpm_for(sid, energy="中", stress=80, mood="兴奋"), low)

    def test_精力高时取区间上沿(self):
        for sid, scape in SOUNDSCAPES.items():
            high = scape.bpm_range[1]
            with self.subTest(sid=sid):
                self.assertEqual(bpm_for(sid, energy="高"), high)
                self.assertEqual(bpm_for(sid, energy=90), high)
                self.assertEqual(bpm_for(sid, energy="high"), high)

    def test_中档精力按下沿取位且心情微调半档(self):
        # desk-hours 区间 68–88：中→0.5、中高→0.75，心情再 ±0.25 后夹在 [0, 1]
        self.assertEqual(bpm_for("desk-hours", energy="中"), 78)
        self.assertEqual(bpm_for("desk-hours", energy="中高"), 83)
        self.assertEqual(bpm_for("desk-hours", energy="中", mood="平静"), 78)
        self.assertEqual(bpm_for("desk-hours", energy="中", mood="焦虑"), 73)
        self.assertEqual(bpm_for("desk-hours", energy="中", mood="疲惫"), 73)
        self.assertEqual(bpm_for("desk-hours", energy="中", mood="开心"), 83)
        # 中高 + 兴奋 → 位置夹到 1.0，直接落到上沿
        self.assertEqual(bpm_for("desk-hours", energy="中高", mood="兴奋"), 88)
        # 中高 + 疲惫 → 位置回到 0.5
        self.assertEqual(bpm_for("desk-hours", energy="中高", mood="疲惫"), 78)

    def test_未知音景抛ValueError(self):
        for raw in ("no-such-scape", "", "   ", None, "high noon"):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    bpm_for(raw, energy="中")
        # 已知 id 允许大小写与首尾空白
        self.assertEqual(
            bpm_for(" HIGH-NOON ", energy="高"), SOUNDSCAPES["high-noon"].bpm_range[1]
        )


class EnergyLabelTests(unittest.TestCase):
    """精力归一化。"""

    def test_数值按四条线分档(self):
        cases = (
            (0, "低"),
            (34, "低"),
            (34.999, "低"),
            (35, "低"),  # 边界取 <=：与 planner / companion 的低精力线一致
            (54, "中"),
            (55, "中高"),
            (74, "中高"),
            (75, "高"),
            (88.0, "高"),
            (100, "高"),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(energy_label(value), expected)

    def test_中文与英文标签直接归一(self):
        cases = (
            ("低", "低"),
            ("偏低", "低"),
            ("low", "低"),
            ("LOW", "低"),
            ("中", "中"),
            ("中低", "中"),
            ("medium", "中"),
            ("中高", "中高"),
            ("偏高", "中高"),
            ("高", "高"),
            ("很高", "高"),
            ("high", "高"),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(energy_label(value), expected)

    def test_首尾空白与数字字符串(self):
        self.assertEqual(energy_label("  中  "), "中")
        self.assertEqual(energy_label(" 80 "), "高")
        self.assertEqual(energy_label("35"), "低")
        self.assertEqual(energy_label("10"), "低")

    def test_脏输入一律回落为中(self):
        # 无法识别时按「中」处理，避免一个脏字段改变整天的音景
        for value in (None, "", "   ", "乱码", "abc", True, [], {}):
            with self.subTest(value=value):
                self.assertEqual(energy_label(value), "中")
        # 标签分支只做精确匹配（不像 _tag 那样做关键词包含）
        self.assertEqual(energy_label("状态很差"), "中")


if __name__ == "__main__":
    unittest.main()
