"""音景目录与 BPM 决策。

音景（soundscape）是前端的音乐素材包：和弦进行、音色、节奏型都实现于
``static/js/soundscapes.js``。后端不做音频合成，只决定两件事：

1. 用哪个音景 id（:func:`select_soundscape`）；
2. 这个音景此刻该给多少 BPM（:func:`bpm_for`）。

两条决策都是纯函数且确定性的：同样的输入永远得到同样的输出。表格里的
``when`` / ``moods`` / ``scenes`` / ``bpm_range`` / ``character`` 与产品
清单逐字对应，改动前请先确认前端已经实现同名 id。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# 时段标签（'' 表示无法判断，不参与打分）。
TIME_TAGS = ("早晨", "上午", "午后", "晚间", "夜间", "全天")
# 心情标签。
MOOD_TAGS = ("平静", "专注", "开心", "兴奋", "低落", "焦虑", "疲惫")
# 场景标签。
SCENE_TAGS = ("久坐", "行走", "通勤", "通用")
# 精力四档标签。
ENERGY_TAGS = ("低", "中", "中高", "高")

# 打分权重：时段 > 心情 = 场景 > 精力。
SCORE_TIME = 3
SCORE_MOOD = 2
SCORE_SCENE = 2
SCORE_ENERGY = 1

# 压力高线（与 planner._bpm 的 65 保持一致）与精力低线。
STRESS_HIGH = 65.0
ENERGY_LOW = 35.0


@dataclass(frozen=True)
class Soundscape:
    """一个音景素材包的元信息。

    ``energies`` 由 ``bpm_range`` 推导（见 :func:`_energies_for`），不单独填写，
    避免同一份事实出现两处来源。``when`` / ``moods`` / ``scenes`` 里的标签都是
    中文，和前端 ``soundscapes.js`` 保持一致。
    """

    id: str
    name: str
    when: tuple[str, ...]
    moods: tuple[str, ...]
    scenes: tuple[str, ...]
    bpm_range: tuple[int, int]
    character: str
    energies: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # 目录里的标签必须落在声明的词表内，写错一个标签在 import 时就会报错。
        for field_name, allowed in (
            ("when", TIME_TAGS),
            ("moods", MOOD_TAGS),
            ("scenes", SCENE_TAGS),
            ("energies", ENERGY_TAGS),
        ):
            unknown = sorted(set(getattr(self, field_name)) - set(allowed))
            if unknown:
                raise ValueError(f"音景 {self.id} 的 {field_name} 含未知标签：{'、'.join(unknown)}")
        if len(self.bpm_range) != 2 or not 30 <= self.bpm_range[0] <= self.bpm_range[1] <= 220:
            raise ValueError(f"音景 {self.id} 的 BPM 区间无效")

    def to_dict(self) -> dict[str, Any]:
        """按 ``GET /api/soundscapes`` 的约定输出目录项。"""
        return {
            "id": self.id,
            "name": self.name,
            "when": list(self.when),
            "moods": list(self.moods),
            "bpm_range": [self.bpm_range[0], self.bpm_range[1]],
            "character": self.character,
        }


def _energies_for(bpm_min: int, bpm_max: int) -> tuple[str, ...]:
    """从中值速度推导适用的精力档位，保证与 BPM 区间同源。"""
    middle = (bpm_min + bpm_max) / 2
    if middle < 70:
        return ("低", "中")
    if middle < 90:
        return ("中",)
    if middle < 110:
        return ("中高",)
    return ("高", "中高")


def _scape(
    soundscape_id: str,
    name: str,
    when: tuple[str, ...],
    moods: tuple[str, ...],
    scenes: tuple[str, ...],
    bpm_min: int,
    bpm_max: int,
    character: str,
) -> Soundscape:
    """构造音景，并补齐由 BPM 区间推导出的精力档位。"""
    if bpm_min > bpm_max:
        raise ValueError(f"音景 {soundscape_id} 的 BPM 区间无效")
    return Soundscape(
        id=soundscape_id,
        name=name,
        when=when,
        moods=moods,
        scenes=scenes,
        bpm_range=(bpm_min, bpm_max),
        character=character,
        energies=_energies_for(bpm_min, bpm_max),
    )


SOUNDSCAPES: dict[str, Soundscape] = {
    "first-light": _scape(
        "first-light", "晨光", ("早晨",), ("平静", "专注", "开心"), ("久坐",),
        58, 76, "钢琴与弦乐，慢",
    ),
    "desk-hours": _scape(
        "desk-hours", "书桌前", ("上午", "午后"), ("专注", "平静"), ("久坐",),
        68, 88, "电钢琴与 lo-fi 鼓",
    ),
    "after-rain": _scape(
        "after-rain", "雨后", ("全天",), ("平静", "低落"), ("行走",),
        92, 112, "木吉他拨弦",
    ),
    "breath": _scape(
        "breath", "呼吸", ("全天",), ("焦虑", "疲惫"), ("通用",),
        52, 68, "极简 pad，无鼓",
    ),
    "high-noon": _scape(
        "high-noon", "午后", ("午后",), ("开心", "兴奋"), ("通用",),
        104, 126, "明亮合成器",
    ),
    "night-lamp": _scape(
        "night-lamp", "夜灯", ("晚间",), ("疲惫", "低落"), ("久坐",),
        62, 80, "电钢琴，暗色",
    ),
    "way-home": _scape(
        "way-home", "归途", ("午后", "晚间"), ("开心", "平静"), ("行走", "通勤"),
        108, 128, "city pop",
    ),
    "settling": _scape(
        "settling", "落地", ("晚间", "夜间"), ("平静", "疲惫"), ("通用",),
        54, 70, "温暖低音与钟声",
    ),
}

# 信息不足时的兜底：字典序最小的 id（与 tie-break 规则一致）。
DEFAULT_SOUNDSCAPE = sorted(SOUNDSCAPES)[0]


_TIME_ALIASES = {
    "早晨": "早晨", "早上": "早晨", "清晨": "早晨", "一早": "早晨", "morning": "早晨",
    "上午": "上午", "am": "上午",
    "午后": "午后", "下午": "午后", "中午": "午后", "noon": "午后", "afternoon": "午后",
    "晚间": "晚间", "晚上": "晚间", "傍晚": "晚间", "evening": "晚间",
    "夜间": "夜间", "深夜": "夜间", "夜晚": "夜间", "夜里": "夜间", "night": "夜间",
    "全天": "全天", "整天": "全天", "全天候": "全天", "all": "全天",
}

_MOOD_ALIASES = {
    "平静": "平静", "放松": "平静", "淡定": "平静", "calm": "平静", "relaxed": "平静",
    "专注": "专注", "集中": "专注", "学习": "专注", "工作": "专注", "focus": "专注",
    "开心": "开心", "高兴": "开心", "愉快": "开心", "happy": "开心",
    "兴奋": "兴奋", "激动": "兴奋", "excited": "兴奋",
    "低落": "低落", "难过": "低落", "沮丧": "低落", "伤心": "低落", "sad": "低落",
    "焦虑": "焦虑", "紧张": "焦虑", "压力": "焦虑", "烦": "焦虑", "anxious": "焦虑",
    "疲惫": "疲惫", "累": "疲惫", "困": "疲惫", "没精神": "疲惫", "tired": "疲惫",
}

_SCENE_ALIASES = {
    "久坐": "久坐", "自习": "久坐", "办公": "久坐", "书桌": "久坐", "坐着": "久坐",
    "sit": "久坐", "sedentary": "久坐", "study": "久坐", "office": "久坐",
    "行走": "行走", "步行": "行走", "散步": "行走", "走路": "行走",
    "walk": "行走", "walking": "行走",
    "通勤": "通勤", "地铁": "通勤", "公交": "通勤", "路上": "通勤", "commute": "通勤",
    "通用": "通用", "智能": "通用", "其他": "通用", "any": "通用", "general": "通用",
}

_ENERGY_ALIASES = {
    "低": "低", "偏低": "低", "很差": "低", "low": "低",
    "中": "中", "中低": "中", "一般": "中", "medium": "中", "normal": "中",
    "中高": "中高", "偏高": "中高", "不错": "中高",
    "高": "高", "很高": "高", "high": "高",
}


def _tag(value: object, aliases: dict[str, str]) -> str:
    """把自由文本归一化成目录里的标签，无法判断时返回空串。"""
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if text in aliases:
        return aliases[text]
    for keyword, tag in aliases.items():
        if keyword and keyword in text:
            return tag
    return ""


def _normalize_time(value: object) -> str:
    return _tag(value, _TIME_ALIASES)


def _normalize_mood(value: object) -> str:
    return _tag(value, _MOOD_ALIASES)


def _normalize_scene(value: object) -> str:
    return _tag(value, _SCENE_ALIASES)


def energy_label(value: object) -> str:
    """把精力标签或 0–100 的数值统一成 低/中/中高/高 四档。

    数值分档与 ``planner`` 的判断线一致：``<= 35`` 视为低精力。无法识别时按
    ``中`` 处理，避免因为一个脏字段改变整天的音景。
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
    else:
        text = str(value if value is not None else "").strip().lower()
        if not text:
            return "中"
        if text in _ENERGY_ALIASES:
            return _ENERGY_ALIASES[text]
        try:
            number = float(text)
        except (TypeError, ValueError):
            return "中"
    if number >= 75:
        return "高"
    if number >= 55:
        return "中高"
    # 注意这里是 > 而不是 >=：`<= 35` 算低精力是 planner 和 companion 共用的
    # 那条线，本函数原先把 35 归成「中」，于是精力恰好 35 时会出现「关怀语说
    # 你精力偏低、音乐却按中档选」的自相矛盾。三条线都取 `<= 35`。
    if number > ENERGY_LOW:
        return "中"
    return "低"


def select_soundscape(
    *,
    time_context: str,
    mood: str,
    scene: str,
    energy: str,
    explicit: str | None = None,
) -> str:
    """按打分制挑选音景 id。

    权重为时段 +3、心情 +2、场景 +2、精力 +1；``when`` 里含 ``全天`` 的音景对
    任何时段都计一次时段分。同分时按 id 字典序取第一个，因此同样的输入在任何
    进程、任何机器上都得到同一个结果。

    ``explicit`` 命中已知 id 时直接返回（用户手动指定优先），否则忽略它继续打分。
    """
    if explicit is not None:
        chosen = str(explicit).strip().lower()
        if chosen in SOUNDSCAPES:
            return chosen

    time_tag = _normalize_time(time_context)
    mood_tag = _normalize_mood(mood)
    scene_tag = _normalize_scene(scene)
    energy_tag = energy_label(energy)

    best_id = DEFAULT_SOUNDSCAPE
    best_score = -1
    for soundscape_id in sorted(SOUNDSCAPES):
        scape = SOUNDSCAPES[soundscape_id]
        score = 0
        if time_tag and (time_tag in scape.when or "全天" in scape.when):
            score += SCORE_TIME
        if mood_tag and mood_tag in scape.moods:
            score += SCORE_MOOD
        if scene_tag and scene_tag in scape.scenes:
            score += SCORE_SCENE
        if energy_tag and energy_tag in scape.energies:
            score += SCORE_ENERGY
        if score > best_score:
            best_id, best_score = soundscape_id, score
    return best_id


def bpm_for(
    soundscape_id: str,
    *,
    energy: str,
    stress: float = 0.0,
    mood: str = "",
) -> int:
    """在音景的 BPM 区间内取一个整数速度。

    核心关怀逻辑：压力高（``>= 65``）或精力低时取区间下沿——累了就慢一点。其余
    情况按精力档位取 0.5 / 0.75 位置，再由心情微调半档，最后夹在区间内。返回值
    一定落在该音景的 ``bpm_range`` 里。
    """
    key = str(soundscape_id or "").strip().lower()
    scape = SOUNDSCAPES.get(key)
    if scape is None:
        raise ValueError(f"未知音景：{soundscape_id}")
    low, high = scape.bpm_range
    try:
        stress_value = float(stress)
    except (TypeError, ValueError):
        stress_value = 0.0

    energy_tag = energy_label(energy)
    if energy_tag == "低" or stress_value >= STRESS_HIGH:
        return int(low)
    if energy_tag == "高":
        return int(high)

    position = 0.75 if energy_tag == "中高" else 0.5
    mood_tag = _normalize_mood(mood)
    if mood_tag in {"疲惫", "低落", "焦虑"}:
        position -= 0.25
    elif mood_tag in {"开心", "兴奋"}:
        position += 0.25
    position = min(1.0, max(0.0, position))
    return int(round(low + (high - low) * position))


def soundscape_catalog() -> list[dict[str, Any]]:
    """按 id 字典序返回音景目录，供 ``GET /api/soundscapes`` 使用。"""
    return [SOUNDSCAPES[key].to_dict() for key in sorted(SOUNDSCAPES)]
