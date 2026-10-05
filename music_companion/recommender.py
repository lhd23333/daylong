"""Turn schedule, mood, and exercise needs into a readable music recommendation.

The module has two layers:
1. ``build_profile`` normalizes free-form user input into a small context model.
2. ``recommend`` applies deterministic local rules, then optionally asks an
   AI client to refine the result. If no AI client is configured or the AI
   call fails, the local result is returned unchanged with an explicit
   ``source`` and ``fallback_reason`` so the frontend can explain the mode.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .gait import build_walking_guidance


class RecommendationError(ValueError):
    """Raised when the submitted input cannot produce a recommendation."""


@dataclass(frozen=True)
class UserContext:
    schedule: str = ""
    mood: str = ""
    exercise: str = ""
    notes: str = ""
    scene: str = "walking"
    height_cm: float | None = None
    weight_kg: float | None = None
    leg_length_cm: float | None = None
    preferred_style: str = ""
    vocal_mode: str = "auto"
    plan_context: dict[str, Any] | None = None
    # Optional manual single-step measurement; this is not a two-step gait stride.
    step_length_cm: float | None = None
    measured_step_length_cm: float | None = None
    target_speed_kmh: float | None = None


@dataclass(frozen=True)
class Profile:
    mood_state: str
    energy_level: str
    time_context: str
    activity: str
    goal: str
    scene_mode: str
    step_frequency: float | None
    uses_body_metrics: bool = False
    music_style: str = ""
    instruments: tuple[str, ...] = ()
    vocal_mode: str = "无"
    lyric_theme: str = ""
    intensity_label: str = "中等"
    walking_guidance: dict[str, Any] | None = None


_MOOD_STATES = {
    "焦虑": ("焦虑", "中高", "降低压力并稳定呼吸"),
    "疲惫": ("疲惫", "低", "温和恢复精力"),
    "开心": ("开心", "高", "保持愉悦和动力"),
    "低落": ("低落", "低", "温和陪伴并逐步提振"),
    "专注": ("专注", "中", "保持稳定专注"),
    "平静": ("平静", "中低", "维持放松与平稳"),
    "其他": ("待确认", "中", "找到适合当前状态的声音"),
}

_MOOD_KEYWORDS = [
    ("焦虑", ("焦虑", "紧张", "压力", "担心", "烦", "焦躁", "anxious", "stressed", "worried")),
    ("疲惫", ("疲惫", "累", "困", "没精神", "tired", "exhausted", "sleepy")),
    ("开心", ("开心", "高兴", "兴奋", "期待", "积极", "happy", "excited", "upbeat")),
    ("低落", ("难过", "低落", "沮丧", "伤心", "sad", "down", "depressed")),
    ("专注", ("专注", "学习", "工作", "集中", "focused", "focus")),
    ("平静", ("平静", "放松", "淡定", "calm", "relaxed")),
]

_TIME_KEYWORDS = [
    ("早晨", ("早上", "早晨", "起床", "清晨", "morning")),
    ("午后", ("中午", "午休", "下午", "午后", "afternoon")),
    ("晚间", ("今晚", "晚上", "夜间", "睡前", "夜晚", "深夜", "night")),
]

_SCHEDULE_ACTIVITIES = [
    ("专注工作/学习", ("工作", "学习", "复习", "写报告", "会议", "代码", "阅读", "work", "study")),
    ("通勤", ("通勤", "地铁", "公交", "开车", "路上", "commute")),
    ("放松/助眠", ("睡觉", "休息", "放松", "冥想", "睡前", "小憩", "sleep", "rest", "meditat")),
    ("聚会/休闲", ("聚会", "做饭", "整理", "散步", "逛街", "party", "cook", "clean")),
]

_PRESSURE_KEYWORDS = ("截止", "赶", "马上", "着急", "deadline", "urgent", "asap")

_EXERCISE_RULES = [
    ("慢跑", 148, 165, "高", ("跑步", "慢跑", "jog", "run")),
    ("快走", 118, 132, "中", ("快走", "健走", "brisk walk", "walk")),
    ("瑜伽/拉伸", 62, 82, "低", ("瑜伽", "拉伸", "冥想", "yoga", "stretch")),
    ("力量训练", 138, 158, "高", ("力量", "举铁", "深蹲", "hiit", "strength", "weight")),
    ("骑行/单车", 128, 148, "中高", ("骑行", "单车", "自行车", "cycling", "bike")),
    ("热身", 100, 120, "中", ("热身", "warmup", "warm up")),
    ("放松/冷却", 70, 92, "低", ("放松", "冷却", "拉伸", "cooldown", "cool down")),
]

_WALKING_SCENE_ACTIVITIES = {"快走", "步行/轻运动", "自定义运动", "热身", "放松/冷却"}

_GENRES_BY_ENERGY = {
    "低": ["lo-fi", "ambient", "acoustic", "轻音乐", "钢琴"],
    "中低": ["city pop", "soft rock", "民谣", "轻电子"],
    "中": ["indie pop", "R&B", "流行", "合成器流行"],
    "中高": ["流行摇滚", "电子流行", "funk", "house"],
    "高": ["电子", "摇滚", "drum & bass", "hardstyle"],
}


def _clean(value: str) -> str:
    return value.strip().lower()


def _match_keyword(text: str, groups: list[tuple[str, tuple[str, ...]]]) -> str | None:
    normalized = _clean(text)
    if not normalized:
        return None
    for label, keywords in groups:
        if any(keyword in normalized for keyword in keywords):
            return label
    return None


def _detect_mood(context: UserContext) -> str:
    selected = _clean(context.mood)
    if selected and selected in _MOOD_STATES:
        return selected
    if selected and selected != "其他":
        detected = _match_keyword(selected, _MOOD_KEYWORDS)
        if detected:
            return detected

    for text in (context.notes, context.schedule, context.exercise):
        detected = _match_keyword(text, _MOOD_KEYWORDS)
        if detected:
            return detected
    return "平静"


def _detect_time_context(context: UserContext) -> str:
    for text in (context.schedule, context.notes, context.exercise):
        detected = _match_keyword(text, _TIME_KEYWORDS)
        if detected:
            return detected
    return "未明确时间段"


def _detect_schedule_activity(context: UserContext) -> tuple[str, bool]:
    schedule_text = f"{context.schedule} {context.notes}"
    activity = _match_keyword(schedule_text, _SCHEDULE_ACTIVITIES)
    under_pressure = any(
        keyword in _clean(schedule_text) for keyword in _PRESSURE_KEYWORDS
    )
    return activity or "日常", under_pressure


def _detect_exercise(context: UserContext) -> tuple[str, int, int, str] | None:
    exercise_text = f"{context.exercise} {context.notes}"
    if not _clean(context.exercise):
        return None
    for activity, bpm_min, bpm_max, energy, keywords in _EXERCISE_RULES:
        if any(keyword in _clean(exercise_text) for keyword in keywords):
            return activity, bpm_min, bpm_max, energy
    return "自定义运动", 108, 132, "中"


def _scene_mode(context: UserContext) -> str:
    normalized = _clean(context.scene)
    if not normalized and any(
        value is not None
        for value in (
            context.target_speed_kmh,
            context.step_length_cm,
            context.measured_step_length_cm,
        )
    ):
        # Gait-specific fields are an explicit signal for the walking scene;
        # ordinary mood/schedule requests keep the historical smart mode.
        return "行走/轻运动"
    if normalized in {"sedentary", "sit", "study", "office", "久坐", "自习", "办公"}:
        return "久坐自习/办公"
    if normalized in {"walking", "walk", "move", "行走", "轻运动", "步行"}:
        return "行走/轻运动"
    return "智能适配"


def _estimate_step_frequency(context: UserContext, scene_mode: str) -> float | None:
    if scene_mode != "行走/轻运动":
        return None

    if context.leg_length_cm is not None:
        base = 104.0 + (context.leg_length_cm - 70.0) * 0.24
    elif context.height_cm is not None:
        base = 96.0 + (context.height_cm - 150.0) * 0.20
    else:
        base = 112.0

    if context.weight_kg is not None:
        base -= max(0.0, (context.weight_kg - 65.0) * 0.05)

    return round(max(96.0, min(138.0, base)))


def _intensity_label(scene_mode: str, energy_level: str) -> str:
    if scene_mode == "久坐自习/办公":
        return "放松"
    if energy_level in {"高", "中高"}:
        return "活力"
    if energy_level == "低":
        return "放松"
    return "中等"


def _resolve_music_style(
    context: UserContext,
    profile_energy: str,
    scene_mode: str,
) -> str:
    preferred = _clean(context.preferred_style)
    if preferred:
        return preferred
    if scene_mode == "久坐自习/办公":
        return "治愈纯音乐 / ambient"
    if profile_energy in {"高", "中高"}:
        return "电子流行 / indie pop"
    if profile_energy == "低":
        return "轻电子 / lo-fi"
    return "轻快流行 / light pop"


def _instruments_for_intensity(
    intensity_label: str,
    music_style: str = "",
) -> tuple[str, ...]:
    style = _clean(music_style)
    if any(keyword in style for keyword in ("电子", "合成器", "electronic", "synth")):
        return ("合成器", "鼓组", "贝斯", "节奏吉他")
    if any(keyword in style for keyword in ("钢琴", "治愈", "ambient", "piano")):
        return ("钢琴", "弦乐", "pad", "轻打击")
    if any(keyword in style for keyword in ("民谣", "木吉他", "acoustic", "guitar")):
        return ("木吉他", "电钢琴", "轻鼓", "贝斯")
    if intensity_label == "放松":
        return ("钢琴", "弦乐", "pad", "轻打击")
    if intensity_label == "活力":
        return ("合成器", "鼓组", "贝斯", "节奏吉他")
    return ("木吉他", "电钢琴", "轻鼓", "贝斯")


def _resolve_vocal_mode(context: UserContext, scene_mode: str) -> str:
    selected = _clean(context.vocal_mode)
    if selected in {"none", "off", "无", "纯音乐", "纯器乐"}:
        return "无"
    if selected in {"light", "hum", "轻", "轻度", "哼唱"}:
        return "轻度人声 / 哼唱"
    if selected in {"full", "on", "有", "演唱"}:
        return "演唱人声"
    if scene_mode == "久坐自习/办公":
        return "无"
    return "轻度人声 / 哼唱"


def _resolve_lyric_theme(scene_mode: str, mood_state: str) -> str:
    if scene_mode == "久坐自习/办公":
        return "放松休憩 / 呼吸"
    if mood_state in {"焦虑", "疲惫"}:
        return "放下压力 / 稳步向前"
    if mood_state in {"开心", "专注"}:
        return "步履 / 活力 / 向前"
    return "行走 / 陪伴 / 轻松"


def _build_lyrics(profile: Profile) -> tuple[str, ...]:
    if profile.scene_mode == "久坐自习/办公":
        return (
            "慢慢呼吸，放下今天的重量",
            "让肩颈松一点，让思绪停一停",
            "给自己几分钟，只属于此刻",
            "再回到书桌前，继续前行",
        )
    return (
        "一步一步，跟着节拍走",
        "让风穿过，让呼吸更自由",
        "不必追赶，保持自己的节奏",
        "今天的路，慢慢走也会到达",
    )


def build_profile(context: UserContext) -> Profile:
    """Normalize raw user text into a compact, human-readable profile."""
    mood_state, mood_energy, goal = _MOOD_STATES[_detect_mood(context)]
    time_context = _detect_time_context(context)
    schedule_activity, _under_pressure = _detect_schedule_activity(context)
    exercise = _detect_exercise(context)
    scene_mode = _scene_mode(context)
    fallback_frequency = _estimate_step_frequency(context, scene_mode)
    walking_guidance = None
    if scene_mode == "行走/轻运动":
        walking_guidance = build_walking_guidance(
            height_cm=context.height_cm,
            leg_length_cm=context.leg_length_cm,
            step_length_cm=context.step_length_cm,
            measured_step_length_cm=context.measured_step_length_cm,
            target_speed_kmh=context.target_speed_kmh,
            fallback_cadence=fallback_frequency or 112,
        )
        step_frequency = walking_guidance["cadence"]
    else:
        step_frequency = fallback_frequency
    uses_body_metrics = any(
        value is not None
        for value in (context.height_cm, context.weight_kg, context.leg_length_cm)
    )

    if scene_mode == "久坐自习/办公":
        activity = "久坐自习/办公"
        energy_level = "低"
        goal = "低干扰休息并恢复专注"
    elif exercise:
        activity, _, _, _ = exercise
        energy_level = exercise[3]
    elif scene_mode == "行走/轻运动":
        activity = "步行/轻运动"
        energy_level = "中"
    else:
        activity = schedule_activity
        if mood_state in {"疲惫", "低落"}:
            energy_level = "低"
        elif mood_state == "开心":
            energy_level = "中高"
        elif mood_state == "焦虑":
            energy_level = "中"
        elif mood_state == "专注":
            energy_level = "中"
        else:
            energy_level = mood_energy if mood_energy in _GENRES_BY_ENERGY else "中"

    intensity_label = _intensity_label(scene_mode, energy_level)
    music_style = _resolve_music_style(context, energy_level, scene_mode)
    instruments = _instruments_for_intensity(intensity_label, music_style)
    vocal_mode = _resolve_vocal_mode(context, scene_mode)
    lyric_theme = _resolve_lyric_theme(scene_mode, mood_state)

    return Profile(
        mood_state=mood_state,
        energy_level=energy_level,
        time_context=time_context,
        activity=activity,
        goal=goal,
        scene_mode=scene_mode,
        step_frequency=step_frequency,
        uses_body_metrics=uses_body_metrics,
        music_style=music_style,
        instruments=instruments,
        vocal_mode=vocal_mode,
        lyric_theme=lyric_theme,
        intensity_label=intensity_label,
        walking_guidance=walking_guidance,
    )


def _tempo_label(bpm: float) -> str:
    if bpm < 60:
        return "慢板"
    if bpm < 90:
        return "中慢板"
    if bpm < 120:
        return "中板"
    if bpm < 168:
        return "快板"
    return "急板"


def _genres_for_energy(energy: str) -> list[str]:
    return _GENRES_BY_ENERGY.get(energy, _GENRES_BY_ENERGY["中"])


def _adjust_for_context(
    bpm_min: int,
    bpm_max: int,
    profile: Profile,
    has_exercise: bool,
) -> tuple[int, int]:
    """Apply small deterministic adjustments based on mood and time context."""
    if has_exercise:
        if profile.mood_state in {"焦虑", "疲惫", "低落"}:
            bpm_min = max(40, bpm_min - 8)
            bpm_max = max(50, bpm_max - 6)
        elif profile.mood_state == "开心":
            bpm_min = min(210, bpm_min + 4)
            bpm_max = min(220, bpm_max + 4)
        elif profile.mood_state == "专注":
            bpm_min = min(210, bpm_min + 2)
            bpm_max = min(220, bpm_max + 2)
        return bpm_min, bpm_max

    if profile.mood_state == "焦虑":
        bpm_min = min(bpm_min, 100)
        bpm_max = min(bpm_max, 118)
    elif profile.mood_state in {"疲惫", "低落"}:
        bpm_min = min(bpm_min, 82)
        bpm_max = min(bpm_max, 104)
    elif profile.mood_state == "开心":
        bpm_min = max(bpm_min, 112)
        bpm_max = max(bpm_max, 128)

    if not has_exercise and profile.time_context == "晚间":
        bpm_min = min(bpm_min, 72)
        bpm_max = min(bpm_max, 96)
    return bpm_min, bpm_max


def _build_local_recommendation(context: UserContext, profile: Profile) -> dict[str, Any]:
    exercise = _detect_exercise(context)
    has_exercise = exercise is not None
    if profile.scene_mode == "久坐自习/办公":
        activity = "久坐自习/办公"
        bpm_min, bpm_max, energy = 60, 78, "低"
        has_exercise = False
    elif exercise and profile.scene_mode == "行走/轻运动" and exercise[0] in _WALKING_SCENE_ACTIVITIES:
        activity = exercise[0]
        step_frequency = int(profile.step_frequency or 112)
        bpm_min = max(90, step_frequency - 8)
        bpm_max = min(140, step_frequency + 8)
        energy = exercise[3]
    elif exercise:
        activity, bpm_min, bpm_max, energy = exercise
    elif profile.scene_mode == "行走/轻运动":
        activity = "步行/轻运动"
        step_frequency = int(profile.step_frequency or 112)
        bpm_min = max(90, step_frequency - 8)
        bpm_max = min(140, step_frequency + 8)
        energy = "中"
    else:
        activity = profile.activity
        if profile.mood_state in {"疲惫", "低落"}:
            bpm_min, bpm_max, energy = 70, 100, "低"
        elif profile.mood_state == "开心":
            bpm_min, bpm_max, energy = 112, 134, "中高"
        elif profile.mood_state == "专注":
            bpm_min, bpm_max, energy = 92, 118, "中"
        elif profile.mood_state == "焦虑":
            bpm_min, bpm_max, energy = 82, 110, "中低"
        else:
            bpm_min, bpm_max, energy = 88, 118, "中"

    walking_guidance = profile.walking_guidance
    plan_context = context.plan_context or {}
    planned_bpm = plan_context.get("target_bpm")
    if planned_bpm is not None:
        try:
            planned_bpm = int(planned_bpm)
        except (TypeError, ValueError):
            planned_bpm = None
    if planned_bpm is not None and 40 <= planned_bpm <= 220:
        bpm_min = max(40, planned_bpm - 8)
        bpm_max = min(220, planned_bpm + 8)

    bpm_min, bpm_max = _adjust_for_context(
        int(bpm_min), int(bpm_max), profile, has_exercise
    )
    if walking_guidance is not None:
        gait_tempo = int(round(walking_guidance["cadence"]))
        bpm_min = max(80, gait_tempo - 8)
        bpm_max = min(140, gait_tempo + 8)
    bpm_min = max(40, min(bpm_min, 210))
    bpm_max = max(bpm_min, min(bpm_max, 220))
    if planned_bpm is not None and 40 <= planned_bpm <= 220:
        target_bpm = planned_bpm
    elif (
        profile.scene_mode == "行走/轻运动"
        and activity in _WALKING_SCENE_ACTIVITIES
        and profile.step_frequency is not None
    ):
        target_bpm = int(profile.step_frequency)
    else:
        target_bpm = round((bpm_min + bpm_max) / 2)
    # Context adjustments may narrow a range around a planned tempo. Keep the
    # emitted target internally consistent instead of claiming an out-of-range BPM.
    target_bpm = max(bpm_min, min(bpm_max, int(target_bpm)))
    genres = _genres_for_energy(profile.energy_level if has_exercise else energy)
    beat_interval_ms = round(60000 / target_bpm)
    label = _tempo_label(target_bpm)
    lyrics = _build_lyrics(profile)
    plan_reason = str(plan_context.get("reason") or "").strip()
    rationale = (
        f"你当前被识别为“{profile.mood_state}”，主要场景是“{activity}”。"
        f"建议将节拍控制在 {bpm_min}–{bpm_max} BPM，中心值约 {target_bpm} BPM"
        f"（{label}），采用“{profile.music_style}”风格，主要乐器为"
        f"{('、'.join(profile.instruments))}，人声模式为“{profile.vocal_mode}”，"
        f"歌词主题围绕“{profile.lyric_theme}”，这样更容易{profile.goal}。"
    )
    if plan_reason:
        rationale += f" 本次音乐来自日程休息计划：{plan_reason}。"
    playlist_prompt = (
        f"为“{activity}”准备一份约 30 分钟、{target_bpm} BPM 左右的播放列表，"
        f"风格可偏向 {('、'.join(genres))}，重点使用"
        f"{('、'.join(profile.instruments))}。"
    )
    return {
        "profile": {
            "mood_state": profile.mood_state,
            "energy_level": profile.energy_level,
            "time_context": profile.time_context,
            "activity": activity,
            "goal": profile.goal,
            "scene_mode": profile.scene_mode,
            "step_frequency": profile.step_frequency,
            "uses_body_metrics": profile.uses_body_metrics,
            "music_style": profile.music_style,
            "instruments": list(profile.instruments),
            "vocal_mode": profile.vocal_mode,
            "lyric_theme": profile.lyric_theme,
            "intensity_label": profile.intensity_label,
            "lyrics": list(lyrics),
            "walking_guidance": walking_guidance,
        },
        "recommendation": {
            "bpm_min": bpm_min,
            "bpm_max": bpm_max,
            "target_bpm": target_bpm,
            "tempo_label": label,
            "beat_interval_ms": beat_interval_ms,
            "genres": genres,
            "rationale": rationale,
            "playlist_prompt": playlist_prompt,
            "music_style": profile.music_style,
            "instruments": list(profile.instruments),
            "vocal_mode": profile.vocal_mode,
            "lyric_theme": profile.lyric_theme,
            "lyrics": list(lyrics),
            "walking_guidance": walking_guidance,
        },
        "source": "local",
        "fallback_reason": None,
    }


def _validate_payload(payload: Any) -> UserContext:
    if not isinstance(payload, Mapping):
        raise RecommendationError("请求内容必须是 JSON 对象")
    schedule = str(payload.get("schedule") or "").strip()
    mood = str(payload.get("mood") or "").strip()
    exercise = str(payload.get("exercise") or "").strip()
    notes = str(payload.get("notes") or "").strip()
    plan_context = payload.get("plan_context")
    if plan_context is not None and not isinstance(plan_context, dict):
        raise RecommendationError("plan_context 必须是 JSON 对象")
    scene = str(payload.get("scene") or "").strip()
    preferred_style = str(payload.get("preferred_style") or "").strip()
    vocal_mode = str(payload.get("vocal_mode") or "auto").strip()
    if not any([schedule, mood, exercise, notes, preferred_style,
                payload.get("height_cm"), payload.get("leg_length_cm"),
                payload.get("step_length_cm"), payload.get("measured_step_length_cm"),
                payload.get("target_speed_kmh")]):
        raise RecommendationError("请至少填写日程、心情、运动需求或音乐风格偏好中的一项")

    height_cm = _parse_optional_metric(payload.get("height_cm"), 80.0, 250.0, "身高")
    weight_kg = _parse_optional_metric(payload.get("weight_kg"), 20.0, 300.0, "体重")
    leg_length_cm = _parse_optional_metric(
        payload.get("leg_length_cm"), 30.0, 160.0, "腿长"
    )
    step_length_cm = _parse_optional_metric(
        payload.get("step_length_cm"), 20.0, 200.0, "单步长度"
    )
    measured_step_length_cm = _parse_optional_metric(
        payload.get("measured_step_length_cm"), 20.0, 200.0, "实测单步长度"
    )
    target_speed_kmh = _parse_optional_metric(
        payload.get("target_speed_kmh"), 1.0, 7.0, "目标速度"
    )
    return UserContext(
        schedule=schedule,
        mood=mood,
        exercise=exercise,
        notes=notes,
        scene=scene,
        height_cm=height_cm,
        weight_kg=weight_kg,
        leg_length_cm=leg_length_cm,
        step_length_cm=step_length_cm,
        measured_step_length_cm=measured_step_length_cm,
        target_speed_kmh=target_speed_kmh,
        preferred_style=preferred_style,
        vocal_mode=vocal_mode,
        plan_context=plan_context,
    )


def _parse_optional_metric(
    value: Any,
    minimum: float,
    maximum: float,
    label: str,
) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        number = float(text)
    except (TypeError, ValueError) as exc:
        raise RecommendationError(f"{label}必须是数字") from exc
    if not (minimum <= number <= maximum):
        raise RecommendationError(f"{label}需在 {minimum:g} 到 {maximum:g} 之间")
    return number


def recommend(
    payload: Mapping[str, Any] | None,
    ai_client: Any | None = None,
) -> dict[str, Any]:
    """Validate input, run local rules, and optionally refine with AI."""
    context = _validate_payload(payload)
    profile = build_profile(context)
    local_result = _build_local_recommendation(context, profile)

    if ai_client is None or not ai_client.is_configured():
        local_result["fallback_reason"] = "未配置 AI 接口，已使用本地规则引擎。"
        return local_result

    try:
        ai_result = ai_client.recommend(context, profile, local_result)
        ai_result["source"] = "ai"
        ai_result["fallback_reason"] = None
        return ai_result
    except Exception as exc:  # noqa: BLE001 - deliberate fallback boundary
        local_result["fallback_reason"] = (
            f"AI 接口调用失败，已回退到本地规则引擎：{type(exc).__name__}"
        )
        return local_result
