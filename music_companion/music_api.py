"""ElevenLabs Music API client.

The web server keeps the local synthesizer as a deterministic fallback.  This
module only handles the optional remote provider and deliberately uses the
Python standard library so the classroom demo has no extra dependency.
"""

from __future__ import annotations

import json
import math
import os
import ssl
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping


class MusicAPIError(RuntimeError):
    """Raised when the remote music provider cannot return usable audio."""


def _build_ssl_context() -> ssl.SSLContext:
    """Create a certificate-verifying SSL context."""
    candidates = ("/etc/ssl/cert.pem", "/private/etc/ssl/cert.pem")
    for candidate in candidates:
        if Path(candidate).is_file():
            return ssl.create_default_context(cafile=candidate)
    return ssl.create_default_context()


def _load_env_file() -> None:
    """Load a minimal project ``.env`` without replacing existing variables."""
    path = Path(__file__).resolve().parent.parent / ".env"
    if not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


_load_env_file()


class ElevenLabsMusicClient:
    """Small synchronous client for ElevenLabs' ``/music`` endpoint."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model_id: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self.api_key = (
            api_key
            if api_key is not None
            else os.getenv("ELEVENLABS_API_KEY", "")
        ).strip()
        self.base_url = (
            base_url or os.getenv("ELEVENLABS_BASE_URL", "https://api.elevenlabs.io/v1")
        ).rstrip("/")
        self.model_id = model_id or os.getenv("ELEVENLABS_MODEL_ID", "music_v2_5")
        if timeout is None:
            raw_timeout = os.getenv("ELEVENLABS_TIMEOUT", "30")
            try:
                timeout = float(raw_timeout)
            except (TypeError, ValueError):
                timeout = 30.0
        self.timeout = float(timeout)

    @staticmethod
    def build_prompt(recommendation: Mapping[str, Any] | None) -> str:
        """Turn safe, structured recommendation fields into a music prompt.

        The prompt intentionally asks for an original instrumental rather than
        naming an artist or requesting imitation.  The local gait calculation
        remains the source of truth for BPM; the provider receives it only as a
        compositional target.
        """
        values = recommendation or {}
        bpm = values.get("target_bpm", values.get("bpm", 120))
        style = str(values.get("music_style") or values.get("style") or "轻快、舒缓")
        instruments = values.get("instruments") or []
        if not isinstance(instruments, (list, tuple)):
            instruments = [instruments]
        instruments_text = "、".join(str(item) for item in instruments if str(item).strip())
        genres = values.get("genres") or []
        if not isinstance(genres, (list, tuple)):
            genres = [genres]
        genres_text = "、".join(str(item) for item in genres if str(item).strip())
        scene = str(values.get("scene") or values.get("scene_mode") or "专注与散步")
        vocal_mode = str(values.get("vocal_mode") or "纯音乐")
        rationale = str(values.get("rationale") or "")

        parts = [
            f"Original instrumental music around {bpm} BPM.",
            f"Style: {style}.",
            f"Scene: {scene}.",
            "Use a steady, comfortable pulse suitable for walking or focused rest.",
            f"Vocal mode: {vocal_mode}. No lyrics and no imitation of any existing artist or recording.",
        ]
        if instruments_text:
            parts.append(f"Instruments: {instruments_text}.")
        if genres_text:
            parts.append(f"Genre colors: {genres_text}.")
        if rationale:
            # Rationale is optional context; cap it so arbitrary user text does
            # not become an unbounded provider prompt.
            parts.append(f"Mood guidance: {rationale[:240]}.")
        return " ".join(parts)

    def generate(
        self,
        prompt: str,
        duration_seconds: float,
        force_instrumental: bool = True,
    ) -> dict[str, Any]:
        """Generate audio and return bytes plus provider metadata."""
        if not self.api_key:
            raise MusicAPIError("ElevenLabs API key is not configured")
        try:
            duration = float(duration_seconds)
        except (TypeError, ValueError) as exc:
            raise MusicAPIError("音乐时长必须是数字") from exc
        if not math.isfinite(duration) or not 3 <= duration <= 600:
            raise MusicAPIError("ElevenLabs 音乐时长必须在 3 到 600 秒之间")
        if not isinstance(prompt, str) or not prompt.strip():
            raise MusicAPIError("音乐 prompt 不能为空")
        if not self.base_url:
            raise MusicAPIError("ElevenLabs base URL is not configured")
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise MusicAPIError("ElevenLabs timeout 无效")

        body = json.dumps(
            {
                "prompt": prompt,
                "music_length_ms": int(round(duration * 1000)),
                "model_id": self.model_id,
                "force_instrumental": bool(force_instrumental),
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/music",
            data=body,
            method="POST",
            headers={
                "xi-api-key": self.api_key,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            },
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=self.timeout,
                context=_build_ssl_context(),
            ) as response:
                audio_bytes = response.read()
                content_type = response.getheader("Content-Type", "")
                song_id = response.getheader("song-id")
                if not song_id:
                    song_id = response.getheader("x-song-id")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise MusicAPIError(f"ElevenLabs HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise MusicAPIError(f"ElevenLabs network error: {exc}") from exc

        normalized_content_type = str(content_type or "").split(";", 1)[0].strip().lower()
        if not normalized_content_type.startswith("audio/"):
            raise MusicAPIError("ElevenLabs response is not audio")
        if not audio_bytes:
            raise MusicAPIError("ElevenLabs returned empty audio")
        return {
            "audio_bytes": audio_bytes,
            "content_type": str(content_type).strip() or "audio/mpeg",
            "song_id": song_id,
            "model_id": self.model_id,
            "provider": "elevenlabs",
        }
