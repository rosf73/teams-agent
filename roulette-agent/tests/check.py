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
            setattr(R, k, 0.0)
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
    check("메시지는 딱 1개 (나머지는 편집)", len(sends) == 1, f"SEND {len(sends)} / UPDATE {len(updates)}")
    check("@태그를 전혀 쓰지 않음", not ctx.mentioned_names(), f"{ctx.mentioned_names()}")

    final = (updates[-1].text or "")
    for field in ("🎲 **당첨자뽑기**", "▸ **종료됨**", "▸ 생존 ", "🎯 **당첨**"):
        check(f"최종 판에 {field!r} 있음", field in final)
    check("후보 10명 이름이 모두 판에 등장 (짧은 이름)",
          all(f"**{M.short_name(n)}**" in final for n in NAMES), "")
    check("긴 표시명은 축약", "(Alpha Tester)" not in final, "")
    check("진척 바가 8/8 로 끝남", "8/8" in final, final.split("\n")[3] if "\n" in final else "")

    # 패널은 "\n\n" 으로 이어진 블록이고 마지막 블록이 대사 필드다.
    # 패널 전체에서 이름을 찾으면 헤더와 생존자 목록의 볼드까지 잡힌다.
    def spoken(message) -> str:
        return (message.text or "").split("\n\n")[-1]

    tease_marks = ("농담", "아 아닙니다", "인 줄 알았죠", "다시 뵙겠습니다",
                   "할 줄 아셨죠", "표정 좋으신데", "어? 아니네요", "긴장하셨나요")
    teases = [spoken(m) for m in updates if any(k in spoken(m) for k in tease_marks)]
    check("마지막 단계 뜸들이기 3~5회", 3 <= len(teases) <= 5, f"{len(teases)}회")

    import re as _re
    called = [(_re.search(r"\*\*(.+?)\*\*", t).group(1)
               if _re.search(r"\*\*(.+?)\*\*", t) else None) for t in teases]
    check("뜸들이기 대상은 남은 후보(3명)뿐", len(set(called)) <= 3, f"{sorted(set(called))}")
    consecutive = sum(1 for a, b in zip(called, called[1:]) if a and a == b)
    check("뜸들이기 이름 연속 중복 없음", consecutive == 0, f"중복 {consecutive}건")

    check("진척 라벨이 '완료' 로 구분됨", "완료 8/8" in final)
    progress = [l for l in final.splitlines() if "완료" in l][0]
    check("진척 바를 백틱으로 감싸지 않는다", "`" not in progress, progress)
    check("진척 바가 채움 문자로 가득 찬다",
          progress.count(app.BAR_FILLED) == app.BAR_WIDTH
          and app.BAR_EMPTY not in progress, progress)

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
          any("🎯 **당첨**" in (m.text or "") for _, k, m in ctx.calls if k == "UPDATE"))

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
          any("🎯 **당첨**" in (m.text or "") for _, k, m in ctx.calls if k == "UPDATE")
          and "채팅방 전원" not in "\n".join(ctx.texts()))


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
    lo = R.estimated_seconds(plan, R.TEASE_MIN)
    hi = R.estimated_seconds(plan, R.TEASE_MAX)
    check("총 소요가 예상 범위 안", lo - 1.0 <= total <= hi + 1.0,
          f"{total:.1f}초 (예상 {lo:.1f}~{hi:.1f}초)")
    check("편집 간격 1.0초 이상", min(gaps) >= 1.0, f"최소 {min(gaps):.2f}초")
    check("초당 호출 1.0회 미만", len(stamps) / total < 1.0, f"{len(stamps) / total:.2f}회/초")
    sends = sum(1 for _, k, _ in ctx.calls if k == "SEND")
    check("메시지 1개 유지", sends == 1, f"SEND {sends}회")


async def main() -> int:
    print("roulette-agent 검증\n")
    test_pure_logic()
    test_candidate_collection()
    await test_handler()
    await test_timing()
    print(f"\n{'=' * 62}")
    print(f"통과 {len(PASS)}건 / 실패 {len(FAIL)}건")
    for name in FAIL:
        print(f"  ❌ {name}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
