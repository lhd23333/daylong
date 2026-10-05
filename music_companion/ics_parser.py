"""Small RFC 5545 subset parser for offline calendar import."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

from .calendar_model import CalendarEvent


def _unfold(text: str) -> list[str]:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    result: list[str] = []
    for line in lines:
        if line.startswith((" ", "\t")) and result:
            result[-1] += line[1:]
        elif line:
            result.append(line)
    return result


def _property(line: str) -> tuple[str, dict[str, str], str] | None:
    if ":" not in line:
        return None
    left, value = line.split(":", 1)
    parts = left.split(";")
    name = parts[0].upper()
    params: dict[str, str] = {}
    for item in parts[1:]:
        if "=" in item:
            key, val = item.split("=", 1)
            params[key.upper()] = val.strip('"')
    return name, params, value


def _parse_ical_datetime(value: str, params: dict[str, str]) -> str:
    raw = value.strip()
    if len(raw) == 8 and raw.isdigit():
        parsed = datetime.strptime(raw, "%Y%m%d").replace(tzinfo=timezone.utc)
    else:
        is_utc = raw.endswith("Z")
        raw = raw.rstrip("Z")
        parsed = datetime.strptime(raw, "%Y%m%dT%H%M%S")
        if is_utc or params.get("TZID", "").upper() in {"UTC", "GMT"}:
            parsed = parsed.replace(tzinfo=timezone.utc)
        else:
            # Without a timezone database, preserve the event as UTC rather than
            # silently applying the machine's local timezone.
            parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat()


def parse_ics(text: str) -> list[CalendarEvent]:
    if len(text.encode("utf-8")) > 2_000_000:
        raise ValueError("ICS 文件过大")
    events: list[CalendarEvent] = []
    current: dict[str, str] | None = None
    for line in _unfold(text):
        upper = line.upper()
        if upper == "BEGIN:VEVENT":
            current = {}
            continue
        if upper == "END:VEVENT":
            if current is None:
                continue
            if not current.get("DTSTART") or not current.get("DTEND"):
                raise ValueError("ICS 事件缺少开始或结束时间")
            uid = current.get("UID") or hashlib.sha1(
                f"{current.get('SUMMARY','')}|{current['DTSTART']}|{current['DTEND']}".encode()
            ).hexdigest()[:20]
            events.append(CalendarEvent(
                id=uid,
                title=current.get("SUMMARY", "未命名日程")[:200],
                start=current["DTSTART"],
                end=current["DTEND"],
                category="other",
                priority="hard",
                interruptible=False,
                source="ics",
                description=current.get("DESCRIPTION", "")[:1000],
            ))
            current = None
            continue
        if current is None:
            continue
        prop = _property(line)
        if not prop:
            continue
        name, params, value = prop
        if name in {"DTSTART", "DTEND"}:
            current[name] = _parse_ical_datetime(value, params)
        elif name in {"UID", "SUMMARY", "DESCRIPTION"}:
            current[name] = value.replace("\\n", "\n").replace("\\,", ",")
    return events
