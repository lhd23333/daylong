import os
import io
import wave
import unittest

from music_companion.ai_client import AIRecommender
from music_companion.audio_generator import AudioGenerationError, generate_music_wav
from music_companion.recommender import RecommendationError, build_profile, recommend


class RecommenderTests(unittest.TestCase):
    def test_empty_payload_is_rejected(self):
        with self.assertRaises(RecommendationError):
            recommend({"schedule": "", "mood": "", "exercise": "", "notes": ""})

    def test_schedule_only_returns_local_result(self):
        result = recommend(
            {
                "schedule": "今晚要写报告，之后想放松",
                "mood": "",
                "exercise": "",
                "notes": "",
            },
            ai_client=None,
        )
        self.assertEqual(result["source"], "local")
        self.assertIn("晚间", result["profile"]["time_context"])
        self.assertEqual("专注工作/学习", result["profile"]["activity"])
        self.assertGreaterEqual(result["recommendation"]["target_bpm"], 40)

    def test_anxious_mood_shifts_tempo_down(self):
        result = recommend(
            {
                "schedule": "晚上要赶 deadline",
                "mood": "焦虑",
                "exercise": "",
                "notes": "",
            },
            ai_client=None,
        )
        self.assertEqual(result["profile"]["mood_state"], "焦虑")
        self.assertLessEqual(result["recommendation"]["bpm_max"], 118)

    def test_run_activity_returns_higher_tempo(self):
        result = recommend(
            {
                "schedule": "",
                "mood": "开心",
                "exercise": "慢跑 20 分钟",
                "notes": "",
            },
            ai_client=None,
        )
        self.assertEqual(result["profile"]["activity"], "慢跑")
        self.assertGreaterEqual(result["recommendation"]["target_bpm"], 140)

    def test_ai_client_can_be_forced_off(self):
        old = os.environ.get("MUSIC_COMPANION_AI")
        os.environ["MUSIC_COMPANION_AI"] = "off"
        try:
            client = AIRecommender(api_key="fake-key")
            self.assertFalse(client.is_configured())
        finally:
            if old is None:
                os.environ.pop("MUSIC_COMPANION_AI", None)
            else:
                os.environ["MUSIC_COMPANION_AI"] = old

    def test_generate_music_wav_returns_valid_wav(self):
        audio = generate_music_wav(120, ["lo-fi", "ambient"], duration_seconds=1.0)
        self.assertTrue(audio.startswith(b"RIFF"))
        self.assertGreater(len(audio), 44)
        with wave.open(io.BytesIO(audio), "rb") as wav:
            self.assertEqual(wav.getnchannels(), 1)
            self.assertEqual(wav.getframerate(), 22050)
            self.assertEqual(wav.getsampwidth(), 2)

    def test_generate_music_wav_rejects_bad_bpm(self):
        with self.assertRaises(AudioGenerationError):
            generate_music_wav(10)

    def test_sedentary_scene_returns_low_bpm(self):
        result = recommend(
            {
                "scene": "sedentary",
                "schedule": "今晚在宿舍复习",
                "mood": "专注",
                "exercise": "",
                "notes": "",
            },
            ai_client=None,
        )
        self.assertEqual(result["profile"]["scene_mode"], "久坐自习/办公")
        self.assertLessEqual(result["recommendation"]["target_bpm"], 80)

    def test_walking_scene_uses_body_metrics(self):
        result = recommend(
            {
                "scene": "walking",
                "schedule": "课间想快走一会儿",
                "mood": "",
                "exercise": "快走",
                "notes": "",
                "height_cm": "168",
                "weight_kg": "58",
                "leg_length_cm": "82",
            },
            ai_client=None,
        )
        self.assertEqual(result["profile"]["scene_mode"], "行走/轻运动")
        self.assertTrue(result["profile"]["uses_body_metrics"])
        self.assertGreater(result["profile"]["step_frequency"], 90)

    def test_invalid_body_metric_is_rejected(self):
        with self.assertRaises(RecommendationError):
            recommend(
                {
                    "scene": "walking",
                    "schedule": "",
                    "mood": "开心",
                    "exercise": "",
                    "notes": "",
                    "height_cm": "999",
                },
                ai_client=None,
            )

    def test_music_spec_follows_state_and_preferences(self):
        result = recommend(
            {
                "scene": "walking",
                "schedule": "课间想快走一会儿",
                "mood": "开心",
                "exercise": "快走",
                "notes": "",
                "preferred_style": "电子流行",
                "vocal_mode": "light",
            },
            ai_client=None,
        )
        recommendation = result["recommendation"]
        self.assertEqual(recommendation["music_style"], "电子流行")
        self.assertIn("合成器", recommendation["instruments"])
        self.assertEqual(recommendation["vocal_mode"], "轻度人声 / 哼唱")
        self.assertTrue(recommendation["lyrics"])

    def test_sedentary_music_spec_uses_soft_instruments(self):
        result = recommend(
            {
                "scene": "sedentary",
                "schedule": "今晚在宿舍复习",
                "mood": "专注",
                "exercise": "",
                "notes": "",
                "vocal_mode": "none",
            },
            ai_client=None,
        )
        self.assertEqual(result["recommendation"]["vocal_mode"], "无")
        self.assertIn("钢琴", result["recommendation"]["instruments"])
        self.assertIn("放松", result["recommendation"]["lyric_theme"])


class _StubAI:
    """一个真的会回 JSON 的本地 HTTP 服务，替代 OpenAI 兼容端点。

    不打桩 urlopen，而是真发一次 HTTP 请求——这样请求体、鉴权头、JSON 解析
    这几段真正容易写错的地方都被覆盖到。
    """

    def __init__(self, content, status=200):
        import json
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler 的约定
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8")
                outer.requests.append(
                    {
                        "path": self.path,
                        "auth": self.headers.get("Authorization"),
                        "body": json.loads(raw),
                    }
                )
                if status != 200:
                    self.send_response(status)
                    self.end_headers()
                    self.wfile.write(b'{"error":"boom"}')
                    return
                payload = json.dumps(
                    {"choices": [{"message": {"content": content}}]}, ensure_ascii=False
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):  # 测试输出保持干净
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class CareChannelTests(unittest.TestCase):
    """关怀语的 AI 通道：能通、能解析、任何异常都退回本地。"""

    def test_未配置时不发请求(self):
        from music_companion.ai_client import AIRecommenderError

        # 「未配置」这个场景必须在测试里**显式造出来**：music_api 在导入时会读
        # 项目根的 .env 并塞进 os.environ，所以开发机上 OPENAI_API_KEY 很可能是
        # 有值的。不清掉它，这条用例测的就不是「未配置」而是「已配置」了。
        touched = ("OPENAI_API_KEY", "OPENAI_BASE_URL")
        saved = {name: os.environ.pop(name, None) for name in touched}
        try:
            client = AIRecommender(api_key="", base_url="http://127.0.0.1:1/v1")
            self.assertFalse(client.is_configured())
            with self.assertRaises(AIRecommenderError):
                client.care({}, {})
        finally:
            for name, value in saved.items():
                if value is not None:
                    os.environ[name] = value

    def test_请求体与解析(self):
        import json

        stub = _StubAI(
            json.dumps(
                {"greeting": "下午还剩两件，先歇十分钟。", "detail": "空档 45 分钟。",
                 "suggestion": "去接杯水。", "tone": "gentle"},
                ensure_ascii=False,
            )
        )
        try:
            client = AIRecommender(api_key="test-key", base_url=stub.base_url)
            result = client.care({"stats": {"free_minutes": 45}}, {"greeting": "本地"})
        finally:
            stub.close()

        self.assertEqual(result["tone"], "gentle")
        self.assertEqual(len(stub.requests), 1)
        sent = stub.requests[0]
        self.assertEqual(sent["path"], "/v1/chat/completions")
        self.assertEqual(sent["auth"], "Bearer test-key")
        # 发的是关怀语指令，不是推荐指令——两条路径不能串
        self.assertIn("关怀语", sent["body"]["messages"][0]["content"])
        self.assertNotIn("bpm_min", sent["body"]["messages"][0]["content"])
        self.assertEqual(sent["body"]["model"], os.getenv("OPENAI_MODEL", "gpt-4o-mini"))

    def test_走完整链路时source变为ai(self):
        import json

        from music_companion.companion import CareCard, compose_care_with_ai

        stub = _StubAI(
            json.dumps(
                {"greeting": "今天慢一点。", "detail": "最长空档 2.5 小时。",
                 "suggestion": "把空档留给难的那件。", "tone": "steady"},
                ensure_ascii=False,
            )
        )
        try:
            client = AIRecommender(api_key="test-key", base_url=stub.base_url)
            local = CareCard(
                greeting="本地问候", detail="本地依据", suggestion="本地建议", tone="steady"
            )
            polished = compose_care_with_ai(local, {"now": "13:00"}, client)
        finally:
            stub.close()

        self.assertEqual(polished.source, "ai")
        self.assertEqual(polished.greeting, "今天慢一点。")
        self.assertEqual(local.source, "local")  # 本地卡不被就地改写

    def test_服务端报错时退回本地(self):
        import json

        from music_companion.companion import CareCard, compose_care_with_ai

        stub = _StubAI("{}", status=500)
        try:
            client = AIRecommender(api_key="test-key", base_url=stub.base_url)
            local = CareCard(
                greeting="本地问候", detail="本地依据", suggestion="本地建议", tone="steady"
            )
            self.assertIs(compose_care_with_ai(local, {}, client), local)
        finally:
            stub.close()

    def test_返回非JSON时退回本地(self):
        from music_companion.companion import CareCard, compose_care_with_ai

        stub = _StubAI("这不是 JSON")
        try:
            client = AIRecommender(api_key="test-key", base_url=stub.base_url)
            local = CareCard(
                greeting="本地问候", detail="本地依据", suggestion="本地建议", tone="steady"
            )
            self.assertIs(compose_care_with_ai(local, {}, client), local)
        finally:
            stub.close()

    def test_返回空字段时丢弃AI结果(self):
        import json

        from music_companion.companion import CareCard, compose_care_with_ai

        stub = _StubAI(
            json.dumps({"greeting": "", "detail": "有依据", "suggestion": "有建议"}, ensure_ascii=False)
        )
        try:
            client = AIRecommender(api_key="test-key", base_url=stub.base_url)
            local = CareCard(
                greeting="本地问候", detail="本地依据", suggestion="本地建议", tone="steady"
            )
            self.assertIs(compose_care_with_ai(local, {}, client), local)
        finally:
            stub.close()


if __name__ == "__main__":
    unittest.main()
