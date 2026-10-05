"""Deterministic, explainable break and walk planning."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable
from uuid import NAMESPACE_URL, uuid5

from .calendar_model import CalendarEvent, validate_events
from .state import StatusSnapshot


@dataclass(frozen=True)
class BreakPlanItem:
    id: str
    start: str
    end: str
    type: str
    reason: str
    reminder_minutes_before: int = 3
    recommended_bpm: int = 90
    status: str = "planned"

    @classmethod
    def from_mapping(cls, value: dict) -> "BreakPlanItem":
        return cls(
            id=str(value.get("id") or ""),
            start=str(value.get("start") or ""),
            end=str(value.get("end") or ""),
            type=str(value.get("type") or "rest"),
            reason=str(value.get("reason") or ""),
            reminder_minutes_before=int(value.get("reminder_minutes_before", 3)),
            recommended_bpm=int(value.get("recommended_bpm", 90)),
            status=str(value.get("status") or "planned"),
        )

    def to_dict(self) -> dict:
        return {"id": self.id, "start": self.start, "end": self.end, "type": self.type,
                "reason": self.reason, "reminder_minutes_before": self.reminder_minutes_before,
                "recommended_bpm": self.recommended_bpm, "status": self.status}


def _dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("规划时间必须包含时区")
    return parsed


def _bpm(kind: str, status: StatusSnapshot | None) -> int:
    if kind == "walk":
        value = 108
        if status and (status.stress >= 65 or status.energy <= 35):
            value -= 12
        return max(80, min(115, value))
    value = 88
    if status and status.stress >= 65:
        value -= 8
    return max(70, min(100, value))


def _clip_events(events: Iterable[CalendarEvent], start: datetime, end: datetime) -> list[tuple[CalendarEvent, datetime, datetime]]:
    clipped: list[tuple[CalendarEvent, datetime, datetime]] = []
    for event in validate_events(events):
        event_start = max(event.start_dt, start)
        event_end = min(event.end_dt, end)
        if event_start < event_end:
            clipped.append((event, event_start, event_end))
    return clipped


def _merge_intervals(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
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
    """Return focus intervals that remain interruptible after protected events."""
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


def _focus_segments(clipped: list[tuple[CalendarEvent, datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    """Merge touching study/work events, preserving genuine idle gaps."""
    focus = [(start, end) for event, start, end in clipped if event.category in {"study", "work"}]
    return _merge_intervals(focus)


def _focus_minutes_ending_at(segments: list[tuple[datetime, datetime]], boundary: datetime) -> int:
    for start, end in reversed(segments):
        if end == boundary:
            return max(0, int((end - start).total_seconds() // 60))
        if end < boundary:
            break
    return 0


def _plan_id(start: datetime, end: datetime, kind: str) -> str:
    """Keep plan identity stable across repeated planning requests/restarts."""
    return str(uuid5(NAMESPACE_URL, f"music-companion:{kind}:{start.isoformat()}:{end.isoformat()}"))


def plan_breaks(events: Iterable[CalendarEvent], status: StatusSnapshot | None = None,
               *, day_start: str, day_end: str) -> list[BreakPlanItem]:
    """Plan breaks only in the requested day and never across protected events."""
    start_bound, end_bound = _dt(day_start), _dt(day_end)
    if end_bound <= start_bound:
        raise ValueError("规划时间范围无效")
    clipped = _clip_events(events, start_bound, end_bound)
    protected = [(start, end) for event, start, end in clipped
                 if event.priority == "hard" and not event.interruptible]
    occupied = _merge_intervals(protected)
    focus_segments = _focus_segments(clipped)
    windows: list[tuple[datetime, datetime]] = []
    cursor = start_bound
    for occupied_start, occupied_end in occupied:
        if cursor < occupied_start:
            windows.append((cursor, occupied_start))
        cursor = max(cursor, occupied_end)
    if cursor < end_bound:
        windows.append((cursor, end_bound))

    plans: list[BreakPlanItem] = []
    # A soft/interruptible long focus block can host a break without violating
    # a protected hard event. Place it after 90 minutes of focus and leave a
    # small buffer before the event resumes.
    interruptible_focus = _subtract_intervals(focus_segments, occupied)
    for focus_start, focus_end in interruptible_focus:
        focus_minutes = int((focus_end - focus_start).total_seconds() // 60)
        if focus_minutes < 110:
            continue
        plan_start = focus_start + timedelta(minutes=90)
        duration = min(15, int((focus_end - plan_start).total_seconds() // 60) - 5)
        if duration <= 0:
            continue
        plan_end = plan_start + timedelta(minutes=duration)
        plans.append(BreakPlanItem(
            _plan_id(plan_start, plan_end, "walk"), plan_start.isoformat(),
            plan_end.isoformat(), "walk",
            f"可打断的连续学习/工作约 {focus_minutes} 分钟，安排中途散步",
            recommended_bpm=_bpm("walk", status),
        ))
    for gap_start, gap_end in windows:
        gap_minutes = int((gap_end - gap_start).total_seconds() // 60)
        if gap_minutes < 10:
            continue
        focus_minutes = _focus_minutes_ending_at(focus_segments, gap_start)
        if focus_minutes >= 90 and gap_minutes >= 20:
            duration, kind = min(15, gap_minutes - 5), "walk"
            reason = f"连续学习/工作约 {focus_minutes} 分钟，空闲窗口 {gap_minutes} 分钟"
        elif focus_minutes >= 50 and gap_minutes >= 10:
            duration, kind = min(5, gap_minutes - 5), "stretch"
            reason = f"连续学习/工作约 {focus_minutes} 分钟，安排短暂伸展"
        else:
            continue
        if duration <= 0:
            continue
        item_end = gap_start + timedelta(minutes=duration)
        plans.append(BreakPlanItem(
            _plan_id(gap_start, item_end, kind), gap_start.isoformat(), item_end.isoformat(), kind, reason,
            recommended_bpm=_bpm(kind, status),
        ))
    return plans


def music_context_for_plan(plan: BreakPlanItem, events: Iterable[CalendarEvent], status: StatusSnapshot | None) -> dict:
    return {"plan_type": plan.type, "plan_start": plan.start, "plan_end": plan.end,
            "target_bpm": plan.recommended_bpm, "reason": plan.reason,
            "status": status.to_dict() if status else None,
            "event_titles": [event.title for event in events]}
