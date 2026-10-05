"""收藏夹：把一段实时合成的音乐存成一个可复现的配方。

这个应用没有音频文件：每一段音乐都是前端 ``static/js/engine.js`` 用 Web Audio
现算出来的，随机的部分由种子驱动，所以**同一个配方永远得到同一段音乐**。
「收藏一首好听的曲子」于是不需要保存任何音频，只要把这八个字段记下来：

    styleId / bpm / density / key / progressionIndex / drums / seed / name

这就是本模块存在的理由——盘上存的是配方，不是 mp3。由此得到两条贯穿全篇的约束：

1. **id 是配方的纯函数**：``uuid5`` + 固定命名空间，不含时间也不含计数器，同一段
   音乐在任何进程、任何机器上都是同一个 id。「这段音乐是不是已经收藏过」才有答案。
2. **存下来的配方必须与用户听到的一模一样**：越界的输入宁可报错，也不静默夹紧或
   截断，否则收藏下来的会是另一段音乐，而「下次还能听到同一段」正是收藏的全部意义。

存储落在 ``data/playlist.json``（已在 .gitignore 里），写盘走「临时文件 +
``os.replace``」，与 ``server.py`` 的做法一致。只依赖标准库。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5


# 18 个音景 id，与 soundscape.SOUNDSCAPES 的键逐字一致。这里刻意另存一份而不是
# import 那边：白名单属于**存储格式**的一部分，将来某个音景从目录里下架时，用户
# 早先的收藏必须还能读出来，不能因为目录变了就把人家的曲子判成非法数据。
# 代价是新增音景时要同步这里，漏了会让新音景的配方存不进来。
STYLE_IDS = frozenset({
    "first-light", "desk-hours", "after-rain", "breath",
    "high-noon", "night-lamp", "way-home", "settling",
    "brisk", "run",
    "volt", "forge", "cloud", "velvet",
    "neon", "pixel", "midnight", "cafe",
})

# 鼓点档位，与前端 soundscapes.js 的 DRUM_LEVELS、engine.js 的 DRUM_LAYERS 逐字对应。
# 和 STYLE_IDS 同理：这是存储格式的一部分，写在盘上的旧收藏不能因为前端改了档位
# 名称就读不出来。None 表示「不指定」——由音景自己的推荐档位决定，语义与 key 的
# null 一致。
DRUM_LEVELS = frozenset({"none", "light", "standard", "strong"})

# 12 个调名，逐字抄自 theory.js 的 PITCH_CLASSES。那边是 `String(key).toUpperCase()`
# 后查表，所以这里也先大写再比对；但注意只有升号拼法，降号（如 "Bb"）在 theory.js
# 里会抛「未知调性」，别在这里放行前端其实不认的写法。
KEY_NAMES = frozenset({"C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"})

# BPM 的全局硬边界。比 soundscape.py 里每个音景各自的 bpm_range 宽，是刻意的：
# 产品决策是「音景只给推荐区间、两边完全解耦」，所以这里只挡明显不合理的输入，
# 不替音景做决定。
BPM_MIN = 40
BPM_MAX = 200
DENSITY_MIN = 0.0
DENSITY_MAX = 1.0
# engine.js 的 mulberry32 用 `seed >>> 0` 取种子，超出 uint32 的值在前端会被回绕，
# 存下来的种子就不是当时听到的那个了，所以按 uint32 收口。
SEED_MAX = 2**32 - 1
# 名字与备注的长度上限：挡掉「塞一整篇作文进来」的请求。60 字足够放下卡片标题。
NAME_MAX = 60
NOTE_MAX = 200
DEFAULT_NAME = "未命名收藏"

# 单文件条数上限，与 calendar_model.validate_events 的 500 条导入上限同一量级：
# 500 条配方约 100 KB，够用，且不会让 JSON 无限膨胀。到顶后**报错而不是淘汰旧条目**
# ——收藏是用户资产，不能因为收藏了新歌就悄悄丢掉旧歌。
MAX_ENTRIES = 500

# 本模块专用的 uuid5 命名空间，生成一次后永久固定：改动它会让所有已收藏的 id 全部
# 对不上号，前端本地记下的 id 也会一起失联。
PLAYLIST_NAMESPACE = UUID("730b8d7f-9bb5-4be3-81f2-1c5b2392d68c")

# 默认落盘位置：项目根的 data/playlist.json。用绝对路径，不受启动时的工作目录影响。
DEFAULT_PATH = Path(__file__).resolve().parents[1] / "data" / "playlist.json"


def _parse_iso(value: object) -> datetime:
    """把 ISO8601 解析成带时区的 datetime；缺时区直接拒绝（项目铁律）。"""
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("时间必须包含时区")
    return parsed


def _as_int(value: object, field: str) -> int:
    """取整数，顺带容忍 ``72.0`` 这种写法——JS 只有一种数字类型，整数值写成浮点是常态。"""
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field} 必须是整数")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        raise ValueError(f"{field} 必须是整数")
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError as exc:
            raise ValueError(f"{field} 必须是整数") from exc
    raise ValueError(f"{field} 必须是整数")


def _as_float(value: object, field: str) -> float:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field} 必须是数字")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError as exc:
            raise ValueError(f"{field} 必须是数字") from exc
    raise ValueError(f"{field} 必须是数字")


def _clean_text(value: object, *, limit: int, field: str, fallback: str = "", truncate: bool = False) -> str:
    """把自由文本压成一行再查长度：换行与连续空格会撑破卡片布局。

    ``truncate=True`` 供读盘路径使用——宁可截断，也不要因为一条超长的名字或备注把
    用户收藏的整首歌判成损坏丢掉；写入路径默认报错，让用户知道没写进去。
    """
    text = " ".join(str(value if value is not None else "").split())
    if not text:
        return fallback
    if len(text) > limit:
        if not truncate:
            raise ValueError(f"{field}最长 {limit} 字")
        text = text[:limit]
    return text


def normalize_recipe(payload: dict) -> dict[str, Any]:
    """校验并规范化配方，返回可直接 ``json.dumps`` 的 camelCase 字典。

    缺省值与 ``engine.js`` 保持一致（``seed ?? 0``、``intensity ?? 0.5``、``key`` /
    ``progressionIndex`` 为 null），于是「不传」与「显式传默认值」得到同一个配方
    指纹，去重才认得出来是同一段音乐。

    越界的 bpm / density / seed 一律抛 ``ValueError``，不做夹紧：夹紧会让存下来的
    配方与用户当时听到的不是同一段音乐（见模块 docstring 的第 2 条约束）。调用方
    （HTTP 层）把 ``ValueError`` 转成 400 即可。未知字段直接忽略，配方之外的东西
    由前端自己算。
    """
    if not isinstance(payload, dict):
        raise ValueError("配方必须是 JSON 对象")

    # 前端 engine.js 把音景 id 叫 soundscapeId，配方文档里叫 styleId；两个都认，
    # 省得调用方在中间层来回改名。大小写与空格按 soundscape.select_soundscape 的
    # 习惯归一化。
    style_id = payload.get("styleId")
    if style_id is None:
        style_id = payload.get("soundscapeId")
    normalized_style = str(style_id or "").strip().lower()
    if normalized_style not in STYLE_IDS:
        raise ValueError(f"未知音景：{style_id}")

    bpm = _as_int(payload.get("bpm"), "bpm")
    if not BPM_MIN <= bpm <= BPM_MAX:
        raise ValueError(f"bpm 必须在 {BPM_MIN} 到 {BPM_MAX} 之间")

    density = _as_float(payload.get("density", 0.5), "density")
    if not DENSITY_MIN <= density <= DENSITY_MAX:
        raise ValueError(f"density 必须在 {DENSITY_MIN} 到 {DENSITY_MAX} 之间")
    # 滑块的精度远不到万分之一，四舍五入掉 0.30000000000000004 这类浮点噪声，免得
    # 同一段音乐被算成两个指纹、重复收藏进来。
    density = round(density, 4)

    raw_key = payload.get("key")
    if raw_key is None or not str(raw_key).strip():
        key_name = None
    else:
        key_name = str(raw_key).strip().upper()
        if key_name not in KEY_NAMES:
            raise ValueError(f"未知调性：{raw_key}")

    # 空串按「不指定」处理：前端表单没选时常常传 ""，语义与 null 相同。
    raw_index = payload.get("progressionIndex")
    if raw_index is None or not str(raw_index).strip():
        progression_index = None
    else:
        progression_index = _as_int(raw_index, "progressionIndex")
        if progression_index < 0:
            raise ValueError("progressionIndex 不能是负数")

    # 鼓点同样认空串：null 表示「跟音景的推荐档位走」，与 key 的 null 是同一套语义。
    raw_drums = payload.get("drums")
    if raw_drums is None or not str(raw_drums).strip():
        drums = None
    else:
        drums = str(raw_drums).strip().lower()
        if drums not in DRUM_LEVELS:
            raise ValueError(f"未知鼓点档位：{raw_drums}")

    seed = _as_int(payload.get("seed", 0), "seed")
    if not 0 <= seed <= SEED_MAX:
        raise ValueError(f"seed 必须在 0 到 {SEED_MAX} 之间")

    return {
        "styleId": normalized_style,
        "bpm": bpm,
        "density": density,
        "key": key_name,
        "progressionIndex": progression_index,
        "drums": drums,
        "seed": seed,
        "name": _clean_text(payload.get("name"), limit=NAME_MAX, field="名字", fallback=DEFAULT_NAME),
    }


def _fingerprint_id(recipe: dict[str, Any]) -> str:
    """对**已规范化**的配方算 uuid5。

    density 固定按四位小数写成文本：指纹不该依赖 ``repr`` 的浮点格式化策略，也不该
    因为 0.1 + 0.2 这类误差把同一段音乐算成两条。
    """
    parts = (
        "music-companion:playlist",
        str(recipe["styleId"]),
        str(recipe["bpm"]),
        f"{recipe['density']:.4f}",
        recipe["key"] or "-",
        "-" if recipe["progressionIndex"] is None else str(recipe["progressionIndex"]),
        recipe["drums"] or "-",
        str(recipe["seed"]),
    )
    return str(uuid5(PLAYLIST_NAMESPACE, ":".join(parts)))


def recipe_id(payload: dict) -> str:
    """配方 → 稳定 id。

    参与指纹的只有决定声音的七个字段，**名字与备注不算**：同一段音乐换个名字再收藏
    仍然是同一段音乐，应该命中已有条目，而不是多出一条。传未规范化的原始配方也行，
    内部先过一遍 :func:`normalize_recipe`（它对已规范化的输入是幂等的）。
    """
    return _fingerprint_id(normalize_recipe(payload))


def _build_entry(recipe: dict[str, Any], *, note: str, created_at: str) -> "PlaylistEntry":
    """把规范化配方组装成条目；id 在这里由配方算出，不接收调用方的 id。"""
    return PlaylistEntry(
        id=_fingerprint_id(recipe),
        style_id=recipe["styleId"],
        bpm=recipe["bpm"],
        density=recipe["density"],
        key=recipe["key"],
        progression_index=recipe["progressionIndex"],
        drums=recipe["drums"],
        seed=recipe["seed"],
        name=recipe["name"],
        created_at=created_at,
        note=note,
    )


@dataclass(frozen=True)
class PlaylistEntry:
    """一条收藏：决定声音的七个字段 + 用户起的名字 + 可选备注 + 服务端时间戳。

    构造即校验（与 ``CalendarEvent`` 同一套路），所以盘上读回来的条目和刚写进去的
    条目走的是同一套规则；字段名在 Python 侧用 snake_case，出门一律 camelCase。
    """

    id: str
    style_id: str
    bpm: int
    density: float
    key: str | None
    progression_index: int | None
    drums: str | None
    seed: int
    name: str
    created_at: str
    note: str = ""

    def __post_init__(self) -> None:
        if not self.id or len(self.id) > 64:
            raise ValueError("收藏 id 无效")
        if self.style_id not in STYLE_IDS:
            raise ValueError(f"未知音景：{self.style_id}")
        if not BPM_MIN <= self.bpm <= BPM_MAX:
            raise ValueError(f"bpm 必须在 {BPM_MIN} 到 {BPM_MAX} 之间")
        if not DENSITY_MIN <= self.density <= DENSITY_MAX:
            raise ValueError(f"density 必须在 {DENSITY_MIN} 到 {DENSITY_MAX} 之间")
        if self.key is not None and self.key not in KEY_NAMES:
            raise ValueError(f"未知调性：{self.key}")
        if self.progression_index is not None and self.progression_index < 0:
            raise ValueError("progressionIndex 不能是负数")
        if self.drums is not None and self.drums not in DRUM_LEVELS:
            raise ValueError(f"未知鼓点档位：{self.drums}")
        if not 0 <= self.seed <= SEED_MAX:
            raise ValueError(f"seed 必须在 0 到 {SEED_MAX} 之间")
        if not self.name.strip() or len(self.name) > NAME_MAX:
            raise ValueError("收藏名字无效")
        if len(self.note) > NOTE_MAX:
            raise ValueError(f"备注最长 {NOTE_MAX} 字")
        _parse_iso(self.created_at)  # 缺时区在这里就炸，不让坏时间流进列表

    @classmethod
    def from_mapping(cls, value: dict) -> "PlaylistEntry":
        """从盘上的一个对象还原条目（camelCase 键）。

        id 直接用配方重算：这样「id 是配方的纯函数」在读盘路径上也成立，文件被手工
        编辑过也不会出现 id 与配方对不上的条目。名字与备注按上限截断而不是报错——
        读盘的目标是把用户的收藏尽量捞回来，不该因为一条超长备注丢了一首歌。
        """
        if not isinstance(value, dict):
            raise ValueError("收藏条目必须是 JSON 对象")
        recipe = normalize_recipe(value)
        return cls(
            id=_fingerprint_id(recipe),
            style_id=recipe["styleId"],
            bpm=recipe["bpm"],
            density=recipe["density"],
            key=recipe["key"],
            progression_index=recipe["progressionIndex"],
            drums=recipe["drums"],
            seed=recipe["seed"],
            name=_clean_text(recipe["name"], limit=NAME_MAX, field="名字", fallback=DEFAULT_NAME, truncate=True),
            created_at=str(value.get("createdAt") or ""),
            note=_clean_text(value.get("note"), limit=NOTE_MAX, field="备注", truncate=True),
        )

    def to_dict(self) -> dict[str, Any]:
        """对外字典（camelCase，可直接 ``json.dumps``）：字段名与前端配方逐字对应。"""
        return {
            "id": self.id,
            "styleId": self.style_id,
            "bpm": self.bpm,
            "density": self.density,
            "key": self.key,
            "progressionIndex": self.progression_index,
            "drums": self.drums,
            "seed": self.seed,
            "name": self.name,
            "note": self.note,
            "createdAt": self.created_at,
        }


class PlaylistStore:
    """``data/playlist.json`` 的读写门面：启动时加载一次，之后改动即整表落盘。

    与 ``MusicCompanionServer`` 一样把条目留在内存里，所有改动共用一把可重入锁——
    HTTP 服务是 ``ThreadingHTTPServer``，多线程同时收藏是常态。
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else DEFAULT_PATH
        self._lock = threading.RLock()
        self._entries: list[PlaylistEntry] = []
        self._load()

    def add(self, payload: dict) -> tuple[dict[str, Any], bool]:
        """收藏一段音乐，返回 ``(条目, 是否新建)``。

        已经收藏过同一段音乐（配方指纹相同）时不会多出一条：返回**已存在的**那条，
        并且 ``created=False``。调用方据此决定回 201 还是 200，前端也能提示「已收藏」。

        名字只在第一次收藏时生效：重复收藏不该悄悄改掉用户原先起的名字，改名走
        :meth:`rename`。``payload`` 里可选的 ``note`` 同样只在新建时写入。
        """
        recipe = normalize_recipe(payload)
        entry = _build_entry(
            recipe,
            note=_clean_text(payload.get("note"), limit=NOTE_MAX, field="备注"),
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        with self._lock:
            for existing in self._entries:
                if existing.id == entry.id:
                    return existing.to_dict(), False
            if len(self._entries) >= MAX_ENTRIES:
                raise ValueError(f"收藏最多保存 {MAX_ENTRIES} 首，先删掉几首再收藏")
            self._entries.append(entry)
            self._save()
            return entry.to_dict(), True

    def list_entries(self) -> list[dict[str, Any]]:
        """按收藏时间倒序返回全部条目（最新的在最前面）。

        时间戳精度到微秒，同一秒内连续收藏的两首也能排出先后；万一真的撞上同一时刻，
        再用 id 兜底，保证同一份数据每次列出的顺序都一样。
        """
        with self._lock:
            ordered = sorted(
                self._entries,
                key=lambda item: (_parse_iso(item.created_at), item.id),
                reverse=True,
            )
            return [item.to_dict() for item in ordered]

    def get(self, entry_id: str) -> dict[str, Any] | None:
        """按 id 取一条；不存在返回 ``None``（前端刷新时 id 失效是正常情况）。"""
        with self._lock:
            entry = self._find(entry_id)
            return entry.to_dict() if entry is not None else None

    def remove(self, entry_id: str) -> bool:
        """删掉一条，返回是否真的删掉了；id 不存在返回 ``False``，不抛异常。"""
        with self._lock:
            for index, entry in enumerate(self._entries):
                if entry.id == str(entry_id or ""):
                    del self._entries[index]
                    self._save()
                    return True
            return False

    def rename(self, entry_id: str, name: object) -> dict[str, Any] | None:
        """改名字，返回改后的条目；id 不存在返回 ``None``；名字非法抛 ``ValueError``。"""
        cleaned = _clean_text(name, limit=NAME_MAX, field="名字", fallback=DEFAULT_NAME)
        with self._lock:
            entry = self._find(entry_id)
            if entry is None:
                return None
            return self._replace(entry, name=cleaned).to_dict()

    def set_note(self, entry_id: str, note: object) -> dict[str, Any] | None:
        """写备注（空串＝清空），返回改后的条目；id 不存在返回 ``None``。"""
        cleaned = _clean_text(note, limit=NOTE_MAX, field="备注")
        with self._lock:
            entry = self._find(entry_id)
            if entry is None:
                return None
            return self._replace(entry, note=cleaned).to_dict()

    def _find(self, entry_id: str) -> PlaylistEntry | None:
        target = str(entry_id or "")
        for entry in self._entries:
            if entry.id == target:
                return entry
        return None

    def _replace(self, entry: PlaylistEntry, **changes: object) -> PlaylistEntry:
        """就地替换并落盘，返回新条目（dataclass 是 frozen 的，只能换一个实例）。"""
        updated = replace(entry, **changes)
        for index, current in enumerate(self._entries):
            if current.id == updated.id:
                self._entries[index] = updated
                break
        self._save()
        return updated

    def _load(self) -> None:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            # 首次运行就是这种情况，空歌单是正常状态，不是错误。
            return
        except UnicodeDecodeError:
            # 编码错乱与 JSON 坏了是同一类问题，走同一套隔离重建。
            self._quarantine()
            return
        except OSError:
            # 读不动（权限、被占用）就先当空歌单，别让服务起不来；下次落盘会覆盖它，
            # 所以不做隔离备份。
            return

        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            self._quarantine()
            return
        if not isinstance(raw, list):
            self._quarantine()
            return

        for item in raw:
            try:
                self._entries.append(PlaylistEntry.from_mapping(item))
            except (ValueError, TypeError):
                # 单条坏数据只丢这一条：没有理由让其余收藏陪葬（server.py 读日历
                # 事件时也是这么做的）。这里不写盘，避免「只读地打开一次」就改动文件。
                continue

    def _quarantine(self) -> None:
        """把损坏的文件改名留档，再以空歌单重建。

        选择「备份」而不是「直接丢弃」：收藏是用户资产，坏文件很可能是手工编辑或
        磁盘问题造成的，留一份带 UTC 时间戳的原件，事后还能照着捞回来。挪不动
        （例如文件被别的进程占着）也不拦启动——继续用空歌单，下次落盘会覆盖它。
        """
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target = self.path.with_name(f"{self.path.stem}.corrupt-{stamp}{self.path.suffix}")
        suffix = 1
        while target.exists():
            target = self.path.with_name(f"{self.path.stem}.corrupt-{stamp}-{suffix}{self.path.suffix}")
            suffix += 1
        try:
            os.replace(self.path, target)
        except OSError:
            pass

    def _save(self) -> None:
        """整表重写：条数上限决定了文件很小，不值得为此做增量更新。"""
        _write_json_atomic(self.path, [entry.to_dict() for entry in self._entries])


def _write_json_atomic(path: Path, payload: object) -> None:
    """先写临时文件再 ``os.replace``，中途失败不会留下半截 JSON。

    与 ``server._write_json_atomic`` 是同一做法；在这里各留一份，是为了不让音乐库
    反过来依赖 HTTP 服务脚本（server.py 已经 import 了 music_companion）。
    """
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
