"""Optional OpenAI-compatible AI refinement for the music companion.

This module intentionally uses only the standard library so the project has no
dependency beyond Python 3.10+. The caller should still use local rules as the
source of truth; AI output is merged into the local schema and any failure is
handled by ``recommend``.
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


class AIRecommenderError(RuntimeError):
    """Raised when the AI request or response cannot be used safely."""


def _tempo_label(bpm: int) -> str:
    if bpm < 60:
        return "慢板"
    if bpm < 90:
        return "中慢板"
    if bpm < 120:
        return "中板"
    if bpm < 168:
        return "快板"
    return "急板"


def _build_ssl_context() -> ssl.SSLContext:
    """Return a verifiable SSL context, preferring the macOS system bundle."""
    candidates = (
        "/etc/ssl/cert.pem",
        "/private/etc/ssl/cert.pem",
    )
    for candidate in candidates:
        if Path(candidate).is_file():
            return ssl.create_default_context(cafile=candidate)
    return ssl.create_default_context()


def _load_env_file(path: Path | None = None) -> None:
    """Load a minimal .env file without adding third-party dependencies."""
    env_path = path or Path(__file__).resolve().parent.parent / ".env"
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


_load_env_file()


class AIRecommender:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float = 8.0,
    ) -> None:
        self.api_key = (api_key or os.getenv("OPENAI_API_KEY", "")).strip()
        self.base_url = (
            base_url or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
        ).rstrip("/")
        self.model = model or os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        self.timeout = timeout
        self.force_off = os.getenv("MUSIC_COMPANION_AI", "").strip().lower() in {
            "off",
            "false",
            "0",
            "mock",
            "local",
        }

    def is_configured(self) -> bool:
        return bool(self.api_key) and not self.force_off

    def recommend(self, context: Any, profile: Any, local_result: dict[str, Any]) -> dict[str, Any]:
        if not self.is_configured():
            raise AIRecommenderError("AI interface is not configured")

        system_prompt = (
            "你是「朝夕」——一个陪伴用户过完一天的助手。请根据用户的日程、心情和运动需求，输出 JSON。"
            "只返回 JSON 对象，不要输出 Markdown 或解释。字段必须包含："
            "profile.mood_state, profile.energy_level, profile.time_context, "
            "profile.activity, profile.goal, profile.scene_mode, "
                "profile.step_frequency, profile.uses_body_metrics, "
                "profile.walking_guidance, "
            "profile.music_style, profile.instruments, profile.vocal_mode, "
            "profile.lyric_theme, profile.intensity_label, profile.lyrics, "
            "recommendation.bpm_min, "
            "recommendation.bpm_max, recommendation.target_bpm, "
            "recommendation.tempo_label, recommendation.beat_interval_ms, "
            "recommendation.genres, recommendation.rationale, "
            "recommendation.playlist_prompt, recommendation.music_style, "
            "recommendation.instruments, recommendation.vocal_mode, "
            "recommendation.lyric_theme, recommendation.lyrics。"
            "BPM 必须为数字且处于 40 到 220 之间，genres 必须是字符串数组。"
        )
        user_payload = {
            "input": {
                "schedule": getattr(context, "schedule", ""),
                "mood": getattr(context, "mood", ""),
                "exercise": getattr(context, "exercise", ""),
                "notes": getattr(context, "notes", ""),
                "scene": getattr(context, "scene", "walking"),
                "height_cm": getattr(context, "height_cm", None),
                "weight_kg": getattr(context, "weight_kg", None),
                "leg_length_cm": getattr(context, "leg_length_cm", None),
                "step_length_cm": getattr(context, "step_length_cm", None),
                "measured_step_length_cm": getattr(context, "measured_step_length_cm", None),
                "target_speed_kmh": getattr(context, "target_speed_kmh", None),
                "preferred_style": getattr(context, "preferred_style", ""),
                "vocal_mode": getattr(context, "vocal_mode", "auto"),
            },
            "local_profile": profile.__dict__ if hasattr(profile, "__dict__") else profile,
            "local_recommendation": local_result.get("recommendation", {}),
        }
        request_payload: dict[str, Any] = {
            "model": self.model,
            "temperature": 0.3,
            "max_tokens": 700,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
        }
        if "minimax" in self.base_url.lower():
            # MiniMax M3 can output reasoning inside `content` by default.
            # Disabling it keeps the response closer to plain JSON and avoids
            # parsing failures in the MVP's strict-merge path.
            request_payload["thinking"] = {"type": "disabled"}

        parsed = self._chat(request_payload)
        return _merge_with_local(local_result, parsed)

    def care(self, context_summary: Any, card: Any) -> dict[str, Any]:
        """让 AI 重写关怀卡的三句话。返回值为候选结果，合格与否由
        ``companion._care_from_ai`` 判定；这里只负责发出请求并解析 JSON。

        与 :meth:`recommend` 的区别是它**不**做本地合并：关怀语只有措辞，
        没有可校验的数值，硬合并没有意义。失败一律抛出 ``AIRecommenderError``，
        由 ``compose_care_with_ai`` 兜底成本地结果。
        """
        if not self.is_configured():
            raise AIRecommenderError("AI interface is not configured")

        system_prompt = (
            "你在为一个作息陪伴应用写三句中文关怀语。用户会给你今天的客观事实"
            "（日程、空档、睡眠、精力、压力、当前时刻）。你要写的不是鼓励，"
            "而是像一个了解他今天安排的人在旁边顺口说的一句话。"
            "只返回 JSON 对象，不要 Markdown、不要解释。字段必须是："
            "greeting、detail、suggestion、tone。"
            "greeting 一句话，不超过 28 个字；detail 是依据，要落到具体数字，"
            "不超过 34 个字；suggestion 是一句可以马上做的事，不超过 24 个字。"
            "tone 只能取 bright / steady / gentle / quiet 之一。"
            "禁止使用「加油」「你可以的」「相信你」「元气满满」这类空话，"
            "禁止感叹号，禁止 emoji。句子里不要出现花括号和占位符。"
        )
        user_payload = {
            "facts": dict(context_summary or {}),
            "local_card": dict(card or {}),
        }
        request_payload: dict[str, Any] = {
            "model": self.model,
            "temperature": 0.7,
            "max_tokens": 300,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
        }
        if "minimax" in self.base_url.lower():
            request_payload["thinking"] = {"type": "disabled"}
        return self._chat(request_payload)

    def understand(
        self,
        text: str,
        *,
        now_iso: str,
        local: dict[str, Any],
        catalog: str,
        history: Any = (),
    ) -> dict[str, Any]:
        """把用户随手写的一段话读成结构化的一天（见 ``music_companion.agent``）。

        与 :meth:`care` 一样只负责发出请求并解析 JSON，返回值合不合法由
        ``agent._merge_ai`` 逐字段判定；不合法就退回本地规则的结果。时间只要
        ``HH:MM``——ISO8601 由 Python 按用户时区拼，模型不碰。
        """
        if not self.is_configured():
            raise AIRecommenderError("AI interface is not configured")

        system_prompt = (
            "你是「朝夕」，一个陪用户过完一天的助手。用户会用随便什么写法说今天怎么过："
            "几点上课、想不想跑步、累不累、爱听什么。你把这段话读成一条时间轴和一整天的音乐。"
            "只返回 JSON 对象，不要 Markdown、不要解释。字段必须是 reply、events、music、state、preferences。"
            "reply：一句回话，不超过 50 个字。像熟人说话，不要感叹号、不要 emoji、"
            "不要「作为你的助手」这类套话，也不要把用户说的话原样复述一遍。"
            "events：数组，元素是 {title, start, end, category, priority}。"
            "title 不超过 12 个字，去掉「我要」「打算」这类词；"
            "start/end 用 24 小时制的 \"HH:MM\"，不要写日期、不要写时区；"
            "没给结束时间就按事情本身的长度补（一节课 45 分钟、自习 1 小时、吃饭 40 分钟）；"
            "category 只能是 study / work / commute / exercise / break / other；"
            "priority 只能是 hard / soft，上课、考试、会议是 hard，其余是 soft；"
            "最多 12 条，按时间先后排。没有具体时间的事不要编时间，宁可少排一条。"
            "music：{soundscape, drums, bpm, why}。soundscape 只能从下面的目录里选 id；"
            "drums 只能是 none / light / standard / strong；bpm 必须是所选音景 BPM 区间内的整数；"
            "why 一句话说明为什么挑它，不超过 30 个字。"
            "state：{energy, stress}，0 到 100 的整数，只填用户真的说出来的"
            "（说了「今天很累」就把 energy 填 25 上下），没说就用 null 或省略。"
            "preferences：{likes, avoids}，两个字符串数组，只说用户提到的乐器、风格、讨厌的东西，"
            "没提到就留空数组。\n\n"
            f"可选音景目录（id=名称 | 时段 | 心情 | 场景 | BPM 区间 | 性格）：\n{catalog}"
        )
        messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
        for turn in list(history)[-6:]:
            if not isinstance(turn, dict):
                continue
            content = str(turn.get("text") or "").strip()[:400]
            if not content:
                continue
            role = "assistant" if str(turn.get("role")) == "agent" else "user"
            messages.append({"role": role, "content": content})
        messages.append({
            "role": "user",
            "content": (
                f"现在是 {now_iso}。用户说：{text}\n\n"
                # 本地正则的草稿只作参考：它常常漏掉句子之间的关系，但「同一句话在
                # 两条通道下读出来差不多」对用户是件好事（配不配密钥都像同一个产品）。
                f"本地规则的草稿（仅供参考，可以推翻）：{json.dumps(_draft_hint(local), ensure_ascii=False)}"
            ),
        })
        request_payload: dict[str, Any] = {
            "model": self.model,
            "temperature": 0.4,
            "max_tokens": 900,
            "response_format": {"type": "json_object"},
            "messages": messages,
        }
        if "minimax" in self.base_url.lower():
            # 与 recommend 同因：M3 默认会把思考过程写进 content，这里要的是纯 JSON。
            request_payload["thinking"] = {"type": "disabled"}
        return self._chat(request_payload)

    def _chat(self, request_payload: dict[str, Any]) -> dict[str, Any]:
        """发一次 OpenAI 兼容的 chat/completions 并解析出 JSON 对象。"""
        request_body = json.dumps(request_payload, ensure_ascii=False).encode("utf-8")

        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=request_body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=self.timeout,
                context=_build_ssl_context(),
            ) as response:
                raw_response = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise AIRecommenderError(f"AI HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise AIRecommenderError(f"AI network error: {exc}") from exc

        try:
            content = raw_response["choices"][0]["message"]["content"]
            return _parse_json_content(content)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise AIRecommenderError(f"AI response is not valid JSON: {exc}") from exc


def _draft_hint(local: dict[str, Any]) -> dict[str, Any]:
    """把本地结果压成给模型看的参考草稿：只留标题、时刻和音乐，不留措辞。"""
    events = []
    for item in (local or {}).get("events", [])[:12]:
        events.append({
            "title": item.get("title"),
            "start": str(item.get("start") or "")[11:16],
            "end": str(item.get("end") or "")[11:16],
            "category": item.get("category"),
        })
    music = (local or {}).get("music") or {}
    return {
        "events": events,
        "music": {key: music.get(key) for key in ("soundscape", "drums", "bpm")},
    }


def _parse_json_content(content: Any) -> dict[str, Any]:
    if isinstance(content, dict):
        return content
    text = str(content).strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    # Some reasoning models prefix the final JSON with a think block.
    for marker in ("</think>", "<|/thinking|>", "<｜end▁of▁thinking｜>"):
        if marker in text:
            text = text.split(marker, 1)[1].strip()
            break
    return json.loads(text)


def _merge_with_local(local: dict[str, Any], ai: dict[str, Any]) -> dict[str, Any]:
    local_profile = local.get("profile", {})
    local_recommendation = local.get("recommendation", {})
    ai_profile = ai.get("profile") or {}
    ai_recommendation = ai.get("recommendation") or {}

    merged_profile = {
        "mood_state": str(ai_profile.get("mood_state", local_profile.get("mood_state", ""))),
        "energy_level": str(ai_profile.get("energy_level", local_profile.get("energy_level", ""))),
        "time_context": str(ai_profile.get("time_context", local_profile.get("time_context", ""))),
        "activity": str(ai_profile.get("activity", local_profile.get("activity", ""))),
        "goal": str(ai_profile.get("goal", local_profile.get("goal", ""))),
        "scene_mode": str(
            ai_profile.get("scene_mode", local_profile.get("scene_mode", "行走/轻运动"))
        ),
        "step_frequency": _safe_optional_number(
            ai_profile.get("step_frequency", local_profile.get("step_frequency"))
        ),
        "uses_body_metrics": _safe_bool(
            ai_profile.get(
                "uses_body_metrics", local_profile.get("uses_body_metrics", False)
            )
        ),
        "walking_guidance": local_profile.get("walking_guidance"),
        "music_style": str(
            ai_profile.get("music_style", local_profile.get("music_style", ""))
        ),
        "instruments": _safe_string_list(
            ai_profile.get("instruments", local_profile.get("instruments", []))
        ),
        "vocal_mode": str(
            ai_profile.get("vocal_mode", local_profile.get("vocal_mode", "无"))
        ),
        "lyric_theme": str(
            ai_profile.get("lyric_theme", local_profile.get("lyric_theme", ""))
        ),
        "intensity_label": str(
            ai_profile.get(
                "intensity_label", local_profile.get("intensity_label", "中等")
            )
        ),
        "lyrics": _safe_string_list(
            ai_profile.get("lyrics", local_profile.get("lyrics", []))
        ),
    }

    bpm_min = _safe_int(ai_recommendation.get("bpm_min", local_recommendation.get("bpm_min")), 88)
    bpm_max = _safe_int(ai_recommendation.get("bpm_max", local_recommendation.get("bpm_max")), 118)
    target_bpm = _safe_int(
        ai_recommendation.get("target_bpm", local_recommendation.get("target_bpm")),
        round((bpm_min + bpm_max) / 2),
    )
    if not (
        40 <= bpm_min <= bpm_max <= 220
        and bpm_min <= target_bpm <= bpm_max
    ):
        raise AIRecommenderError("AI returned an invalid BPM range")

    walking_guidance = local_recommendation.get("walking_guidance")
    if isinstance(walking_guidance, dict) and walking_guidance.get("cadence") is not None:
        # Gait-derived tempo is deterministic: AI may refine wording, not the
        # one-beat-per-step BPM or its dependent interval.
        bpm_min = _safe_int(local_recommendation.get("bpm_min"), bpm_min)
        bpm_max = _safe_int(local_recommendation.get("bpm_max"), bpm_max)
        gait_target = _safe_int(walking_guidance["cadence"], target_bpm)
        # Local recommender keeps this range around the gait cadence; retain
        # that range rather than letting an AI-proposed range hide the formula.
        if not (40 <= bpm_min <= bpm_max <= 220):
            raise AIRecommenderError("Local gait BPM range is invalid")
        target_bpm = max(bpm_min, min(bpm_max, gait_target))

    genres = ai_recommendation.get("genres", local_recommendation.get("genres", []))
    if not isinstance(genres, list) or not genres:
        genres = local_recommendation.get("genres", [])

    merged_recommendation = {
        "bpm_min": bpm_min,
        "bpm_max": bpm_max,
        "target_bpm": target_bpm,
        "tempo_label": _tempo_label(target_bpm),
        "beat_interval_ms": round(60000 / target_bpm),
        "genres": [str(item) for item in genres[:8]],
        "rationale": str(
            ai_recommendation.get(
                "rationale", local_recommendation.get("rationale", "")
            )
        ),
        "playlist_prompt": str(
            ai_recommendation.get(
                "playlist_prompt", local_recommendation.get("playlist_prompt", "")
            )
        ),
        "music_style": str(
            ai_recommendation.get(
                "music_style", local_recommendation.get("music_style", "")
            )
        ),
        "instruments": _safe_string_list(
            ai_recommendation.get(
                "instruments", local_recommendation.get("instruments", [])
            )
        ),
        "vocal_mode": str(
            ai_recommendation.get(
                "vocal_mode", local_recommendation.get("vocal_mode", "无")
            )
        ),
        "lyric_theme": str(
            ai_recommendation.get(
                "lyric_theme", local_recommendation.get("lyric_theme", "")
            )
        ),
        "lyrics": _safe_string_list(
            ai_recommendation.get("lyrics", local_recommendation.get("lyrics", []))
        ),
        "walking_guidance": walking_guidance,
    }
    return {
        "profile": merged_profile,
        "recommendation": merged_recommendation,
        "source": "ai",
        "fallback_reason": None,
    }


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_optional_number(value: Any) -> int | None:
    if value in (None, "", "None", "null"):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes", "y"}


def _safe_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value[:10]]
