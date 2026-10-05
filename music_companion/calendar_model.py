"""Validated calendar event primitives used by planning and API layers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable


CATEGORIES = {"study", "work", "commute", "exercise", "break", "other"}
PRIORITIES = {"hard", "soft"}
SOURCES = {"manual", "ics"}
EVENT_STATUSES = {"planned", "started", "done", "snoozed", "dismissed"}


def _parse_datetime(value: str) -> datetime:
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("时间必须包含时区")
    return parsed


@dataclass(frozen=True)
class CalendarEvent:
    id: str
    title: str
    start: str
    end: str
    category: str = "other"
    priority: str = "hard"
    interruptible: bool = False
    source: str = "manual"
    description: str = ""
    metadata: dict[str, str] = field(default_factory=dict)
    status: str = "planned"

    def __post_init__(self) -> None:
        if not self.id or len(self.id) > 160:
            raise ValueError("日程 id 无效")
        if not self.title.strip() or len(self.title) > 200:
            raise ValueError("日程标题无效")
        start = _parse_datetime(self.start)
        end = _parse_datetime(self.end)
        if end <= start:
            raise ValueError("日程结束时间必须晚于开始时间")
        if self.category not in CATEGORIES:
            raise ValueError("日程类别无效")
        if self.priority not in PRIORITIES:
            raise ValueError("日程优先级无效")
        if self.source not in SOURCES:
            raise ValueError("日程来源无效")
        if self.status not in EVENT_STATUSES:
            raise ValueError("日程状态无效")

    @property
    def start_dt(self) -> datetime:
        return _parse_datetime(self.start)

    @property
    def end_dt(self) -> datetime:
        return _parse_datetime(self.end)

    @classmethod
    def from_mapping(cls, value: dict) -> "CalendarEvent":
        interruptible = value.get("interruptible", False)
        if isinstance(interruptible, str):
            normalized = interruptible.strip().lower()
            if normalized in {"true", "1", "yes", "y", "on"}:
                interruptible = True
            elif normalized in {"false", "0", "no", "n", "off", ""}:
                interruptible = False
            else:
                raise ValueError("interruptible 必须是布尔值")
        else:
            interruptible = bool(interruptible)
        return cls(
            id=str(value.get("id") or ""),
            title=str(value.get("title") or ""),
            start=str(value.get("start") or ""),
            end=str(value.get("end") or ""),
            category=str(value.get("category") or "other"),
            priority=str(value.get("priority") or "hard"),
            interruptible=interruptible,
            source=str(value.get("source") or "manual"),
            description=str(value.get("description") or "")[:1000],
            metadata={str(k): str(v) for k, v in (value.get("metadata") or {}).items()} if isinstance(value.get("metadata") or {}, dict) else {},
            status=str(value.get("status") or "planned"),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id, "title": self.title, "start": self.start, "end": self.end,
            "category": self.category, "priority": self.priority,
            "interruptible": self.interruptible, "source": self.source,
            "description": self.description, "metadata": dict(self.metadata),
            "status": self.status,
        }


def validate_events(events: Iterable[CalendarEvent]) -> list[CalendarEvent]:
    result = list(events)
    if len(result) > 500:
        raise ValueError("单次最多导入 500 条日程")
    return sorted(result, key=lambda item: (item.start_dt, item.end_dt, item.id))


def find_conflicts(events: Iterable[CalendarEvent]) -> list[tuple[str, str]]:
    ordered = validate_events(events)
    conflicts: list[tuple[str, str]] = []
    for index, current in enumerate(ordered):
        for other in ordered[index + 1:]:
            if other.start_dt >= current.end_dt:
                break
            if other.start_dt < current.end_dt and current.start_dt < other.end_dt:
                conflicts.append((current.id, other.id))
    return conflicts
