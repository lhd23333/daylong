import io
import json
import threading
import unittest
import wave
from http.client import HTTPConnection
import tempfile

from server import MusicCompanionServer
from music_companion.audio_generator import generate_music_wav


class AudioDurationTests(unittest.TestCase):
    def test_requested_duration_is_reflected_in_wav_header(self):
        audio = generate_music_wav(120, duration_seconds=2)
        with wave.open(io.BytesIO(audio), "rb") as wav:
            self.assertEqual(wav.getnframes(), 2 * wav.getframerate())
            self.assertAlmostEqual(wav.getnframes() / wav.getframerate(), 2.0, places=3)

    def test_default_duration_remains_thirty_seconds_for_api_contract(self):
        # The generator's historical default remains long; the HTTP handler
        # supplies its own 30-second default to keep direct callers compatible.
        audio = generate_music_wav(120, duration_seconds=1)
        with wave.open(io.BytesIO(audio), "rb") as wav:
            self.assertEqual(wav.getframerate(), 22050)

    def test_audio_handler_accepts_duration_and_returns_wav(self):
        with tempfile.TemporaryDirectory() as data_dir:
            server = MusicCompanionServer(("127.0.0.1", 0), data_dir=data_dir)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
                body = json.dumps({"target_bpm": 120, "duration_seconds": 2}).encode()
                connection.request("POST", "/api/generate-audio", body, {"Content-Type": "application/json"})
                response = connection.getresponse()
                payload = response.read()
                connection.close()
                self.assertEqual(response.status, 200)
                with wave.open(io.BytesIO(payload), "rb") as wav:
                    self.assertAlmostEqual(wav.getnframes() / wav.getframerate(), 2.0, places=3)
            finally:
                server.shutdown()
                server.server_close()

    def test_audio_handler_rejects_duration_outside_api_limit(self):
        with tempfile.TemporaryDirectory() as data_dir:
            server = MusicCompanionServer(("127.0.0.1", 0), data_dir=data_dir)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
                body = json.dumps({"target_bpm": 120, "duration_seconds": 1801}).encode()
                connection.request("POST", "/api/generate-audio", body, {"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(response.status, 400)
                self.assertIn("1800", response.read().decode("utf-8"))
                connection.close()
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
