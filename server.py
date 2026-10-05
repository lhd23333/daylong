"""Dependency-free local web server for the Smart Music Companion MVP.

Run from the project root:
    python3 server.py

The server exposes a small JSON API and serves the static frontend from
``static/``. It is intentionally a single standard-library file so it can run
on the classroom machine without installing Flask or Node packages.
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from music_companion.ai_client import AIRecommender
from music_companion.audio_generator import AudioGenerationError, generate_music_wav
from music_companion.music_api import MusicAPIError, create_music_client
from music_companion.recommender import RecommendationError, recommend
from music_companion.calendar_model import CalendarEvent, EVENT_STATUSES, find_conflicts, validate_events
from music_companion.companion import compose_care_with_ai
from music_companion.day_plan import build_day, day_soundscape_hint, hydrate_timeline
from music_companion.ics_parser import parse_ics
from music_companion.planner import BreakPlanItem, plan_breaks
from music_companion.playlist import PlaylistStore
from music_companion.soundscape import soundscape_catalog
from music_companion.state import StatusSnapshot, parse_status_import


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
MAX_BODY_BYTES = 100_000
DATA_DIR = ROOT / "data"
PLAN_STATUSES = {"planned", "started", "done", "snoozed", "dismissed"}


def _write_json_atomic(path: Path, payload: object) -> None:
    """Persist a small session document without exposing partial JSON."""
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


def _read_json(path: Path, fallback: object) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return fallback


class MusicCompanionHandler(BaseHTTPRequestHandler):
    server_version = "MusicCompanion/0.1"

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._send_common_headers()
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/calendar/events":
            requested_date = parse_qs(parsed.query).get("date", [""])[0]
            events = self.server.calendar_events
            if requested_date:
                events = [event for event in events if event.start[:10] == requested_date]
            self._send_json(200, {"events": [event.to_dict() for event in events]})
            return
        if parsed.path == "/api/state/latest":
            self._send_json(200, {"state": self.server.latest_state.to_dict() if self.server.latest_state else None})
            return
        if parsed.path == "/api/health":
            client = self.server.music_client
            self._send_json(
                200,
                {
                    "ok": True,
                    "mode": "ai" if self.server.ai_client.is_configured() else "local",
                    # 音频生成永远为真：本地合成器不需要任何外部依赖。
                    "audio_generation": True,
                    # 远端通路是可选加成，单独报，前端据此决定要不要显示
                    # 「用 AI 生成一段」那个入口。
                    "remote_music": client.health() if client is not None else {"configured": False},
                },
            )
            return
        if parsed.path == "/api/soundscapes":
            # 目录本身就是一个数组（前端按 id 校验并展示）。
            self._send_json(200, soundscape_catalog())
            return
        if parsed.path == "/api/playlist":
            # 收藏。盘上存的是配方（音景/节拍/节奏量/调性/进行/种子），不是音频，
            # 所以这里返回的就是几条能直接回放的小 JSON。
            self._send_json(200, {"items": self.server.playlist.list_entries()})
            return
        self._serve_static(parsed.path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/calendar/") or parsed.path.startswith("/api/planner/") or parsed.path.startswith("/api/state/"):
            self._handle_planning_api(parsed.path)
            return
        if parsed.path == "/api/generate-audio":
            self._handle_generate_audio()
            return
        if parsed.path == "/api/generate-ai-audio":
            self._handle_generate_ai_audio()
            return
        if parsed.path == "/api/day":
            self._handle_day_plan()
            return
        if parsed.path == "/api/day/break-status":
            self._handle_break_status()
            return
        if parsed.path == "/api/playlist":
            self._handle_playlist_add()
            return
        if parsed.path != "/api/recommend":
            self._send_json(404, {"error": "接口不存在"})
            return
        try:
            length = self._content_length()
            if length > MAX_BODY_BYTES:
                self._send_json(413, {"error": "请求内容过大"})
                return
            raw = self.rfile.read(length)
            if not raw:
                self._send_json(400, {"error": "请求内容不能为空"})
                return
            payload = json.loads(raw.decode("utf-8"))
            result = recommend(payload, self.server.ai_client)
        except (RecommendationError, ValueError) as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._send_json(400, {"error": f"JSON 格式错误：{exc}"})
            return
        except Exception as exc:  # noqa: BLE001 - JSON API boundary
            self._send_json(500, {"error": f"服务器内部错误：{type(exc).__name__}"})
            return
        self._send_json(200, result)

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        prefix = "/api/calendar/events/"
        playlist_prefix = "/api/playlist/"
        if parsed.path.startswith(playlist_prefix):
            entry_id = unquote(parsed.path[len(playlist_prefix):])
            if not entry_id or self.server.playlist.remove(entry_id) is False:
                self._send_json(404, {"error": "收藏不存在"})
                return
            self._send_json(200, {"deleted": entry_id})
            return
        if not parsed.path.startswith(prefix):
            self._send_json(404, {"error": "接口不存在"})
            return
        event_id = unquote(parsed.path[len(prefix):])
        before = len(self.server.calendar_events)
        self.server.calendar_events = [item for item in self.server.calendar_events if item.id != event_id]
        if len(self.server.calendar_events) == before:
            self._send_json(404, {"error": "日程不存在"})
            return
        self.server.persist_events()
        self._send_json(200, {"deleted": event_id})

    def _handle_playlist_add(self) -> None:
        """收藏一段配方。

        200 还是 201 有讲究：配方 id 是那六个字段的纯函数，同一段音乐重复收藏
        命中已有条目（``created=False``），这时回 200 而不是 201——前端据此提示
        「已经在收藏里了」。越界或超长一律走 ValueError → 400，**不夹紧**：
        静默改数值会让存下来的和用户听到的不是同一段。
        """
        try:
            payload = self._read_json_body()
            entry, created = self.server.playlist.add(payload)
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._send_json(400, {"error": f"JSON 格式错误：{exc}"})
            return
        except Exception as exc:  # noqa: BLE001 - JSON API boundary
            self._send_json(500, {"error": f"服务器内部错误：{type(exc).__name__}"})
            return
        self._send_json(201 if created else 200, {"item": entry, "duplicate": not created})

    def _content_length(self) -> int:
        raw_length = self.headers.get("Content-Length", "")
        try:
            length = int(raw_length)
        except (TypeError, ValueError) as exc:
            raise ValueError("请求内容长度无效") from exc
        if length < 0:
            raise ValueError("请求内容长度无效")
        return length

    def _read_json_body(self) -> dict:
        length = self._content_length()
        if length <= 0 or length > MAX_BODY_BYTES:
            raise ValueError("请求内容无效")
        value = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("请求必须是 JSON 对象")
        return value

    def _handle_planning_api(self, path: str) -> None:
        try:
            payload = self._read_json_body()
            if path == "/api/calendar/events":
                event = CalendarEvent.from_mapping(payload)
                self.server.calendar_events = [item for item in self.server.calendar_events if item.id != event.id] + [event]
                self.server.persist_events()
                self._send_json(201, {"event": event.to_dict(), "conflicts": find_conflicts(self.server.calendar_events)})
                return
            if path == "/api/calendar/import-ics":
                events = parse_ics(str(payload.get("content") or ""))
                self.server.calendar_events = [item for item in self.server.calendar_events if item.source != "ics"] + events
                self.server.persist_events()
                self._send_json(201, {"imported": len(events), "events": [item.to_dict() for item in events], "conflicts": find_conflicts(self.server.calendar_events)})
                return
            if path == "/api/state/manual":
                self.server.latest_state = StatusSnapshot.from_mapping(payload, "manual")
                self.server.persist_state()
                self._send_json(201, {"state": self.server.latest_state.to_dict()})
                return
            if path == "/api/state/import":
                self.server.latest_state = parse_status_import(str(payload.get("content") or ""), str(payload.get("format") or "json"))
                self.server.persist_state()
                self._send_json(201, {"state": self.server.latest_state.to_dict()})
                return
            if path.startswith("/api/calendar/events/") and path.endswith("/status"):
                event_id = unquote(path[len("/api/calendar/events/"):-len("/status")].strip("/"))
                status = str(payload.get("status") or "")
                if status not in EVENT_STATUSES:
                    raise ValueError("日程状态无效")
                for index, event in enumerate(self.server.calendar_events):
                    if event.id == event_id:
                        updated = CalendarEvent.from_mapping({**event.to_dict(), "status": status})
                        self.server.calendar_events[index] = updated
                        self.server.persist_events()
                        self._send_json(200, {"event": updated.to_dict()})
                        return
                self._send_json(404, {"error": "日程不存在"})
                return
            if path == "/api/planner/plan":
                plans = plan_breaks(self.server.calendar_events, self.server.latest_state, day_start=str(payload.get("day_start")), day_end=str(payload.get("day_end")))
                hydrated_plans = []
                for plan in plans:
                    plan_status = self.server.plan_statuses.get(plan.id)
                    if plan_status:
                        plan = BreakPlanItem.from_mapping({**plan.to_dict(), "status": plan_status})
                    self.server.plan_statuses.setdefault(plan.id, plan.status)
                    self.server.plan_records[plan.id] = plan.to_dict()
                    hydrated_plans.append(plan)
                self.server.persist_plans()
                self._send_json(200, {"plans": [item.to_dict() for item in hydrated_plans], "conflicts": find_conflicts(self.server.calendar_events)})
                return
            if path.startswith("/api/planner/plans/") and path.endswith("/status"):
                plan_id = unquote(path[len("/api/planner/plans/"):-len("/status")].strip("/"))
                status = str(payload.get("status") or "")
                if status not in PLAN_STATUSES:
                    raise ValueError("计划状态无效")
                if plan_id not in self.server.plan_records:
                    self._send_json(404, {"error": "计划不存在"})
                    return
                self.server.plan_statuses[plan_id] = status
                self.server.persist_plans()
                self._send_json(200, {"id": plan_id, "status": status})
                return
            self._send_json(404, {"error": "接口不存在"})
        except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._send_json(400, {"error": str(exc)})

    def _handle_day_plan(self) -> None:
        """一整天的时间轴编排：日程 + 休息点 + 关怀语 + 音景提示。"""
        try:
            payload = self._read_json_body()
            raw_events = payload.get("events")
            if raw_events is None:
                # 省略 events 时用服务器已保存的日程，便于前端直接复用日历。
                events = list(self.server.calendar_events)
            elif isinstance(raw_events, list):
                events = [CalendarEvent.from_mapping(item) for item in raw_events]
            else:
                raise ValueError("events 必须是数组")

            raw_state = payload.get("state")
            state = None
            if raw_state is not None:
                if not isinstance(raw_state, dict):
                    raise ValueError("state 必须是 JSON 对象")
                state = StatusSnapshot.from_mapping(raw_state)

            raw_profile = payload.get("profile")
            if raw_profile is not None and not isinstance(raw_profile, dict):
                raise ValueError("profile 必须是 JSON 对象")

            raw_hint = payload.get("soundscape_hint")
            plan = build_day(
                events=events,
                state=state,
                day_start=str(payload.get("day_start") or ""),
                day_end=str(payload.get("day_end") or ""),
                now=str(payload.get("now")) if payload.get("now") is not None else None,
                soundscape_hint=str(raw_hint) if raw_hint else None,
                profile=raw_profile,
            )
        except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001 - JSON API boundary
            self._send_json(500, {"error": f"服务器内部错误：{type(exc).__name__}"})
            return

        result = plan.to_dict()
        result["timeline"] = [
            item.to_dict()
            for item in hydrate_timeline(plan, self.server.plan_statuses)
        ]
        result["soundscape_hint"] = day_soundscape_hint(plan)

        # 关怀语的 AI 通道。本地版本先算好（它永远是完整的），AI 只是把措辞
        # 改得像人话；没配密钥、超时、返回不合规，都会原样退回本地结果。
        # 这里传的是"事实"，不是提示词——措辞约束写在 AIRecommender.care 里。
        polished = compose_care_with_ai(
            plan.care,
            {
                "date": plan.date,
                "now": str(payload.get("now") or ""),
                "stats": plan.stats,
                "state": raw_state or {},
                "profile": raw_profile or {},
                "events": [
                    {
                        "title": event.title,
                        "start": event.start,
                        "end": event.end,
                        "category": event.category,
                        "interruptible": event.interruptible,
                    }
                    for event in events
                ],
            },
            self.server.ai_client,
        )
        if polished is not plan.care:
            result["care"] = polished.to_dict()

        self._send_json(200, result)

    def _handle_break_status(self) -> None:
        """休息点的开始/完成状态，复用 plans.json 持久化，重启后仍在。"""
        try:
            payload = self._read_json_body()
            break_id = str(payload.get("id") or "").strip()
            status = str(payload.get("status") or "").strip()
            if not break_id or len(break_id) > 160:
                raise ValueError("休息点 id 无效")
            if status not in PLAN_STATUSES:
                raise ValueError("休息点状态无效")
            self.server.plan_statuses[break_id] = status
            self.server.persist_plans()
            self._send_json(200, {"id": break_id, "status": status})
        except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._send_json(400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - JSON API boundary
            self._send_json(500, {"error": f"服务器内部错误：{type(exc).__name__}"})

    def _handle_generate_audio(self) -> None:
        try:
            length = self._content_length()
            if length <= 0 or length > MAX_BODY_BYTES:
                self._send_json(400, {"error": "音频请求内容无效"})
                return
            raw = self.rfile.read(length)
            payload = json.loads(raw.decode("utf-8"))
            target_bpm = float(payload.get("target_bpm", 120))
            duration_seconds = float(payload.get("duration_seconds", 30))
            if not 1 <= duration_seconds <= 1800:
                self._send_json(400, {"error": "音频时长必须在 1 到 1800 秒之间"})
                return
            genres = payload.get("genres") or []
            music_style = str(payload.get("music_style") or "")
            instruments = payload.get("instruments") or []
            if not isinstance(genres, list) or not all(
                isinstance(item, str) for item in genres
            ):
                self._send_json(400, {"error": "genres 必须是字符串数组"})
                return
            if not isinstance(instruments, list) or not all(
                isinstance(item, str) for item in instruments
            ):
                self._send_json(400, {"error": "instruments 必须是字符串数组"})
                return
            audio_bytes = generate_music_wav(
                target_bpm,
                genres,
                music_style=music_style,
                instruments=instruments,
                duration_seconds=duration_seconds,
            )
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError) as exc:
            self._send_json(400, {"error": f"音频请求格式错误：{exc}"})
            return
        except AudioGenerationError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001 - JSON API boundary
            self._send_json(500, {"error": f"音频生成失败：{type(exc).__name__}"})
            return

        filename = f"music_companion_{round(target_bpm)}bpm.wav"
        self.send_response(200)
        self._send_common_headers()
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(audio_bytes)))
        self.end_headers()
        self.wfile.write(audio_bytes)

    def _handle_generate_ai_audio(self) -> None:
        """Generate provider audio, falling back to the deterministic WAV loop."""
        try:
            payload = self._read_json_body()
            target_bpm = float(payload.get("target_bpm", 120))
            duration_seconds = float(payload.get("duration_seconds", 30))
            if not 1 <= duration_seconds <= 1800:
                raise ValueError("音频时长必须在 1 到 1800 秒之间")
            genres = payload.get("genres") or []
            instruments = payload.get("instruments") or []
            music_style = str(payload.get("music_style") or "")
            if not isinstance(genres, list) or not all(isinstance(item, str) for item in genres):
                raise ValueError("genres 必须是字符串数组")
            if not isinstance(instruments, list) or not all(isinstance(item, str) for item in instruments):
                raise ValueError("instruments 必须是字符串数组")

            recommendation = {
                "target_bpm": target_bpm,
                "music_style": music_style,
                "genres": genres,
                "instruments": instruments,
                "vocal_mode": str(payload.get("vocal_mode") or "纯音乐 / 无人声"),
                "scene": str(payload.get("scene") or payload.get("scene_mode") or "专注与散步"),
                "rationale": str(payload.get("rationale") or ""),
            }
            try:
                client = self.server.music_client
                if client is None:
                    raise MusicAPIError(
                        "远端音乐通路没配置好（检查 .env 里的 MUSIC_PROVIDER 和对应的 KEY）"
                    )
                prompt = client.build_prompt(recommendation)
                generated = client.generate(
                    prompt,
                    duration_seconds=duration_seconds,
                    force_instrumental=True,
                )
                audio_bytes = generated["audio_bytes"]
                content_type = str(generated.get("content_type") or "audio/mpeg")
                if not isinstance(audio_bytes, bytes) or not audio_bytes:
                    raise MusicAPIError("音乐接口返回空音频")
                if not content_type.lower().split(";", 1)[0].strip().startswith("audio/"):
                    raise MusicAPIError("音乐接口返回的不是音频")
                self._send_audio_response(
                    audio_bytes,
                    content_type,
                    source="ai",
                    provider=str(generated.get("provider") or "elevenlabs"),
                    model_id=str(generated.get("model_id") or ""),
                    song_id=str(generated.get("song_id") or ""),
                )
                return
            except (MusicAPIError, KeyError, TypeError, ValueError) as exc:
                # A remote provider is optional. Never label a local WAV as AI.
                fallback_reason = str(exc)[:180]

            audio_bytes = generate_music_wav(
                target_bpm,
                genres,
                music_style=music_style,
                instruments=instruments,
                duration_seconds=duration_seconds,
            )
            self._send_audio_response(
                audio_bytes,
                "audio/wav",
                source="local-fallback",
                fallback_reason=fallback_reason,
            )
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError) as exc:
            self._send_json(400, {"error": f"音频请求格式错误：{exc}"})
        except AudioGenerationError as exc:
            self._send_json(400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - JSON API boundary
            self._send_json(500, {"error": f"音频生成失败：{type(exc).__name__}"})

    def _send_audio_response(
        self,
        audio_bytes: bytes,
        content_type: str,
        *,
        source: str,
        provider: str = "",
        model_id: str = "",
        song_id: str = "",
        fallback_reason: str = "",
    ) -> None:
        normalized = content_type.lower().split(";", 1)[0].strip()
        extension = ".wav" if normalized in {"audio/wav", "audio/x-wav"} else ".mp3"
        self.send_response(200)
        self._send_common_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f'attachment; filename="music_companion{extension}"')
        self.send_header("Content-Length", str(len(audio_bytes)))
        self.send_header("X-Music-Source", source)
        if provider:
            self.send_header("X-Music-Provider", self._safe_header_value(provider, 80))
        if model_id:
            self.send_header("X-Music-Model", self._safe_header_value(model_id, 80))
        if song_id:
            self.send_header("X-Music-Song-Id", self._safe_header_value(song_id, 120))
        if fallback_reason:
            # HTTP headers are Latin-1 on the stdlib server; encode arbitrary
            # provider messages so a Chinese/error response cannot corrupt the
            # already-started binary response.
            reason_bytes = fallback_reason.encode("utf-8", errors="replace")[:240]
            reason_token = base64.urlsafe_b64encode(reason_bytes).decode("ascii").rstrip("=")
            self.send_header("X-Music-Fallback-Reason-B64", reason_token)
        self.end_headers()
        self.wfile.write(audio_bytes)

    @staticmethod
    def _safe_header_value(value: str, limit: int) -> str:
        """Keep provider metadata safe for BaseHTTPRequestHandler headers."""
        cleaned = str(value).replace("\r", " ").replace("\n", " ")
        return cleaned.encode("ascii", errors="replace")[:limit].decode("ascii")

    def _serve_static(self, path: str) -> None:
        relative = "index.html" if path in {"", "/"} else unquote(path.lstrip("/"))
        candidate = (STATIC_DIR / relative).resolve()
        if not candidate.is_relative_to(STATIC_DIR.resolve()) or not candidate.is_file():
            self._send_json(404, {"error": "页面不存在"})
            return
        self.send_response(200)
        self._send_common_headers()
        content_type, _ = mimetypes.guess_type(candidate.name)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(candidate.read_bytes())

    def _send_json(self, status: int, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._send_common_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_common_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def log_message(self, format: str, *args) -> None:
        print(f"[server] {self.address_string()} - {format % args}")


class MusicCompanionServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], data_dir: str | Path | None = None) -> None:
        super().__init__(address, MusicCompanionHandler)
        self.ai_client = AIRecommender()
        # 远端音乐通路按 MUSIC_PROVIDER 选（elevenlabs / tempolor）。选不出来
        # 不该拦住整个服务：本地合成器才是主线，远端只是可选加成。
        try:
            self.music_client = create_music_client()
        except Exception as exc:  # noqa: BLE001 - 配置错误不该让服务起不来
            print(f"[server] 远端音乐通路未启用：{exc}")
            self.music_client = None
        self.data_dir = Path(data_dir) if data_dir is not None else DATA_DIR
        self.events_path = self.data_dir / "calendar_events.json"
        self.state_path = self.data_dir / "latest_state.json"
        self.plans_path = self.data_dir / "plans.json"
        self._data_lock = threading.RLock()
        # 收藏夹。PlaylistStore 在构造时把整表读进内存、每次改动整表重写，
        # **整个进程只能有一个实例**（每请求 new 一个会互相覆盖），所以挂在
        # server 上。路径跟 data_dir 走，测试用临时目录时才不会碰真实数据。
        self.playlist = PlaylistStore(self.data_dir / "playlist.json")
        raw_events = _read_json(self.events_path, [])
        self.calendar_events = []
        if isinstance(raw_events, list):
            for raw_event in raw_events:
                try:
                    self.calendar_events.append(CalendarEvent.from_mapping(raw_event))
                except (ValueError, TypeError):
                    continue
        raw_state = _read_json(self.state_path, None)
        self.latest_state = None
        if isinstance(raw_state, dict):
            try:
                self.latest_state = StatusSnapshot.from_mapping(raw_state)
            except (ValueError, TypeError):
                self.latest_state = None
        raw_plans = _read_json(self.plans_path, {})
        self.plan_records = raw_plans if isinstance(raw_plans, dict) else {}
        self.plan_statuses = {
            str(plan_id): str(value.get("status") or "planned")
            for plan_id, value in self.plan_records.items()
            if isinstance(value, dict)
        }

    def persist_events(self) -> None:
        with self._data_lock:
            _write_json_atomic(self.events_path, [event.to_dict() for event in self.calendar_events])

    def persist_state(self) -> None:
        with self._data_lock:
            _write_json_atomic(self.state_path, self.latest_state.to_dict() if self.latest_state else None)

    def persist_plans(self) -> None:
        with self._data_lock:
            for plan_id, status in self.plan_statuses.items():
                record = self.plan_records.get(plan_id)
                if isinstance(record, dict):
                    record["status"] = status
                else:
                    self.plan_records[plan_id] = {"id": plan_id, "status": status}
            _write_json_atomic(self.plans_path, self.plan_records)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the music companion website")
    parser.add_argument("--host", default="127.0.0.1", help="bind host")
    parser.add_argument("--port", type=int, default=8000, help="bind port")
    args = parser.parse_args()

    server = MusicCompanionServer((args.host, args.port))
    print(f"朝夕已启动：http://{args.host}:{args.port}")
    print("按 Ctrl+C 停止服务。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()


