"""vote-agent 자체 검증. 의존성 없이 돌아간다.

    ./.venv/bin/python vote-agent/tests/check.py

픽스처는 **실제 API 가 주는 타입**으로 만든다 (DEV_RULE 규칙 6).
DB 는 임시 파일을 쓰고 프로젝트의 data/votes.db 는 건드리지 않는다.
"""

import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ["VOTE_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "check.db")

from microsoft_teams.api import (  # noqa: E402
    AdaptiveCardInvokeActivity, MessageActivity,
)
from microsoft_teams.api.activities.sent_activity import SentActivity  # noqa: E402

import cards  # noqa: E402
import parsing as P  # noqa: E402

KST = ZoneInfo("Asia/Seoul")
PASS, FAIL = [], []

BOT = {"id": "28:bot", "name": "투표만들기", "role": "bot"}
PEOPLE = {
    "owner": ("29:owner", "샘플2 (Echo Sample)"),
    "alice": ("29:alice", "더미1 (Foxtrot Dummy)"),
    "bob": ("29:bob", "테스터3 (Charlie Tester)"),
}
CONVERSATION = "19:example-group-chat@thread.v2"
SERVICE_URL = "https://smba.trafficmanager.net/kr/<테넌트-GUID>/"


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '❌'} {name}{f'  — {detail}' if detail else ''}")


def section(title: str) -> None:
    print(f"\n── {title} " + "─" * max(0, 54 - len(title)))


_seq = 0


def message(text: str, who: str = "owner") -> MessageActivity:
    global _seq
    _seq += 1
    user_id, user_name = PEOPLE[who]
    return MessageActivity.model_validate({
        "type": "message", "id": f"msg{_seq}", "text": f"<at>투표만들기</at> {text}",
        "serviceUrl": SERVICE_URL,
        "from": {"id": user_id, "name": user_name}, "recipient": BOT,
        "conversation": {"id": CONVERSATION, "conversationType": "groupChat", "isGroup": True},
        "entities": [{"type": "mention", "mentioned": BOT, "text": "<at>투표만들기</at>"}],
    })


def invoke(verb: str, data: dict, who: str = "owner",
           reply_to: str = "card1") -> AdaptiveCardInvokeActivity:
    global _seq
    _seq += 1
    user_id, user_name = PEOPLE[who]
    return AdaptiveCardInvokeActivity.model_validate({
        "type": "invoke", "id": f"inv{_seq}", "name": "adaptiveCard/action",
        "serviceUrl": SERVICE_URL, "replyToId": reply_to,
        "from": {"id": user_id, "name": user_name}, "recipient": BOT,
        "conversation": {"id": CONVERSATION, "conversationType": "groupChat", "isGroup": True},
        "value": {"action": {"type": "Action.Execute", "verb": verb, "data": data}},
    })


class FakeContext:
    """ctx.send / ctx.api.conversations.activities(...).update 를 기록한다."""

    _next_id = 0

    def __init__(self, activity) -> None:
        self.activity = activity
        self.calls: list[tuple[str, str, object]] = []
        outer = self

        class Activities:
            async def update(self, activity_id, msg):
                outer.calls.append(("UPDATE", activity_id, msg))

        self.api = type("Api", (), {"conversations": type("Conv", (), {
            "activities": staticmethod(lambda cid: Activities()),
        })()})()

    async def send(self, msg):
        FakeContext._next_id += 1
        activity_id = f"card{FakeContext._next_id}"
        self.calls.append(("SEND", activity_id, msg))
        return SentActivity(id=activity_id, activity_params=msg)

    def cards(self) -> list:
        out = []
        for _, _, msg in self.calls:
            for att in (getattr(msg, "attachments", None) or []):
                out.append(att.content)
        return out

    def texts(self) -> list[str]:
        return [(getattr(m, "text", None) or "") for _, _, m in self.calls]


async def route(app_module, ctx):
    """**라우터를 통해** 핸들러를 호출한다.

    핸들러 함수를 직접 부르면 라우팅 조건을 검사하지 못한다. 실제로
    `data["action"]` 을 빼먹어 매칭이 안 되는 버그가 101건 통과 상태에서 살아남았다.
    반환값은 (매칭된 핸들러 수, 마지막 응답).
    """
    handlers = app_module.app.router.select_handlers(ctx.activity)
    result = None
    for handler in handlers:
        result = await handler(ctx)
    return len(handlers), result


def card_text(card) -> str:
    """카드 본문 전체를 하나의 문자열로. dict/모델 양쪽을 받는다."""
    body = card.get("body", []) if isinstance(card, dict) else (card.body or [])
    parts = []
    for item in body:
        text = item.get("text") if isinstance(item, dict) else getattr(item, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts)


def invoke_from_card(card, index: int, who: str = "owner",
                     reply_to: str = "card1") -> AdaptiveCardInvokeActivity:
    """카드에 실제로 실린 액션 데이터로 invoke 를 만든다.

    테스트가 data 를 손으로 짜면 카드와 어긋나도 통과해 버린다.
    Teams 는 카드의 data 를 그대로 되돌려 보내므로 그대로 쓴다.
    """
    action = (card.actions or [])[index]
    return invoke(getattr(action, "verb", None) or "", action_data(action),
                  who=who, reply_to=reply_to)


def action_data(action) -> dict:
    """ExecuteAction.data 는 dict 로 넘겨도 SubmitActionData 모델로 감싸진다.
    (수신 쪽 AdaptiveCardInvokeAction.data 는 평범한 dict 이므로 핸들러는 .get 을 쓴다.)
    """
    data = action.get("data") if isinstance(action, dict) else getattr(action, "data", None)
    if data is None:
        return {}
    if isinstance(data, dict):
        return data
    return data.model_dump(by_alias=True, exclude_none=True)


def card_actions(card) -> list[tuple[str, str]]:
    """(title, verb) 목록."""
    acts = card.get("actions", []) if isinstance(card, dict) else (card.actions or [])
    out = []
    for a in acts:
        if isinstance(a, dict):
            out.append((a.get("title"), a.get("verb")))
        else:
            out.append((getattr(a, "title", None), getattr(a, "verb", None)))
    return out


# ═══════════════════════════════════════════════════════════════════
def test_parsing() -> None:
    now = datetime(2026, 8, 20, 14, 30, tzinfo=KST)   # 목요일

    section("마감 파싱")
    for text, want in [
        ("", "마감 없음"), ("18시", "8/20(목) 18:00"), ("10시", "8/21(금) 10:00"),
        ("금요일 18시", "8/21(금) 18:00"), ("목요일 10시", "8/27(목) 10:00"),
        ("내일 14시 30분", "8/21(금) 14:30"), ("8/22 18:00", "8/22(토) 18:00"),
        ("2026-09-01 09:00", "9/1(화) 09:00"), ("3시간", "8/20(목) 17:30"),
        ("오후 6시", "8/20(목) 18:00"), ("저녁 7시", "8/20(목) 19:00"),
        ("8월 22일 18시", "8/22(토) 18:00"), ("모레 9시", "8/22(토) 09:00"),
    ]:
        dt, err = P.parse_deadline(text, now)
        got = P.format_deadline(dt) if dt else (f"오류: {err}" if err else "마감 없음")
        check(f"{text!r} → {want}", got == want, got if got != want else "")

    section("애매한 마감은 오류로")
    for text in ["아무말", "25시", "13/45", "0분", "다음주쯤", "8/19 10:00"]:
        dt, err = P.parse_deadline(text, now)
        check(f"{text!r} 거절", dt is None and err is not None, err or "통과해버림")

    section("연도 이월 — 오타와 연말을 구분한다")
    year_end = datetime(2026, 12, 20, 14, 30, tzinfo=KST)
    for base, text, want in [
        (now, "8/19 10:00", None),            # 하루 전 → 오타로 보고 거절
        (year_end, "12/19 10:00", None),      # 하루 전 → 거절
        (year_end, "1/5 10:00", "1/5(화) 10:00"),   # 두 달 뒤 → 내년
        (now, "1/5 10:00", "1/5(화) 10:00"),
    ]:
        dt, err = P.parse_deadline(text, base)
        got = P.format_deadline(dt) if dt else None
        check(f"{base:%m/%d} 기준 {text!r} → {want or '거절'}", got == want,
              f"{got or err}")

    section("요청 파싱 — 구분자를 쓰지 않는다")
    # 사용자가 실제로 보고한 깨지던 케이스들
    r = P.parse_request("9/10(목) 회식 메뉴 선정\n삼겹살\n치킨")
    check("날짜가 든 제목(`/` 포함)이 그대로 유지",
          not r.error and not r.needs_form and r.title == "9/10(목) 회식 메뉴 선정"
          and r.options == ["삼겹살", "치킨"], f"{r.title!r} {r.options}")

    r = P.parse_request(
        "9/10(목) 회식 메뉴 선정\n"
        "땡땡식당 (https://naver.me/GdymUOVY)\n"
        "무슨식당 (https://naver.me/Fk738sTW)\n"
        "마감: 금요일 18시\n복수")
    check("항목에 링크(`//` 포함)를 넣을 수 있다",
          r.options == ["땡땡식당 (https://naver.me/GdymUOVY)",
                        "무슨식당 (https://naver.me/Fk738sTW)"], str(r.options))
    check("링크가 있어도 마감·복수 지시어가 해석된다",
          r.multi_select and r.closes_at is not None, f"복수={r.multi_select}")

    r = P.parse_request("긴급! 회식 #3차 어디로?\n삼겹살\n치킨")
    check("제목에 `!` `#` `?` 자유", r.title == "긴급! 회식 #3차 어디로?", r.title)

    r = P.parse_request("매장 선택\n땡땡식당 (강남, 2호점)\n무슨식당 (역삼, 본점)")
    check("항목 이름 안의 쉼표가 보존된다",
          r.options == ["땡땡식당 (강남, 2호점)", "무슨식당 (역삼, 본점)"], str(r.options))

    section("요청 파싱 — 줄바꿈 형태")
    r = P.parse_request("회식<br>삼겹살<br>치킨<br>마감: 3시간")
    check("Teams 가 `<br>` 로 보내도 처리 (textFormat=xml)",
          r.options == ["삼겹살", "치킨"] and r.closes_at is not None, str(r.options))
    r = P.parse_request("회식\r\n삼겹살\r\n치킨")
    check("CRLF 처리", r.options == ["삼겹살", "치킨"], str(r.options))
    r = P.parse_request("회식\n\n삼겹살\n\n\n치킨\n")
    check("빈 줄 무시", r.options == ["삼겹살", "치킨"], str(r.options))

    section("요청 파싱 — 안전한 강등 (파싱 위험 0)")
    r = P.parse_request("9/10(목) 회식 메뉴 선정")
    check("제목만 오면 폼을 제목 채워서 연다",
          r.needs_form and r.title == "9/10(목) 회식 메뉴 선정" and not r.error, f"{r!r}")
    r = P.parse_request("")
    check("빈 입력은 빈 폼", r.needs_form and not r.title and not r.error)

    section("요청 파싱 — 옛 `/` 문법 마이그레이션")
    r = P.parse_request("회식장소 / 삼겹살, 치킨, 초밥 / 금요일 18시 / 복수")
    check("옛 문법은 폼에 옮겨 담는다 (바로 만들지 않는다)", r.needs_form)
    check("제목·항목·복수·마감이 폼에 채워진다",
          r.title == "회식장소" and r.options == ["삼겹살", "치킨", "초밥"]
          and r.multi_select and r.deadline_text == "금요일 18시",
          f"{r.title!r} {r.options} 복수={r.multi_select} 마감={r.deadline_text!r}")
    check("문법이 바뀐 것을 안내한다", r.note is not None and "줄바꿈" in (r.note or ""))
    r = P.parse_request("9/10(목) 회식 메뉴 선정")
    check("날짜 제목을 옛 문법으로 오해하지 않는다",
          r.title == "9/10(목) 회식 메뉴 선정" and not r.options, f"{r.title!r} {r.options}")

    section("요청 파싱 — 거절")
    for text, expect in [("회식\n삼겹살", "2개 이상"),
                         ("회식\n삼겹살\n삼겹살", "중복"),
                         ("회식\n삼겹살\n치킨\n마감: 아무말", "이해하지 못"),
                         ("회식\n" + "\n".join(f"o{i}" for i in range(P.MAX_OPTIONS + 1)), "최대")]:
        r = P.parse_request(text)
        check(f"{text[:24]!r} 거절", r.error is not None and expect in r.error,
              r.error or "통과해버림")
    r = P.parse_request("가" * (P.MAX_TITLE + 1) + "\n삼겹살\n치킨")
    check("제목 길이 초과 거절", r.error is not None and "제목" in r.error, r.error or "")

    section("항목 블록 파싱")
    check("줄바꿈 구분", P.parse_options_block("삼겹살\n치킨\n초밥") == ["삼겹살", "치킨", "초밥"])
    check("한 줄이면 쉼표로 구분", P.parse_options_block("삼겹살, 치킨") == ["삼겹살", "치킨"])
    # 줄바꿈으로 나눈 목록을 다시 쉼표로 쪼개면 `(강남, 2호점)` 같은 이름이 깨진다
    check("줄바꿈이 있으면 쉼표로 재분할하지 않는다",
          P.parse_options_block("땡땡식당 (강남, 2호점)\n무슨식당 (역삼, 본점)")
          == ["땡땡식당 (강남, 2호점)", "무슨식당 (역삼, 본점)"],
          str(P.parse_options_block("땡땡식당 (강남, 2호점)\n무슨식당 (역삼, 본점)")))
    check("`<br>` 도 줄바꿈으로", P.parse_options_block("a<br>b<br>c") == ["a", "b", "c"])


def test_store() -> None:
    from store import Store

    section("저장소")
    store = Store(":memory:")
    poll = store.create_poll(
        conversation_id=CONVERSATION, service_url=SERVICE_URL,
        creator_id=PEOPLE["owner"][0], creator_name=PEOPLE["owner"][1],
        title="회식 장소", options=["삼겹살", "치킨", "초밥"],
        multi_select=False, closes_at=None)
    check("생성 + 항목 3개", len(poll.options) == 3 and poll.title == "회식 장소")

    store.toggle_vote(poll, 0, *PEOPLE["alice"])
    store.toggle_vote(store.get_poll(poll.id), 0, *PEOPLE["bob"])
    p = store.get_poll(poll.id)
    check("2명 투표 집계", p.options[0].count == 2 and p.total_votes == 2)
    check("투표자 이름 기록", set(p.options[0].voters) == {PEOPLE["alice"][1], PEOPLE["bob"][1]})

    msg = store.toggle_vote(p, 1, *PEOPLE["alice"])
    p = store.get_poll(poll.id)
    check("단일 선택은 표를 옮긴다",
          p.options[0].count == 1 and p.options[1].count == 1 and p.total_votes == 2,
          msg)

    msg = store.toggle_vote(p, 1, *PEOPLE["alice"])
    p = store.get_poll(poll.id)
    check("같은 항목 재클릭은 취소", p.options[1].count == 0 and p.total_votes == 1, msg)

    multi = store.create_poll(
        conversation_id=CONVERSATION, service_url=None,
        creator_id=PEOPLE["owner"][0], creator_name=PEOPLE["owner"][1],
        title="복수", options=["a", "b", "c"], multi_select=True, closes_at=None)
    store.toggle_vote(multi, 0, *PEOPLE["alice"])
    store.toggle_vote(store.get_poll(multi.id), 1, *PEOPLE["alice"])
    m = store.get_poll(multi.id)
    check("복수 선택은 둘 다 유지",
          m.options[0].count == 1 and m.options[1].count == 1 and m.voter_count == 1)

    check("마감은 한 번만 성공", store.close_poll(poll.id) and not store.close_poll(poll.id))
    check("마감 후 closed=True", store.get_poll(poll.id).closed)
    check("열린 투표 목록에서 제외",
          poll.id not in {p.id for p in store.open_polls()}
          and multi.id in {p.id for p in store.open_polls()})

    expiring = store.create_poll(
        conversation_id=CONVERSATION, service_url=None,
        creator_id="29:x", creator_name="x", title="지남", options=["a", "b"],
        multi_select=False, closes_at=datetime.now(timezone.utc) - timedelta(minutes=1))
    check("지난 마감 시각 감지", store.get_poll(expiring.id).is_expired())

    section("보존정책 — 오래된 투표 자동 삭제")
    import store as store_mod
    ret = Store(":memory:")
    old = ret.create_poll(conversation_id="c", service_url=None, creator_id="29:x",
                          creator_name="x", title="오래된 마감", options=["a", "b"],
                          multi_select=False, closes_at=None)
    ret.toggle_vote(ret.get_poll(old.id), 0, "29:v", "이름있는사람")
    ret.close_poll(old.id, at=datetime.now(timezone.utc) - timedelta(days=3))
    recent = ret.create_poll(conversation_id="c", service_url=None, creator_id="29:x",
                             creator_name="x", title="방금 마감", options=["a", "b"],
                             multi_select=False, closes_at=None)
    ret.close_poll(recent.id)
    live = ret.create_poll(conversation_id="c", service_url=None, creator_id="29:x",
                           creator_name="x", title="진행 중", options=["a", "b"],
                           multi_select=False, closes_at=None)
    closed_n, stale_n = ret.purge_old(closed_days=1, open_days=30)
    check("보존 기간 지난 마감분만 삭제", (closed_n, stale_n) == (1, 0), f"{closed_n}/{stale_n}")
    check("오래된 투표가 사라짐", ret.get_poll(old.id) is None)
    check("방금 마감분은 남음", ret.get_poll(recent.id) is not None)
    check("진행 중은 남음", ret.get_poll(live.id) is not None)
    check("표(실명 포함)도 CASCADE 로 함께 삭제",
          ret.counts()["votes"] == 0, str(ret.counts()))

    stale = ret.create_poll(conversation_id="c", service_url=None, creator_id="29:x",
                            creator_name="x", title="방치", options=["a", "b"],
                            multi_select=False, closes_at=None)
    ret._conn.execute("UPDATE polls SET created_at = ? WHERE id = ?",
                      ((datetime.now(timezone.utc) - timedelta(days=60)).isoformat(), stale.id))
    ret._conn.commit()
    closed_n, stale_n = ret.purge_old(closed_days=1, open_days=30)
    check("마감 안 된 방치 투표도 정리", (closed_n, stale_n) == (0, 1), f"{closed_n}/{stale_n}")
    check("counts() 는 실명·식별자를 반환하지 않음",
          set(ret.counts()) == {"polls", "open", "votes"}, str(ret.counts()))
    ret.close()

    section("동표 처리")
    tie = store.create_poll(
        conversation_id=CONVERSATION, service_url=None, creator_id="29:x", creator_name="x",
        title="동표", options=["a", "b", "c"], multi_select=False, closes_at=None)
    store.toggle_vote(tie, 0, *PEOPLE["alice"])
    store.toggle_vote(store.get_poll(tie.id), 1, *PEOPLE["bob"])
    t = store.get_poll(tie.id)
    check("동표는 leaders 2개", len(t.leaders) == 2 and {o.idx for o in t.leaders} == {0, 1})
    empty = store.create_poll(
        conversation_id=CONVERSATION, service_url=None, creator_id="29:x", creator_name="x",
        title="무표", options=["a", "b"], multi_select=False, closes_at=None)
    check("0표면 leaders 없음", store.get_poll(empty.id).leaders == [])
    store.close()


def test_cards() -> None:
    from store import Store

    section("카드")
    store = Store(":memory:")
    poll = store.create_poll(
        conversation_id=CONVERSATION, service_url=None,
        creator_id=PEOPLE["owner"][0], creator_name=PEOPLE["owner"][1],
        title="회식 장소", options=["삼겹살", "치킨", "초밥"],
        multi_select=True, closes_at=datetime.now(timezone.utc) + timedelta(hours=5))
    store.toggle_vote(poll, 0, *PEOPLE["alice"])
    store.toggle_vote(store.get_poll(poll.id), 0, *PEOPLE["bob"])
    store.toggle_vote(store.get_poll(poll.id), 1, *PEOPLE["owner"])
    poll = store.get_poll(poll.id)

    card = cards.poll_card(poll)
    text = card_text(card)
    actions = card_actions(card)
    check("제목 표시", "📊 **회식 장소**" in text)
    check("복수 선택 표시", "복수 선택 가능" in text)
    # 참여자 3명 중 2명이 삼겹살 -> 67%. top 기준이었다면 100% 로 고정됐다.
    lead_line = [l for l in text.splitlines() if "삼겹살" in l][0]
    check("막대 + 표수 + 퍼센트", "**2표** · 67%" in text, lead_line)
    # Teams 가 코드 스팬에 배경 박스를 그려서 빈 막대가 채워진 것처럼 보였다.
    check("막대를 백틱으로 감싸지 않는다", "`" not in lead_line, lead_line)
    check("채움/빈칸 문자가 구분된다",
          cards.BAR_FILLED in lead_line and cards.BAR_EMPTY in lead_line
          and cards.BAR_FILLED != cards.BAR_EMPTY, lead_line)

    # ── 게이지가 실제로 움직이는지 ────────────────────────────────
    # 최다 득표 기준으로 채우면 1위는 1표든 10표든 항상 꽉 찬 상태여서
    # 후보 2~3개인 실제 상황에서 막대가 고정돼 보였다. 참여자 기준으로 고쳤다.
    moving = Store(":memory:")
    mp = moving.create_poll(
        conversation_id=CONVERSATION, service_url=None, creator_id="29:x", creator_name="x",
        title="점심", options=["김밥", "라면", "덮밥"], multi_select=False, closes_at=None)
    seen_bars, seen_lines = [], []
    for i, opt in enumerate([0, 1, 0, 0, 1]):
        moving.toggle_vote(moving.get_poll(mp.id), opt, f"29:v{i}", f"사람{i}")
        fresh = moving.get_poll(mp.id)
        seen_bars.append(cards._bar(fresh.options[0].count, fresh.voter_count))
        seen_lines.append(card_text(cards.poll_card(fresh)))
    check("1위 막대가 표 상황에 따라 변한다", len(set(seen_bars)) >= 3,
          f"{len(set(seen_bars))}종: {sorted(set(seen_bars))}")
    check("표가 들어올 때마다 카드가 달라진다", len(set(seen_lines)) == 5,
          f"{len(set(seen_lines))}/5종")

    final = moving.get_poll(mp.id)
    check("막대는 참여자 대비 비율", cards._percent(final.options[0].count, final.voter_count) == 60,
          f"{final.options[0].count}표 / {final.voter_count}명")

    multi_full = Store(":memory:")
    mf = multi_full.create_poll(
        conversation_id=CONVERSATION, service_url=None, creator_id="29:x", creator_name="x",
        title="복수", options=["a", "b"], multi_select=True, closes_at=None)
    for i in range(3):
        multi_full.toggle_vote(multi_full.get_poll(mf.id), 0, f"29:m{i}", f"m{i}")
        multi_full.toggle_vote(multi_full.get_poll(mf.id), 1, f"29:m{i}", f"m{i}")
    mfp = multi_full.get_poll(mf.id)
    check("복수 선택도 100% 를 넘지 않는다",
          all(cards._percent(o.count, mfp.voter_count) <= 100 for o in mfp.options)
          and mfp.total_votes == 6 and mfp.voter_count == 3)
    moving.close(); multi_full.close()
    check("투표자 이름 (짧은 이름)", "더미1, 테스터3" in text)
    check("긴 표시명 축약", "(Foxtrot Dummy)" not in text)
    check("0표는 —", "—" in text)
    check("투표 버튼 3개 + 마감", [a[1] for a in actions] ==
          ["vote", "vote", "vote", "close"], str(actions))
    check("만든 사람 표시", "만든 사람 샘플2" in text)
    check("@태그 없음 (카드에 mention 엔터티 불가)", "<at>" not in text)

    store.close_poll(poll.id)
    closed = cards.poll_card(store.get_poll(poll.id))
    ctext, cactions = card_text(closed), card_actions(closed)
    check("마감판에 버튼 없음", cactions == [], str(cactions))
    check("마감 표시", "마감됨" in ctext)
    # 미리 닫으면 예정 시각과 실제 마감 시각이 다르다. 실제를 보여야 한다.
    scheduled = P.format_deadline(poll.closes_at)
    actual = P.format_deadline(store.get_poll(poll.id).closed_at)
    check("마감판은 실제 마감 시각을 표시", actual in ctext and scheduled not in ctext,
          f"예정 {scheduled} / 실제 {actual}")
    check("1위 강조", "🏆" in ctext and "결과 — **삼겹살**" in ctext)

    form = cards.form_card(PEOPLE["owner"][1])
    ftext = card_text(form)
    ids = [getattr(i, "id", None) for i in (form.body or [])]
    check("폼에 4개 입력", {"title", "options", "deadline", "multi"} <= set(ids), str(ids))
    check("폼 제출 verb=create", card_actions(form) == [("투표 시작", "create")])
    check("폼에 만든 사람 안내", "샘플2 님이 만드는 중" in ftext)
    err = card_text(cards.form_card("x", error="항목을 2개 이상 적어주세요."))
    check("폼 오류 표시", "⚠️ 항목을 2개 이상" in err)
    store.close()


async def test_routing() -> None:
    """카드에 실린 액션이 실제로 핸들러에 도달하는지.

    이 검사가 없어서 `data["action"]` 누락 버그가 실환경까지 갔다.
    Teams 는 200 만 받고 "응답이 앱으로 전송되었습니다" 를 띄운 뒤 아무 일도 하지 않았다.
    """
    import app
    from store import Store

    section("라우팅 — 카드 액션이 핸들러에 도달하는가")
    tmp = Store(":memory:")
    poll = tmp.create_poll(
        conversation_id=CONVERSATION, service_url=None,
        creator_id=PEOPLE["owner"][0], creator_name=PEOPLE["owner"][1],
        title="테스트", options=["참", "불참"], multi_select=False, closes_at=None)
    board = cards.poll_card(tmp.get_poll(poll.id))

    for index, label in [(0, "투표 버튼 1"), (1, "투표 버튼 2"), (2, "마감 버튼")]:
        act = invoke_from_card(board, index)
        matched = app.app.router.select_handlers(act)
        check(f"{label} → 핸들러 1개 매칭", len(matched) == 1,
              f"{len(matched)}개 / data={act.value.action.data}")

    form = cards.form_card(PEOPLE["owner"][1])
    act = invoke_from_card(form, 0)
    check("폼 제출 → 핸들러 1개 매칭", len(app.app.router.select_handlers(act)) == 1,
          f"data={act.value.action.data}")

    check("모든 액션 data 에 'action' 키가 있다",
          all("action" in action_data(a) for a in (board.actions or []) + (form.actions or [])))

    unknown = invoke("vote", {"action": "없는액션", "p": poll.id, "i": 0})
    check("모르는 액션은 매칭되지 않는다", len(app.app.router.select_handlers(unknown)) == 0)
    tmp.close()

    section("라우팅 — 메시지")
    check("@멘션 메시지 → 핸들러 매칭",
          len(app.app.router.select_handlers(message("제목\n항목1\n항목2"))) >= 1)


async def test_handler() -> None:
    import app

    section("핸들러 — 줄바꿈으로 바로 생성")
    ctx = FakeContext(message(
        "9/10(목) 회식 메뉴 선정\n"
        "땡땡식당 (https://naver.me/GdymUOVY)\n"
        "무슨식당 (https://naver.me/Fk738sTW)\n"
        "초밥집\n마감: 3시간\n복수"))
    await app.handle_message(ctx)
    check("메시지 1개 전송", len(ctx.calls) == 1 and ctx.calls[0][0] == "SEND")
    posted = ctx.cards()
    check("카드 1개 첨부", len(posted) == 1)
    board_text = card_text(posted[0])
    check("투표판이 바로 나옴", "9/10(목) 회식 메뉴 선정" in board_text, board_text[:60])
    check("링크가 항목에 그대로 들어감",
          "https://naver.me/GdymUOVY" in board_text)
    check("`/` 가 든 제목이 깨지지 않음", "9/10(목)" in board_text)
    poll_id = action_data(posted[0].actions[0]).get("p")
    stored = app.store.get_poll(poll_id)
    check("DB 에 activity_id 저장", stored.activity_id == ctx.calls[0][1],
          f"{stored.activity_id} vs {ctx.calls[0][1]}")
    check("마감 타이머 등록", poll_id in app._timers)
    app._timers[poll_id].cancel()

    section("핸들러 — 폼 생성 → 같은 메시지가 투표판으로")
    ctx = FakeContext(message(""))
    await app.handle_message(ctx)
    form_activity_id = ctx.calls[0][1]
    check("폼 카드 전송", len(ctx.calls) == 1 and
          any(getattr(i, "id", None) == "title" for i in ctx.cards()[0].body))

    ctx2 = FakeContext(invoke("create", {
        "action": "create", "title": "점심 메뉴", "options": "김밥\n라면\n덮밥",
        "deadline": "3시간", "multi": "false"}, reply_to=form_activity_id))
    matched, result = await route(app, ctx2)
    check("라우터를 통해 생성 핸들러 도달", matched == 1, f"{matched}개")
    check("SEND 없음 — 같은 메시지를 UPDATE",
          [k for k, _, _ in ctx2.calls] == ["UPDATE"], str([k for k, _, _ in ctx2.calls]))
    check("폼이 붙어 있던 activity 를 갱신", ctx2.calls[0][1] == form_activity_id)
    updated = ctx2.cards()[0]
    check("투표판으로 바뀜", "점심 메뉴" in card_text(updated))
    check("클릭한 사람에게만 확인 응답", "투표를 시작했습니다" in str(result.value))
    lunch_id = action_data(updated.actions[0]).get("p")
    app._timers[lunch_id].cancel()

    section("핸들러 — 폼 오류는 값을 보존하며 폼 유지")
    ctx3 = FakeContext(invoke("create", {"action": "create", "title": "제목",
                                         "options": "하나만", "deadline": "",
                                         "multi": "false"}, reply_to=form_activity_id))
    matched, _ = await route(app, ctx3)
    check("라우터를 통해 생성 핸들러 도달", matched == 1, f"{matched}개")
    again = ctx3.cards()[0]
    atext = card_text(again)
    values = {getattr(i, "id", None): getattr(i, "value", None) for i in (again.body or [])}
    check("오류 메시지 표시", "2개 이상" in atext)
    check("입력값 보존", values.get("title") == "제목" and values.get("options") == "하나만",
          str(values))

    section("핸들러 — 투표")
    # 카드에 실린 data 를 그대로 써서 라우터를 통과시킨다
    ctx4 = FakeContext(invoke_from_card(updated, 1, who="alice"))
    matched, result = await route(app, ctx4)
    check("라우터를 통해 투표 핸들러 도달", matched == 1, f"{matched}개")
    check("투표 반영 + 카드 갱신", [k for k, _, _ in ctx4.calls] == ["UPDATE"])
    check("클릭한 사람에게 확인", "'라면' 에 투표했습니다" in str(result.value), str(result.value))
    board = card_text(ctx4.cards()[0])
    check("득표와 투표자 표시", "**1표**" in board and "더미1" in board)

    ctx5 = FakeContext(invoke_from_card(updated, 0, who="alice"))
    _, result = await route(app, ctx5)
    check("단일 선택은 이동", "'김밥' 으로 변경했습니다" in str(result.value), str(result.value))
    p = app.store.get_poll(lunch_id)
    check("총 1표 유지", p.total_votes == 1 and p.options[0].count == 1)

    ctx6 = FakeContext(invoke_from_card(updated, 0, who="alice"))
    _, result = await route(app, ctx6)
    check("재클릭은 취소", "취소했습니다" in str(result.value)
          and app.store.get_poll(lunch_id).total_votes == 0)

    result = await app.handle_vote(FakeContext(invoke("vote", {"p": "없는투표", "i": 0})))
    check("없는 투표는 안내", "찾을 수 없습니다" in str(result.value))
    result = await app.handle_vote(FakeContext(invoke("vote", {"p": lunch_id, "i": "x"})))
    check("잘못된 인덱스는 안내", "잘못된 선택" in str(result.value))

    section("핸들러 — 마감 권한")
    _, result = await route(app, FakeContext(invoke_from_card(updated, 3, who="bob")))
    check("만든 사람이 아니면 거절", "만든 사람(샘플2)만" in str(result.value), str(result.value))
    check("거절 시 마감되지 않음", not app.store.get_poll(lunch_id).closed)

    ctx7 = FakeContext(invoke_from_card(updated, 3, who="owner"))
    matched, result = await route(app, ctx7)
    check("라우터를 통해 마감 핸들러 도달", matched == 1, f"{matched}개")
    check("만든 사람은 마감 가능", app.store.get_poll(lunch_id).closed)
    check("마감판으로 갱신 (버튼 제거)", card_actions(ctx7.cards()[0]) == [])
    _, result = await route(app, FakeContext(invoke_from_card(updated, 3, who="owner")))
    check("중복 마감은 무시", "이미 마감" in str(result.value))

    section("핸들러 — 마감 시각 지연 검사")
    expired = app.store.create_poll(
        conversation_id=CONVERSATION, service_url=SERVICE_URL,
        creator_id=PEOPLE["owner"][0], creator_name=PEOPLE["owner"][1],
        title="지난 투표", options=["a", "b"], multi_select=False,
        closes_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    app.store.set_activity_id(expired.id, "card-expired")
    expired_board = cards.poll_card(app.store.get_poll(expired.id))
    ctx8 = FakeContext(invoke_from_card(expired_board, 0, who="alice"))
    _, result = await route(app, ctx8)
    check("타이머 없이도 마감된다", app.store.get_poll(expired.id).closed)
    check("이미 마감 안내", "이미 마감된 투표" in str(result.value))
    check("표는 기록되지 않음", app.store.get_poll(expired.id).total_votes == 0)

    section("핸들러 — 재전송 방어")
    act = message("중복\n항목A\n항목B")
    c1 = FakeContext(act)
    await app.handle_message(c1)
    c2 = FakeContext(act)
    await app.handle_message(c2)
    check("같은 activity.id 무시", len(c1.calls) == 1 and len(c2.calls) == 0)
    dup_id = action_data(c1.cards()[0].actions[0]).get("p")
    if dup_id in app._timers:
        app._timers[dup_id].cancel()

    section("핸들러 — 항목이 모자라면 안내")
    ctx9 = FakeContext(message("회식\n하나만"))
    await app.handle_message(ctx9)
    check("오류 + 사용법 안내",
          "2개 이상" in ctx9.texts()[0] and "사용법" in ctx9.texts()[0])
    check("카드를 만들지 않음", not ctx9.cards())

    section("핸들러 — 제목만 보내면 폼이 채워져 열린다")
    ctx10 = FakeContext(message("9/10(목) 회식 메뉴 선정"))
    await app.handle_message(ctx10)
    form = ctx10.cards()[0]
    values = {getattr(i, "id", None): getattr(i, "value", None) for i in (form.body or [])}
    check("폼이 열림", any(getattr(i, "id", None) == "title" for i in form.body))
    check("제목이 미리 채워짐", values.get("title") == "9/10(목) 회식 메뉴 선정", str(values))
    check("투표를 바로 만들지 않음", not any("📊" in card_text(c) and "표**" in card_text(c)
                                             for c in ctx10.cards()))

    section("핸들러 — 옛 `/` 문법은 폼으로 안내")
    ctx11 = FakeContext(message("회식장소 / 삼겹살, 치킨, 초밥 / 금요일 18시 / 복수"))
    await app.handle_message(ctx11)
    migrated = ctx11.cards()[0]
    mvalues = {getattr(i, "id", None): getattr(i, "value", None) for i in (migrated.body or [])}
    mtext = card_text(migrated)
    check("옛 문법 값이 폼에 옮겨짐",
          mvalues.get("title") == "회식장소"
          and mvalues.get("options") == "삼겹살\n치킨\n초밥"
          and mvalues.get("deadline") == "금요일 18시", str(mvalues))
    check("문법 변경 안내가 보임", "ℹ️" in mtext and "줄바꿈" in mtext, mtext[:80])


async def test_concurrency() -> None:
    """동시 투표·동시 생성·여러 대화방."""
    import app

    section("동시성 — 같은 투표에 20명이 동시 클릭")
    poll = app.store.create_poll(
        conversation_id="19:room-a", service_url=SERVICE_URL,
        creator_id=PEOPLE["owner"][0], creator_name=PEOPLE["owner"][1],
        title="동시 투표", options=["A", "B"], multi_select=False, closes_at=None)
    app.store.set_activity_id(poll.id, "card-conc")
    board = cards.poll_card(app.store.get_poll(poll.id))

    def voter_invoke(i: int, option: int):
        act = invoke_from_card(board, option, reply_to="card-conc")
        act.from_.id = f"29:u{i}"
        act.from_.name = f"사람{i}"
        act.id = f"inv-conc-{i}-{option}"
        return act

    ctxs = [FakeContext(voter_invoke(i, i % 2)) for i in range(20)]
    await asyncio.gather(*(route(app, c) for c in ctxs))

    fresh = app.store.get_poll(poll.id)
    check("표가 하나도 유실되지 않음 (20표)", fresh.total_votes == 20,
          f"{fresh.total_votes}표 / A={fresh.options[0].count} B={fresh.options[1].count}")
    check("A 10표 · B 10표 정확", fresh.options[0].count == 10 and fresh.options[1].count == 10)
    check("참여자 20명 (중복 집계 없음)", fresh.voter_count == 20, f"{fresh.voter_count}명")

    # 락이 갱신 순서를 지켜야 한다. 마지막 갱신 카드가 최종 DB 상태와 일치해야 한다.
    last_card = [m for c in ctxs for k, _, m in c.calls if k == "UPDATE"][-1]
    final_text = card_text(last_card.attachments[0].content)
    check("마지막 갱신 카드가 최종 상태와 일치",
          "**10표**" in final_text and final_text.count("**10표**") == 2, final_text[:80])

    section("개인정보 — 로그에 실명이 나가지 않는가")
    hashed = app._who(PEOPLE["owner"][0])
    check("사용자 id 를 8자 해시로 익명화", len(hashed) == 8 and hashed.isalnum(), hashed)
    check("해시가 실명·원본 id 를 담지 않음",
          PEOPLE["owner"][1] not in hashed and PEOPLE["owner"][0] not in hashed)
    check("같은 사용자는 같은 해시 (추적 가능)",
          app._who(PEOPLE["owner"][0]) == hashed)
    check("다른 사용자는 다른 해시", app._who(PEOPLE["alice"][0]) != hashed)

    section("동시성 — 같은 사람이 같은 항목을 20번 연타")
    spam = app.store.create_poll(
        conversation_id="19:room-a", service_url=SERVICE_URL,
        creator_id=PEOPLE["owner"][0], creator_name=PEOPLE["owner"][1],
        title="연타", options=["A", "B"], multi_select=False, closes_at=None)
    app.store.set_activity_id(spam.id, "card-spam")
    spam_board = cards.poll_card(app.store.get_poll(spam.id))
    spam_ctxs = []
    for i in range(20):
        act = invoke_from_card(spam_board, 0, who="alice", reply_to="card-spam")
        act.id = f"inv-spam-{i}"
        spam_ctxs.append(FakeContext(act))
    await asyncio.gather(*(route(app, c) for c in spam_ctxs))
    after = app.store.get_poll(spam.id)
    # 짝수 번 토글이면 0표, 홀수면 1표. 어느 쪽이든 1명을 넘을 수 없다.
    check("연타해도 1표를 넘지 않음", after.total_votes in (0, 1),
          f"{after.total_votes}표")
    check("PK 제약으로 중복 행이 안 생김", after.voter_count <= 1, f"{after.voter_count}명")

    section("동시성 — 여러 대화방에서 동시 생성 + 동시 투표")
    creates = []
    for r in range(5):
        ctx = FakeContext(message(f"방{r} 투표\nA\nB", who="owner"))
        ctx.activity.conversation.id = f"19:multi-{r}"
        creates.append(ctx)
    await asyncio.gather(*(app.handle_message(c) for c in creates))
    ids = [action_data(c.cards()[0].actions[0]).get("p") for c in creates]
    check("5개 대화방에 각각 별도 투표 생성", len(set(ids)) == 5, f"{len(set(ids))}개")
    check("대화방 id 가 정확히 기록됨",
          all(app.store.get_poll(pid).conversation_id == f"19:multi-{i}"
              for i, pid in enumerate(ids)))
    for pid in ids:
        if pid in app._timers:
            app._timers[pid].cancel()

    # 각 방에서 3명씩 동시 투표
    vote_ctxs = []
    for r, pid in enumerate(ids):
        b = cards.poll_card(app.store.get_poll(pid))
        for u in range(3):
            act = invoke_from_card(b, u % 2, reply_to=app.store.get_poll(pid).activity_id or "x")
            act.from_.id = f"29:m{r}-{u}"
            act.from_.name = f"방{r}사람{u}"
            act.id = f"inv-multi-{r}-{u}"
            vote_ctxs.append((pid, FakeContext(act)))
    await asyncio.gather(*(route(app, c) for _, c in vote_ctxs))
    counts = {pid: app.store.get_poll(pid).total_votes for pid in ids}
    check("방마다 정확히 3표", all(v == 3 for v in counts.values()), str(counts))
    check("방끼리 표가 섞이지 않음",
          all(app.store.get_poll(pid).voter_count == 3 for pid in ids))

    section("동시성 — 투표와 마감이 동시에")
    race = app.store.create_poll(
        conversation_id="19:race", service_url=SERVICE_URL,
        creator_id=PEOPLE["owner"][0], creator_name=PEOPLE["owner"][1],
        title="경합", options=["A", "B"], multi_select=False, closes_at=None)
    app.store.set_activity_id(race.id, "card-race")
    rb = cards.poll_card(app.store.get_poll(race.id))
    close_act = invoke_from_card(rb, 2, who="owner", reply_to="card-race")
    close_act.id = "inv-race-close"
    vote_acts = []
    for i in range(5):
        a = invoke_from_card(rb, 0, reply_to="card-race")
        a.from_.id = f"29:r{i}"; a.from_.name = f"경합{i}"; a.id = f"inv-race-v{i}"
        vote_acts.append(FakeContext(a))
    await asyncio.gather(route(app, FakeContext(close_act)),
                         *(route(app, c) for c in vote_acts))
    r = app.store.get_poll(race.id)
    check("마감이 정확히 한 번만 적용", r.closed and r.closed_at is not None)
    check("마감 후 들어온 표는 기록되지 않음 (표 ≤ 5)", r.total_votes <= 5, f"{r.total_votes}표")
    check("마감 카드가 최종 상태", not cards.poll_card(r).actions)


async def test_timer() -> None:
    import app
    section("마감 타이머 (실제 시계)")
    poll = app.store.create_poll(
        conversation_id=CONVERSATION, service_url=SERVICE_URL,
        creator_id=PEOPLE["owner"][0], creator_name=PEOPLE["owner"][1],
        title="곧 마감", options=["a", "b"], multi_select=False,
        closes_at=datetime.now(timezone.utc) + timedelta(seconds=1))
    app.store.set_activity_id(poll.id, "card-timer")
    ctx = FakeContext(invoke("noop", {}))
    app._schedule_close(ctx, app.store.get_poll(poll.id))
    check("타이머 등록", poll.id in app._timers)
    for _ in range(60):
        if app.store.get_poll(poll.id).closed:
            break
        await asyncio.sleep(0.1)
    check("1초 후 자동 마감", app.store.get_poll(poll.id).closed)
    check("마감판으로 갱신", any(k == "UPDATE" for k, _, _ in ctx.calls))
    check("타이머 정리됨", poll.id not in app._timers)


async def main() -> int:
    print("vote-agent 검증\n")
    test_parsing()
    test_store()
    test_cards()
    await test_routing()
    await test_handler()
    await test_concurrency()
    await test_timer()
    print(f"\n{'=' * 62}\n통과 {len(PASS)}건 / 실패 {len(FAIL)}건")
    for name in FAIL:
        print(f"  ❌ {name}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
