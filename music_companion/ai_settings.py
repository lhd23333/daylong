"""本机 AI 通道设置：在页面上填 Key，不必去改 .env。

设计约束（为什么是这个形状）：

- **设置覆盖 .env，清空即回到 .env**。.env 是给「部署」准备的，这个文件是
  给「坐在电脑前的人」准备的；两者互不改对方的文件。
- **只存本机**：文件落在 ``data/ai_settings.json``（打包脚本排除整个 data/），
  不进 git、不随作品提交。页面永远读不到完整 Key，只有尾 4 位提示。
- **原子写**：和 server.py 的 ``_write_json_atomic`` 同一手法（fsync + replace）。
  这里不 import server——server 反过来要 import 本模块，会绕成环。

数据形状（归一化后只保留非空字段）::

    {
      "chat":  {"api_key": "...", "base_url": "...", "model": "..."},
      "music": {"provider": "elevenlabs|tempolor", "api_key": "...", "callback_url": "..."}
    }

``save()`` 的合并语义，是给「页面只提交用户实际填了的字段」用的：

- 字段缺席   → 保持原值
- 空串/None  → 清除该字段
- 非空字符串 → 校验后写入
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

SETTINGS_FILENAME = "ai_settings.json"

MAX_KEY_LEN = 512
MAX_URL_LEN = 512
MAX_MODEL_LEN = 128

MUSIC_PROVIDERS = ("elevenlabs", "tempolor")

_CHAT_FIELDS = ("api_key", "base_url", "model")
_MUSIC_FIELDS = ("provider", "api_key", "callback_url")

_FIELD_LABELS = {
    ("chat", "api_key"): "对话 API Key",
    ("chat", "base_url"): "接口地址",
    ("chat", "model"): "模型名",
    ("music", "api_key"): "音乐 API Key",
    ("music", "provider"): "音乐服务商",
    ("music", "callback_url"): "callback 地址",
}

_EMPTY = {"chat": {}, "music": {}}


def load(path: str | Path) -> dict:
    """读设置文件；缺失或损坏一律当作「没配」。

    读取路径**永不抛异常**：文件被手改坏一个字段，只丢那个字段，不牵连整份。
    """
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {"chat": {}, "music": {}}
    result: dict = {"chat": {}, "music": {}}
    if not isinstance(raw, dict):
        return result
    for block, fields in (("chat", _CHAT_FIELDS), ("music", _MUSIC_FIELDS)):
        chunk = raw.get(block)
        if not isinstance(chunk, dict):
            continue
        for field in fields:
            value = chunk.get(field)
            if not isinstance(value, str):
                continue
            try:
                cleaned = _clean_field(block, field, value)
            except ValueError:
                continue  # 手改坏的字段：忽略
            if cleaned:
                result[block][field] = cleaned
    return result


def save(path: str | Path, patch: dict) -> dict:
    """把 patch 合并进现有设置并落盘，返回合并后的完整设置。

    校验失败抛 ``ValueError``（消息直接给用户看），**不写盘**；写盘用原子替换。
    """
    if not isinstance(patch, dict):
        raise ValueError("设置必须是 JSON 对象")
    current = load(path)
    result: dict = {"chat": dict(current["chat"]), "music": dict(current["music"])}
    for block, fields in (("chat", _CHAT_FIELDS), ("music", _MUSIC_FIELDS)):
        chunk = patch.get(block)
        if chunk is None:
            continue
        if not isinstance(chunk, dict):
            raise ValueError(f"{block} 必须是 JSON 对象")
        for field in fields:
            if field not in chunk:
                continue
            value = chunk[field]
            label = _FIELD_LABELS[(block, field)]
            if value is None:
                result[block].pop(field, None)  # 空 = 清除
                continue
            if not isinstance(value, str):
                raise ValueError(f"{label}必须是文本")
            if not value.strip():
                result[block].pop(field, None)
                continue
            cleaned = _clean_field(block, field, value)
            if cleaned:
                result[block][field] = cleaned
            else:
                result[block].pop(field, None)
    _write_atomic(Path(path), result)
    return result


def clear(path: str | Path) -> bool:
    """删掉设置文件，回到 .env 的行为。不存在也算清干净；返回是否真的删了。"""
    try:
        Path(path).unlink()
        return True
    except FileNotFoundError:
        return False


def key_hint(key: str) -> str:
    """只回尾 4 位。整串 Key 永远不出服务端——页面连一半都拿不到。

    4 位以内的一律只给一个省略号：字符太少时「尾 4 位」等于全给了。
    """
    text = str(key or "").strip()
    if not text:
        return ""
    if len(text) <= 4:
        return "…"
    return f"…{text[-4:]}"


def _clean_field(block: str, field: str, value: str) -> str:
    """按字段各自的规则归一化；空串表示「未设置」。"""
    if field == "provider":
        text = value.strip().lower()
        if not text:
            return ""
        if text not in MUSIC_PROVIDERS:
            raise ValueError("音乐服务商只能是 " + " 或 ".join(MUSIC_PROVIDERS))
        return text
    if field == "api_key":
        return _clean_text(value, "API Key", MAX_KEY_LEN)
    if field == "base_url":
        return _clean_url(value, "接口地址")
    if field == "callback_url":
        return _clean_url(value, "callback 地址")
    if field == "model":
        return _clean_text(value, "模型名", MAX_MODEL_LEN)
    raise ValueError(f"未知字段：{field}")


def _clean_text(value: str, label: str, max_length: int) -> str:
    text = value.strip()
    if not text:
        return ""
    if "\n" in text or "\r" in text:
        raise ValueError(f"{label}不能包含换行")
    if len(text) > max_length:
        raise ValueError(f"{label}太长（最多 {max_length} 个字符）")
    return text


def _clean_url(value: str, label: str) -> str:
    text = _clean_text(value, label, MAX_URL_LEN)
    if text and not (text.startswith("http://") or text.startswith("https://")):
        raise ValueError(f"{label}需要以 http:// 或 https:// 开头")
    return text


def _write_atomic(path: Path, payload: object) -> None:
    """Persist the settings document without exposing partial JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
