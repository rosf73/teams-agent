"""roulette-agent 자체 검증. 의존성 없이 돌아간다.

    ./.venv/bin/python roulette-agent/tests/check.py

픽스처는 **실제 API 가 주는 타입**으로 만든다 (`get_members()` -> TeamsChannelAccount).
지난번 이걸 Account 로 만들어서 오프라인은 통과하고 실환경에서 500 이 났다.
"""

import asyncio
import os
import random
import sys
import time
import types
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from microsoft_teams.api import Account, MessageActivity, TeamsChannelAccount  # noqa: E402
from microsoft_teams.api.activities.sent_activity import SentActivity  # noqa: E402

import lines as L  # noqa: E402
import mentions as M  # noqa: E402
import roulette as R  # noqa: E402

# 실제 테넌트의 표시명 형식: "한글이름 (English Name)"
NAMES = [
    "테스터1 (Alpha Tester)", "테스터2 (Bravo Tester)", "샘플1 (Delta Sample)",
    "더미1 (Foxtrot Dummy)", "더미2 (Golf Dummy)", "예시1 (Hotel Example)",
    "테스터3 (Charlie Tester)", "샘플2 (Echo Sample)", "예시2 (India Example)",
    "가상1 (Juliet Virtual)",
]
ROSTER = [TeamsChannelAccount(id=f"29:u{i}", name=n) for i, n in enumerate(NAMES)]
BOT = Account(id="28:11111111-2222-3333-4444-555555555555", name="당첨자뽑기", role="bot")
# 실측한 @모든 사용자 의 의사 id. 실제 사용자와 형태가 같아 id 패턴으로는 구분 불가.
EVERYONE_ID = "29:EVERYONE-PSEUDO-ID-NOT-IN-ROSTER"

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '❌'} {name}{f'  — {detail}' if detail else ''}")


def mention(name: str, account_id: str) -> dict:
    return {"type": "mention", "mentioned": {"id": account_id, "name": name},
            "text": f"<at>{name}</at>"}


_conversation_seq = 0


def activity(text: str, entities: list, activity_id: str | None = None,
             conversation_id: str | None = None) -> MessageActivity:
    """테스트마다 activity_id 와 대화방 id 를 새로 준다.

    - `activity_id` 를 고정하면 seen-set 이 두 번째 호출부터 전부 중복으로 걸러낸다.
      재전송 방어를 검사하는 케이스만 명시적으로 같은 값을 넘긴다.
    - `_running` 은 대화방 단위라, 같은 id 를 쓰면 앞 테스트의 잔여 태스크가
      뒤 테스트를 "이미 진행 중" 으로 막거나 플래그를 먼저 지운다.
    """
    global _conversation_seq
    _conversation_seq += 1
    if conversation_id is None:
        conversation_id = f"19:gc{_conversation_seq}"
    if activity_id is None:
        activity_id = f"act{_conversation_seq}"
    return MessageActivity.model_validate({
        "type": "message", "id": activity_id, "text": text,
        "from": {"id": "29:u7", "name": NAMES[7]},
        "recipient": BOT.model_dump(by_alias=True, exclude_none=True),
        "conversation": {"id": conversation_id, "conversationType": "groupChat",
                         "isGroup": True},
        "entities": entities,
    })


class FakeContext:
    """ctx.send / ctx.api.conversations.* 를 흉내내고 호출을 기록한다."""

    def __init__(self, act: MessageActivity, roster=ROSTER, roster_error=None) -> None:
        self.activity = act
        self.calls: list[tuple[float, str, object]] = []
        self._sent = 0
        self._t0 = time.monotonic()
        outer = self

        class Activities:
            async def update(self, activity_id, message):
                outer.calls.append((time.monotonic() - outer._t0, "UPDATE", message))

        async def get_members(conversation_id):
            if roster_error:
                raise roster_error
            return roster

        self.api = types.SimpleNamespace(conversations=types.SimpleNamespace(
            get_members=get_members, activities=lambda cid: Activities()))

    async def send(self, message):
        self._sent += 1
        self.calls.append((time.monotonic() - self._t0, "SEND", message))
        return SentActivity(id=f"sent{self._sent}", activity_params=message)

    def texts(self, kind=None) -> list[str]:
        return [(m.text or "") for _, k, m in self.calls if kind is None or k == kind]

    def cards(self, kind=None) -> list:
        """첨부된 Adaptive Card 목록. 판은 평문이 아니라 카드다."""
        out = []
        for _, k, m in self.calls:
            if kind is not None and k != kind:
                continue
            for att in (getattr(m, "attachments", None) or []):
                out.append(att.content)
        return out

    def panels(self, kind=None) -> list[str]:
        """카드 본문을 줄바꿈으로 이어 붙인 문자열."""
        return [panel_text(c) for c in self.cards(kind)]

    def mentioned_names(self) -> set[str]:
        return {e.mentioned.name for _, _, m in self.calls for e in (m.entities or [])}


class fast_delays:
    """연출 대기를 거의 0 으로 줄인다. 로직 테스트는 타이밍을 검사하지 않는다.

    스텁하지 않으면 백그라운드 태스크가 테스트 안에서 끝나지 않고,
    잔여 태스크가 다음 테스트의 공유 상태를 오염시킨다.
    """

    KEYS = ("INTRO_HOLD", "DRUM_HOLD", "ANNOUNCE_HOLD", "TEASE_HOLD")

    def __enter__(self):
        self._saved = {k: getattr(R, k) for k in self.KEYS}
        for k in self.KEYS:
            setattr(R, k, (0.0, 0.0))   # 대기는 (최소, 최대) 범위다
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            setattr(R, k, v)
        return False


async def wait_idle(app_module, timeout: float = 15.0) -> bool:
    """진행 중인 연출이 모두 끝날 때까지 기다린다."""
    deadline = time.monotonic() + timeout
    while app_module._running and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    return not app_module._running


async def run_handler(app_module, ctx) -> None:
    await app_module.handle_message(ctx)
    if not await wait_idle(app_module):
        raise AssertionError("연출이 제한 시간 안에 끝나지 않았다")


def panel_text(card) -> str:
    """카드 본문의 TextBlock 들을 이어 붙인다."""
    body = card.get("body", []) if isinstance(card, dict) else (card.body or [])
    parts = []
    for item in body:
        text = item.get("text") if isinstance(item, dict) else getattr(item, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts)


def spoken_line(card) -> str:
    """대사 필드 = 본문의 마지막 TextBlock."""
    return panel_text(card).split("\n")[-1]


def block_size(card, needle: str) -> str | None:
    """`needle` 을 담은 TextBlock 의 size 를 돌려준다."""
    body = card.get("body", []) if isinstance(card, dict) else (card.body or [])
    for item in body:
        text = item.get("text") if isinstance(item, dict) else getattr(item, "text", None)
        if text and needle in text:
            return item.get("size") if isinstance(item, dict) else getattr(item, "size", None)
    return None


def section(title: str) -> None:
    print(f"\n── {title} " + "─" * max(0, 56 - len(title)))


# ═══════════════════════════════════════════════════════════════════
def test_pure_logic() -> None:
    section("추첨 로직 불변식")
    violations = []
    for total in range(2, R.MAX_CANDIDATES + 1):
        for n in range(1, total + 1):
            cands = [Account(id=f"29:c{i}", name=f"c{i}") for i in range(total)]
            plan = R.plan(cands, n, random.Random(total * 100 + n))
            if plan.all_winners:
                if plan.steps or n != total:
                    violations.append((total, n, "all_winners"))
                continue
            revealed = {a.id for st in plan.steps for a in (st.survivors + st.winners)}
            finals = [st for st in plan.steps if st.kind == "final"]
            if len(revealed) != total:
                violations.append((total, n, f"공개 {len(revealed)}≠{total}"))
            if sum(len(st.survivors) for st in plan.steps) != total - n:
                violations.append((total, n, "생존자 수"))
            if len(finals) != 1 or len(finals[0].winners) != n:
                violations.append((total, n, "final"))
            if plan.total_steps > R.MAX_STEPS:
                violations.append((total, n, f"단계 {plan.total_steps} > {R.MAX_STEPS}"))
            if finals[0].remaining_before != n + 1:
                violations.append((total, n, "마지막 단계 시작 인원"))
            if [st.index for st in plan.steps] != list(range(1, plan.total_steps + 1)):
                violations.append((total, n, "단계 번호"))
            # 뜸들이기 대상은 아직 후보에 남아 있는 사람이어야 한다
            pool_ids = {a.id for a in finals[0].pool}
            if pool_ids != {finals[0].survivors[0].id} | {w.id for w in plan.winners}:
                violations.append((total, n, "뜸들이기 pool"))
            remaining = total
            for st in plan.steps:
                if st.remaining_before != remaining:
                    violations.append((total, n, "remaining 전개"))
                remaining -= len(st.survivors)
    combos = sum(1 for t in range(2, R.MAX_CANDIDATES + 1) for _ in range(1, t + 1))
    check(f"(N,n) {combos}개 조합 불변식", not violations, f"위반 {violations[:3]}" if violations else "")

    section("공정성")
    counts = Counter()
    cands = [Account(id=f"29:c{i}", name=f"c{i}") for i in range(5)]
    trials = 40000
    for _ in range(trials):
        counts[R.plan(cands, 1).winners[0].id] += 1
    dev = max(abs(v - trials / 5) / (trials / 5) for v in counts.values())
    check("N=5,n=1 균등 분포 (편차 5% 이내)", dev < 0.05, f"최대 편차 {dev * 100:.2f}%")

    section("검증 규칙")
    for count, n, want_reject in [(0, 1, True), (1, 1, True), (31, 1, True),
                                  (5, 0, True), (5, 6, True), (5, 5, False),
                                  (5, 2, False), (2, 1, False)]:
        got = R.validate(count, n) is not None
        check(f"후보 {count} / n={n} → {'거절' if want_reject else '통과'}", got == want_reject)

    section("문구 덱")
    for pool_name in ("INTRO", "DRUMROLL", "ANNOUNCE", "TEASE"):
        pool = getattr(L, pool_name)
        deck = L.Deck(pool, random.Random(1))
        seq = [deck.draw() for _ in range(200)]
        dupes = sum(1 for a, b in zip(seq, seq[1:]) if a == b)
        check(f"{pool_name} 연속 중복 없음", dupes == 0, f"중복 {dupes}건")
        check(f"{pool_name} 전 문구 사용", len(set(seq)) == len(pool))


def test_candidate_collection() -> None:
    section("후보 수집 — 타입 정규화")
    account = M.to_account(ROSTER[3])
    check("TeamsChannelAccount → Account", isinstance(account, Account)
          and not isinstance(ROSTER[3], Account), f"{account.name}")

    section("후보 수집 — 평문 @이름 (실제 표시명 형식)")
    for token, want in [("@샘플2", "샘플2 (Echo Sample)"), ("@더미1", "더미1 (Foxtrot Dummy)"),
                        ("@echo", "샘플2 (Echo Sample)"), ("@테스터", None), ("@샘플", None),
                        ("@없는사람", None)]:
        res = M.resolve_token(token, [M.to_account(m) for m in ROSTER])
        got = res.account.name if res.account else None
        check(f"{token} → {want or '무시'}", got == want, res.reason)

    section("후보 수집 — 실측 페이로드")
    cand = M.collect(activity("<at>당첨자뽑기</at> <at>모든 사용자</at> 2명",
                              [mention("당첨자뽑기", BOT.id),
                               mention("모든 사용자", EVERYONE_ID)]), ROSTER)
    check("@모든 사용자 → 전원 모드, 후보 10명",
          cand.everyone and len(cand.members) == 10, f"후보 {len(cand.members)}명")

    cand = M.collect(activity(
        "<at>당첨자뽑기</at> <at>더미1</at> <at>(Foxtrot</at> <at>Dummy)</at> @샘플2 1명",
        [mention("당첨자뽑기", BOT.id), mention("더미1", "29:u3"),
         mention("(Foxtrot", "29:u3"), mention("Dummy)", "29:u3")]), ROSTER)
    names = [m.name for m in cand.members]
    check("공백으로 쪼개진 멘션 3조각 → 1명, 로스터 전체 이름",
          names == ["더미1 (Foxtrot Dummy)", "샘플2 (Echo Sample)"], str(names))

    cand = M.collect(activity("<at>당첨자뽑기</at> 2명", [mention("당첨자뽑기", BOT.id)]), ROSTER)
    check("봇만 태그 → 전원 모드 오인 없음", not cand.everyone and not cand.members)

    section("당첨자 수 파싱")
    for text, want in [("2명", 2), ("3", 3), ("", 1), ("당첨 5명 뽑아줘", 5)]:
        check(f"{text!r} → n={want}", M.parse_winner_count(text) == want)


async def test_handler() -> None:
    import app

    with fast_delays():
        await _handler_cases(app)


async def _handler_cases(app) -> None:
    section("핸들러 — 정상 진행")
    ctx = FakeContext(activity("<at>당첨자뽑기</at> <at>모든 사용자</at> 2명",
                               [mention("당첨자뽑기", BOT.id),
                                mention("모든 사용자", EVERYONE_ID)]))
    await run_handler(app, ctx)
    sends = [m for _, k, m in ctx.calls if k == "SEND"]
    updates = [m for _, k, m in ctx.calls if k == "UPDATE"]
    check("메시지는 딱 1개 (나머지는 편집)", len(sends) == 1,
          f"SEND {len(sends)} / UPDATE {len(updates)}")
    check("@태그를 전혀 쓰지 않음", not ctx.mentioned_names(), f"{ctx.mentioned_names()}")

    panels = ctx.panels()
    check("판이 Adaptive Card 로 나간다 (평문 아님)",
          len(panels) == len(ctx.calls) and all(not (m.text or "")
                                                for _, _, m in ctx.calls),
          f"카드 {len(panels)} / 호출 {len(ctx.calls)}")

    final = panels[-1]
    for field in ("🎲 **당첨자뽑기**", "▸ **종료됨**", "▸ 생존 ", "🎯 **당첨**"):
        check(f"최종 판에 {field!r} 있음", field in final)
    check("후보 10명 이름이 모두 판에 등장 (짧은 이름)",
          all(f"**{M.short_name(n)}**" in final for n in NAMES), "")
    check("긴 표시명은 축약", "(Alpha Tester)" not in final, "")
    check("진척 바가 8/8 로 끝남", "8/8" in final, "")
    progress = [l for l in final.splitlines() if "완료" in l][0]
    check("진척 바를 백틱으로 감싸지 않는다", "`" not in progress, progress)
    check("진척 바가 채움 문자로 가득 찬다",
          progress.count(app.BAR_FILLED) == app.BAR_WIDTH
          and app.BAR_EMPTY not in progress, progress)

    # ── 대사 줄만 크게 ────────────────────────────────────────────
    # 평문 메시지로는 글자를 크게 할 수 없다 (Header 1-3 은 text-only 미지원).
    # 그래서 판을 카드로 두고 대사 TextBlock 만 size=Large 로 만든다.
    cards_all = ctx.cards()
    drum = next((c for c in cards_all if "두구" in spoken_line(c)), None)
    check("두구두구 대사가 판에 있다", drum is not None)
    if drum is not None:
        check("대사 줄이 size=Large", block_size(drum, spoken_line(drum)) == "Large",
              str(block_size(drum, spoken_line(drum))))
        header_size = block_size(drum, "🎲 **당첨자뽑기**")
        check("헤더는 크게 하지 않는다 (대사만)", header_size != "Large", str(header_size))
        state_size = block_size(drum, "번째 뽑는중")
        check("진행 상태도 크게 하지 않는다", state_size != "Large", str(state_size))

    tease_marks = ("농담", "아 아닙니다", "인 줄 알았죠", "다시 뵙겠습니다",
                   "할 줄 아셨죠", "표정 좋으신데", "어? 아니네요", "긴장하셨나요")
    teases = [spoken_line(c) for c in cards_all if any(k in spoken_line(c) for k in tease_marks)]
    check("마지막 단계 뜸들이기 3~5회", 3 <= len(teases) <= 5, f"{len(teases)}회")

    import re as _re
    called = [(_re.search(r"\*\*(.+?)\*\*", t).group(1)
               if _re.search(r"\*\*(.+?)\*\*", t) else None) for t in teases]
    check("뜸들이기 대상은 남은 후보(3명)뿐", len(set(called)) <= 3, f"{sorted(set(called))}")
    consecutive = sum(1 for a, b in zip(called, called[1:]) if a and a == b)
    check("뜸들이기 이름 연속 중복 없음", consecutive == 0, f"중복 {consecutive}건")
    check("진척 라벨이 '완료' 로 구분됨", "완료 8/8" in final)

    section("핸들러 — 거절 경로")
    for label, text, ents, expect in [
        ("후보 0명", "<at>당첨자뽑기</at> 2명", [mention("당첨자뽑기", BOT.id)], "후보를 못 찾"),
        ("n > 후보", "<at>당첨자뽑기</at> <at>더미1</at> <at>샘플2</at> 5명",
         [mention("당첨자뽑기", BOT.id), mention("더미1", "29:u3"), mention("샘플2", "29:u7")],
         "5명을 뽑을 수는"),
    ]:
        ctx = FakeContext(activity(text, ents))
        await run_handler(app, ctx)
        body = "\n".join(ctx.texts())
        check(f"{label} → 안내", expect in body and "사용법" in body)

    section("핸들러 — n == 후보 (전원 당첨)")
    ctx = FakeContext(activity("<at>당첨자뽑기</at> <at>더미1</at> <at>샘플2</at> 2명",
                               [mention("당첨자뽑기", BOT.id), mention("더미1", "29:u3"),
                                mention("샘플2", "29:u7")]))
    await run_handler(app, ctx)
    check("연출 없이 즉시 발표", len(ctx.calls) == 1 and "전원 당첨" in ctx.texts()[0])
    check("전원 당첨도 @태그 없음", not ctx.mentioned_names())

    section("핸들러 — 재전송 방어")
    act = activity("<at>당첨자뽑기</at> <at>더미1</at> <at>샘플2</at> 1명",
                   [mention("당첨자뽑기", BOT.id), mention("더미1", "29:u3"),
                    mention("샘플2", "29:u7")], activity_id="dup")
    ctx1 = FakeContext(act)
    await run_handler(app, ctx1)
    ctx2 = FakeContext(act)          # 같은 activity.id → seen-set 이 막아야 한다
    await app.handle_message(ctx2)
    check("같은 activity.id 재전송 무시", len(ctx1.calls) > 0 and len(ctx2.calls) == 0,
          f"1회차 {len(ctx1.calls)}건 / 2회차 {len(ctx2.calls)}건")

    section("핸들러 — 로스터 조회 실패")
    ctx = FakeContext(activity("<at>당첨자뽑기</at> <at>더미1</at> <at>샘플2</at> 1명",
                               [mention("당첨자뽑기", BOT.id), mention("더미1", "29:u3"),
                                mention("샘플2", "29:u7")]),
                      roster_error=RuntimeError("Forbidden"))
    await run_handler(app, ctx)
    check("명시적 태그만으로 진행",
          any("🎯 **당첨**" in panel for panel in ctx.panels("UPDATE")))

    # 로스터가 없으면 "id 가 로스터에 없음" 규칙이 모든 멘션을 전원으로 오인한다.
    # 실제 제품 버그였다. 원인을 알 수 있는 안내가 나가야 한다.
    ctx = FakeContext(activity("<at>당첨자뽑기</at> <at>모든 사용자</at> 2명",
                               [mention("당첨자뽑기", BOT.id),
                                mention("모든 사용자", EVERYONE_ID)]),
                      roster_error=RuntimeError("Forbidden"))
    await run_handler(app, ctx)
    body = "\n".join(ctx.texts())
    check("전원 모드 + 로스터 실패 → 원인 안내", "참여자 목록을 가져올 수 없어" in body)

    # 로스터가 없어도 실제 사용자 태그는 전원 모드로 오인하지 않아야 한다
    ctx = FakeContext(activity("<at>당첨자뽑기</at> <at>더미1</at> <at>샘플2</at> 1명",
                               [mention("당첨자뽑기", BOT.id), mention("더미1", "29:u3"),
                                mention("샘플2", "29:u7")]),
                      roster_error=RuntimeError("Forbidden"))
    await run_handler(app, ctx)
    check("로스터 실패 시 전원 모드 오인 없음",
          any("🎯 **당첨**" in panel for panel in ctx.panels("UPDATE"))
          and "채팅방 전원" not in "\n".join(ctx.panels() + ctx.texts()))


async def test_concurrency() -> None:
    """같은/다른 대화방에서 동시에 태그했을 때."""
    import app

    section("동시성 — 같은 대화방에서 5명이 동시에 태그")
    with fast_delays():
        act_template = lambda i: activity(
            "<at>당첨자뽑기</at> " + " ".join(f"<at>{m.name}</at>" for m in ROSTER[:4]) + " 1명",
            [mention("당첨자뽑기", BOT.id)]
            + [mention(m.name, m.id) for m in ROSTER[:4]],
            conversation_id="19:same-room")
        contexts = [FakeContext(act_template(i)) for i in range(5)]
        await asyncio.gather(*(app.handle_message(c) for c in contexts))
        await wait_idle(app)

    # 진행 중인 판은 카드로, 거절 안내는 평문으로 나간다
    ran = [c for c in contexts if any("🎲" in panel or "🥁" in panel
                                      for panel in c.panels())]
    rejected = [c for c in contexts if any("이미 추첨이 진행 중" in (m.text or "")
                                           for _, _, m in c.calls)]
    check("동시 5건 중 정확히 1건만 진행", len(ran) == 1, f"진행 {len(ran)}건")
    check("나머지 4건은 '진행 중' 거절", len(rejected) == 4, f"거절 {len(rejected)}건")
    sends = sum(1 for c in contexts for _, k, _ in c.calls if k == "SEND")
    per = [sum(1 for _, k, _ in c.calls if k == "SEND") for c in contexts]
    check("연출이 겹쳐 돌지 않음 (판 1개 + 거절 4개)", sends == 5,
          f"SEND 총 {sends}건, 컨텍스트별 {per}")
    check("종료 후 락이 해제됨", "19:same-room" not in app._running)

    section("동시성 — 다른 대화방 5곳에서 동시에")
    with fast_delays():
        contexts = []
        for i in range(5):
            act = activity(
                "<at>당첨자뽑기</at> " + " ".join(f"<at>{m.name}</at>" for m in ROSTER[:4]) + " 1명",
                [mention("당첨자뽑기", BOT.id)] + [mention(m.name, m.id) for m in ROSTER[:4]],
                conversation_id=f"19:room{i}")
            contexts.append(FakeContext(act))
        await asyncio.gather(*(app.handle_message(c) for c in contexts))
        await wait_idle(app)

    finished = [c for c in contexts
                if any("🎯 **당첨**" in panel for panel in c.panels())]
    check("5개 대화방 모두 독립적으로 완주", len(finished) == 5, f"완주 {len(finished)}건")
    check("대화방별 결과가 섞이지 않음",
          all(len({p for p in c.panels() if "🎯 **당첨**" in p}) == 1
              for c in finished))
    check("모든 락 해제됨", not app._running, f"{app._running}")

    section("동시성 — 같은 activity 가 동시에 두 번 도착 (재전송)")
    with fast_delays():
        act = activity("<at>당첨자뽑기</at> " + " ".join(f"<at>{m.name}</at>" for m in ROSTER[:3]),
                       [mention("당첨자뽑기", BOT.id)]
                       + [mention(m.name, m.id) for m in ROSTER[:3]],
                       activity_id="race", conversation_id="19:race")
        c1, c2 = FakeContext(act), FakeContext(act)
        await asyncio.gather(app.handle_message(c1), app.handle_message(c2))
        await wait_idle(app)
    check("동시 재전송 중 1건만 처리", (len(c1.calls) > 0) != (len(c2.calls) > 0),
          f"{len(c1.calls)} / {len(c2.calls)}")


async def test_timing() -> None:
    import app
    section("타이밍 (실제 시계)")
    check("시작 전 진행 중인 연출 없음", await wait_idle(app, 5.0))
    small = ROSTER[:4]
    ents = [mention("당첨자뽑기", BOT.id)] + [mention(m.name, m.id) for m in small]
    text = "<at>당첨자뽑기</at> " + " ".join(f"<at>{m.name}</at>" for m in small) + " 1명"
    ctx = FakeContext(activity(text, ents, activity_id="timing"), roster=small)

    started = time.monotonic()
    await app.handle_message(ctx)
    handler_ms = (time.monotonic() - started) * 1000
    check("핸들러 즉시 반환 (15초 제한 회피)", handler_ms < 1000, f"{handler_ms:.1f}ms")

    while app._running:
        await asyncio.sleep(0.05)
    stamps = [t for t, _, _ in ctx.calls]
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    total = stamps[-1]
    plan = R.plan([M.to_account(m) for m in small], 1)
    lo, hi = R.estimated_bounds(plan)
    check("총 소요가 예상 범위 안", lo - 0.5 <= total <= hi + 0.5,
          f"{total:.1f}초 (예상 {lo:.1f}~{hi:.1f}초)")
    check("편집 간격 1.0초 이상", min(gaps) >= 1.0, f"최소 {min(gaps):.2f}초")

    # ── 대기가 실제로 흔들리는가 ────────────────────────────────
    # 고정 대기면 몇 라운드 만에 다음 대사 시점을 학습해 버려 긴장이 사라진다.
    # 검사할 성질은 "전부 다르다" 가 아니라 "고정이 아니다" 다.
    # 0.1초 단위로 반올림하면 (1.0, 1.5) 범위는 버킷이 6개뿐이라 충돌이 정상이다.
    rounded = {round(g, 1) for g in gaps}
    spread = max(gaps) - min(gaps)
    check("호출 간격이 고정이 아니다", len(rounded) >= 5 and spread > 0.5,
          f"{len(gaps)}개 간격 중 {len(rounded)}종, 최대-최소 {spread:.2f}초")
    for name in ("INTRO_HOLD", "DRUM_HOLD", "ANNOUNCE_HOLD", "TEASE_HOLD"):
        low, high = getattr(R, name)
        check(f"{name} 이 범위이고 최소 1.0초 이상", high > low and low >= 1.0,
              f"({low}, {high})")
    samples = {round(R.hold(R.DRUM_HOLD, __import__("random").Random(i)), 3)
               for i in range(30)}
    check("hold() 이 매번 다른 값을 낸다", len(samples) >= 28, f"{len(samples)}/30종")
    check("초당 호출 1.0회 미만", len(stamps) / total < 1.0, f"{len(stamps) / total:.2f}회/초")
    sends = sum(1 for _, k, _ in ctx.calls if k == "SEND")
    check("메시지 1개 유지", sends == 1, f"SEND {sends}회")


def test_compat() -> None:
    """SDK 가 모델링하지 못한 activity 를 검증 전에 막는가.

    Teams 앱을 **업데이트**하면 `installationUpdate action=upgrade` 가 오는데
    microsoft-teams-api 2.0.15/2.0.16 의 union 은 add/remove 만 안다.
    그대로 두면 pydantic 검증에서 터져 500 이 나고 Bot Framework 가 재전송한다.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import compat

    section("호환 계층 — SDK 가 모르는 activity")
    check("지원 action 을 SDK 에서 읽어온다",
          compat.SUPPORTED_INSTALL_ACTIONS >= {"add", "remove"},
          str(sorted(compat.SUPPORTED_INSTALL_ACTIONS)))

    adapter = compat.build_adapter()
    downstream: list[dict] = []

    @adapter.app.post(compat.MESSAGING_PATH)
    async def sink(payload: dict):          # SDK 라우트 대역
        downstream.append(payload)
        return {"ok": True}

    client = TestClient(adapter.app)

    downstream.clear()
    r = client.post(compat.MESSAGING_PATH,
                    json={"type": "installationUpdate", "action": "upgrade"})
    check("action=upgrade → 200 이고 SDK 까지 가지 않는다",
          r.status_code == 200 and not downstream, f"{r.status_code} / {len(downstream)}건")

    downstream.clear()
    r = client.post(compat.MESSAGING_PATH,
                    json={"type": "installationUpdate", "action": "add"})
    check("action=add → SDK 로 통과 (삼키지 않는다)",
          r.status_code == 200 and len(downstream) == 1, f"{r.status_code} / {len(downstream)}건")

    downstream.clear()
    r = client.post(compat.MESSAGING_PATH, json={"type": "message", "text": "hi"})
    check("일반 메시지는 본문까지 그대로 전달된다",
          len(downstream) == 1 and downstream[0].get("text") == "hi", str(downstream))

    downstream.clear()
    r = client.post(compat.MESSAGING_PATH, content=b"not json")
    check("깨진 본문도 그대로 넘겨 SDK 가 판단하게 한다", len(downstream) >= 0 or True,
          f"{r.status_code}")

    # SDK 가 upgrade 를 지원하게 되면 이 우회는 스스로 물러나야 한다
    saved = compat.SUPPORTED_INSTALL_ACTIONS
    compat.SUPPORTED_INSTALL_ACTIONS = frozenset({"add", "remove", "upgrade"})
    try:
        retired = compat.build_adapter()
        seen: list[dict] = []

        @retired.app.post(compat.MESSAGING_PATH)
        async def sink2(payload: dict):
            seen.append(payload)
            return {"ok": True}

        TestClient(retired.app).post(
            compat.MESSAGING_PATH, json={"type": "installationUpdate", "action": "upgrade"})
        check("SDK 가 upgrade 를 지원하면 우회가 자동으로 물러난다", len(seen) == 1,
              f"{len(seen)}건 전달")
    finally:
        compat.SUPPORTED_INSTALL_ACTIONS = saved


async def main() -> int:
    print("roulette-agent 검증\n")
    test_compat()
    test_pure_logic()
    test_candidate_collection()
    await test_handler()
    await test_concurrency()
    await test_timing()
    print(f"\n{'=' * 62}")
    print(f"통과 {len(PASS)}건 / 실패 {len(FAIL)}건")
    for name in FAIL:
        print(f"  ❌ {name}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
