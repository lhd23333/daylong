"""TDD contract tests for the optional ElevenLabs Music provider.

These tests deliberately exercise the provider through a tiny standard-library
HTTP seam.  The server tests inject a per-test fake provider, so they do not
share an HTTP server or an API key between cases.
"""

from __future__ import annotations

import io
import json
import tempfile
import threading
import unittest
import urllib.error
from email.message import Message
from http.client import HTTPConnection
from unittest.mock import patch

from music_companion.music_api import ElevenLabsMusicClient, MusicAPIError
from server import MusicCompanionServer


class _FakeResponse:
    def __init__(self, body: bytes, content_type: str, song_id: str = "") -> None:
        self._body = body
        self.status = 200
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        if song_id:
            self.headers["song-id"] = song_id

    def read(self) -> bytes:
        return self._body

    def getheader(self, name: str, default: str | None = None) -> str | None:
        return self.headers.get(name, default)

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None


class ElevenLabsMusicClientTests(unittest.TestCase):
    def test_build_prompt_includes_tempo_style_instruments_and_instrumental_mode(self):
        client = ElevenLabsMusicClient(api_key="test-key")
        prompt = client.build_prompt(
            {
                "target_bpm": 108,
                "music_style": "轻快电子",
                "instruments": ["木吉他", "轻鼓"],
                "vocal_mode": "纯音乐",
                "scene": "午后散步",
            }
        )

        self.assertIsInstance(prompt, str)
        for expected in ("108", "轻快电子", "木吉他", "轻鼓", "纯音乐"):
            self.assertIn(expected, prompt)

    def test_missing_api_key_is_reported_before_a_network_request(self):
        client = ElevenLabsMusicClient(api_key="")
        with patch("urllib.request.urlopen") as urlopen:
            with self.assertRaises(MusicAPIError):
                client.generate("纯音乐，约 100 BPM", duration_seconds=30)
        urlopen.assert_not_called()

    def test_generate_returns_binary_audio_and_sends_elevenlabs_payload(self):
        response = _FakeResponse(b"ID3\x04\x00music", "audio/mpeg", "song-123")
        captured: dict[str, object] = {}

        def fake_urlopen(request: object, *args: object, **kwargs: object) -> _FakeResponse:
            captured["request"] = request
            captured["args"] = args
            captured["kwargs"] = kwargs
            return response

        client = ElevenLabsMusicClient(
            api_key="test-key", base_url="https://music.example/v1", model_id="music_v2_5"
        )
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = client.generate("轻快电子，约 108 BPM", duration_seconds=12.5)

        self.assertEqual(result["audio_bytes"], b"ID3\x04\x00music")
        self.assertEqual(result["content_type"], "audio/mpeg")
        self.assertEqual(result["song_id"], "song-123")
        self.assertEqual(result["model_id"], "music_v2_5")
        self.assertEqual(result["provider"], "elevenlabs")

        request = captured["request"]
        self.assertEqual(request.full_url, "https://music.example/v1/music")
        self.assertEqual(request.get_header("Xi-api-key"), "test-key")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["prompt"], "轻快电子，约 108 BPM")
        self.assertEqual(payload["music_length_ms"], 12500)
        self.assertEqual(payload["model_id"], "music_v2_5")
        self.assertTrue(payload["force_instrumental"])

    def test_http_error_is_wrapped_as_music_api_error(self):
        error = urllib.error.HTTPError(
            "https://music.example/v1/music",
            401,
            "Unauthorized",
            Message(),
            io.BytesIO(b"invalid key"),
        )
        client = ElevenLabsMusicClient(api_key="bad-key", base_url="https://music.example/v1")
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(MusicAPIError):
                client.generate("纯音乐", duration_seconds=30)

    def test_non_audio_content_type_is_rejected(self):
        response = _FakeResponse(b'{"error":"not audio"}', "application/json")
        client = ElevenLabsMusicClient(api_key="test-key")
        with patch("urllib.request.urlopen", return_value=response):
            with self.assertRaises(MusicAPIError):
                client.generate("纯音乐", duration_seconds=30)

    def test_music_duration_must_stay_within_elevenlabs_limits(self):
        client = ElevenLabsMusicClient(api_key="test-key")
        for duration in (2, 601):
            with self.subTest(duration=duration):
                with patch("urllib.request.urlopen") as urlopen:
                    with self.assertRaises(MusicAPIError):
                        client.generate("纯音乐", duration_seconds=duration)
                urlopen.assert_not_called()


class _StubMusicClient:
    def __init__(self, result: dict[str, object] | None = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error

    def build_prompt(self, *args: object, **kwargs: object) -> str:
        return "测试音乐，纯音乐，约 108 BPM"

    def generate(self, *args: object, **kwargs: object) -> dict[str, object]:
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


class GenerateAIAudioEndpointTests(unittest.TestCase):
    def _request_with_provider(
        self, provider: _StubMusicClient, payload: dict[str, object]
    ) -> tuple[object, bytes]:
        data_dir = tempfile.TemporaryDirectory()
        server = MusicCompanionServer(("127.0.0.1", 0), data_dir=data_dir.name)
        server.music_client = provider
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            connection.request(
                "POST", "/api/generate-ai-audio", body, {"Content-Type": "application/json"}
            )
            response = connection.getresponse()
            raw = response.read()
            connection.close()
            # Copy the response fields before shutting down the isolated server.
            result = type(
                "ResponseSnapshot",
                (),
                {
                    "status": response.status,
                    "headers": response.headers,
                    "getheader": response.getheader,
                },
            )()
            return result, raw
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            data_dir.cleanup()

    def test_successful_ai_audio_is_returned_with_ai_source_header(self):
        provider = _StubMusicClient(
            result={
                "audio_bytes": b"ID3\x04\x00generated",
                "content_type": "audio/mpeg",
                "song_id": "song-demo",
                "model_id": "music_v2_5",
                "provider": "elevenlabs",
            }
        )
        response, raw = self._request_with_provider(
            provider,
            {"target_bpm": 108, "duration_seconds": 3, "music_style": "轻快电子"},
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(raw, b"ID3\x04\x00generated")
        self.assertEqual(response.getheader("Content-Type"), "audio/mpeg")
        self.assertEqual(response.getheader("X-Music-Source"), "ai")

    def test_ai_failure_returns_local_wav_with_fallback_source_header(self):
        provider = _StubMusicClient(error=MusicAPIError("ElevenLabs API key 未配置"))
        response, raw = self._request_with_provider(
            provider,
            {"target_bpm": 108, "duration_seconds": 1},
        )

        self.assertEqual(response.status, 200)
        self.assertTrue(raw.startswith(b"RIFF"))
        self.assertEqual(response.getheader("Content-Type"), "audio/wav")
        self.assertEqual(response.getheader("X-Music-Source"), "local-fallback")
        reason_token = response.getheader("X-Music-Fallback-Reason-B64")
        self.assertIsNotNone(reason_token)
        self.assertRegex(reason_token or "", r"^[A-Za-z0-9_-]+$")

    def test_ai_endpoint_rejects_subsecond_duration_consistently(self):
        provider = _StubMusicClient(error=MusicAPIError("should not be called"))
        response, raw = self._request_with_provider(
            provider,
            {"target_bpm": 108, "duration_seconds": 0.5},
        )
        self.assertEqual(response.status, 400)
        self.assertIn("1 到 1800", raw.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
