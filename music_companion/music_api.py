"""远端音乐生成客户端。

本地的程序化合成器永远是主线：它确定、免费、离线可用，而且「同一个配方
永远得到同一段音乐」正是收藏功能的前提。这个模块只负责**可选的**远端通路，
用在「想听一段这个提示词对应的真·AI 生成音频」的场景。

这里刻意只依赖标准库：比赛现场那台教室机器上不能要求装 SDK。

目前两个实现：

- ``ElevenLabsMusicClient``：一次 POST 同步返回 mp3，最简单，但需要海外网络。
- ``TemPolorMusicClient``（天谱乐）：中国大陆可直连，异步任务——提交拿 id、
  轮询拿下载地址、再把音频取回来。

两者对上层暴露同一个 ``generate(prompt, duration_seconds, force_instrumental)
-> {audio_bytes, content_type, provider, ...}``，``server.py`` 不关心用的是哪家。
"""

from __future__ import annotations

import json
import math
import os
import ssl
import time
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


def build_music_prompt(recommendation: Mapping[str, Any] | None) -> str:
    """把结构化的推荐结果翻成一段音乐提示词。

    提示词刻意要求「原创纯音乐」，不点名任何艺人和具体作品：既不给自己惹
    版权麻烦，也让远端模型只能靠风格/乐器/场景去理解。BPM 以本地步态计算
    出来的值为准，远端只把它当作作曲目标。

    写成英文是因为主流音乐模型的提示词语料以英文为主（天谱乐官方示例也是
    英文提示词），不是崇洋——换中文反而更容易被模型忽略细节。
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
        # Rationale 是可选的补充语境；截断它，免得用户的自由文本变成一段
        # 无边界的提示词。
        parts.append(f"Mood guidance: {rationale[:240]}.")
    return " ".join(parts)


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

    # 保留这个名字：老的调用点和单测走的是 `ElevenLabsMusicClient.build_prompt`，
    # 而提示词的构造其实和具体厂商无关，已经提到模块级 `build_music_prompt`。
    build_prompt = staticmethod(build_music_prompt)

    def health(self) -> dict[str, Any]:
        """供 /api/health 报告这条通路的配置状态。不含密钥本身。"""
        return {
            "provider": "elevenlabs",
            "configured": bool(self.api_key),
            "base_url": self.base_url,
            "model": self.model_id,
        }

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


def _post_json(
    url: str,
    payload: Mapping[str, Any],
    headers: Mapping[str, str],
    timeout: float,
) -> dict[str, Any]:
    """POST 一段 JSON，返回解析后的对象。错误统一翻成 MusicAPIError。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST", headers=dict(headers))
    try:
        with urllib.request.urlopen(
            request, timeout=timeout, context=_build_ssl_context()
        ) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise MusicAPIError(f"HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise MusicAPIError(f"网络错误: {exc}") from exc
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MusicAPIError("远端返回的不是 JSON") from exc
    if not isinstance(parsed, dict):
        raise MusicAPIError("远端返回的 JSON 结构异常")
    return parsed


class TemPolorMusicClient:
    """天谱乐（TemPolor）音乐生成客户端——中国大陆可直连的付费通路。

    和 ElevenLabs 最大的不同：这是**异步任务**，不是一次请求就出音频。
    完整链路是「提交 → 轮询 → 下载」，所以这个类里有一个小状态机。

    接口事实（2026-10-05 逐条核对官方文档 platform.tianpuyue.cn）：

    - 调用域名 ``https://api.tianpuyue.cn``（海外节点 api.tempolor.com，
      除域名外用法一致）。
    - 鉴权走 header ``Authorization: <api_key>``，**不加 ``Bearer`` 前缀**。
      真实的 key 形如 ``Tempo-****-3w``。
    - ``POST /open-apis/v1/song/generate``，body 里 ``model`` / ``prompt`` /
      ``callback_url`` 三个是必填；``instrumental: true`` 才是纯音乐。
      返回 ``{code, message, request_id, data: {item_ids: [...]}}``。
    - ``POST /open-apis/v1/song/query``，body ``{item_ids: [...]}``。官方建议
      提交后先等约 20 秒，再以约 2 秒的间隔轮询（查询接口有并发限制）。
    - 纯音乐模型 30 创作点（约 ¥0.3）/首，最长 270 秒。

    ``callback_url`` 是必填但**这里用不上**：回调要求一个公网可达的地址，
    而这个应用跑在 127.0.0.1 上。官方同时提供了上面那个查询接口专门给轮询用，
    所以我们传一个占位地址、走轮询。这个占位值从 ``TEMPOLOR_CALLBACK_URL``
    读，没配就报错——不替用户编一个可能被平台判非法的 URL。
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        callback_url: str | None = None,
        timeout: float | None = None,
        poll_interval: float | None = None,
        poll_timeout: float | None = None,
    ) -> None:
        self.api_key = (
            api_key if api_key is not None else os.getenv("TEMPOLOR_API_KEY", "")
        ).strip()
        env_base = (
            base_url
            or os.getenv("TEMPOLOR_BASE_URL", "https://api.tianpuyue.cn")
        )
        self.base_url = str(env_base).rstrip("/")
        self.model = model or os.getenv("TEMPOLOR_MODEL", "tempolor-latest")
        self.callback_url = (
            callback_url if callback_url is not None else os.getenv("TEMPOLOR_CALLBACK_URL", "")
        ).strip()
        self.timeout = _env_float(timeout, "TEMPOLOR_TIMEOUT", 30.0)
        # 官方建议「提交后先等约 20s，再以约 2s 间隔轮询」。
        self.poll_interval = max(0.2, _env_float(poll_interval, "TEMPOLOR_POLL_INTERVAL", 2.0))
        self.poll_timeout = _env_float(poll_timeout, "TEMPOLOR_POLL_TIMEOUT", 180.0)

    # 提示词构造与厂商无关，和 ElevenLabs 共用一份。
    build_prompt = staticmethod(build_music_prompt)

    def health(self) -> dict[str, Any]:
        return {
            "configured": bool(self.api_key and self.callback_url),
            "provider": "tempolor",
            "base_url": self.base_url,
            "model": self.model,
        }

    def generate(
        self,
        prompt: str,
        duration_seconds: float,
        force_instrumental: bool = True,
    ) -> dict[str, Any]:
        """提交一次生成任务，等它完成，把音频字节取回来。"""
        if not self.api_key:
            raise MusicAPIError("天谱乐 API Key 未配置（TEMPOLOR_API_KEY）")
        if not self.callback_url:
            # 平台的 callback_url 是必填项。我们不用它（走轮询），但仍要给一个值，
            # 而且不能替用户瞎编——被平台判成非法 URL 时报错会很难查。
            raise MusicAPIError(
                "天谱乐要求必填 callback_url。请把 TEMPOLOR_CALLBACK_URL 设成"
                "任意你控制的 https 地址（本应用用轮询取结果，不会真的回调它）"
            )
        if not isinstance(prompt, str) or not prompt.strip():
            raise MusicAPIError("音乐 prompt 不能为空")
        try:
            duration = float(duration_seconds)
        except (TypeError, ValueError) as exc:
            raise MusicAPIError("音乐时长必须是数字") from exc
        if not math.isfinite(duration) or duration <= 0:
            raise MusicAPIError("音乐时长必须是正数")
        # 天谱乐纯音乐最长 270 秒。超了不静默截断：截出来的不是用户要的那段。
        if duration > 270:
            raise MusicAPIError("天谱乐纯音乐最长 270 秒")

        item_ids = self._submit(prompt, duration, force_instrumental)
        song = self._await_song(item_ids)
        audio_url = str(song.get("audio_hi_url") or song.get("audio_url") or "").strip()
        if not audio_url:
            raise MusicAPIError("天谱乐任务完成但没有返回音频地址")
        audio_bytes, content_type = self._download(audio_url)
        return {
            "audio_bytes": audio_bytes,
            "content_type": content_type,
            "song_id": str(song.get("item_id") or (item_ids[0] if item_ids else "")),
            "model_id": self.model,
            "provider": "tempolor",
            "title": str(song.get("title") or ""),
            "duration": song.get("duration"),
        }

    # ── 内部：三步链路 ────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        return {
            # 注意：天谱乐不吃 `Bearer` 前缀，原样发 key。
            "Authorization": self.api_key,
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
        }

    def _submit(self, prompt: str, duration: float, instrumental: bool) -> list[str]:
        # 天谱乐的 body 里没有「时长」这个字段，长度由模型和提示词决定。
        # 与其假装精确控制，不如把目标时长写进提示词，让模型去理解。
        target = f"Target length: about {int(round(duration))} seconds. "
        payload = {
            "model": self.model,
            "prompt": target + prompt,
            "callback_url": self.callback_url,
            "instrumental": bool(instrumental),
        }
        data = _post_json(
            f"{self.base_url}/open-apis/v1/song/generate",
            payload,
            self._headers(),
            self.timeout,
        )
        _raise_for_code(data, "天谱乐提交任务")
        body = data.get("data")
        item_ids = body.get("item_ids") if isinstance(body, Mapping) else None
        if not isinstance(item_ids, list) or not item_ids:
            raise MusicAPIError("天谱乐提交成功但没有返回 item_ids")
        return [str(item) for item in item_ids]

    def _await_song(self, item_ids: list[str]) -> dict[str, Any]:
        """轮询直到有一首成功。

        官方建议先等约 20 秒再开始轮询（查询接口有并发限制），所以第一次查询
        放在一个睡眠之后。
        """
        deadline = time.monotonic() + self.poll_timeout
        first = True
        while True:
            if first:
                # 官方建议提交后先等约 20 秒再查。这个等待按轮询间隔的十倍算，
                # 上限 20 秒：默认间隔 2 秒时正好落在建议值上，而把间隔调小
                # （测试里就是这么做的）整条链路也跟着快起来，不用另开一个
                # 「测试模式」开关。同时不许睡穿总预算。
                time.sleep(max(0.0, min(20.0, self.poll_interval * 10, self.poll_timeout - 1.0)))
                first = False
            elif time.monotonic() >= deadline:
                raise MusicAPIError(
                    f"天谱乐任务等待超时（{self.poll_timeout:.0f} 秒），可以稍后重试"
                )
            else:
                time.sleep(self.poll_interval)

            data = _post_json(
                f"{self.base_url}/open-apis/v1/song/query",
                {"item_ids": item_ids},
                self._headers(),
                self.timeout,
            )
            _raise_for_code(data, "天谱乐查询任务")
            for song in _songs_of(data):
                status = str(song.get("status") or "").strip().lower()
                if status in {"succeeded", "success", "completed", "done"}:
                    return song
                if status in {"failed", "error", "canceled", "cancelled"}:
                    raise MusicAPIError(
                        f"天谱乐任务失败：{song.get('message') or song.get('msg') or status}"
                    )
            if time.monotonic() >= deadline:
                raise MusicAPIError(
                    f"天谱乐任务等待超时（{self.poll_timeout:.0f} 秒），可以稍后重试"
                )

    def _download(self, url: str) -> tuple[bytes, str]:
        if not url.lower().startswith(("http://", "https://")):
            raise MusicAPIError("天谱乐返回的音频地址不是 http(s)")
        request = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout, context=_build_ssl_context()
            ) as response:
                audio_bytes = response.read()
                content_type = response.getheader("Content-Type", "")
        except urllib.error.HTTPError as exc:
            raise MusicAPIError(f"下载天谱乐音频失败：HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise MusicAPIError(f"下载天谱乐音频失败：{exc}") from exc
        if not audio_bytes:
            raise MusicAPIError("天谱乐返回了空音频")
        normalized = str(content_type or "").split(";", 1)[0].strip().lower()
        if not normalized.startswith("audio/"):
            # 有些对象存储不返 Content-Type。按魔数兜底，别因为一个响应头
            # 就把已经下载好的音频丢掉。
            normalized = _guess_audio_type(audio_bytes)
        return audio_bytes, normalized


def _guess_audio_type(data: bytes) -> str:
    if data[:3] == b"ID3" or data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "audio/mpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav"
    if data[:4] == b"fLaC":
        return "audio/flac"
    if data[:4] == b"OggS":
        return "audio/ogg"
    return "audio/mpeg"


def _songs_of(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """从查询/回调响应里取出歌曲数组。

    文档里查询接口的响应结构没有给出完整示例，但回调接口给的是
    ``{"songs": [{...}]}``，两者共用同一套 song 对象。这里对两种包法都认，
    结构对不上就当作「还没有结果」继续轮询，而不是直接崩掉。
    """
    data: Any = payload.get("data", payload)
    if isinstance(data, Mapping):
        for key in ("songs", "items", "list", "results"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return []
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def _raise_for_code(payload: Mapping[str, Any], action: str) -> None:
    """天谱乐用 ``code`` 表达业务错误。0/200 之外一律当失败。

    ``code`` 缺失时不拦——错误码的取值集合会变，而真正决定成败的是后面
    有没有拿到 item_ids / audio_url，那两处各自有明确的报错。
    """
    if "code" not in payload:
        return
    code = payload.get("code")
    if code in (0, 200, "0", "200", None):
        return
    message = payload.get("message") or payload.get("msg") or ""
    raise MusicAPIError(f"{action}失败：{code} {message}".strip())


def _env_float(explicit: float | None, name: str, default: float) -> float:
    if explicit is not None:
        try:
            value = float(explicit)
        except (TypeError, ValueError):
            return default
        return value if math.isfinite(value) and value > 0 else default
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) and value > 0 else default


def create_music_client(provider: str | None = None):
    """按 ``MUSIC_PROVIDER`` 选一个远端音乐客户端。

    默认 ``elevenlabs``：它一次请求就返回音频，链路最短，出事的地方最少。
    ``tempolor`` 是给「必须中国大陆直连」的场合准备的备选。
    """
    name = (provider or os.getenv("MUSIC_PROVIDER") or "elevenlabs").strip().lower()
    if name in {"tempolor", "tianpuyue", "天谱乐"}:
        return TemPolorMusicClient()
    if name in {"elevenlabs", "11labs"}:
        return ElevenLabsMusicClient()
    raise MusicAPIError(f"未知的 MUSIC_PROVIDER：{name}（可选 elevenlabs / tempolor）")
