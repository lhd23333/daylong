"""Manual and imported wearable state validation."""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class StatusSnapshot:
    captured_at: str
    energy: float
    stress: float
    focus: float
    sleep_hours: float = 8.0
    heart_rate: float = 0.0
    steps: float = 0.0
    sedentary_minutes: float = 0.0
    source: str = "manual"

    @classmethod
    def from_mapping(cls, value: dict[str, Any], source: str | None = None) -> "StatusSnapshot":
        """Build a snapshot while ignoring unknown transport fields."""
        if not isinstance(value, dict):
            raise ValueError("状态必须是 JSON 对象")
        return _snapshot(value, source)

    def __post_init__(self) -> None:
        parsed = datetime.fromisoformat(self.captured_at.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("状态时间必须包含时区")
        for name in ("energy", "stress", "focus"):
            value = float(getattr(self, name))
            if not 0 <= value <= 100:
                raise ValueError(f"{name} 必须在 0 到 100 之间")
        for name, maximum in (("sleep_hours", 24), ("heart_rate", 300), ("steps", 2_000_000), ("sedentary_minutes", 1440)):
            value = float(getattr(self, name))
            if not 0 <= value <= maximum:
                raise ValueError(f"{name} 超出合理范围")
        if self.source not in {"manual", "watch_import"}:
            raise ValueError("状态来源无效")

    def to_dict(self) -> dict[str, Any]:
        return {"captured_at": self.captured_at, "energy": self.energy, "stress": self.stress,
                "focus": self.focus, "sleep_hours": self.sleep_hours, "heart_rate": self.heart_rate,
                "steps": self.steps, "sedentary_minutes": self.sedentary_minutes, "source": self.source}


def _snapshot(value: dict[str, Any], source: str | None = None) -> StatusSnapshot:
    data = dict(value)
    if source:
        data["source"] = source
    data["captured_at"] = str(data.get("captured_at") or "")
    return StatusSnapshot(
        captured_at=data["captured_at"], energy=float(data.get("energy", 50)),
        stress=float(data.get("stress", 30)), focus=float(data.get("focus", 50)),
        sleep_hours=float(data.get("sleep_hours", 8)), heart_rate=float(data.get("heart_rate", 0)),
        steps=float(data.get("steps", 0)), sedentary_minutes=float(data.get("sedentary_minutes", 0)),
        source=str(data.get("source") or "manual"),
    )


def parse_status_import(text: str, format: str = "json") -> StatusSnapshot:
    if len(text.encode("utf-8")) > 500_000:
        raise ValueError("状态文件过大")
    if format.lower() == "json":
        value = json.loads(text)
        if isinstance(value, list):
            value = value[-1] if value else {}
        if not isinstance(value, dict):
            raise ValueError("JSON 状态必须是对象")
        return _snapshot(value, "watch_import")
    if format.lower() == "csv":
        rows = list(csv.DictReader(io.StringIO(text)))
        if not rows:
            raise ValueError("CSV 状态为空")
        return _snapshot(rows[-1], "watch_import")
    raise ValueError("仅支持 JSON 或 CSV 状态导入")
