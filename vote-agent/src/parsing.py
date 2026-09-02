"""입력 파싱 — LLM 없이 정규식으로 처리한다.

## 구분자를 쓰지 않는다

처음에는 `제목 / 항목1, 항목2 / 마감` 형태로 `/` 를 구분자로 썼는데 실패했다.

- `9/10(목) 회식 메뉴 선정` — 날짜가 든 제목을 쓸 수 없다
- `땡땡식당 (https://naver.me/xxx)` — 항목에 링크를 넣을 수 없다

`#`, `!`, `|` 로 바꿔도 같은 문제가 반복된다. 제목과 항목에는 **어떤 문자든** 들어갈 수 있다.

그래서 **줄바꿈**을 구조로 쓴다. 줄바꿈은 제목이나 URL 안에 들어갈 수 없으므로
충돌이 구조적으로 불가능하다.

    @투표만들기 9/10(목) 회식 메뉴 선정        <- 첫 줄 = 제목 (문자 제약 없음)
    땡땡식당 (https://naver.me/GdymUOVY)      <- 각 줄 = 항목 하나 (URL 자유)
    무슨식당 (https://naver.me/Fk738sTW)
    마감: 금요일 18시                          <- 지시어
    복수

제목만 오면 **폼을 제목 채워서 연다.** 파싱할 것이 없으니 위험도 없다.
Teams 가 줄바꿈을 삼켜서 한 줄로 오더라도 이 경로로 떨어지므로 조용히 잘못
해석되는 일이 없다.

마감 표현: `금요일 18시`, `8/22 18:00`, `내일 14시 30분`, `3시간`, `2일`, `18:00`
시각은 KST 로 해석하고 저장은 UTC 다. 애매하면 **파싱 실패로 처리**한다 —
엉뚱한 시각으로 조용히 마감되는 것이 더 나쁘다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")

MAX_OPTIONS = 10
MAX_TITLE = 80
MAX_LABEL = 60

MULTI_WORDS = ("복수", "다중", "multi", "여러")

# 지시어 줄. 나머지 줄은 모두 항목으로 본다.
DEADLINE_LINE = re.compile(r"^(?:마감|마감일|마감시각|deadline|due)\s*[:：]\s*(.*)$", re.IGNORECASE)
MULTI_LINE = re.compile(r"^(?:복수|복수선택|다중|다중선택|중복|multi|multiple)"
                        r"\s*(?:선택|가능|허용)?\s*$", re.IGNORECASE)

# "1/5" 를 12월에 적으면 내년으로 보지만, 하루 이틀 지난 날짜는 오타로 보고 거절한다.
YEAR_ROLLOVER_DAYS = 60

_WEEKDAYS = {
    "월": 0, "화": 1, "수": 2, "목": 3, "금": 4, "토": 5, "일": 6,
}


def _now_kst() -> datetime:
    return datetime.now(KST)


def _to_utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc)


def parse_deadline(text: str, now: datetime | None = None) -> tuple[datetime | None, str | None]:
    """마감 시각을 해석한다.

    반환: (UTC datetime 또는 None, 오류 메시지 또는 None)
    빈 문자열은 "마감 없음" 이므로 오류가 아니다.
    """
    raw = (text or "").strip()
    if not raw:
        return None, None

    now = now or _now_kst()

    # 상대 시간: "3시간", "30분", "2일"
    m = re.fullmatch(r"(\d+)\s*(분|시간|일)", raw)
    if m:
        amount, unit = int(m.group(1)), m.group(2)
        delta = {"분": timedelta(minutes=amount), "시간": timedelta(hours=amount),
                 "일": timedelta(days=amount)}[unit]
        if delta <= timedelta(0):
            return None, "마감까지 남은 시간이 0 이하입니다."
        return _to_utc(now + delta), None

    # 시각 부분을 먼저 떼어낸다: "18시", "18시 30분", "18:00", "오후 6시"
    hour, minute = None, 0
    pm = bool(re.search(r"오후|저녁|밤", raw))
    am = bool(re.search(r"오전|아침", raw))
    # 오전/오후 단어는 해석에 반영한 뒤 지운다. 남겨두면 "이해 못 함" 으로 잘못 떨어진다.
    raw = re.sub(r"오후|저녁|밤|오전|아침", " ", raw)

    tm = re.search(r"(\d{1,2})\s*:\s*(\d{2})", raw)
    if tm:
        hour, minute = int(tm.group(1)), int(tm.group(2))
        raw = raw[:tm.start()] + " " + raw[tm.end():]
    else:
        tm = re.search(r"(\d{1,2})\s*시(?:\s*(\d{1,2})\s*분)?", raw)
        if tm:
            hour = int(tm.group(1))
            minute = int(tm.group(2) or 0)
            raw = raw[:tm.start()] + " " + raw[tm.end():]

    if hour is not None:
        if pm and hour < 12:
            hour += 12
        if am and hour == 12:
            hour = 0
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None, f"시각을 이해하지 못했습니다: {text!r}"

    rest = raw.strip()

    # 날짜 부분
    date = None
    dm = re.search(r"(\d{4})\s*[-./]\s*(\d{1,2})\s*[-./]\s*(\d{1,2})", rest)
    if dm:
        try:
            date = datetime(int(dm.group(1)), int(dm.group(2)), int(dm.group(3)), tzinfo=KST)
        except ValueError:
            return None, f"날짜를 이해하지 못했습니다: {text!r}"
    else:
        dm = re.search(r"(\d{1,2})\s*[-./월]\s*(\d{1,2})\s*일?", rest)
        if dm:
            month, day = int(dm.group(1)), int(dm.group(2))
            year = now.year
            try:
                date = datetime(year, month, day, tzinfo=KST)
            except ValueError:
                return None, f"날짜를 이해하지 못했습니다: {text!r}"
            # 지난 날짜를 무조건 내년으로 넘기면 오타를 조용히 삼킨다.
            # 12월에 "1/5" 를 적는 건 내년이 맞지만, 8/20 에 "8/19" 는 오타일 가능성이 크다.
            # 많이 지난 경우에만 내년으로 본다.
            days_past = (now.date() - date.date()).days
            if days_past > YEAR_ROLLOVER_DAYS:
                date = date.replace(year=year + 1)
        elif "모레" in rest:
            date = now + timedelta(days=2)
        elif "내일" in rest:
            date = now + timedelta(days=1)
        elif "오늘" in rest:
            date = now
        else:
            wm = re.search(r"([월화수목금토일])\s*요?일?", rest)
            if wm:
                target = _WEEKDAYS[wm.group(1)]
                ahead = (target - now.weekday()) % 7
                # 같은 요일이고 시각이 이미 지났으면 다음 주로
                if ahead == 0 and hour is not None:
                    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                    if candidate <= now:
                        ahead = 7
                date = now + timedelta(days=ahead)
            elif rest:
                return None, f"마감 표현을 이해하지 못했습니다: {text!r}"

    if date is None and hour is None:
        return None, f"마감 표현을 이해하지 못했습니다: {text!r}"

    if date is None:
        date = now
    if hour is None:
        hour, minute = 23, 59

    result = date.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if result <= now:
        # 시각만 준 경우엔 다음 날로 넘긴다. 날짜까지 명시했다면 오류다.
        if dm is None and not re.search(r"내일|모레|오늘|[월화수목금토일]\s*요?일", text):
            result += timedelta(days=1)
        else:
            return None, f"이미 지난 시각입니다: {result.strftime('%m/%d %H:%M')}"

    return _to_utc(result), None


@dataclass
class Request:
    """채팅 한 건을 해석한 결과.

    셋 중 하나로 끝난다.
      - `error`      : 사용자에게 문제를 알린다
      - `needs_form` : 폼을 (있는 값으로 미리 채워서) 연다
      - 그 외        : 바로 투표를 만든다
    """

    title: str = ""
    options: list[str] = field(default_factory=list)
    multi_select: bool = False
    closes_at: datetime | None = None
    deadline_text: str = ""
    error: str | None = None
    needs_form: bool = False
    note: str | None = None


def normalize_lines(text: str) -> list[str]:
    """줄 목록으로 만든다.

    Teams 의 `textFormat` 은 `plain` 일 수도 `xml` 일 수도 있어서 줄바꿈이
    `\n` 으로 오거나 `<br>` 로 온다. 둘 다 처리한다.
    """
    raw = text or ""
    raw = re.sub(r"<br\s*/?>", "\n", raw, flags=re.IGNORECASE)
    raw = re.sub(r"</(?:div|p|li)>", "\n", raw, flags=re.IGNORECASE)
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    return [line.strip() for line in raw.split("\n") if line.strip()]


def _split_legacy(line: str) -> dict | None:
    """옛 `제목 / 항목1, 항목2 / 마감` 문법으로 보이면 최선으로 분해한다.

    확신할 수 없는 추측이므로 **투표를 바로 만들지 않고 폼을 채우는 데만** 쓴다.
    틀려도 사용자가 폼에서 고치면 되니 손해가 없다.

    `9/10(목) 회식 메뉴 선정` 처럼 날짜가 든 제목은 항목 자리에 쉼표가 없으므로
    옛 문법으로 오해하지 않는다.
    """
    if "/" not in line:
        return None
    parts = [p.strip() for p in line.split("/")]
    if len(parts) < 2 or not parts[0]:
        return None
    if "," not in parts[1] and "，" not in parts[1]:
        return None
    options = parse_options_block(parts[1])
    if len(options) < 2:
        return None
    tail = " ".join(parts[2:])
    return {"title": parts[0], "options": options, "tail": tail}


def parse_request(text: str) -> Request:
    """멘션을 제거한 텍스트를 해석한다."""
    lines = normalize_lines(text)

    if not lines:
        return Request(needs_form=True)

    # 한 줄뿐이면 항목이 없다. 폼을 제목 채워서 연다.
    if len(lines) == 1:
        legacy = _split_legacy(lines[0])
        if legacy:
            # '복수' 같은 지시어는 마감 칸에 남기지 않는다. 폼의 토글로 옮긴다.
            deadline_only = " ".join(_strip_multi_words(legacy["tail"]).split())
            deadline, error = parse_deadline(deadline_only)
            return Request(
                title=legacy["title"], options=legacy["options"],
                multi_select=_has_multi_word(legacy["tail"]),
                closes_at=None if error else deadline,
                deadline_text="" if error else deadline_only,
                needs_form=True,
                note="이제 `/` 구분자를 쓰지 않습니다. 값을 옮겨 놓았으니 확인 후 시작하세요. "
                     "다음부터는 **줄바꿈**(Shift+Enter)으로 항목을 나눠주세요.")
        return Request(title=lines[0], needs_form=True)

    title, rest = lines[0], lines[1:]
    if len(title) > MAX_TITLE:
        return Request(error=f"제목이 너무 깁니다 ({len(title)}자 / 최대 {MAX_TITLE}자).")

    options: list[str] = []
    deadline_text = ""
    multi = False
    for line in rest:
        matched = DEADLINE_LINE.match(line)
        if matched:
            deadline_text = matched.group(1).strip()
            continue
        if MULTI_LINE.match(line):
            multi = True
            continue
        options.append(line)

    problem = validate_options(options)
    if problem:
        return Request(error=problem)

    closes_at, error = parse_deadline(deadline_text)
    if error:
        return Request(error=error)

    return Request(title=title, options=options, multi_select=multi,
                   closes_at=closes_at, deadline_text=deadline_text)


def _has_multi_word(text: str) -> bool:
    return any(w in (text or "") for w in MULTI_WORDS)


def _strip_multi_words(text: str) -> str:
    out = text or ""
    for w in MULTI_WORDS:
        out = out.replace(w, " ")
    return out


def validate_options(options: list[str]) -> str | None:
    if len(options) < 2:
        return "항목을 2개 이상 적어주세요. 한 줄에 하나씩 씁니다."
    if len(options) > MAX_OPTIONS:
        return f"항목이 {len(options)}개입니다. 최대 {MAX_OPTIONS}개까지 됩니다."
    if len(set(options)) != len(options):
        return "중복된 항목이 있습니다."
    too_long = [o for o in options if len(o) > MAX_LABEL]
    if too_long:
        return f"항목이 너무 깁니다 (최대 {MAX_LABEL}자): {too_long[0][:20]}…"
    return None


def parse_options_block(text: str) -> list[str]:
    """항목 목록을 만든다. **줄바꿈이 우선**이고, 한 줄일 때만 쉼표로 나눈다.

    항목 이름에 쉼표가 들어갈 수 있다 (`땡땡식당 (강남, 2호점)`).
    줄바꿈으로 나눈 목록을 다시 쉼표로 쪼개면 그런 이름이 깨진다.
    """
    lines = normalize_lines(text)
    if len(lines) >= 2:
        return lines
    single = lines[0] if lines else ""
    return [p.strip() for p in re.split(r"[,，]", single) if p.strip()]


def format_deadline(dt: datetime | None) -> str:
    """UTC 시각을 KST 표시로. `8/22(금) 18:00`"""
    if dt is None:
        return "마감 없음"
    local = dt.astimezone(KST)
    weekday = "월화수목금토일"[local.weekday()]
    return f"{local.month}/{local.day}({weekday}) {local:%H:%M}"
