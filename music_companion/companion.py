"""关怀语引擎：把一天的日程、状态和空档写成一两句具体的话。

产品定位是「会陪用户过完一天的伙伴」，所以主页那一句话必须**引用具体事实**
（几点、几件、连续多久、有没有空档），语气像安静周到的人，而不是客服或励志博主。

规则：

* 全部文案走 :data:`_TEMPLATES` 模板表，每条分支 3–5 种措辞；
* 选择是确定性的（事实字符串的 SHA-1 取模），同一天同一状态刷新页面不会变，
  而且跨进程稳定——内置 ``hash()`` 受 ``PYTHONHASHSEED`` 影响，不能用；
* 模板里出现的占位符在事实缺失时会整条跳过（见 :func:`_usable`），
  所以「离第一件还有几小时」这类句子不会在事实不存在时变成病句。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping

from .calendar_model import CalendarEvent, validate_events


# 语气标签，前端据此决定视觉风格。
TONES = ("steady", "gentle", "bright", "quiet")

# 文案长度上限（按字符数计）。
GREETING_MAX = 28
DETAIL_MAX = 40
SUGGESTION_MAX = 24

# 禁用词：空洞安慰、客服腔，以及本产品不使用的第二人称敬语。
FORBIDDEN_PHRASES = ("亲爱的", "用户", "让我们一起", "加油", "相信你", "棒棒哒", "您")

# 睡眠不足线（小时）。
SLEEP_SHORT_HOURS = 6.5
# 压力高线（与 soundscape.STRESS_HIGH / planner._bpm 一致）。
STRESS_HIGH = 65.0
# 精力低线（与 planner 的 energy <= 35 一致）。
ENERGY_LOW = 35.0
# 小于这个分钟数的相邻日程视为「挨着」。
TIGHT_GAP_MINUTES = 10
# 一天里空出来的分钟数达到这个量级，才算「今天不赶」。
LOOSE_GAP_MINUTES = 120
# 第一个日程进入这个分钟数内就算「马上开始」。
FIRST_SOON_MINUTES = 60


@dataclass(frozen=True)
class CareCard:
    """主页那张关怀卡片。

    ``greeting`` 是主句（≤28 字），``detail`` 说明依据（≤40 字），
    ``suggestion`` 给一个可执行的小动作（≤24 字），``tone`` 决定视觉风格，
    ``source`` 区分本地规则与 AI 润色。
    """

    greeting: str
    detail: str
    suggestion: str
    tone: str
    source: str = "local"

    def __post_init__(self) -> None:
        if self.tone not in TONES:
            raise ValueError("关怀语气无效")
        if self.source not in {"local", "ai"}:
            raise ValueError("关怀语来源无效")
        if not self.greeting or len(self.greeting) > GREETING_MAX:
            raise ValueError("关怀主句长度必须在 1 到 28 字之间")
        if not self.detail or len(self.detail) > DETAIL_MAX:
            raise ValueError("关怀依据长度必须在 1 到 40 字之间")
        if not self.suggestion or len(self.suggestion) > SUGGESTION_MAX:
            raise ValueError("关怀建议长度必须在 1 到 24 字之间")

    def to_dict(self) -> dict[str, Any]:
        return {
            "greeting": self.greeting,
            "detail": self.detail,
            "suggestion": self.suggestion,
            "tone": self.tone,
            "source": self.source,
        }


# 每条分支：tone + 三个措辞池。占位符用 {fact_name}，事实缺失时该条会被跳过。
_TEMPLATES: dict[str, dict[str, Any]] = {
    "deep_night": {
        "tone": "quiet",
        "greeting": (
            "现在 {now_hm}，先把灯调暗。",
            "{now_hm} 了，今天到这里就够。",
            "这个点还醒着，剩下的交给明天。",
            "夜里 {now_hm}，不用再安排什么。",
        ),
        "detail": (
            "最后一件 {last_end} 结束，已经过去 {since_last}。",
            "今天 {event_count} 件事都过去了，第一件 {first_start} 就开始了。",
            "离第一件还有 {hours_to_first}，先合上电脑。",
            "{event_count} 件事排到今天 {last_end} 才收尾。",
        ),
        "suggestion": (
            "把屏幕亮度调到最低。",
            "写下明天第一件就去睡。",
            "关掉通知，躺下。",
            "别再翻日历了。",
        ),
    },
    "no_events": {
        "tone": "bright",
        "greeting": (
            "今天没有安排，一整天都是你的。",
            "日历是空的，今天没有硬性的事。",
            "今天没有必须做的事。",
            "日历干干净净，一件事都没排。",
        ),
        "detail": (
            "从 {wake_hm} 到 {sleep_hm} 都没有日程。",
            "没有任何硬性日程，也没有要赶的时间。",
            "今天一件安排也没有，时间是整块的。",
            "日历上零条日程，也就不用赶什么。",
        ),
        "suggestion": (
            "挑一件一直想做的事。",
            "出去走一圈，不用带耳机。",
            "给自己留一段无所事事。",
            "想休息就休息，今天不欠谁。",
        ),
    },
    "sleep_debt": {
        "tone": "gentle",
        "greeting": (
            "昨晚睡了 {sleep_hours} 小时，今天慢一点。",
            "睡眠只有 {sleep_hours} 小时，别硬撑。",
            "昨晚睡得少，{phase_label}先挑轻的做。",
            "睡了 {sleep_hours} 小时，别按满格排今天。",
        ),
        "detail": (
            "睡眠 {sleep_hours} 小时，低于 6.5 小时。",
            "今天 {event_count} 件事在排队，睡眠只有 {sleep_hours} 小时。",
            "睡了 {sleep_hours} 小时，第一件 {first_start} 就要开始。",
            "比平时少睡，注意力会先掉下来。",
        ),
        "suggestion": (
            "中午补 20 分钟，别超过半小时。",
            "先喝水，再做第一件。",
            "今天少排一件事也没关系。",
            "累了就停一下，别硬扛。",
        ),
    },
    "high_stress": {
        "tone": "quiet",
        "greeting": (
            "压力 {stress}，今天不用样样做到。",
            "压力偏高，节奏可以放慢一点。",
            "压力到了 {stress}，先稳住呼吸。",
            "指标显示压力在 {stress}，慢一点。",
        ),
        "detail": (
            "压力 {stress}，第一件 {first_start} 开始，{last_end} 收尾。",
            "压力读数 {stress}，超过 65 的提醒线。",
            "{event_count} 件事排在今天，压力读数 {stress}。",
            "压力 {stress}，今天还有 {event_count} 件事。",
        ),
        "suggestion": (
            "先做三次深呼吸再开始。",
            "把清单砍到三件以内。",
            "提前十分钟出门。",
            "一次只做眼前这一件。",
        ),
    },
    "low_energy": {
        "tone": "gentle",
        "greeting": (
            "精力只有 {energy}，今天按低配来。",
            "精力偏低，别按满格排今天。",
            "今天电量不足，先做最要紧的。",
            "精力 {energy}，能省一步是一步。",
        ),
        "detail": (
            "精力 {energy}，今天还有 {event_count} 件事。",
            "精力读数 {energy}，低于 35 的提醒线。",
            "精力 {energy}，第一件 {first_start} 就要开始。",
            "精力 {energy}，休息点已经排进时间轴。",
        ),
        "suggestion": (
            "先喝口水，再动笔。",
            "把不重要的往后放一放。",
            "走两分钟，比硬撑有用。",
            "累了就停一下，没人催。",
        ),
    },
    "first_soon": {
        "tone": "steady",
        "greeting": (
            "还有 {minutes_to_first} 分钟，不用急。",
            "第一件 {first_start} 开始，还剩 {minutes_to_first} 分钟。",
            "快到 {first_start} 了，先静一静。",
            "{minutes_to_first} 分钟后开始第一件。",
        ),
        "detail": (
            "现在 {now_hm}，第一件 {first_start} 开始。",
            "今天 {event_count} 件事，第一件就在 {minutes_to_first} 分钟后。",
            "离第一件还有 {minutes_to_first} 分钟，够喝杯水。",
            "第一件 {first_start} 到 {first_end}，现在就快了。",
        ),
        "suggestion": (
            "先喝口水，把桌面收干净。",
            "打开要用的那一页就好。",
            "提前两分钟坐好。",
            "深呼吸一次再开始。",
        ),
    },
    "tight": {
        "tone": "steady",
        "greeting": (
            "{tight_count} 件事连成一串，中间没有缝。",
            "{tight_start} 起连着几件事，中间没有空档。",
            "日程连成一片，休息得自己找。",
            "今天几件事是挨着来的。",
        ),
        "detail": (
            "{tight_start} 到 {tight_end} 连着 {tight_count} 件事。",
            "相邻两件之间不到 10 分钟，等于没有空档。",
            "{event_count} 件事挤在一起，最长空档只有 {longest_gap_phrase}。",
            "从 {tight_start} 到 {tight_end}，中间没有坐下的缝。",
        ),
        "suggestion": (
            "两件之间站起来两分钟。",
            "把水杯放远，逼自己起身。",
            "下一个整点抬头看远处。",
            "先去接杯水，再回来。",
        ),
    },
    "loose": {
        "tone": "bright",
        "greeting": (
            "今天节奏不快，中间有几个空档。",
            "日程之间留着缝，可以慢慢来。",
            "{free_phrase}是空的，不用赶。",
            "今天不赶，{event_count} 件事之外都是你的。",
        ),
        "detail": (
            "{event_count} 件事之外，还有 {free_phrase}空档。",
            "最长的一段空档 {longest_gap_phrase}。",
            "空档 {longest_gap_phrase}，够出去走一圈。",
            "第一件 {first_start}，最后一件 {last_start} 开始。",
        ),
        "suggestion": (
            "空档里出去走十分钟。",
            "把长空档留给难的那件。",
            "空档别全用来看手机。",
            "留一段什么都不做。",
        ),
    },
    "morning": {
        "tone": "steady",
        "greeting": (
            "早上好，第一件 {first_start} 开始。",
            "今天从 {first_start} 开始，先吃早饭。",
            "清晨这段时间，先给自己十分钟。",
            "{chronotype_phrase}，{phase_label}第一件 {first_start} 开始。",
        ),
        "detail": (
            "第一件 {first_start}，今天一共 {event_count} 件事。",
            "现在 {now_hm}，离第一件还有 {hours_to_first}。",
            "上午的安排从 {first_start} 到 {last_end}。",
            "{event_count} 件事排在今天，{break_phrase}。",
            "今天学习/工作约 {focus_hours} 小时，{break_phrase}。",
        ),
        "suggestion": (
            "先喝水，再开始第一件。",
            "出门前看一眼天气。",
            "把今天的三件事写下来。",
            "早饭别省。",
        ),
    },
    "afternoon": {
        "tone": "steady",
        "greeting": (
            "下午 {now_hm}，下一件 {next_start} 开始。",
            "午后这段时间，安排还在继续。",
            "现在 {now_hm}，离下一件还有 {minutes_to_next} 分钟。",
            "下午还有 {remaining_count} 件事。",
        ),
        "detail": (
            "今天 {event_count} 件事，下一件 {next_start} 开始。",
            "下一件 {next_start} 到 {next_end}，还有 {minutes_to_next} 分钟。",
            "现在 {now_hm}，{break_phrase}。",
            "今天 {event_count} 件事都排完了，剩下的是你的时间。",
        ),
        "suggestion": (
            "下一件开始前站起来一下。",
            "中午没歇就补五分钟。",
            "喝水，然后继续。",
            "把手头这件收个尾。",
        ),
    },
    "evening": {
        "tone": "steady",
        "greeting": (
            "晚上 {now_hm}，今天的事在收尾。",
            "现在 {now_hm}，剩下的慢慢做。",
            "到晚上了，{last_end} 结束是今天最后一件。",
            "晚间这段时间，可以放慢一点。",
        ),
        "detail": (
            "最后一件 {last_end} 结束，已经过去 {since_last}。",
            "今天 {event_count} 件事，最后一件 {last_end} 收尾。",
            "现在 {now_hm}，今天还有 {remaining_count} 件事。",
            "日程排到 {last_end}，之后就是你的时间。",
        ),
        "suggestion": (
            "睡前把明天第一件写下来。",
            "把屏幕调暗一点。",
            "留半小时不看屏幕。",
            "今天到这儿就可以收了。",
        ),
    },
}

_PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]*)\}")
_HM_PATTERN = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class _Facts(dict):
    """缺字段时返回空串，模板里未登记的占位符不会抛异常。"""

    def __missing__(self, key: str) -> str:
        return ""


def _dt(value: Any, label: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip().replace("Z", "+00:00")
        if not text:
            raise ValueError(f"{label} 不能为空")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"{label} 不是合法的 ISO8601 时间") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} 必须包含时区")
    return parsed


def _minutes(start: datetime, end: datetime) -> int:
    return int((end - start).total_seconds() // 60)


def _clip(text: str, limit: int) -> str:
    """压缩空白并按上限裁剪，尽量落在标点处，避免半句话。"""
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    window = cleaned[:limit]
    for mark in ("。", "；", "，", "、"):
        index = window.rfind(mark)
        if index >= limit // 2:
            return window[: index + 1]
    return window


def _contains_forbidden(text: str) -> bool:
    return any(phrase in text for phrase in FORBIDDEN_PHRASES)


def _number_phrase(value: Any) -> str:
    """把数值写成 5.5 / 7 这样的紧凑形式。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    return f"{number:.1f}".rstrip("0").rstrip(".")


def _hours_phrase(minutes: int) -> str:
    if minutes < 60:
        return "不到 1 小时"
    return f"{_number_phrase(round(minutes / 6) / 10)} 小时"


def _minutes_phrase(minutes: int) -> str:
    """问候语里的时长读法：满一小时改用小时。

    「539 分钟是空的」在语法上没错，但没人这样说话；写成「9 小时」才像人写的。
    不足一小时仍旧用分钟——「45 分钟」比「0.8 小时」自然。
    """
    return _hours_phrase(minutes) if minutes >= 60 else f"{minutes} 分钟"


def _since_phrase(minutes: int) -> str:
    if minutes < 30:
        return "不到半小时"
    return _hours_phrase(minutes)


def _phase_of(moment: datetime) -> str:
    hour = moment.hour
    if 5 <= hour < 12:
        return "早晨"
    if 12 <= hour < 18:
        return "午后"
    if 18 <= hour < 23:
        return "晚间"
    return "夜间"


def _normalize_phase(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    # 「上午」（day_plan._time_context 在 9–12 点用的词）必须排在「午」前面：
    # 单字「午」会先命中，把上午归一成午后——正好是相反的一半天。
    # 归到「早晨」是对的，两者在音景目录里同属 morning 段。
    if "夜" in text or "night" in text:
        return "夜间"
    if "晚" in text or "evening" in text:
        return "晚间"
    if "上午" in text or "早" in text or "morning" in text:
        return "早晨"
    if "午" in text or "afternoon" in text or "noon" in text:
        return "午后"
    return ""


def _profile_value(profile: Any, *names: str) -> str:
    for name in names:
        if isinstance(profile, Mapping):
            value = profile.get(name)
        else:
            value = getattr(profile, name, None)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _chronotype_phrase(profile: Any) -> str:
    text = _profile_value(profile, "chronotype", "chrono").lower()
    if text in {"night_owl", "night-owl", "owl", "晚睡", "夜猫子", "夜型"}:
        return "你偏晚睡"
    if text in {"early_bird", "early-bird", "lark", "早起", "早鸟", "晨型"}:
        return "你习惯早起"
    return ""


def _state_number(state: Any, name: str) -> float | None:
    if state is None:
        return None
    value = getattr(state, name, None)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _build_facts(
    *,
    now_dt: datetime,
    event_list: list[CalendarEvent],
    state: Any,
    profile: Any,
    stats: Mapping[str, Any] | None,
    phase: str,
) -> dict[str, str]:
    """把日程与状态整理成模板可直接引用的字符串事实。

    ``free_minutes`` / ``break_count`` 优先取 ``stats``（来自 ``day_plan``，它知
    道整天的窗口）；没有 ``stats`` 时只按相邻日程之间的空档估算。
    """
    facts: dict[str, str] = {
        "now_hm": now_dt.strftime("%H:%M"),
        "phase_label": phase,
        "day_phase": phase,
        "tight_count": "",
        "tight_start": "",
        "tight_end": "",
        "longest_gap_minutes": "",
        "longest_gap_phrase": "",
        "free_minutes": "",
        "free_phrase": "",
        "break_count": "",
        "break_phrase": "",
        "remaining_count": "",
        "minutes_to_first": "",
        "hours_to_first": "",
        "minutes_to_next": "",
        "next_start": "",
        "next_end": "",
        "since_last": "",
        "first_start": "",
        "first_end": "",
        "last_start": "",
        "last_end": "",
        "event_count": str(len(event_list)),
        "sleep_hours": "",
        "stress": "",
        "energy": "",
        "wake_hm": "",
        "sleep_hm": "",
        "chronotype_phrase": "",
        "focus_hours": "",
    }

    if event_list:
        ordered = sorted(event_list, key=lambda item: (item.start_dt, item.end_dt, item.id))
        first, last = ordered[0], ordered[-1]
        facts["first_start"] = first.start_dt.strftime("%H:%M")
        facts["first_end"] = first.end_dt.strftime("%H:%M")
        facts["last_start"] = last.start_dt.strftime("%H:%M")
        facts["last_end"] = last.end_dt.strftime("%H:%M")

        gaps = [
            _minutes(earlier.end_dt, later.start_dt)
            for earlier, later in zip(ordered, ordered[1:])
        ]
        longest_gap = max(gaps) if gaps else 0
        if longest_gap >= 20:
            facts["longest_gap_minutes"] = str(longest_gap)
            facts["longest_gap_phrase"] = _minutes_phrase(int(longest_gap))
        if gaps:
            # 取最长的一段「挨着来」的日程作为文案依据。
            streak: list[CalendarEvent] = [ordered[0]]
            best: list[CalendarEvent] = []
            for previous, current, gap in zip(ordered, ordered[1:], gaps):
                if gap <= TIGHT_GAP_MINUTES:
                    streak.append(current)
                else:
                    if len(streak) > len(best):
                        best = list(streak)
                    streak = [current]
            if len(streak) > len(best):
                best = list(streak)
            if len(best) >= 2:
                facts["tight_count"] = str(len(best))
                facts["tight_start"] = best[0].start_dt.strftime("%H:%M")
                facts["tight_end"] = best[-1].end_dt.strftime("%H:%M")

        if first.start_dt > now_dt:
            minutes = _minutes(now_dt, first.start_dt)
            facts["minutes_to_first"] = str(minutes)
            facts["hours_to_first"] = _hours_phrase(minutes)
        if last.end_dt <= now_dt:
            facts["since_last"] = _since_phrase(_minutes(last.end_dt, now_dt))

        upcoming = [event for event in ordered if event.start_dt > now_dt]
        if upcoming:
            facts["next_start"] = upcoming[0].start_dt.strftime("%H:%M")
            facts["next_end"] = upcoming[0].end_dt.strftime("%H:%M")
            facts["minutes_to_next"] = str(_minutes(now_dt, upcoming[0].start_dt))
        remaining = sum(1 for event in ordered if event.end_dt > now_dt)
        if remaining > 0:
            facts["remaining_count"] = str(remaining)

        focus_minutes = sum(
            _minutes(event.start_dt, event.end_dt)
            for event in ordered
            if event.category in {"study", "work"}
        )
        if focus_minutes:
            facts["focus_hours"] = _number_phrase(round(focus_minutes / 6) / 10)

    free_value = 0
    if stats:
        free_minutes = stats.get("free_minutes")
        if isinstance(free_minutes, (int, float)) and free_minutes > 0:
            free_value = int(free_minutes)
    elif len(event_list) >= 2:
        rough_free = sum(
            gap
            for gap in (
                _minutes(earlier.end_dt, later.start_dt)
                for earlier, later in zip(event_list, event_list[1:])
            )
            if gap >= 5
        )
        if rough_free > 0:
            free_value = int(rough_free)
    if free_value > 0:
        facts["free_minutes"] = str(free_value)
        facts["free_phrase"] = _minutes_phrase(free_value)

    break_count = None
    if stats:
        raw_breaks = stats.get("break_count")
        if isinstance(raw_breaks, int):
            break_count = raw_breaks
    if break_count is not None:
        facts["break_count"] = str(break_count)
        facts["break_phrase"] = (
            f"排了 {break_count} 个休息点" if break_count > 0 else "还没有休息点"
        )

    sleep_hours = _state_number(state, "sleep_hours")
    if sleep_hours is not None:
        facts["sleep_hours"] = _number_phrase(sleep_hours)
    stress = _state_number(state, "stress")
    if stress is not None:
        facts["stress"] = str(int(round(stress)))
    energy = _state_number(state, "energy")
    if energy is not None:
        facts["energy"] = str(int(round(energy)))

    wake = _profile_value(profile, "wake", "wake_time", "wake_up")
    if _HM_PATTERN.match(wake):
        facts["wake_hm"] = wake
    sleep = _profile_value(profile, "sleep", "sleep_time", "bedtime")
    if _HM_PATTERN.match(sleep):
        facts["sleep_hm"] = sleep
    facts["chronotype_phrase"] = _chronotype_phrase(profile)
    return facts


def _select_branch(facts: Mapping[str, str], event_count: int) -> str:
    """按优先级挑分支：先看时间，再看身体状态，最后看日程形状。"""
    phase = facts["phase_label"]
    if phase == "夜间":
        return "deep_night"
    if event_count == 0:
        return "no_events"

    sleep_hours = float(facts["sleep_hours"]) if facts["sleep_hours"] else None
    if sleep_hours is not None and sleep_hours < SLEEP_SHORT_HOURS:
        return "sleep_debt"
    stress = float(facts["stress"]) if facts["stress"] else None
    if stress is not None and stress >= STRESS_HIGH:
        return "high_stress"
    energy = float(facts["energy"]) if facts["energy"] else None
    if energy is not None and energy <= ENERGY_LOW:
        return "low_energy"

    minutes_to_first = int(facts["minutes_to_first"]) if facts["minutes_to_first"] else None
    if phase == "早晨" and minutes_to_first is not None and minutes_to_first < FIRST_SOON_MINUTES:
        return "first_soon"
    if facts["tight_count"]:
        return "tight"
    # 早晨优先给早晨的措辞（先吃早饭、第一件几点），空档多的话术留给午后与晚间。
    free_minutes = int(facts["free_minutes"]) if facts["free_minutes"] else 0
    if phase != "早晨" and free_minutes >= LOOSE_GAP_MINUTES:
        return "loose"
    return {"早晨": "morning", "午后": "afternoon", "晚间": "evening"}.get(phase, "afternoon")


def _tone_for(branch: str, facts: Mapping[str, str]) -> str:
    tone = str(_TEMPLATES[branch]["tone"])
    if branch != "no_events":
        return tone
    # 空日程遇上好状态才是轻快的；状态一般时改成平稳陪伴。
    sleep_hours = float(facts["sleep_hours"]) if facts["sleep_hours"] else None
    stress = float(facts["stress"]) if facts["stress"] else None
    energy = float(facts["energy"]) if facts["energy"] else None
    good = (
        (sleep_hours is None or sleep_hours >= SLEEP_SHORT_HOURS)
        and (stress is None or stress < STRESS_HIGH)
        and (energy is None or energy > ENERGY_LOW)
    )
    return "bright" if good else "steady"


def _usable(options: Iterable[str], facts: Mapping[str, str]) -> list[str]:
    """过滤掉引用了空事实的措辞，避免出现「还有  分钟」这类病句。"""
    usable = [
        option
        for option in options
        if all(str(facts.get(name, "")) not in {"", "None"} for name in _PLACEHOLDER.findall(option))
    ]
    return usable or list(options)


def _stable_index(seed: str, size: int) -> int:
    """用 SHA-1 取模而不是内置 ``hash()``：后者受 PYTHONHASHSEED 影响，跨进程会变。"""
    if size <= 1:
        return 0
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % size


def _render(pool: Iterable[str], facts: Mapping[str, str], seed: str) -> str:
    options = _usable(pool, facts)
    template = options[_stable_index(seed, len(options))]
    return template.format_map(_Facts(facts)).strip()


def compose_care(
    *,
    now: Any,
    events: Iterable[CalendarEvent],
    state: Any = None,
    profile: Any = None,
    stats: Mapping[str, Any] | None = None,
    day_phase: str | None = None,
) -> CareCard:
    """生成一张本地关怀卡。

    ``now`` 必须是带时区的 ISO8601 字符串（或 ``datetime``）；时间段默认由它推导，
    也可以用 ``day_phase`` 显式指定（``早晨/午后/晚间/夜间`` 或英文近义词）。
    措辞由「日期 + 时段 + 分支 + 关键事实」的哈希决定，同一天同一状态保持稳定。
    """
    now_dt = _dt(now, "现在时间")
    event_list = validate_events(events or [])
    phase = _normalize_phase(day_phase) or _phase_of(now_dt)

    facts = _build_facts(
        now_dt=now_dt,
        event_list=event_list,
        state=state,
        profile=profile,
        stats=stats,
        phase=phase,
    )
    branch = _select_branch(facts, len(event_list))
    tone = _tone_for(branch, facts)
    seed = "|".join(
        (
            now_dt.date().isoformat(),
            phase,
            branch,
            tone,
            facts["event_count"],
            facts["first_start"],
            facts["last_end"],
            facts["sleep_hours"],
            facts["stress"],
            facts["energy"],
        )
    )
    pools = _TEMPLATES[branch]
    greeting = _render(pools["greeting"], facts, f"{seed}|greeting")
    detail = _render(pools["detail"], facts, f"{seed}|detail")
    suggestion = _render(pools["suggestion"], facts, f"{seed}|suggestion")
    return CareCard(
        greeting=_clip(greeting, GREETING_MAX),
        detail=_clip(detail, DETAIL_MAX),
        suggestion=_clip(suggestion, SUGGESTION_MAX),
        tone=tone,
        source="local",
    )


def _care_from_ai(raw: Any, fallback: CareCard) -> CareCard | None:
    """把 AI 返回的内容收进 CareCard；不合格就返回 ``None`` 表示丢弃。"""
    if isinstance(raw, CareCard):
        candidate = raw
    elif isinstance(raw, Mapping):
        candidate = CareCard(
            greeting=_clip(str(raw.get("greeting") or ""), GREETING_MAX),
            detail=_clip(str(raw.get("detail") or ""), DETAIL_MAX),
            suggestion=_clip(str(raw.get("suggestion") or ""), SUGGESTION_MAX),
            tone=str(raw.get("tone") or fallback.tone).strip().lower(),
            source="ai",
        )
    else:
        return None
    if candidate.tone not in TONES:
        return None
    texts = (candidate.greeting, candidate.detail, candidate.suggestion)
    if any(not text for text in texts):
        return None
    if any(_contains_forbidden(text) for text in texts):
        return None
    if any(len(text) > limit for text, limit in zip(texts, (GREETING_MAX, DETAIL_MAX, SUGGESTION_MAX))):
        return None
    return candidate


def compose_care_with_ai(
    care_local: CareCard,
    context_summary: Mapping[str, Any],
    ai_client: Any,
) -> CareCard:
    """可选 AI 润色：任何失败都原样返回本地结果，绝不把异常抛给调用方。

    客户端沿用 ``ai_client`` 的约定：有 ``is_configured()`` 且为假时直接跳过；
    润色方法名为 ``care``（或 ``compose_care``），签名 ``(context_summary, card)``。
    目前 :class:`music_companion.ai_client.AIRecommender` 只实现了推荐接口，
    因此默认走本地结果；接入新方法后无需改这里。
    """
    if ai_client is None or not isinstance(care_local, CareCard):
        return care_local
    try:
        configured = getattr(ai_client, "is_configured", None)
        if callable(configured) and not configured():
            return care_local
        polish = getattr(ai_client, "care", None) or getattr(ai_client, "compose_care", None)
        if not callable(polish):
            return care_local
        raw = polish(dict(context_summary or {}), care_local.to_dict())
        polished = _care_from_ai(raw, care_local)
    except Exception:  # noqa: BLE001 - 关怀语是旁路功能，任何异常都不能冒泡
        return care_local
    return polished if polished is not None else care_local
