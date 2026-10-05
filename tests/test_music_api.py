"""远端音乐客户端的测试。

重点在 ``TemPolorMusicClient``：它的链路是「提交 → 轮询 → 下载」三步，任何
一步的字段名写错都要等到真花钱调用时才会暴露。所以这里起一个**本地假服务端**
按官方文档的形状应答，把三步都走通——没有真 key 也能验证协议层是对的。

ElevenLabs 那条线是一次同步 POST，同样用假服务端验一遍请求体形状。
"""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from music_companion.music_api import (
    ElevenLabsMusicClient,
    MusicAPIError,
    TemPolorMusicClient,
    build_music_prompt,
    create_music_client,
)

MP3 = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 64


class _FakeProvider(BaseHTTPRequestHandler):
    """假的天谱乐 / ElevenLabs。行为由类属性控制，各用例自己设。"""

    calls = []
    # 天谱乐：第几次查询才返回成功。1 = 第一次查询就成功。
    succeed_on_query = 1
    # 天谱乐：提交时返回的 item_ids
    item_ids = ["item-1"]
    # 天谱乐：查询结果里的 status 序列（按查询次数取，超出取最后一个）
    statuses = ["succeeded"]
    audio_bytes = MP3
    audio_content_type = "audio/mpeg"
    error_payload = None

    def log_message(self, *args):  # 别把请求打到测试输出里
        pass

    def _json(self, payload, status=200):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8"))

    def do_POST(self):
        type(self).calls.append(
            {
                "path": self.path,
                "auth": self.headers.get("Authorization"),
                "xi_key": self.headers.get("xi-api-key"),
                "content_type": self.headers.get("Content-Type"),
                "body": self._body(),
            }
        )
        if self.error_payload is not None:
            self._json(self.error_payload)
            return
        if self.path.endswith("/song/generate"):
            self._json({"code": 0, "message": "ok", "request_id": "r1",
                        "data": {"item_ids": self.item_ids}})
            return
        if self.path.endswith("/song/query"):
            queries = sum(1 for c in type(self).calls if c["path"].endswith("/song/query"))
            statuses = self.statuses
            status = statuses[min(queries - 1, len(statuses) - 1)]
            self._json({"code": 0, "message": "ok", "data": {"songs": [
                {"item_id": "item-1", "status": status, "event": "wav_complete",
                 "audio_url": f"{self.base_url()}/audio.mp3",
                 "audio_hi_url": f"{self.base_url()}/audio.mp3",
                 "title": "晨光", "duration": 30},
            ]}})
            return
        if self.path.endswith("/music"):
            raw = self.audio_bytes
            self.send_response(200)
            self.send_header("Content-Type", self.audio_content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("song-id", "song-9")
            self.end_headers()
            self.wfile.write(raw)
            return
        self._json({"error": "not found"}, 404)

    def do_GET(self):
        if self.path.endswith("/audio.mp3"):
            raw = self.audio_bytes
            self.send_response(200)
            # 故意不返 Content-Type：真实对象存储经常这样，客户端要能靠魔数兜底。
            self.send_header("Content-Type", "")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        self._json({"error": "not found"}, 404)

    def base_url(self):
        host, port = self.server.server_address[:2]
        return f"http://127.0.0.1:{port}"


class MusicApiTests(unittest.TestCase):
    def setUp(self):
        _FakeProvider.calls = []
        _FakeProvider.succeed_on_query = 1
        _FakeProvider.statuses = ["succeeded"]
        _FakeProvider.item_ids = ["item-1"]
        _FakeProvider.error_payload = None
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeProvider)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address[:2]
        self.base = f"http://127.0.0.1:{port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def _client(self, **overrides):
        kwargs = dict(
            api_key="Tempo-xxxx-3w",
            base_url=self.base,
            callback_url="https://example.com/callback",
            # 把两个等待压到最短：测的是协议，不是耐心。
            poll_interval=0.05,
            poll_timeout=5.0,
            timeout=5.0,
        )
        kwargs.update(overrides)
        return TemPolorMusicClient(**kwargs)

    def test_tempolor_submit_poll_download(self):
        result = self._client().generate("calm piano", 30)
        self.assertEqual(result["provider"], "tempolor")
        self.assertEqual(result["audio_bytes"], MP3)
        self.assertEqual(result["song_id"], "item-1")
        self.assertEqual(result["title"], "晨光")

        paths = [call["path"] for call in _FakeProvider.calls]
        self.assertIn("/open-apis/v1/song/generate", paths)
        self.assertIn("/open-apis/v1/song/query", paths)

        submit = next(c for c in _FakeProvider.calls if c["path"].endswith("/song/generate"))
        # 鉴权头是裸 key，**不带 Bearer**——带错了平台会当没鉴权。
        self.assertEqual(submit["auth"], "Tempo-xxxx-3w")
        self.assertEqual(submit["body"]["model"], "tempolor-latest")
        self.assertTrue(submit["body"]["instrumental"])
        self.assertEqual(submit["body"]["callback_url"], "https://example.com/callback")
        self.assertIn("about 30 seconds", submit["body"]["prompt"])

        query = next(c for c in _FakeProvider.calls if c["path"].endswith("/song/query"))
        self.assertEqual(query["body"], {"item_ids": ["item-1"]})

    def test_tempolor_polls_until_success(self):
        # 前两次查询还在跑，第三次才成功——轮询必须撑得住这个中间态。
        _FakeProvider.statuses = ["running", "running", "succeeded"]
        result = self._client().generate("calm piano", 20)
        self.assertEqual(result["audio_bytes"], MP3)
        queries = [c for c in _FakeProvider.calls if c["path"].endswith("/song/query")]
        self.assertEqual(len(queries), 3)

    def test_tempolor_reports_task_failure(self):
        _FakeProvider.statuses = ["failed"]
        with self.assertRaises(MusicAPIError) as caught:
            self._client().generate("calm piano", 20)
        self.assertIn("失败", str(caught.exception))

    def test_tempolor_reports_business_error_code(self):
        _FakeProvider.error_payload = {"code": 40001, "message": "余额不足"}
        with self.assertRaises(MusicAPIError) as caught:
            self._client().generate("calm piano", 20)
        self.assertIn("余额不足", str(caught.exception))

    def test_tempolor_rejects_over_long_duration(self):
        with self.assertRaises(MusicAPIError) as caught:
            self._client().generate("calm piano", 300)
        self.assertIn("270", str(caught.exception))

    def test_tempolor_requires_callback_url(self):
        # callback_url 是平台必填项，而本应用用轮询取结果。缺了就明确报错，
        # 不要替用户编一个可能被平台判非法的地址。
        with self.assertRaises(MusicAPIError) as caught:
            self._client(callback_url="").generate("calm piano", 20)
        self.assertIn("TEMPOLOR_CALLBACK_URL", str(caught.exception))

    def test_tempolor_requires_api_key(self):
        with self.assertRaises(MusicAPIError) as caught:
            self._client(api_key="").generate("calm piano", 20)
        self.assertIn("TEMPOLOR_API_KEY", str(caught.exception))

    def test_tempolor_guesses_content_type_without_header(self):
        # 假服务端刻意不返 Content-Type，客户端要按 ID3 魔数认出 mp3。
        result = self._client().generate("calm piano", 20)
        self.assertEqual(result["content_type"], "audio/mpeg")

    def test_elevenlabs_request_shape(self):
        client = ElevenLabsMusicClient(api_key="sk-test", base_url=self.base, timeout=5.0)
        result = client.generate("warm piano", 12.5, force_instrumental=True)
        self.assertEqual(result["provider"], "elevenlabs")
        self.assertEqual(result["song_id"], "song-9")
        call = next(c for c in _FakeProvider.calls if c["path"].endswith("/music"))
        # ElevenLabs 走 `xi-api-key`，天谱乐走 `Authorization`。两家都别写混。
        self.assertEqual(call["xi_key"], "sk-test")
        self.assertEqual(call["body"]["music_length_ms"], 12500)
        self.assertTrue(call["body"]["force_instrumental"])

    def test_prompt_is_shared_and_original(self):
        prompt = build_music_prompt({"target_bpm": 88, "music_style": "轻快", "instruments": ["钢琴"]})
        self.assertIn("88 BPM", prompt)
        self.assertIn("钢琴", prompt)
        self.assertIn("no imitation", prompt.lower())

    def test_client_factory_selects_provider(self):
        self.assertIsInstance(create_music_client("elevenlabs"), ElevenLabsMusicClient)
        self.assertIsInstance(create_music_client("tempolor"), TemPolorMusicClient)
        with self.assertRaises(MusicAPIError):
            create_music_client("suno")


if __name__ == "__main__":
    unittest.main()
