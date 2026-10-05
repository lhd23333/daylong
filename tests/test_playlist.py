# -*- coding: utf-8 -*-
"""playlist 模块的单元测试：收藏夹的校验、去重、持久化与损坏恢复。

被测文件：music_companion/playlist.py
覆盖重点：
* 配方校验（styleId / bpm / density / key / progressionIndex / seed / name）及边界值；
* 去重：同一段音乐（哪怕换了名字）只留一条，换个字段才算新的一条；
* 增删改查与 camelCase 字段，且对外字典能直接 json.dumps；
* 持久化：另开一个 store 指向同一文件能读到；文件不存在＝空歌单；
* 损坏文件：备份留档后重建，不静默丢数据，也不让服务起不来；
* 确定性：同一配方 + 同一命名空间，反复两次算出同一个 id。

所有用例都在临时目录里跑，绝不碰项目真实的 data/ 目录。
"""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from uuid import UUID

from music_companion.playlist import (
    DEFAULT_NAME,
    MAX_ENTRIES,
    PlaylistEntry,
    PlaylistStore,
    normalize_recipe,
    recipe_id,
)


def recipe(**overrides):
    """一份合法配方，字段可以用关键字覆盖。"""
    payload = {
        "styleId": "first-light",
        "bpm": 72,
        "density": 0.5,
        "key": "C",
        "progressionIndex": 2,
        "seed": 0,
        "name": "午后的一段",
    }
    payload.update(overrides)
    return payload


class PlaylistStoreTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.path = Path(self._temp.name) / "data" / "playlist.json"

    def open_store(self):
        return PlaylistStore(self.path)

    def write_raw(self, text):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(text, encoding="utf-8")

    def write_full_store(self):
        """把上限条数直接写进文件，避免用例里跑 500 次落盘。"""
        stored = [
            PlaylistEntry(
                id=recipe_id(recipe(seed=index)),
                style_id="first-light", bpm=72, density=0.5, key="C",
                progression_index=2, drums=None, seed=index, name=f"第 {index} 首",
                created_at="2026-10-05T00:00:00+00:00",
            ).to_dict()
            for index in range(MAX_ENTRIES)
        ]
        self.write_raw(json.dumps(stored, ensure_ascii=False))

    # ---------- 基本收发 ----------

    def test_添加后可以列出与按id取到且字段为camelCase(self):
        store = self.open_store()
        entry, created = store.add(recipe())

        self.assertTrue(created)
        self.assertEqual(entry["styleId"], "first-light")
        self.assertEqual(entry["bpm"], 72)
        self.assertEqual(entry["density"], 0.5)
        self.assertEqual(entry["key"], "C")
        self.assertEqual(entry["progressionIndex"], 2)
        self.assertEqual(entry["seed"], 0)
        self.assertEqual(entry["name"], "午后的一段")
        self.assertEqual(entry["note"], "")
        # id 是 uuid5，createdAt 必须带时区
        self.assertEqual(UUID(entry["id"]).version, 5)
        self.assertTrue(entry["createdAt"].endswith("+00:00"))
        # 对外字典必须能直接交给 json.dumps（前端拿到的就是这一份）
        json.dumps(entry, ensure_ascii=False)

        self.assertEqual(store.list_entries(), [entry])
        self.assertEqual(store.get(entry["id"]), entry)

    def test_取不存在的id返回None(self):
        self.assertIsNone(self.open_store().get("没有这一条"))

    def test_新增时可以直接带备注(self):
        store = self.open_store()
        entry, _ = store.add(recipe(note="副歌很好听"))
        self.assertEqual(entry["note"], "副歌很好听")

    def test_空名字给默认名且换行被压成空格(self):
        normalized = normalize_recipe(recipe(name="   "))
        self.assertEqual(normalized["name"], DEFAULT_NAME)
        self.assertEqual(normalize_recipe(recipe(name="午后\n的一段"))["name"], "午后 的一段")

    # ---------- 去重与确定性 ----------

    def test_同一配方重复添加不产生第二条且换名字也认得出来(self):
        store = self.open_store()
        first, created = store.add(recipe())
        self.assertTrue(created)

        second, created_again = store.add(recipe(name="另一个名字"))
        self.assertFalse(created_again)
        self.assertEqual(second["id"], first["id"])
        # 重复收藏不改动原来的名字，改名要走 rename
        self.assertEqual(second["name"], "午后的一段")
        self.assertEqual(len(store.list_entries()), 1)

        # 决定声音的字段变了，才是另一段音乐
        other, created_other = store.add(recipe(seed=1))
        self.assertTrue(created_other)
        self.assertNotEqual(other["id"], first["id"])
        self.assertEqual(len(store.list_entries()), 2)

    def test_同一配方两次算出同一个id(self):
        first = recipe_id(recipe())
        self.assertEqual(first, recipe_id(recipe()))
        # id 与存储位置无关：另一个目录里的 store 也会给出同一个 id
        other = PlaylistStore(Path(self._temp.name) / "别处" / "playlist.json")
        stored, _ = other.add(recipe())
        self.assertEqual(stored["id"], first)
        # 名字与备注不参与指纹
        self.assertEqual(recipe_id(recipe(name="换个名字")), first)
        # 音景 id 的大小写按白名单归一化，不产生第二个指纹
        self.assertEqual(recipe_id(recipe(styleId="FIRST-LIGHT")), first)

    def test_省略的字段与显式默认值得到同一个指纹(self):
        trimmed = recipe()
        # drums 本来就不在基础配方里：省略它必须等价于显式传 null
        for field in ("density", "key", "progressionIndex", "seed"):
            trimmed.pop(field)
        normalized = normalize_recipe(trimmed)
        self.assertEqual(normalized["density"], 0.5)
        self.assertIsNone(normalized["key"])
        self.assertIsNone(normalized["progressionIndex"])
        self.assertIsNone(normalized["drums"])
        self.assertEqual(normalized["seed"], 0)
        # 「省略」与「显式写默认值」是同一段音乐，指纹必须一致，去重才认得出来
        explicit = recipe(density=0.5, key=None, progressionIndex=None, drums=None, seed=0)
        self.assertEqual(recipe_id(trimmed), recipe_id(explicit))

    # ---------- 鼓点轴 ----------

    def test_鼓点档位保存并原样读回(self):
        store = self.open_store()
        entry, created = store.add(recipe(drums="strong"))
        self.assertTrue(created)
        self.assertEqual(entry["drums"], "strong")
        self.assertEqual(store.get(entry["id"])["drums"], "strong")
        # 重新打开一次（走读盘路径）仍然是同一档
        self.assertEqual(self.open_store().get(entry["id"])["drums"], "strong")

    def test_鼓点缺省时不指定而不是默认打鼓(self):
        # None 的语义是「跟音景的推荐档位走」，不是「无鼓」——这两件事在引擎里
        # 走的是不同分支，存错了会让收藏回来时听到的不是当时那段。
        normalized = normalize_recipe(recipe())
        self.assertIsNone(normalized["drums"])
        # 空串与缺省等价（前端表单没选时会传 ""）
        self.assertIsNone(normalize_recipe(recipe(drums=""))["drums"])
        self.assertIsNone(normalize_recipe(recipe(drums=None))["drums"])

    def test_未知鼓点档位被拒绝(self):
        for value in ("重", "heavy", "NONE!", 0):
            with self.assertRaises(ValueError):
                normalize_recipe(recipe(drums=value))
        # 大小写与空格按白名单归一化，不产生第二个指纹
        self.assertEqual(normalize_recipe(recipe(drums=" Strong "))["drums"], "strong")

    def test_不同鼓点档位是两段不同的音乐(self):
        # 鼓点参与指纹：同一组参数只换档位，必须存成两条，否则收藏「变强的那版」
        # 会被判成重复而存不进去。
        ids = {level: recipe_id(recipe(drums=level)) for level in ("none", "light", "standard", "strong")}
        self.assertEqual(len(set(ids.values())), 4)
        self.assertNotEqual(recipe_id(recipe(drums=None)), recipe_id(recipe(drums="none")))

    def test_旧的收藏文件没有鼓点字段也能读出来(self):
        """加字段不能把用户早先的收藏判成坏数据。"""
        legacy = recipe()
        entry, _ = self.open_store().add(legacy)
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        for item in stored:
            item.pop("drums", None)
        self.write_raw(json.dumps(stored, ensure_ascii=False))
        reopened = self.open_store()
        self.assertEqual(len(reopened.list_entries()), 1)
        self.assertIsNone(reopened.get(entry["id"])["drums"])

    # ---------- 校验 ----------

    def test_bpm越界被拒绝(self):
        for value in (39, 201, 0, -5):
            with self.assertRaises(ValueError):
                normalize_recipe(recipe(bpm=value))
        with self.assertRaises(ValueError):
            normalize_recipe(recipe(bpm=72.5))
        with self.assertRaises(ValueError):
            self.open_store().add(recipe(bpm=300))

    def test_bpm边界值可以通过(self):
        for value in (40, 200):
            self.assertEqual(normalize_recipe(recipe(bpm=value))["bpm"], value)
        # JS 只有一种数字类型，整数值写成浮点也要认
        self.assertEqual(normalize_recipe(recipe(bpm=72.0))["bpm"], 72)

    def test_density越界被拒绝(self):
        for value in (-0.01, 1.01, -1):
            with self.assertRaises(ValueError):
                normalize_recipe(recipe(density=value))

    def test_density边界值可以通过(self):
        self.assertEqual(normalize_recipe(recipe(density=0))["density"], 0.0)
        self.assertEqual(normalize_recipe(recipe(density=1))["density"], 1.0)

    def test_未知音景被拒绝(self):
        with self.assertRaises(ValueError):
            normalize_recipe(recipe(styleId="jazz-club"))
        with self.assertRaises(ValueError):
            normalize_recipe(recipe(styleId=""))
        with self.assertRaises(ValueError):
            normalize_recipe(recipe(styleId=None))

    def test_非法调名被拒绝(self):
        # 白名单逐字取自 theory.js 的 PITCH_CLASSES，只有升号拼法
        with self.assertRaises(ValueError):
            normalize_recipe(recipe(key="Bb"))
        with self.assertRaises(ValueError):
            normalize_recipe(recipe(key="H"))
        # 合法的写法大小写不敏感，统一归一化成表里的标准拼写
        self.assertEqual(normalize_recipe(recipe(key="a#"))["key"], "A#")

    def test_名字超长被拒绝(self):
        self.assertEqual(len(normalize_recipe(recipe(name="好" * 60))["name"]), 60)
        with self.assertRaises(ValueError):
            normalize_recipe(recipe(name="好" * 61))
        store = self.open_store()
        entry, _ = store.add(recipe())
        with self.assertRaises(ValueError):
            store.rename(entry["id"], "好" * 61)

    def test_seed与进行下标非法被拒绝(self):
        for value in (-1, 2**32, 1.5, "abc"):
            with self.assertRaises(ValueError):
                normalize_recipe(recipe(seed=value))
        with self.assertRaises(ValueError):
            normalize_recipe(recipe(progressionIndex=-1))
        # null 与空串都表示「按种子自动选」
        self.assertIsNone(normalize_recipe(recipe(progressionIndex=""))["progressionIndex"])

    # ---------- 删除 / 改名 / 备注 ----------

    def test_删除存在的条目(self):
        store = self.open_store()
        entry, _ = store.add(recipe())
        self.assertTrue(store.remove(entry["id"]))
        self.assertEqual(store.list_entries(), [])
        self.assertIsNone(store.get(entry["id"]))

    def test_删除不存在的条目不抛异常(self):
        store = self.open_store()
        self.assertFalse(store.remove("没有这一条"))
        self.assertFalse(store.remove(""))

    def test_重命名存在的条目(self):
        store = self.open_store()
        entry, _ = store.add(recipe())
        renamed = store.rename(entry["id"], "夜里的那段")
        self.assertEqual(renamed["id"], entry["id"])
        self.assertEqual(renamed["name"], "夜里的那段")
        self.assertEqual(store.get(entry["id"])["name"], "夜里的那段")
        # 只改名字，配方其余字段不动
        self.assertEqual(renamed["seed"], entry["seed"])

    def test_重命名不存在的条目返回None(self):
        self.assertIsNone(self.open_store().rename("没有这一条", "新名字"))

    def test_备注可以写入与清空(self):
        store = self.open_store()
        entry, _ = store.add(recipe())
        self.assertEqual(store.set_note(entry["id"], " 开头 30 秒 ")["note"], "开头 30 秒")
        self.assertEqual(store.set_note(entry["id"], "")["note"], "")
        self.assertIsNone(store.set_note("没有这一条", "写不进去"))

    # ---------- 持久化与损坏恢复 ----------

    def test_文件不存在时是空歌单(self):
        store = self.open_store()
        self.assertEqual(store.list_entries(), [])
        self.assertIsNone(store.get("任意 id"))

    def test_新store指向同一文件能读到且按收藏时间倒序(self):
        first_store = self.open_store()
        # 收藏时间注入两个确定的时刻，不依赖真实时钟：Windows 上
        # datetime.now() 的粒度约 16 ms，两次 add 很可能落在同一刻度，
        # 撞上就按 id 兜底排序（产品行为，见 list_entries），这条用例会偶发假失败。
        with mock.patch("music_companion.playlist.datetime") as fake_now:
            fake_now.now.side_effect = (
                datetime(2026, 10, 5, 10, 0, 0, tzinfo=timezone.utc),
                datetime(2026, 10, 5, 10, 0, 1, tzinfo=timezone.utc),
            )
            first, _ = first_store.add(recipe(name="第一首"))
            second, _ = first_store.add(recipe(seed=7, name="第二首"))

        # 文件本身就是一份可直接读的 JSON 列表（数组顺序＝收藏顺序）
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertIsInstance(raw, list)
        self.assertEqual([item["id"] for item in raw], [first["id"], second["id"]])

        reopened = self.open_store()
        listed = reopened.list_entries()
        self.assertEqual([item["id"] for item in listed], [second["id"], first["id"]])
        self.assertEqual(reopened.get(first["id"]), first)

    def test_改名与删除也会落盘(self):
        store = self.open_store()
        entry, _ = store.add(recipe())
        store.rename(entry["id"], "新名字")
        self.assertEqual(self.open_store().get(entry["id"])["name"], "新名字")
        store.remove(entry["id"])
        self.assertEqual(self.open_store().list_entries(), [])

    def test_损坏的json被备份后以空歌单重建(self):
        self.write_raw("{这不是合法 JSON")
        store = self.open_store()

        self.assertEqual(store.list_entries(), [])
        backups = sorted(self.path.parent.glob("playlist.corrupt-*.json"))
        self.assertEqual(len(backups), 1)
        # 坏文件原样留档，用户还能照着捞回来
        self.assertIn("这不是合法 JSON", backups[0].read_text(encoding="utf-8"))
        # 重建之后照常可用
        entry, created = store.add(recipe())
        self.assertTrue(created)
        self.assertEqual([item["id"] for item in self.open_store().list_entries()], [entry["id"]])

    def test_顶层不是列表同样按损坏处理(self):
        self.write_raw(json.dumps({"entries": []}))
        store = self.open_store()
        self.assertEqual(store.list_entries(), [])
        self.assertEqual(len(list(self.path.parent.glob("playlist.corrupt-*.json"))), 1)

    def test_单条坏数据只丢那一条(self):
        store = self.open_store()
        good, _ = store.add(recipe())
        broken = dict(recipe(seed=9), bpm=999)  # 合法 JSON，但 bpm 越界
        self.write_raw(json.dumps([good, broken], ensure_ascii=False))

        reopened = self.open_store()
        self.assertEqual([item["id"] for item in reopened.list_entries()], [good["id"]])

    # ---------- 容量上限 ----------

    def test_达到上限后拒绝新增(self):
        self.write_full_store()
        store = self.open_store()
        self.assertEqual(len(store.list_entries()), MAX_ENTRIES)
        with self.assertRaises(ValueError):
            store.add(recipe(seed=9999))
        # 拒绝新增不能顺手动到已有条目
        self.assertEqual(len(store.list_entries()), MAX_ENTRIES)

    def test_已收藏的配方在上限处仍然命中去重(self):
        self.write_full_store()
        store = self.open_store()
        entry, created = store.add(recipe(seed=0))
        self.assertFalse(created)
        self.assertEqual(entry["id"], recipe_id(recipe(seed=0)))


if __name__ == "__main__":
    unittest.main()
