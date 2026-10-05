"""自由的一段话 → 一天的日程 + 今天的音乐。

为什么单独成一模块
==================

``recommender`` / ``day_plan`` 要的都是结构化输入（ISO 时间、category、priority），
而用户嘴里说的是「上午数学课，下午四点半去操场跑半小时，今天有点累」。这一层做的
就是中间那段翻译。

两条通道，一条底线
------------------

* ``plan_locally``：正则 + 关键词，纯本地、无网络、同样输入永远同样输出。没配
  模型密钥时它就是全部；配了也是兜底。
* ``plan_day_from_text``：配了密钥就让模型在**同样的结构**上重写一遍（模型的自由
  文本理解比正则强得多），返回前逐字段校验，任何一个字段不合法就退回本地那一个。
  「本地是底线，AI 是加成」——和 ``recommender`` 走的是同一条原则。

时间格式不交给模型
------------------

模型只说 ``HH:MM``，ISO8601 由 Python 按用户所在时区拼出来。让模型写带时区的
ISO 是稳定的出错来源；交给 Python 拼之后，整条链路在测试里可以用固定的 ``now``
完整复现（见 ``tests/test_agent.py``）。

输出契约
--------

``{reply, events[], music{}, state{}, preferences{}, source, fallback_reason}``。
``events`` 里的每条都是完整的 ``CalendarEvent.to_dict()``，前端可以原样 POST 回
``/api/calendar/events`` —— 后端已经用 ``CalendarEvent`` 自己校验过一遍。
"""

from __future__ import annotations

import re
from datetime import date as date_cls
from datetime import datetime, time as clock_time, timedelta
from typing import Any, Mapping, Sequence
from uuid import NAMESPACE_URL, uuid5

from .ai_client import AIRecommenderError
from .calendar_model import CalendarEvent
from .playlist import DRUM_LEVELS
from .soundscape import SOUNDSCAPES, bpm_for, select_soundscape


MAX_TEXT_CHARS = 2000
MAX_EVENTS = 12
# 一件事没说多长时间时，按类别给一个常识长度（分钟）。
DEFAULT_DURATION = {"study": 45, "work": 60, "commute": 40, "exercise": 40, "break": 30, "other": 30}
# 两件事挨得太近时不再截断后一件的结尾——那是「时间撞了」，交给冲突提示去说。
MIN_DURATION = 10
# 没写时间的条目接在上一件之后；一件都没有时从这里起步（下一个整半点）。
FALLBACK_START_MINUTE = 9 * 60

# 一天里的时段词 → 默认钟点。只在**没有**具体时刻（9:30 / 九点半）时用来落点。
_PERIOD_TOKENS: tuple[tuple[str, int, int], ...] = (
    ("凌晨", 5, 30),
    ("清晨", 6, 30),
    ("起床后", 6, 50),
    ("早上", 7, 0),
    ("早晨", 7, 0),
    ("上午", 9, 0),
    ("中午", 12, 0),
    ("午饭", 12, 10),
    ("午休", 12, 50),
    ("下午", 14, 30),
    ("傍晚", 17, 30),
    ("放学后", 17, 0),
    ("晚饭", 18, 30),
    ("晚上", 19, 30),
    ("夜里", 22, 0),
    ("睡前", 22, 30),
)

# 「明天上午考试」——整段话里的日程日偏移。逐句判断太容易误伤（「明天」只出现一次
# 却管着后面所有条目），所以按整段话取一个偏移量，写清楚是这个口径。
_DAY_OFFSETS: tuple[tuple[re.Pattern[str], int], ...] = (
    (re.compile(r"后天|后日"), 2),
    (re.compile(r"明天|明日|明早|明晚|次日"), 1),
)

# 下午/晚上的说法，用来把「三点」补成 15:00。
_PM_MARKERS = ("下午", "午后", "傍晚", "晚上", "夜里", "晚")
_AM_MARKERS = ("早上", "早晨", "清晨", "上午", "凌晨", "半夜")

_CLAUSE_SPLIT = re.compile(r"[\n\r；;。！？!?，,、]+")
# 只认真正的连接词：「9 点到 11 点」是区间，「9 点 11 点」不是。
_RANGE_CONNECTOR = re.compile(r"^\s*(?:到|至|~|～|—|–|-)\s*$")
# 「写作业到十点」里的十点是结束时刻。要求连接词前面还压着一个实字，
# 免得把 markdown 项目符号（「- 9 点上课」）误判成区间。
_TRAILING_CONNECTOR = re.compile(r"[^\s\-—–~～至到、，,；;:：](?:到|至|~|～|—|–|-)\s*$")
_CLOCK_RE = re.compile(r"(?<![\d:])([0-9]{1,2})\s*[:：]\s*([0-9]{2})(?![\d])")
_OCLOCK_RE = re.compile(
    r"(?<![\d:])([0-9]{1,2}|[一二两三四五六七八九十]{1,3})\s*点"
    r"(?:\s*(半|[0-9]{1,2}|[一二两三四五六七八九十]{1,3})\s*分?)?"
)
_HOUR_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*个?\s*小时")
_MINUTE_RE = re.compile(r"([0-9]+)\s*分钟")

_NUMERALS = {
    "一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8,
    "九": 9, "十": 10, "十一": 11, "十二": 12, "十五": 15, "二十": 20, "二十五": 25,
    "三十": 30, "三十五": 35, "四十": 40, "四十五": 45, "五十": 50, "五十五": 55,
}

# 顺序即优先级：先认「跟拍子有关的事」，再认学习/工作，最后才是吃饭休息。
# 「骑车」只算通勤不算运动——它决定不了步频，跟跑步不是一回事。
_SUBJECTS = "语文|数学|英语|物理|化学|生物|历史|地理|政治|科学|信息技术|通用技术|美术"
_CATEGORY_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"跑|慢跑|长跑|马拉松|jog|running|球|游泳|健身|锻炼|体育|跳绳|仰卧|俯卧|引体|拉伸|散步|走路|遛|爬山"), "exercise"),
    (re.compile(r"通勤|路上|公交|地铁|班车|骑车|骑行|回家|上学|赶车|车站|机场|高铁|火车"), "commute"),
    (re.compile(r"开会|会议|值班|汇报|报告|工作|项目|实习|打工"), "work"),
    (re.compile(rf"课|{_SUBJECTS}|作业|自习|复习|预习|考试|测验|月考|期中|期末|背|读|写|上机|练习|实验|讲座|面试|答辩|竞赛|比赛|study|work"), "study"),
    (re.compile(r"吃|饭|餐|食堂|外卖|加餐|零食"), "break"),
    (re.compile(r"休息|午休|小憩|睡|躺|眯|放松|发呆|洗澡|洗漱|收拾|整理|日记|复盘|总结|游戏|看剧|视频"), "break"),
)

# 不能被打断的事：课、考试、会议。这些一旦被休息点插进去，课就听不成了。
# 光写科目标题（「上午语文」）也算课——学生说的「语文」就是「语文课」。
_HARD_RULES = re.compile(rf"课|{_SUBJECTS}|考试|测验|月考|期中|期末|开会|会议|面试|答辩|讲座|竞赛|比赛|实验")
# 可以被打断但仍在「专注」里的：自习、写作业、项目。休息点会安插在这些长块的
# 第 90 / 50 分钟（见 ``day_plan._plan_breaks``）——这正是「智能」该有的那点分寸。
_FOCUS_RULES = re.compile(r"自习|作业|复习|预习|写|背|读|上机|练习|项目|工作|报告")

# 「今天全是自习」——这类说法给的是一整块时间，不是某一刻。块长与收尾时刻见
# ``_ALL_DAY_END_MINUTE``：一天里最晚排到 21:30，再晚就成了「熬夜计划」。
_ALL_DAY = re.compile(r"整天|一整天|一天都|全天|全是|全都是|一直")
_ALL_DAY_MINUTES = 8 * 60
_ALL_DAY_END_MINUTE = 21 * 60 + 30

_LOW_ENERGY = re.compile(r"累|疲惫|没力气|没精神|困|熬夜|睡不好|失眠|乏")
_HIGH_ENERGY = re.compile(r"精力充沛|有劲|状态好|精神好|睡得很好|很兴奋|元气")
# 「压力大」「压力有点大」都是同一件事，中间夹几个字照样要认出来。
_HIGH_STRESS = re.compile(r"压力[^，。；、,;]{0,4}大|紧张|焦虑|烦|deadline|赶不完|要崩|慌")
_CALM = re.compile(r"还不错|挺好的|轻松|放松|不紧张|心情好|开心|期待")
_LIKES = re.compile(r"(?:喜欢|爱听|想听|来点|听点|偏好)\s*([^\s，。；、,;]{1,8})")
_AVOIDS = re.compile(r"(?:不喜欢|讨厌|别放|不要听|受不了)\s*([^\s，。；、,;]{1,8})")

# 音乐直接决定步频的只有跑和走两种场景，其中只有跑步值得把鼓点钉死成最强档；
# 其余一律交回音景自己的推荐档（``drums=None`` 就是「按它自己的来」）。
_RUN_DRUMS = "strong"
_DRUM_PHRASE = {"none": "不加鼓", "light": "鼓点轻", "standard": "鼓点标准", "strong": "鼓点打满"}


# ── 对外入口 ──────────────────────────────────────────────────


def plan_day_from_text(
    text: str,
    *,
    now: str | datetime,
    date: str | None = None,
    history: Sequence[Mapping[str, Any]] = (),
    ai: Any = None,
) -> dict[str, Any]:
    """把一段自由文本读成 ``{reply, events, music, state, preferences, source, ...}``。

    ``ai`` 是 ``AIRecommender``（或任何实现了 ``understand`` 的对象）；没配密钥、
    超时、返回不合规，都会原样退回 ``plan_locally`` 的结果，并在
    ``fallback_reason`` 里留下原因——前端据此决定要不要提示「这次用的是本地规则」。
    """
    moment = _as_datetime(now)
    local = plan_locally(text, now=moment, date=date)
    if ai is None or not getattr(ai, "is_configured", lambda: False)():
        return local
    try:
        payload = ai.understand(
            text,
            now_iso=moment.isoformat(),
            local=local,
            catalog=_catalog_text(),
            history=history,
        )
        return _merge_ai(local, payload, now=moment)
    except (AIRecommenderError, ValueError, TypeError, KeyError, AttributeError) as exc:
        local["fallback_reason"] = f"{type(exc).__name__}: {exc}"[:200]
        return local


def plan_locally(text: str, *, now: str | datetime, date: str | None = None) -> dict[str, Any]:
    """纯本地的解析：正则 + 关键词，不联网、确定性。

    只看**当前这句话**（多轮上下文是模型通道才有的能力），但「再加一件事」这类
    补充说明本来就是一句完整的话，单独解析同样成立。
    """
    moment = _as_datetime(now)
    cleaned = str(text or "").strip()
    if not cleaned:
        raise ValueError("还没写要告诉它的话")
    if len(cleaned) > MAX_TEXT_CHARS:
        raise ValueError(f"一次最多 {MAX_TEXT_CHARS} 个字")

    offset = _day_offset(cleaned)
    day = _as_date(date, moment) + timedelta(days=offset)

    drafts = [draft for draft in (_parse_clause(c) for c in _split_clauses(cleaned)) if draft]
    events = _build_events(drafts, day=day, moment=moment)

    mood = _mood_of(cleaned)
    energy = _energy_of(cleaned)
    stress = _stress_of(cleaned)
    scene = _scene_of(drafts)
    scape_id, why = _choose_soundscape(
        drafts, scene=scene, mood=mood, energy=energy, stress=stress, moment=moment
    )
    music = {
        "soundscape": scape_id,
        # 只有跑步会把鼓点钉到最强档；走路交给音景自己的推荐（疾走本来就是强鼓点）。
        "drums": _RUN_DRUMS if scape_id == "run" else None,
        "bpm": bpm_for(scape_id, energy=energy if energy is not None else 50, stress=stress or 0.0, mood=mood),
        "why": why,
    }
    state = {"energy": energy, "stress": stress, "mood": mood}
    preferences = _preferences(cleaned)
    return {
        "reply": _reply_text(events, music, state, day_offset=offset, day=day),
        "events": events,
        "music": music,
        "state": state,
        "preferences": preferences,
        "source": "local",
        "fallback_reason": None,
    }


# ── 时间与文本的拆解 ──────────────────────────────────────────


def _split_clauses(text: str) -> list[str]:
    """按标点切句，并把「9 点到 11 点」这类纯时间尾巴并回上一句。

    不并回来的话，「数学课，9 点到 11 点」会拆成一条没有标题的日程和一条没有
    时长信息的日程——两边都不完整。
    """
    clauses: list[str] = []
    for raw in _CLAUSE_SPLIT.split(text):
        piece = raw.strip()
        if not piece:
            continue
        if clauses and _is_fragment(piece):
            clauses[-1] = f"{clauses[-1]}，{piece}"
        else:
            clauses.append(piece)
    return clauses


def _is_fragment(piece: str) -> bool:
    """整句只剩时间或时长——它是上一句的补充，不是独立的一件事。

    「下午跑步，大概半小时」拆开之后，「大概半小时」自己站不住：它没有标题，
    但那个 30 分钟必须还给「跑步」，否则跑步会被排成默认的 40 分钟。
    """
    spans = [(s, e) for s, e, _ in _time_tokens(piece)]
    remainder = _strip_spans(piece, spans)
    _, remainder = _extract_duration(remainder)
    return len(_FILLER.sub("", remainder).strip(" ，,。、；;的")) < 2 and bool(spans or remainder != piece)


_FILLER = re.compile(r"大概|大约|差不多|左右|前后|可能|估计|应该|先|再|然后")


def _time_tokens(text: str) -> list[tuple[int, int, int]]:
    """找出所有明确时刻，返回 ``(起点, 终点, 当天的分钟数)``，按出现顺序。"""
    found: list[tuple[int, int, int]] = []
    for match in _CLOCK_RE.finditer(text):
        hour = int(match.group(1))
        minute = int(match.group(2))
        if hour <= 24 and minute <= 59:
            found.append((match.start(), match.end(), (hour % 24) * 60 + minute))
    for match in _OCLOCK_RE.finditer(text):
        hour = _to_int(match.group(1))
        if hour is None or hour > 24:
            continue
        raw_minute = match.group(2)
        if raw_minute in (None, ""):
            minute = 0
        elif raw_minute == "半":
            minute = 30
        else:
            value = _to_int(raw_minute)
            if value is None or value > 59:
                continue
            minute = value
        prefix = text[max(0, match.start() - 4):match.start()]
        found.append((match.start(), match.end(), _apply_period(hour, minute, prefix)))
    found.sort(key=lambda item: item[0])
    # 两种写法理论上不会重叠，这里只是别让重复匹配把同一段算两次。
    deduped: list[tuple[int, int, int]] = []
    for token in found:
        if deduped and token[0] < deduped[-1][1]:
            continue
        deduped.append(token)
    return deduped


def _apply_period(hour: int, minute: int, prefix: str) -> int:
    """把「下午三点」补成 15:00；没写上下文的 1–5 点按下午算（学生的「3 点」几乎
    不会指凌晨三点，真要写凌晨会写「凌晨三点」）。"""
    afternoon = any(marker in prefix for marker in _PM_MARKERS)
    morning = any(marker in prefix for marker in _AM_MARKERS)
    if hour <= 11 and afternoon:
        hour += 12
    elif hour == 12 and morning:
        hour = 0
    elif not morning and 1 <= hour <= 5:
        hour += 12
    return hour * 60 + minute


def _to_int(token: Any) -> int | None:
    text = str(token or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    return _NUMERALS.get(text)


def _strip_spans(text: str, spans: Sequence[tuple[int, int]]) -> str:
    if not spans:
        return text
    kept: list[str] = []
    cursor = 0
    for start, end in sorted(spans):
        if start < cursor:
            continue
        kept.append(text[cursor:start])
        cursor = end
    kept.append(text[cursor:])
    return "".join(kept)


def _match_period(text: str) -> tuple[str, int, int] | None:
    """找出句子里最先出现的时段词（同一位置取更长的那个：「起床后」优先于「起」）。"""
    best: tuple[int, str, int, int] | None = None
    for token, hour, minute in _PERIOD_TOKENS:
        index = text.find(token)
        if index < 0:
            continue
        if best is None or index < best[0] or (index == best[0] and len(token) > len(best[1])):
            best = (index, token, hour, minute)
    if best is None:
        return None
    return (best[1], best[2], best[3])


def _extract_duration(text: str) -> tuple[int | None, str]:
    """从文本里取时长，并把它从标题里减掉。

    「一个半小时」必须排在「半小时」前面，否则前者会被读成 30 分钟。
    """
    for pattern, minutes in ((re.compile(r"一?个半\s*(?:个)?\s*小时"), 90), (re.compile(r"半\s*个?\s*小时"), 30)):
        match = pattern.search(text)
        if match:
            return minutes, _strip_spans(text, [(match.start(), match.end())]).strip()
    match = _HOUR_RE.search(text)
    if match:
        hours = float(match.group(1))
        return int(round(hours * 60)), _strip_spans(text, [(match.start(), match.end())]).strip()
    match = _MINUTE_RE.search(text)
    if match:
        return int(match.group(1)), _strip_spans(text, [(match.start(), match.end())]).strip()
    return None, text


_LEAD_NOISE = (
    "然后", "接着", "之后", "打算", "准备", "计划", "需要", "全部", "全都",
    "今天", "明天", "后天", "今日", "明日", "明早", "明晚", "还有",
    "再", "就", "要", "去", "我", "在", "有", "是", "把", "得", "会", "想", "的",
)


def _clean_title(text: str) -> str:
    title = _FILLER.sub("", str(text or ""))
    title = re.sub(r"\s+", " ", title).strip(" -—–~～·:：,，。、的了吧呢到至")
    changed = True
    while changed and len(title) > 2:
        changed = False
        for noise in _LEAD_NOISE:
            if title.startswith(noise) and len(title) - len(noise) >= 2:
                title = title[len(noise):]
                changed = True
    return title.strip(" -—–~～·:：,，。、的了吧呢")


def _parse_clause(clause: str) -> dict[str, Any] | None:
    """一句话 → 一条日程草稿（时间还可能是空的，等下一步按顺序补）。

    不构成日程的句子在这里就被挡掉——这是整个本地解析里最要紧的一条：
    「今天有点累」「别太吵」不是日程，把心情句排成 15:00 的「一件事」，
    比什么都不排更糟。判据是**要么有时间、要么有事情**（能动出类别关键词）。
    """
    tokens = _time_tokens(clause)
    spans: list[tuple[int, int]] = []
    start_min: int | None = None
    end_min: int | None = None
    if tokens:
        first = tokens[0]
        # 「写作业到十点」里的十点是**结束**时刻，不是开始。前面挂着连接词的
        # 时间不属于开始时间，交给时段词去定开始。
        if _TRAILING_CONNECTOR.search(clause[:first[0]]):
            end_min = first[2]
            spans.append((first[0], first[1]))
        else:
            start_min = first[2]
            spans.append((first[0], first[1]))
            if len(tokens) > 1 and _RANGE_CONNECTOR.match(clause[first[1]:tokens[1][0]]):
                end_min = tokens[1][2]
                spans.append((tokens[1][0], tokens[1][1]))
                # 连接词本身也要从标题里去掉，否则会剩下「到数学」这种残句。
                spans.append((first[1], tokens[1][0]))

    # 「晚上写作业到十点」：开始取时段词的 19:30，结束取那个「十点」。十点离
    # 时段词太远，取不到「晚上」这层上下文，会算成上午 10:00——这里按开始时刻
    # 把明显早了半天的结束时间推到下午。
    period = _match_period(clause)
    if start_min is None and period is not None:
        start_min = period[1] * 60 + period[2]
    if end_min is not None and start_min is not None and end_min <= start_min and end_min + 12 * 60 > start_min:
        end_min += 12 * 60

    remainder = _strip_spans(clause, spans)
    duration, remainder = _extract_duration(remainder)
    title = _clean_title(remainder)
    if period is not None:
        title = _drop_time_words(title)
    all_day = bool(_ALL_DAY.search(title))
    if all_day:
        title = _drop_time_words(_ALL_DAY.sub("", title, count=1))
    if len(title) < 2:
        return None

    category = _category_of(title)
    if start_min is None and end_min is None and category == "other":
        # 既说不出时间、又认不出是件什么事：这是心情、喜好或者客套话。
        return None
    if all_day and category != "other" and end_min is None:
        end_min = _all_day_end(start_min)
    return {
        "title": title[:24],
        "start_min": start_min,
        "end_min": end_min,
        "duration": duration,
        "all_day": all_day and category != "other",
        "category": category,
        "priority": _priority_of(title),
        "interruptible": bool(_FOCUS_RULES.search(title)) and not _HARD_RULES.search(title),
    }


def _all_day_end(start_min: int | None) -> int | None:
    """「今天全是自习」这类整块时间的收尾时刻；起头还没定就先不定。"""
    if start_min is None:
        return None
    return max(start_min + 60, min(start_min + _ALL_DAY_MINUTES, _ALL_DAY_END_MINUTE))


def _drop_time_words(title: str) -> str:
    """把标题里剩下的时段词去掉：「下午跑步」→「跑步」。

    只在还剩两个字的时候才删——「午休」「晚饭」这种时段词本身就是这件事的名字，
    删掉就什么都不剩了。
    """
    for token, _, _ in _PERIOD_TOKENS:
        while token in title and len(title) - len(token) >= 2:
            title = _clean_title(title.replace(token, "", 1))
    return title.strip()


def _category_of(title: str) -> str:
    for pattern, category in _CATEGORY_RULES:
        if pattern.search(title):
            return category
    return "other"


def _priority_of(title: str) -> str:
    if _HARD_RULES.search(title):
        return "hard"
    if _FOCUS_RULES.search(title):
        return "hard"
    return "soft"


def _build_events(drafts: list[dict[str, Any]], *, day: date_cls, moment: datetime) -> list[dict[str, Any]]:
    """补时间、排序、截尾，再交给 ``CalendarEvent`` 校验成可直接回传的 JSON。"""
    if not drafts:
        return []
    drafts = drafts[:MAX_EVENTS]

    # 第一遍（按原句顺序）：没写时间的接在上一件之后。
    #
    # cursor 只在**上一件**身上取值，不跟「此刻之后的整半点」取 max：那句
    # 「9:00 数学课，10 点英语课，然后写作业」的写作业，应该接在 10:45，
    # 而不是被下午两点半的下界拽走。
    cursor: int | None = None
    for draft in drafts:
        if draft["start_min"] is None:
            draft["start_min"] = cursor if cursor is not None else _base_minute(moment)
        if draft["end_min"] is None and draft.get("all_day"):
            draft["end_min"] = _all_day_end(draft["start_min"])
        length = draft["duration"] or DEFAULT_DURATION[draft["category"]]
        finish = draft["end_min"] if draft["end_min"] is not None else draft["start_min"] + length
        cursor = max(finish, draft["start_min"] + MIN_DURATION)

    # 第二遍（按时间排序）：结尾撞到下一件的，截到下一件的开头。
    drafts.sort(key=lambda item: (item["start_min"], item["title"]))
    events: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for index, draft in enumerate(drafts):
        start_min = draft["start_min"]
        length = draft["duration"] or DEFAULT_DURATION[draft["category"]]
        end_min = draft["end_min"] if draft["end_min"] is not None else start_min + length
        if end_min <= start_min:
            end_min = start_min + max(MIN_DURATION, length)
        if index + 1 < len(drafts):
            nxt = drafts[index + 1]["start_min"]
            if start_min < nxt < end_min and nxt - start_min >= MIN_DURATION:
                end_min = nxt
        end_min = min(end_min, 24 * 60 - 1)
        if end_min <= start_min:
            continue
        key = (draft["title"], start_min)
        if key in seen:
            continue
        seen.add(key)

        start_dt = _at(day, start_min, moment)
        event = CalendarEvent(
            id=_event_id(day, draft["title"], start_dt),
            title=draft["title"],
            start=start_dt.isoformat(),
            end=_at(day, end_min, moment).isoformat(),
            category=draft["category"],
            priority=draft["priority"],
            interruptible=draft["interruptible"],
            source="manual",
            description="",
            metadata={"via": "agent"},
        )
        events.append(event.to_dict())
    return events


def _base_minute(moment: datetime) -> int:
    """一件都没写时间时的起步点：此刻之后的第一个整半点，且不早于 9 点。

    取「此刻之后」是因为用户多半在说「接下来干嘛」；保留 9 点这个下界是为了
    清早和深夜写下的计划不会被排到 00:15 这种没人会用的时刻上。
    """
    minute = moment.hour * 60 + moment.minute
    rounded = ((minute + 29) // 30) * 30
    if rounded >= 24 * 60:
        return FALLBACK_START_MINUTE
    return max(rounded, FALLBACK_START_MINUTE)


def _at(day: date_cls, minute: int, moment: datetime) -> datetime:
    hour, minute_of_hour = divmod(max(0, min(minute, 24 * 60 - 1)), 60)
    aware = datetime.combine(day, clock_time(hour, minute_of_hour))
    return aware.replace(tzinfo=moment.tzinfo)


def _event_id(day: date_cls, title: str, start: datetime) -> str:
    """同一天、同一件事、同一时刻 → 同一个 id：改一遍再说一次是覆盖，不是叠加。"""
    key = f"music-companion:agent:{day.isoformat()}:{title}:{start.isoformat()}"
    return f"agent-{uuid5(NAMESPACE_URL, key).hex[:12]}"


# ── 状态、心情与音乐 ──────────────────────────────────────────


def _day_offset(text: str) -> int:
    for pattern, offset in _DAY_OFFSETS:
        if pattern.search(text):
            return offset
    return 0


def _energy_of(text: str) -> int | None:
    if _LOW_ENERGY.search(text):
        return 28
    if _HIGH_ENERGY.search(text):
        return 78
    return None


def _stress_of(text: str) -> int | None:
    if _HIGH_STRESS.search(text):
        return 72
    if _CALM.search(text):
        return 20
    return None


def _mood_of(text: str) -> str:
    if _HIGH_STRESS.search(text):
        return "焦虑"
    if _LOW_ENERGY.search(text):
        return "疲惫"
    if _HIGH_ENERGY.search(text) or re.search(r"开心|高兴|兴奋|期待", text):
        return "开心"
    return "平静"


def _scene_of(drafts: list[dict[str, Any]]) -> str:
    """今天的主场景：有跑步就跑步，有走路就走路，其余看是不是久坐的活。

    只看排出来的日程标题，不看原话——「今天不想跑步」里的「跑」不该把整天
    的音乐带成奔跑。
    """
    titles = "".join(draft["title"] for draft in drafts)
    if re.search(r"跑|马拉松|jog|running|球|游泳|跳绳|体育|体测", titles):
        return "跑步"
    if re.search(r"散步|走路|走一|遛|漫步|爬山", titles):
        return "行走"
    if re.search(r"通勤|路上|公交|地铁|回家|上学", titles):
        return "通勤"
    if drafts and all(draft["category"] in {"study", "work"} for draft in drafts):
        return "久坐"
    return "通用"


def _time_context_of(drafts: list[dict[str, Any]], moment: datetime) -> str:
    """取今天日程里出现最多的那个时段；没有日程就用此刻。

    用「出现最多」而不是「最早」：一天里 19:00 的晚自习和 20:00 的作业占大头时，
    整天的音乐应该偏晚间，而不是被早上那一节课拉走。
    """
    contexts = [
        _time_context(min(draft["start_min"] // 60, 23))
        for draft in drafts
        if draft["start_min"] is not None
    ]
    if not contexts:
        return _time_context(moment.hour)
    counts: dict[str, int] = {}
    for name in contexts:
        counts[name] = counts.get(name, 0) + 1
    return sorted(counts.items(), key=lambda pair: (-pair[1], contexts.index(pair[0])))[0][0]


def _time_context(hour: int) -> str:
    if 5 <= hour < 9:
        return "早晨"
    if 9 <= hour < 12:
        return "上午"
    if 12 <= hour < 18:
        return "午后"
    if 18 <= hour < 22:
        return "晚间"
    return "夜间"


def _choose_soundscape(
    drafts: list[dict[str, Any]],
    *,
    scene: str,
    mood: str,
    energy: int | None,
    stress: int | None,
    moment: datetime,
) -> tuple[str, str]:
    """挑今天的音景，并给出一句人能听懂的理由。

    打分本身复用 ``soundscape.select_soundscape``（时段 3 / 心情 2 / 场景 2 / 精力 1），
    这里只负责把「用户说的那些话」翻译成它要的四个标签，以及说清楚为什么。

    「要跑步」这种明确说出口的场景按 ``explicit`` 处理，直接钉到奔跑——打分表
    里奔跑和疾走同分（都是 3+2），字典序会让疾走赢，而这两者差着 30 拍。
    和 ``day_plan._walk_soundscape`` 一样，累了或压力大就不钉：那种时候被一首
    160 拍的曲子推着跑只会更难受。
    """
    context = _time_context_of(drafts, moment)
    energy_tag = energy if energy is not None else 50
    exhausted = (energy is not None and energy <= 35) or (stress is not None and stress >= 65)
    explicit = None
    if not exhausted:
        if scene == "跑步":
            explicit = "run"
        elif scene == "行走":
            explicit = "brisk"
    scape_id = select_soundscape(
        time_context=context, mood=mood, scene=scene, energy=energy_tag, explicit=explicit
    )

    if scape_id == "run":
        why = "你要跑，拍子给到能一步一拍的速度"
    elif scape_id == "brisk":
        why = "有走路的一段，鼓点密度按步子来"
    elif exhausted:
        why = "你说累，速度压在区间下沿"
    elif scene == "久坐":
        why = "大半时间在桌前，挑的是不抢注意力的那种"
    else:
        why = f"没有特别的信号，按{context}的时段挑的"
    return scape_id, why


def _preferences(text: str) -> dict[str, list[str]]:
    return {
        "likes": _clean_preferences(_LIKES, text),
        "avoids": _clean_preferences(_AVOIDS, text),
    }


def _clean_preferences(pattern: re.Pattern[str], text: str) -> list[str]:
    """「想听点安静的钢琴」→「安静的钢琴」：去掉量词和语气词，留下的才是能被
    记住的偏好。"""
    found = []
    for match in pattern.finditer(text):
        item = match.group(1).strip().strip("的了些点一 ")
        if len(item) >= 2:
            found.append(item[:8])
    return found[:4]


def _reply_text(
    events: list[dict[str, Any]],
    music: Mapping[str, Any],
    state: Mapping[str, Any],
    *,
    day_offset: int,
    day: date_cls,
) -> str:
    when = {0: "今天", 1: "明天", 2: "后天"}.get(day_offset, f"{day.month} 月 {day.day} 日")
    parts: list[str] = []
    if events:
        listed = "、".join(f"{item['start'][11:16]} {item['title']}" for item in events[:4])
        tail = f"，等 {len(events)} 件" if len(events) > 4 else ""
        parts.append(f"{when}记下了 {len(events)} 件事：{listed}{tail}。")
    else:
        parts.append(f"{when}没排上具体的安排，先把音乐备着。")

    scape = SOUNDSCAPES[music["soundscape"]]
    drums = _DRUM_PHRASE.get(music.get("drums") or "", "")
    parts.append(f"听「{scape.name}」，{music['bpm']} BPM{'，' + drums if drums else ''}——{music['why']}。")
    # 「你说累」在理由里已经说过一次就不再重复——同一句话换个说法讲两遍，
    # 是最像机器的写法。
    if state.get("energy") is not None and state["energy"] <= 35 and "累" not in str(music["why"]):
        parts.append("你说累，快的地方我都往下压了一档。")
    return "".join(parts)


def _catalog_text() -> str:
    """给模型看的音景目录：id + 标签 + BPM 区间，让它只能在真实存在的音景里挑。"""
    lines = []
    for soundscape_id in sorted(SOUNDSCAPES):
        scape = SOUNDSCAPES[soundscape_id]
        lines.append(
            f"- {soundscape_id}={scape.name} | {'/'.join(scape.when)} | {'/'.join(scape.moods)} | "
            f"{'/'.join(scape.scenes)} | {scape.bpm_range[0]}-{scape.bpm_range[1]} | {scape.character}"
        )
    return "\n".join(lines)


# ── 模型结果的校验与合并 ──────────────────────────────────────


def _merge_ai(local: dict[str, Any], payload: Mapping[str, Any], *, now: datetime) -> dict[str, Any]:
    """逐字段校验模型输出，不合法的一律保留本地值。

    刻意**不**在合并后再跑一遍本地解析：模型漏掉某一件事，那是模型的理解问题，
    应该被看见（用户会发现并补一句），而不是被本地正则悄悄补回来变成一份谁也说不清
    来历的混合日程。
    """
    merged = dict(local)
    merged["source"] = "ai"
    merged["fallback_reason"] = None

    reply = str((payload or {}).get("reply") or "").strip()
    if 0 < len(reply) <= 200:
        merged["reply"] = reply

    events = _normalize_ai_events((payload or {}).get("events"), now=now)
    if events:
        merged["events"] = events
    else:
        merged["fallback_reason"] = "模型没读出行程，用的是本地规则"

    music = _normalize_ai_music((payload or {}).get("music"), merged["music"])
    merged["music"] = music

    state = dict(local["state"])
    ai_state = (payload or {}).get("state") or {}
    for field in ("energy", "stress"):
        value = _safe_score(ai_state.get(field))
        if value is not None:
            state[field] = value
    merged["state"] = state

    preferences = dict(local["preferences"])
    ai_preferences = (payload or {}).get("preferences") or {}
    for field in ("likes", "avoids"):
        values = [str(item).strip()[:12] for item in (ai_preferences.get(field) or []) if str(item).strip()]
        if values:
            preferences[field] = values[:4]
    merged["preferences"] = preferences
    return merged


def _normalize_ai_music(raw: Any, fallback: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        return dict(fallback)
    soundscape = str(raw.get("soundscape") or "").strip().lower()
    if soundscape not in SOUNDSCAPES:
        soundscape = str(fallback["soundscape"])
    low, high = SOUNDSCAPES[soundscape].bpm_range
    bpm = _safe_score(raw.get("bpm"))
    if bpm is None:
        bpm = int(fallback["bpm"]) if soundscape == fallback["soundscape"] else (low + high) // 2
    bpm = max(low, min(high, int(bpm)))

    drums = raw.get("drums")
    drums = str(drums).strip().lower() if drums is not None else None
    if drums not in DRUM_LEVELS:
        drums = fallback.get("drums")

    why = str(raw.get("why") or "").strip()
    if not why or len(why) > 60:
        why = str(fallback["why"])
    return {"soundscape": soundscape, "drums": drums, "bpm": bpm, "why": why}


def _normalize_ai_events(raw: Any, *, now: datetime) -> list[dict[str, Any]]:
    """模型给的是 ``HH:MM``，日期与时区在这里补齐——模型不碰 ISO8601。"""
    if not isinstance(raw, list):
        return []
    drafts: list[dict[str, Any]] = []
    for item in raw[:MAX_EVENTS]:
        if not isinstance(item, Mapping):
            continue
        title = str(item.get("title") or "").strip()[:24]
        start_min = _parse_clock(item.get("start"))
        if not title or start_min is None:
            continue
        end_min = _parse_clock(item.get("end"))
        category = str(item.get("category") or "").strip().lower()
        if category not in DEFAULT_DURATION:
            category = _category_of(title)
        priority = str(item.get("priority") or "").strip().lower()
        if priority not in {"hard", "soft"}:
            priority = _priority_of(title)
        if end_min is None or end_min <= start_min:
            end_min = start_min + DEFAULT_DURATION[category]
        elif end_min - start_min < MIN_DURATION:
            end_min = start_min + MIN_DURATION
        drafts.append({
            "title": title,
            "start_min": start_min,
            "end_min": min(end_min, 24 * 60 - 1),
            "category": category,
            "priority": priority,
            "interruptible": bool(_FOCUS_RULES.search(title)) and not _HARD_RULES.search(title),
        })

    day = now.date()
    events: list[dict[str, Any]] = []
    for draft in drafts:
        start_dt = _at(day, draft["start_min"], now)
        event = CalendarEvent(
            id=_event_id(day, draft["title"], start_dt),
            title=draft["title"],
            start=start_dt.isoformat(),
            end=_at(day, draft["end_min"], now).isoformat(),
            category=draft["category"],
            priority=draft["priority"],
            interruptible=draft["interruptible"],
            source="manual",
            description="",
            metadata={"via": "agent"},
        )
        events.append(event.to_dict())
    return events


def _parse_clock(value: Any) -> int | None:
    """``"9:30"`` / ``"9点半"`` / ``9`` → 当天的分钟数。"""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        hour = int(value)
        return hour * 60 if 0 <= hour <= 24 else None
    text = str(value or "").strip()
    if not text:
        return None
    match = _CLOCK_RE.fullmatch(text)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        return hour * 60 + minute if hour <= 24 and minute <= 59 else None
    tokens = _time_tokens(text)
    return tokens[0][2] if tokens else None


def _safe_score(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return None
    if not 0 <= number <= 100:
        return None
    return number


def _as_datetime(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip().replace("Z", "+00:00")
        if not text:
            raise ValueError("now 不能为空")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError("now 不是合法的 ISO8601 时间") from exc
    if parsed.tzinfo is None:
        raise ValueError("now 必须包含时区")
    return parsed


def _as_date(value: str | None, moment: datetime) -> date_cls:
    text = str(value or "").strip()
    if not text:
        return moment.date()
    try:
        return date_cls.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("date 必须是 YYYY-MM-DD") from exc
