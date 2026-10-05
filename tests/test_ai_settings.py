"""ai_settings 单元测试：合并语义、校验、脱敏、清除、抗损坏。"""

import json
import tempfile
import unittest
from pathlib import Path

from music_companion import ai_settings


class AiSettingsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / ai_settings.SETTINGS_FILENAME

    def tearDown(self):
        self._tmp.cleanup()

    def test_load_missing_file_is_empty(self):
        state = ai_settings.load(self.path)
        self.assertEqual(state, {"chat": {}, "music": {}})

    def test_save_and_load_roundtrip(self):
        ai_settings.save(self.path, {
            "chat": {"api_key": "sk-a", "base_url": "https://api.example.com/v1", "model": "m1"},
            "music": {"provider": "tempolor", "api_key": "tempo-x", "callback_url": "https://example.com/cb"},
        })
        state = ai_settings.load(self.path)
        self.assertEqual(state["chat"]["api_key"], "sk-a")
        self.assertEqual(state["chat"]["base_url"], "https://api.example.com/v1")
        self.assertEqual(state["chat"]["model"], "m1")
        self.assertEqual(state["music"]["provider"], "tempolor")
        self.assertEqual(state["music"]["callback_url"], "https://example.com/cb")
        # 落盘的就是 UTF-8 JSON，好排查。
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["chat"]["api_key"], "sk-a")

    def test_partial_patch_keeps_other_fields(self):
        ai_settings.save(self.path, {"chat": {"api_key": "sk-a"}})
        ai_settings.save(self.path, {"chat": {"model": "m2"}})
        state = ai_settings.load(self.path)
        self.assertEqual(state["chat"]["api_key"], "sk-a")
        self.assertEqual(state["chat"]["model"], "m2")

    def test_empty_string_clears_field(self):
        ai_settings.save(self.path, {"chat": {"api_key": "sk-a", "model": "m1"}})
        ai_settings.save(self.path, {"chat": {"api_key": ""}})
        state = ai_settings.load(self.path)
        self.assertNotIn("api_key", state["chat"])
        self.assertEqual(state["chat"]["model"], "m1")

    def test_save_rejects_bad_values(self):
        with self.assertRaises(ValueError):
            ai_settings.save(self.path, {"chat": {"base_url": "ftp://bad"}})
        with self.assertRaises(ValueError):
            ai_settings.save(self.path, {"music": {"provider": "spotify"}})
        with self.assertRaises(ValueError):
            ai_settings.save(self.path, {"chat": {"api_key": "x" * 600}})
        with self.assertRaises(ValueError):
            ai_settings.save(self.path, {"chat": {"api_key": "line1\nline2"}})
        with self.assertRaises(ValueError):
            ai_settings.save(self.path, {"chat": {"model": 42}})
        # 校验失败不落盘：文件始终不存在。
        self.assertFalse(self.path.exists())

    def test_rejected_save_keeps_existing_settings(self):
        ai_settings.save(self.path, {"chat": {"api_key": "sk-keep"}})
        with self.assertRaises(ValueError):
            ai_settings.save(self.path, {"chat": {"api_key": "sk-new", "base_url": "nope"}})
        state = ai_settings.load(self.path)
        self.assertEqual(state["chat"]["api_key"], "sk-keep")

    def test_key_hint_masks_all_but_last_four(self):
        self.assertEqual(ai_settings.key_hint("sk-test-abcd1234"), "…1234")
        self.assertEqual(ai_settings.key_hint(""), "")
        self.assertEqual(ai_settings.key_hint(None), "")
        # 太短的 Key 连尾 4 位也不给：那几乎等于全给了。
        self.assertEqual(ai_settings.key_hint("abc"), "…")

    def test_clear_removes_file_and_is_idempotent(self):
        ai_settings.save(self.path, {"chat": {"api_key": "sk-a"}})
        self.assertTrue(ai_settings.clear(self.path))
        self.assertFalse(self.path.exists())
        self.assertFalse(ai_settings.clear(self.path))
        self.assertEqual(ai_settings.load(self.path), {"chat": {}, "music": {}})

    def test_load_survives_corrupt_and_unknown_content(self):
        self.path.write_text("{ not json", encoding="utf-8")
        self.assertEqual(ai_settings.load(self.path), {"chat": {}, "music": {}})
        self.path.write_text(json.dumps({
            "chat": {"api_key": 123, "model": "ok-model"},
            "music": "not-a-dict",
            "extra": {"ignored": True},
        }), encoding="utf-8")
        state = ai_settings.load(self.path)
        # 坏字段丢弃，好字段保留；未知块完全忽略。
        self.assertNotIn("api_key", state["chat"])
        self.assertEqual(state["chat"]["model"], "ok-model")
        self.assertEqual(state["music"], {})


if __name__ == "__main__":
    unittest.main()
