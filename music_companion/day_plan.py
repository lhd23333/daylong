"""把一天的日程与休息点编排成一条统一时间轴。

设计要点：

* 只读且确定性：同样输入 → 同样输出。休息点 id 用 ``uuid5`` 生成，前端可以拿它
  持久化「已开始 / 已完成」状态（与 ``planner._plan_id`` 同一思路）。
* 绝不跨越 ``priority == "hard" and not interruptible`` 的日程；``soft`` 或可打断
  的日程内部可以安插休息点，用的是 ``planner._subtract_intervals`` 的区间相减思路。
* 时间轴按 ``start`` 排序，同一时刻休息点排在日程前面。
* 日程条目保留原始起止时间；统计与休息点规划按当天窗口 ``[day_start, day_end]``
  裁剪，所以跨出窗口的那部分不计入 ``stats``。

本模块内部 import ``companion`` 取关怀卡；``companion`` 不反向依赖本模块。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping
from uuid import NAMESPACE_URL, uuid5

from .calendar_model import CalendarEvent, find_conflicts, validate_events
from .companion import CareCard, compose_care
from .soundscape import (
    DEFAULT_SOUNDSCAPE,
    STRESS_HIGH,
    bpm_for,
    energy_label,
    select_soundscape,
)
from .state import StatusSnapshot


# 五类休息点：窗口下限、建议时长、强度、提前提醒分钟数。
BREAK_TYPES: dict[str, dict[str, Any]] = {
    "walk": {
        "label": "出去走一圈", "window_minutes": 20, "duration_minutes": 15,
        "intensity": "低", "reminder_minutes_before": 3,
    },
    "stretch": {
        "label": "站起来伸展", "window_minutes": 10, "duration_minutes": 5,
        "intensity": "低", "reminder_minutes_before": 2,
    },
    "breathe": {
        "label": "做几次深呼吸", "window_minutes": 5, "duration_minutes": 3,
        "intensity": "极低", "reminder_minutes_before": 2,
    },
    "eyes": {
        "label": "望向远处", "window_minutes": 3, "duration_minutes": 2,
        "intensity": "极低", "reminder_minutes_before": 1,
    },
    "water": {
        "label": "喝一杯水", "window_minutes": 2, "duration_minutes": 2,
        "intensity": "极低", "reminder_minutes_before": 1,
    },
}

# 空档地板：不到 5 分钟的缝一律不插休息点（这一条压过 eyes / water 各自的 3 / 2 分钟下限）。
MIN_WINDOW_MINUTES = 5
# 长连续学习块内部安插休息点时，末尾留出的缓冲。
_IN_BLOCK_BUFFER_MINUTES = 5
# 空档里安排休息点时，末尾留出的缓冲。
_GAP_BUFFER_MINUTES = 2
# 触发休息的连续学习/工作门槛（分钟）。
WALK_FOCUS_MINUTES = 90
STRETCH_FOCUS_MINUTES = 50
BREATHE_FOCUS_MINUTES = 30
WATER_FOCUS_MINUTES = 20
# 空档型 water 需要的最短空档。
WATER_GAP_MINUTES = 30
# 久坐提醒线（来自手表/手动状态的 sedentary_minutes）。
SEDENTARY_MINUTES = 60


@dataclass(frozen=True)
class TimelineItem:
    """时间轴上的一个条目：要么是日程（``event``），要么是休息点（``break``）。

    日程条目用 ``CalendarEvent`` 的原始起止时间，休息点条目用窗口内的时间；
    ``break_*`` 字段只对 ``kind == "break"`` 有意义，日程侧留空。
    """

    kind: str
    id: str
    start: str
    end: str
    title: str = ""
    category: str = ""
    priority: str = ""
    interruptible: bool = False
    break_type: str = ""
    label: str = ""
    duration_minutes: int = 0
    reason: str = ""
    soundscape: str = ""
    bpm: int = 0
    intensity: str = ""
    reminder_minutes_before: int = 0
    status: str = "planned"

    def __post_init__(self) -> None:
        if self.kind not in {"event", "break"}:
            raise ValueError("时间轴条目类型无效")
        if not self.id:
            raise ValueError("时间轴条目 id 不能为空")
        if self.start_dt >= self.end_dt:
            raise ValueError("时间轴条目结束时间必须晚于开始时间")

    @property
    def start_dt(self) -> datetime:
        return _dt(self.start, "start")

    @property
    def end_dt(self) -> datetime:
        return _dt(self.end, "end")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "id": self.id,
            "start": self.start,
            "end": self.end,
            "title": self.title,
            "category": self.category,
            "priority": self.priority,
            "interruptible": self.interruptible,
            "break_type": self.break_type,
            "label": self.label,
            "duration_minutes": self.duration_minutes,
            "reason": self.reason,
            "soundscape": self.soundscape,
            "bpm": self.bpm,
            "intensity": self.intensity,
            "reminder_minutes_before": self.reminder_minutes_before,
            "status": self.status,
        }


@dataclass(frozen=True)
class DayPlan:
    """一整天的编排结果：时间轴 + 统计 + 冲突 + 关怀卡。"""

    date: str
    timeline: tuple[TimelineItem, ...]
    stats: dict[str, Any]
    conflicts: list[list[str]]
    care: CareCard

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "timeline": [item.to_dict() for item in self.timeline],
            "stats": dict(self.stats),
            "conflicts": [list(pair) for pair in self.conflicts],
            "care": self.care.to_dict(),
        }


@dataclass(frozen=True)
class _Candidate:
    """规划阶段的候选休息点，最后才绑音景与 BPM。"""

    start: datetime
    end: datetime
    kind: str
    reason: str


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


def _merge_intervals(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    """合并重叠或相接的区间。"""
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged: list[tuple[datetime, datetime]] = [ordered[0]]
    for current_start, current_end in ordered[1:]:
        previous_start, previous_end = merged[-1]
        if current_start <= previous_end:
            merged[-1] = (previous_start, max(previous_end, current_end))
        else:
            merged.append((current_start, current_end))
    return merged


def _subtract_intervals(
    intervals: list[tuple[datetime, datetime]],
    blocked: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    """从 ``intervals`` 里挖掉 ``blocked``（沿用 planner 的区间相减思路）。"""
    available: list[tuple[datetime, datetime]] = []
    for interval_start, interval_end in intervals:
        fragments = [(interval_start, interval_end)]
        for blocked_start, blocked_end in blocked:
            next_fragments: list[tuple[datetime, datetime]] = []
            for fragment_start, fragment_end in fragments:
                if blocked_end <= fragment_start or blocked_start >= fragment_end:
                    next_fragments.append((fragment_start, fragment_end))
                    continue
                if fragment_start < blocked_start:
                    next_fragments.append((fragment_start, blocked_start))
                if blocked_end < fragment_end:
                    next_fragments.append((blocked_end, fragment_end))
            fragments = next_fragments
        available.extend((start, end) for start, end in fragments if start < end)
    return available


def _overlaps(first_start: datetime, first_end: datetime, second_start: datetime, second_end: datetime) -> bool:
    return first_start < second_end and second_start < first_end


def _overlaps_any(interval: tuple[datetime, datetime], others: list[tuple[datetime, datetime]]) -> bool:
    return any(_overlaps(interval[0], interval[1], start, end) for start, end in others)


def _focus_minutes_ending_at(segments: list[tuple[datetime, datetime]], boundary: datetime) -> int:
    """返回刚好在 ``boundary`` 结束的那段连续学习/工作的分钟数。"""
    for start, end in reversed(segments):
        if end == boundary:
            return max(0, _minutes(start, end))
        if end < boundary:
            break
    return 0


def _time_context(moment: datetime) -> str:
    """把时刻映射成音景目录使用的时段标签。"""
    hour = moment.hour
    if 5 <= hour < 9:
        return "早晨"
    if 9 <= hour < 12:
        return "上午"
    if 12 <= hour < 18:
        return "午后"
    if 18 <= hour < 22:
        return "晚间"
    return "夜间"


def _scene_for_break(kind: str) -> str:
    return "行走" if kind == "walk" else "久坐"


# 「出去走一圈」默认配哪首。休息点的音景本来是打分选出来的，但这件事对音乐的要求
# 比「适合行走」更具体：它得有一根**能踩着走的拍子**。打分表给不了这个偏好——
# 雨后与归途同样带「行走」标签，而雨后的「全天」在任何时段都拿满 3 分，最后只能
# 靠 id 字典序分胜负。所以这里直接钉住疾走。
#
# 但累到不想动、或压力已经很高的时候不钉：那种时候被一首 120 拍的曲子推着快走
# 只会更烦，交回打分表挑一首慢的（雨后 / 归途都带「行走」）。这是「场景智能默认」
# 而不是「场景强制」的分界。
WALK_SOUNDSCAPE = "brisk"


def _walk_soundscape(*, energy: str, stress: float) -> str | None:
    """走一圈默认给疾走；状态不好时返回 None，交回打分表。"""
    if energy == "低" or stress >= STRESS_HIGH:
        return None
    return WALK_SOUNDSCAPE


def _mood_from_state(state: StatusSnapshot | None) -> str:
    if state is None:
        return "平静"
    try:
        stress = float(state.stress)
        energy = float(state.energy)
    except (TypeError, ValueError):
        return "平静"
    if stress >= 65:
        return "焦虑"
    if energy <= 35:
        return "疲惫"
    return "平静"


def _state_energy(state: StatusSnapshot | None) -> str:
    if state is None:
        return "中"
    try:
        return energy_label(float(state.energy))
    except (TypeError, ValueError):
        return "中"


def _state_stress(state: StatusSnapshot | None) -> float:
    if state is None:
        return 0.0
    try:
        return float(state.stress)
    except (TypeError, ValueError):
        return 0.0


def _sedentary_note(state: StatusSnapshot | None) -> str:
    """久坐时间够长时，在休息理由后补一句事实。"""
    if state is None:
        return ""
    try:
        sedentary = float(state.sedentary_minutes)
    except (TypeError, ValueError):
        return ""
    if sedentary < SEDENTARY_MINUTES:
        return ""
    return f"；已连续久坐 {int(sedentary)} 分钟"


def _break_id(kind: str, start: datetime, end: datetime) -> str:
    """休息点 id：同一输入永远得到同一个 id，方便前端持久化状态。"""
    return str(uuid5(NAMESPACE_URL, f"music-companion:break:{kind}:{start.isoformat()}:{end.isoformat()}"))


def _flow_break(kind: str, start: datetime, duration: int, reason: str) -> _Candidate:
    return _Candidate(start=start, end=start + timedelta(minutes=duration), kind=kind, reason=reason)


def _plan_breaks(
    *,
    protected: list[tuple[datetime, datetime]],
    free_windows: list[tuple[datetime, datetime]],
    focus_segments: list[tuple[datetime, datetime]],
    interruptible_focus: list[tuple[datetime, datetime]],
    start_bound: datetime,
    end_bound: datetime,
    state: StatusSnapshot | None,
) -> list[_Candidate]:
    """在长连续学习块内部与空闲窗口里安排五类休息点。"""
    candidates: list[_Candidate] = []
    sedentary = _sedentary_note(state)

    # 1）可打断的长块内部：按块长选择散步或伸展，位置放在第 90 / 50 分钟。
    for focus_start, focus_end in interruptible_focus:
        total = _minutes(focus_start, focus_end)
        if total >= WALK_FOCUS_MINUTES + BREAK_TYPES["walk"]["duration_minutes"] + _IN_BLOCK_BUFFER_MINUTES:
            kind, offset = "walk", WALK_FOCUS_MINUTES
            reason = f"一段可打断的学习/工作有 {total} 分钟，第 90 分钟出去走一圈{sedentary}"
        elif total >= STRETCH_FOCUS_MINUTES + BREAK_TYPES["stretch"]["duration_minutes"] + _IN_BLOCK_BUFFER_MINUTES:
            kind, offset = "stretch", STRETCH_FOCUS_MINUTES
            reason = f"一段可打断的学习/工作有 {total} 分钟，中途站起来伸展{sedentary}"
        else:
            continue
        duration = BREAK_TYPES[kind]["duration_minutes"]
        plan_start = focus_start + timedelta(minutes=offset)
        if _minutes(plan_start, focus_end) < duration + _IN_BLOCK_BUFFER_MINUTES:
            continue
        candidates.append(_flow_break(kind, plan_start, duration, reason))

    # 2）空闲窗口：按连续学习时长与窗口大小走 walk → stretch → breathe/eyes → water。
    #
    # 这里刻意不用 elif 级联。级联的问题是：只要一天里几个空档形状相近，选出来
    # 的休息类型就一模一样，四个休息点全写着「出去走一圈」——读起来像机器排的，
    # 正是产品最想避免的味道。改成先把「这个空档装得下」的类型全收集起来，再
    # 优先挑当天还没出现过的那个；实在都出现过了，才退回优先级最高的。
    for gap_start, gap_end in free_windows:
        gap = _minutes(gap_start, gap_end)
        if gap < MIN_WINDOW_MINUTES:
            continue
        focus_before = _focus_minutes_ending_at(focus_segments, gap_start)

        # (优先级, 类型) —— 优先级沿用原来级联的顺序，高的先选。
        eligible: list[tuple[int, str]] = []
        if focus_before >= WALK_FOCUS_MINUTES and gap >= BREAK_TYPES["walk"]["window_minutes"]:
            eligible.append((4, "walk"))
        if focus_before >= STRETCH_FOCUS_MINUTES and gap >= BREAK_TYPES["stretch"]["window_minutes"]:
            eligible.append((3, "stretch"))
        if focus_before >= BREATHE_FOCUS_MINUTES:
            # 窗口小：慢一点的呼吸或抬头远眺，按窗口大小二选一。
            eligible.append((2, "eyes" if gap < 8 else "breathe"))
        if focus_before >= WATER_FOCUS_MINUTES and gap >= WATER_GAP_MINUTES:
            eligible.append((1, "water"))
        if not eligible:
            continue

        eligible.sort(key=lambda item: item[0], reverse=True)
        used = {item.kind for item in candidates}
        kind = next(
            (name for _, name in eligible if name not in used),
            eligible[0][1],
        )

        if kind == "walk":
            reason = f"连续学习/工作约 {focus_before} 分钟，空档 {gap} 分钟，够出去走一圈{sedentary}"
        elif kind == "stretch":
            reason = f"连续学习/工作约 {focus_before} 分钟，空档 {gap} 分钟，先站起来伸展{sedentary}"
        elif kind == "water":
            reason = f"连续学习/工作已有 {focus_before} 分钟，空档 {gap} 分钟，先补一杯水"
        else:
            reason = f"连续学习/工作约 {focus_before} 分钟，空档只有 {gap} 分钟，{BREAK_TYPES[kind]['label']}"

        duration = min(BREAK_TYPES[kind]["duration_minutes"], gap - _GAP_BUFFER_MINUTES)
        if duration <= 0:
            continue
        candidates.append(_flow_break(kind, gap_start, duration, reason))

    # 3）去重：先按时间排序，再丢掉跨出当天、压到硬日程或与已选休息点重叠的候选。
    candidates.sort(key=lambda item: (item.start, item.end, item.kind))
    chosen: list[_Candidate] = []
    for candidate in candidates:
        if candidate.start < start_bound or candidate.end > end_bound:
            continue
        if _overlaps_any((candidate.start, candidate.end), protected):
            continue
        if any(_overlaps(candidate.start, candidate.end, other.start, other.end) for other in chosen):
            continue
        chosen.append(candidate)
    return chosen


def build_day(
    *,
    events: Iterable[CalendarEvent],
    state: StatusSnapshot | None = None,
    day_start: str,
    day_end: str,
    now: str | None = None,
    soundscape_hint: str | None = None,
    profile: Mapping[str, Any] | None = None,
) -> DayPlan:
    """编排一天的日程与休息点。

    ``profile`` 是新增的可选参数（旧调用方式不受影响）：新版产品把唤醒/入睡时间
    等个人信息放在这里，只用于关怀语措辞，不参与休息点规划。
    ``soundscape_hint`` 命中已知音景 id 时视为用户手动指定，所有休息点都用它。
    """
    start_bound = _dt(day_start, "day_start")
    end_bound = _dt(day_end, "day_end")
    if end_bound <= start_bound:
        raise ValueError("结束时间必须晚于开始时间")
    moment = _dt(now, "now") if now is not None else start_bound
    event_list = validate_events(events or [])

    clipped: list[tuple[CalendarEvent, datetime, datetime]] = []
    for event in event_list:
        begin = max(event.start_dt, start_bound)
        finish = min(event.end_dt, end_bound)
        if begin < finish:
            clipped.append((event, begin, finish))

    protected = _merge_intervals([
        (begin, finish)
        for event, begin, finish in clipped
        if event.priority == "hard" and not event.interruptible
    ])
    free_windows = _subtract_intervals([(start_bound, end_bound)], protected)
    focus_segments = _merge_intervals([
        (begin, finish)
        for event, begin, finish in clipped
        if event.category in {"study", "work"}
    ])
    interruptible_focus = _subtract_intervals(focus_segments, protected)

    candidates = _plan_breaks(
        protected=protected,
        free_windows=free_windows,
        focus_segments=focus_segments,
        interruptible_focus=interruptible_focus,
        start_bound=start_bound,
        end_bound=end_bound,
        state=state,
    )

    mood = _mood_from_state(state)
    energy = _state_energy(state)
    stress = _state_stress(state)
    break_items: list[TimelineItem] = []
    for candidate in candidates:
        # 用户手动指定的一律优先；没指定且是「出去走一圈」，才用疾走兜底。
        hint = soundscape_hint
        if not hint and candidate.kind == "walk":
            hint = _walk_soundscape(energy=energy, stress=stress)
        scape = select_soundscape(
            time_context=_time_context(candidate.start),
            mood=mood,
            scene=_scene_for_break(candidate.kind),
            energy=energy,
            explicit=hint,
        )
        spec = BREAK_TYPES[candidate.kind]
        break_items.append(TimelineItem(
            kind="break",
            id=_break_id(candidate.kind, candidate.start, candidate.end),
            start=candidate.start.isoformat(),
            end=candidate.end.isoformat(),
            title=spec["label"],
            label=spec["label"],
            break_type=candidate.kind,
            duration_minutes=_minutes(candidate.start, candidate.end),
            reason=candidate.reason,
            soundscape=scape,
            bpm=bpm_for(scape, energy=energy, stress=stress, mood=mood),
            intensity=spec["intensity"],
            reminder_minutes_before=spec["reminder_minutes_before"],
        ))

    timeline: list[TimelineItem] = [
        TimelineItem(
            kind="event",
            id=event.id,
            start=event.start,
            end=event.end,
            title=event.title,
            category=event.category,
            priority=event.priority,
            interruptible=event.interruptible,
            duration_minutes=_minutes(begin, finish),
            status=event.status,
        )
        for event, begin, finish in clipped
    ]
    timeline.extend(break_items)
    timeline.sort(key=lambda item: (item.start_dt, 0 if item.kind == "break" else 1, item.end_dt, item.id))

    day_events = [event for event, _, _ in clipped]
    first_event = min((event.start_dt for event, _, _ in clipped), default=None)
    last_event = max((event.start_dt for event, _, _ in clipped), default=None)
    stats: dict[str, Any] = {
        "focus_minutes": sum(_minutes(begin, finish) for begin, finish in focus_segments),
        "protected_minutes": sum(_minutes(begin, finish) for begin, finish in protected),
        "free_minutes": sum(_minutes(begin, finish) for begin, finish in free_windows),
        "break_count": len(break_items),
        "break_minutes": sum(item.duration_minutes for item in break_items),
        "event_count": len(clipped),
        "first_event": first_event.strftime("%H:%M") if first_event else None,
        "last_event": last_event.strftime("%H:%M") if last_event else None,
        "soundscape": _dominant_soundscape(break_items) or select_soundscape(
            time_context=_time_context(start_bound + (end_bound - start_bound) / 2),
            mood=mood,
            scene="通用",
            energy=energy,
            explicit=soundscape_hint,
        ),
    }

    care = compose_care(
        now=moment,
        events=day_events,
        state=state,
        profile=profile,
        stats=stats,
    )
    conflicts = [[first, second] for first, second in find_conflicts(day_events)]
    return DayPlan(
        date=start_bound.date().isoformat(),
        timeline=tuple(timeline),
        stats=stats,
        conflicts=conflicts,
        care=care,
    )


def _dominant_soundscape(items: Iterable[TimelineItem]) -> str:
    """取休息点里出现次数最多的音景；同票按 id 字典序，保证确定性。"""
    counts: dict[str, int] = {}
    for item in items:
        if item.kind == "break" and item.soundscape:
            counts[item.soundscape] = counts.get(item.soundscape, 0) + 1
    if not counts:
        return ""
    return sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))[0][0]


def day_soundscape_hint(plan: DayPlan) -> str:
    """当天主导音景 id，供 ``POST /api/day`` 的 ``soundscape_hint`` 字段使用。"""
    hint = str(plan.stats.get("soundscape") or "").strip()
    return hint or DEFAULT_SOUNDSCAPE


def hydrate_timeline(
    plan: DayPlan,
    statuses: Mapping[str, str],
) -> tuple[TimelineItem, ...]:
    """把已持久化的休息点状态合并回时间轴（不修改原计划）。"""
    hydrated: list[TimelineItem] = []
    for item in plan.timeline:
        status = statuses.get(item.id) if item.kind == "break" else None
        hydrated.append(replace(item, status=status) if status else item)
    return tuple(hydrated)
