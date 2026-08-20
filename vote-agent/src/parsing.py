"""입력 파싱 — LLM 없이 정규식으로 처리한다.

한 줄 생성:  제목 / 항목1, 항목2, 항목3 / 마감 / 복수
마감 표현:   `금요일 18시`, `8/22 18:00`, `내일 14시 30분`, `3시간`, `2일`, `18:00`

시각은 KST 로 해석하고 저장은 UTC 다. 애매하면 **파싱 실패로 처리**한다 —
엉뚱한 시각으로 조용히 마감되는 것이 더 나쁘다.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")

MAX_OPTIONS = 10
MAX_TITLE = 80
MAX_LABEL = 60

MULTI_WORDS = ("복수", "다중", "multi", "여러")

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


def parse_one_liner(text: str) -> tuple[dict | None, str | None]:
    """`제목 / 항목1, 항목2 / 마감 / 복수` 를 해석한다.

    반환: (필드 dict 또는 None, 오류 메시지 또는 None)
    `/` 가 없으면 한 줄 생성 시도가 아니므로 (None, None) 을 준다.
    """
    raw = (text or "").strip()
    if "/" not in raw:
        return None, None

    parts = [p.strip() for p in raw.split("/")]
    title = parts[0]
    if not title:
        return None, "제목이 비었습니다."
    if len(title) > MAX_TITLE:
        return None, f"제목이 너무 깁니다 ({len(title)}자 / 최대 {MAX_TITLE}자)."

    options = [o.strip() for o in re.split(r"[,，]", parts[1] if len(parts) > 1 else "") if o.strip()]
    problem = validate_options(options)
    if problem:
        return None, problem

    tail = " ".join(parts[2:])
    multi = any(w in tail for w in MULTI_WORDS)
    for w in MULTI_WORDS:
        tail = tail.replace(w, " ")
    deadline, error = parse_deadline(tail)
    if error:
        return None, error

    return {"title": title, "options": options, "multi_select": multi,
            "closes_at": deadline}, None


def validate_options(options: list[str]) -> str | None:
    if len(options) < 2:
        return "항목을 2개 이상 적어주세요. 쉼표로 구분합니다."
    if len(options) > MAX_OPTIONS:
        return f"항목이 {len(options)}개입니다. 최대 {MAX_OPTIONS}개까지 됩니다."
    if len(set(options)) != len(options):
        return "중복된 항목이 있습니다."
    too_long = [o for o in options if len(o) > MAX_LABEL]
    if too_long:
        return f"항목이 너무 깁니다 (최대 {MAX_LABEL}자): {too_long[0][:20]}…"
    return None


def parse_options_block(text: str) -> list[str]:
    """폼에서 받은 여러 줄 또는 쉼표 구분 항목을 목록으로 만든다."""
    parts = re.split(r"[\n,，]", text or "")
    return [p.strip() for p in parts if p.strip()]


def format_deadline(dt: datetime | None) -> str:
    """UTC 시각을 KST 표시로. `8/22(금) 18:00`"""
    if dt is None:
        return "마감 없음"
    local = dt.astimezone(KST)
    weekday = "월화수목금토일"[local.weekday()]
    return f"{local.month}/{local.day}({weekday}) {local:%H:%M}"
