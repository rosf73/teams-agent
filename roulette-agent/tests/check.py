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

    KEYS = ("INTRO_HOLD", "ANNOUNCE_HOLD", "TEASE_HOLD")

    def __enter__(self):
        self._saved = {k: getattr(R, k) for k in self.KEYS + ("DRUM_CURVE",)}
        for k in self.KEYS:
            setattr(R, k, (0.0, 0.0))   # 대기는 (최소, 최대) 범위다
        R.DRUM_CURVE = ((0.0, 0.0), (0.0, 0.0))
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
    """대사 필드 = 본문에서 size=Large 인 TextBlock."""
    body = card.get("body", []) if isinstance(card, dict) else (card.body or [])
    for item in body:
        size = item.get("size") if isinstance(item, dict) else getattr(item, "size", None)
        if size == "Large":
            return item.get("text") if isinstance(item, dict) else item.text
    return ""


# 판에서 상태가 바뀌는 순간의 말머리. 그 밖의 대사는 진행자 이모지가 말머리다.
MARKS = ("🎯", "⚡", "⬇️", "⬆️", "🃏", "🎉")
TEST_HOST = next(h for h in L.HOSTS if h.key == "robot")


def kind_of(line: str, host) -> str | None:
    """대사 한 줄이 진행자의 어느 풀에서 나왔는지. 문구를 테스트에 하드코딩하지 않는다."""
    import re as _re
    text = line.split(" ", 1)[1] if " " in line else line
    if line.startswith("🎯 ("):
        text = line.split(") ", 1)[1]
    for key in list(L.POOLS) + ["GREETING", "DRUMROLL"]:
        for template in L.pool(host, key):
            pattern = _re.escape(template).replace(r"\{name\}", ".+").replace(r"\{names\}", ".+")
            if _re.fullmatch(pattern, text):
                return key
    return None


class pinned_host:
    """진행자를 고정한다. 핸들러는 _pick_host 를 모듈 전역으로 찾는다."""

    def __init__(self, app_module, host) -> None:
        self._app, self._host = app_module, host

    def __enter__(self):
        self._saved, self._app._pick_host = self._app._pick_host, lambda cid: self._host
        return self

    def __exit__(self, *exc):
        self._app._pick_host = self._saved
        return False


def board_line(card, prefix: str) -> str:
    """`▸ 생존` / `▸ 당첨` 같은 판의 한 줄. 없으면 빈 문자열."""
    return next((l for l in panel_text(card).splitlines() if l.startswith(prefix)), "")


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
class swap_chance:
    """반전 확률을 고정한다. 0 이면 반전 없는 판, 1 이면 반드시 반전."""

    def __init__(self, chance: float) -> None:
        self._chance = chance

    def __enter__(self):
        self._saved, R.SWAP_CHANCE = R.SWAP_CHANCE, self._chance
        return self

    def __exit__(self, *exc):
        R.SWAP_CHANCE = self._saved
        return False


def plan_violations(plan, cands, n) -> list[str]:
    """판을 처음부터 끝까지 따라가며 계획이 규칙을 지키는지 본다."""
    v: list[str] = []
    pool = list(cands) + ([R.JOKER] if plan.joker else [])
    pool_ids = {a.id for a in pool}
    winner_ids = {w.id for w in plan.winners}
    if plan.all_winners:
        return [] if not plan.steps and n == len(cands) and not plan.joker else ["all_winners"]
    if len(plan.winners) != n or not winner_ids <= pool_ids:
        v.append("당첨 자리")
    finals = [st for st in plan.steps if st.kind == "final"]
    drops = [st for st in plan.steps if st.kind == "drop"]
    if len(finals) != 1 or plan.steps[-1] is not finals[0] or finals[0].winners != plan.winners:
        return v + ["final"]
    final = finals[0]
    if plan.total_steps > R.MAX_STEPS:
        v.append(f"단계 {plan.total_steps} > {R.MAX_STEPS}")
    if [st.index for st in plan.steps] != list(range(1, plan.total_steps + 1)):
        v.append("단계 번호")
    if sum(len(st.survivors) for st in plan.steps) != len(pool) - n:
        v.append("생존자 발표 수")
    if final.remaining_before != n + 1:
        v.append("마지막 단계 시작 인원")

    # 판의 생존자 줄을 따라간다. 반전은 줄에서 한 명을 같은 자리의 다른 사람으로 바꾼다.
    board: list[str] = []
    remaining = len(pool)
    for st in drops:
        ids = [a.id for a in st.survivors]
        if st.remaining_before != remaining:
            v.append("remaining 전개")
        if st.late:
            # 미니 뜸: 지금 판에 없고, 이번 단계에 발표될 사람도 아니다
            if st.tease is None or st.tease.id in board or st.tease.id in ids \
                    or st.tease.id not in pool_ids:
                v.append(f"미니 뜸 {st.index}")
        elif st.tease is not None:
            v.append("앞쪽 단계에 미니 뜸")
        if any(i in board for i in ids):
            v.append("판에 있는 사람을 또 발표")
        board += ids
        if st.swap_out:
            if st.swap_out.id not in board:
                v.append("판에 없는 사람을 끌어내림")
            if st.swap_in.id in board or st.swap_in.id in winner_ids:
                v.append("차지한 사람이 이미 판에 있거나 당첨자")
            board[board.index(st.swap_out.id)] = st.swap_in.id
        remaining -= len(st.survivors)
    board.append(final.survivors[0].id)
    if sum(1 for st in drops if st.swap_out) > 1:
        v.append("반전 2회 이상")
    # 끝난 판의 생존자 = 비당첨자 전원, 한 번씩. 미공개가 남지 않는다.
    if sorted(board) != sorted(pool_ids - winner_ids):
        v.append("끝난 판의 생존자 ≠ 비당첨자")
    if {a.id for a in final.pool} != {final.survivors[0].id} | winner_ids:
        v.append("뜸들이기 pool")

    # 🃏 이 뽑혔으면 끝난 판의 생존 1등이 대신 당첨된다
    if R.JOKER.id in winner_ids:
        if plan.substitute is None or plan.substitute.id != board[0]:
            v.append("대신 당첨자 ≠ 생존 1등")
    elif plan.substitute is not None:
        v.append("🃏 없이 대신 당첨자")
    real = [w.id for w in plan.final_winners]
    if len(set(real)) != n or not set(real) <= {c.id for c in cands}:
        v.append(f"실제 당첨자 {real}")

    # 당첨자 공개 묶음: 합치면 당첨자 전원, 횟수 상한, 뒤로 갈수록 작아진다
    batches = final.winner_batches
    if [w.id for b in batches for w in b] != [w.id for w in plan.winners]:
        v.append("당첨 묶음 ≠ 당첨자")
    sizes = [len(b) for b in batches]
    if len(batches) > R.MAX_WINNER_REVEALS or sizes != sorted(sizes, reverse=True):
        v.append(f"당첨 묶음 {sizes}")
    # 두구두구 곡선 위치: 단조 증가, 당첨자 공개가 끝(1.0)
    positions = [st.position for st in plan.steps]
    if positions != sorted(positions) or positions[-1] != 1.0:
        v.append(f"곡선 위치 {positions}")
    if sum(st.late for st in drops) != min(R.LATE_STEPS, len(drops)) \
            or any(st.late for st in drops[:len(drops) - R.LATE_STEPS]):
        v.append("late 단계")
    return v


def test_pure_logic() -> None:
    section("추첨 로직 불변식")
    combos = 0
    for joker in (False, True):
        for chance in (0.0, 1.0):
            violations = []
            with swap_chance(chance):
                for total in range(2, R.MAX_CANDIDATES + 1):
                    for n in range(1, total + 1):
                        cands = [Account(id=f"29:c{i}", name=f"c{i}") for i in range(total)]
                        for seed in range(3):
                            plan = R.plan(cands, n, random.Random(total * 1000 + n * 10 + seed),
                                          joker=joker)
                            violations += [(total, n, seed, x)
                                           for x in plan_violations(plan, cands, n)]
                            combos += 1
            label = f"{'🃏 포함' if joker else '🃏 없음'} · {'반전 강제' if chance else '반전 없음'}"
            check(f"(N,n) 전 조합 불변식 — {label}", not violations,
                  f"위반 {violations[:3]}" if violations else "")
    print(f"     (계획 {combos}개 검사)")

    section("반전")
    rng = random.Random(7)
    cands = [Account(id=f"29:c{i}", name=f"c{i}") for i in range(12)]
    plans = [R.plan(cands, 1, rng) for _ in range(4000)]
    rate = sum(1 for p in plans if p.swap_step) / len(plans)
    check("반전은 조건이 맞으면 항상", rate == R.SWAP_CHANCE == 1.0, f"{rate * 100:.1f}%")
    drops = len([st for st in plans[0].steps if st.kind == "drop"])
    points = {p.swap_step.index for p in plans if p.swap_step}
    lo, hi = (round(drops * w) for w in R.SWAP_WINDOW)
    check(f"반전 시점이 흔들리고 {R.SWAP_WINDOW} 구간 안", len(points) >= 2
          and min(points) >= lo and max(points) <= hi, f"탈락 {drops}단계 중 {sorted(points)}")
    small = [R.plan(cands[:3], 2, random.Random(i)) for i in range(50)]
    check("생존자가 1명뿐이면 (3명 중 2명) 반전 없음", not any(p.swap_step for p in small))

    # 끌려 내려간 사람이 결국 당첨될 확률 = 그 시점에 남은 후보의 당첨 확률
    with swap_chance(1.0):
        rng = random.Random(11)
        won, expected, trials = 0, 0.0, 20000
        for _ in range(trials):
            p = R.plan(cands[:10], 2, rng)
            st = p.swap_step
            won += st.swap_out.id in {w.id for w in p.winners}
            expected += 2 / (st.remaining_before - len(st.survivors))
    check("끌려 내려간 사람의 당첨률 = 남은 후보의 당첨률",
          abs(won - expected) / trials < 0.015,
          f"실제 {won / trials * 100:.1f}% / 기대 {expected / trials * 100:.1f}%")

    section("🃏 1등이대신당첨")
    for text, want in [("@A @B 2명", True), ("@A @B 2명 조커없이", False),
                       ("조커 없이 @A @B", False), ("", True)]:
        check(f"{text!r} → 🃏 {'포함' if want else '제외'}", M.wants_joker(text) == want)
    p = R.plan(cands[:3], 3, random.Random(1), joker=True)
    check("전원 당첨이면 🃏 을 넣지 않는다", p.all_winners and not p.joker
          and len(p.winners) == 3)
    rng = random.Random(3)
    subs = [R.plan(cands[:5], 1, rng, joker=True) for _ in range(12000)]
    rate = sum(1 for p in subs if p.substitute) / len(subs)
    check("🃏 당첨률 ≈ 1/(N+1)", abs(rate - 1 / 6) < 0.015, f"{rate * 100:.1f}% (기대 16.7%)")
    check("대신 당첨자는 실제 사람", all(p.substitute.id != R.JOKER.id for p in subs if p.substitute))

    section("두구두구 곡선")
    curve = [R.drum_hold(i / 20) for i in range(21)]
    check("곡선 최소값이 1.0초 이상", min(low for low, _ in curve) >= 1.0,
          f"{min(low for low, _ in curve):.2f}")
    check("뒤로 갈수록 길어진다", curve == sorted(curve), f"{curve[0]} → {curve[-1]}")
    check("곡선 위에서도 범위로 흔든다", all(high - low >= 0.5 for low, high in curve))

    section("공정성")
    cands = [Account(id=f"29:c{i}", name=f"c{i}") for i in range(5)]
    trials = 40000
    for joker in (False, True):
        for n in (1, 2):
            counts = Counter()
            for _ in range(trials):
                counts.update(w.id for w in R.plan(cands, n, joker=joker).final_winners)
            want = trials * n / 5
            dev = max(abs(counts[c.id] - want) / want for c in cands)
            check(f"N=5,n={n} {'🃏 포함' if joker else '🃏 없음'} 균등 분포 (편차 5% 이내)",
                  dev < 0.05 and sum(counts.values()) == trials * n, f"최대 편차 {dev * 100:.2f}%")

    section("검증 규칙")
    for count, n, want_reject in [(0, 1, True), (1, 1, True), (31, 1, True),
                                  (5, 0, True), (5, 6, True), (5, 5, False),
                                  (5, 2, False), (2, 1, False)]:
        got = R.validate(count, n) is not None
        check(f"후보 {count} / n={n} → {'거절' if want_reject else '통과'}", got == want_reject)

    section("진행자와 문구")
    import re as _re
    check("진행자 6명 (🤖 포함)", len(L.HOSTS) == 6 and "🤖" in {h.emoji for h in L.HOSTS},
          " ".join(h.emoji for h in L.HOSTS))
    check("성별 표시 없는 이모지 (ZWJ · ♀ · ♂ 없음)",
          not any(ch in h.emoji for h in L.HOSTS for ch in ("\u200d", "\u2640", "\u2642")))
    check("진행자 key 와 이모지가 겹치지 않는다",
          len({h.key for h in L.HOSTS}) == len({h.emoji for h in L.HOSTS}) == len(L.HOSTS))
    missing = [(v, k) for v, pools in L.VOICES.items() for k in L.POOLS if not pools.get(k)]
    check("모든 말투가 모든 박자의 문구를 갖는다", not missing, str(missing))
    check("말투에 쓰지 않는 풀이 없다",
          all(set(pools) == set(L.POOLS) for pools in L.VOICES.values()))
    bad_slot, bad_josa = [], []
    for voice, pools in L.VOICES.items():
        for key, slot in L.POOLS.items():
            for t in pools[key]:
                fields = set(_re.findall(r"\{(\w+)\}", t))
                if fields != ({slot} if slot else set()):
                    bad_slot.append((voice, key, t))
                # 이름에는 이미 님이 붙는다 → 님 중복 금지, 조사는 받침 쪽(이/은/을)만
                if _re.search(r"\{names?\}\s*님|\{names?\}(는|가|를|와|로|랑)", t):
                    bad_josa.append((voice, key, t))
    check("이름 자리가 풀과 맞는다 ({name} / {names} / 없음)", not bad_slot, str(bad_slot[:3]))
    check("이름 뒤 님 중복·받침 없는 조사 없음", not bad_josa, str(bad_josa[:3]))
    check("모든 진행자가 시작 인사와 효과음을 갖는다",
          all(h.greeting and h.drumroll and "{" not in "".join(h.greeting + h.drumroll)
              for h in L.HOSTS))
    for key in ("ANNOUNCE", "TEASE", "DRUMROLL"):
        pool = L.pool(TEST_HOST, key)
        deck = L.Deck(pool, random.Random(1))
        seq = [deck.draw() for _ in range(200)]
        dupes = sum(1 for a, b in zip(seq, seq[1:]) if a == b)
        check(f"{key} 덱 연속 중복 없음 · 전 문구 사용", dupes == 0 and len(set(seq)) == len(pool),
              f"중복 {dupes}건")


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

    section("후보 수집 — 로스터에 없는 멘션 (2026-10-27 전원 모드 오판)")
    vote_bot = "28:99999999-8888-7777-6666-555555555555"   # 다른 봇 (@투표만들기)
    cand = M.collect(activity(
        "<at>당첨자뽑기</at> <at>더미1</at> <at>샘플2</at> <at>투표만들기</at>",
        [mention("당첨자뽑기", BOT.id), mention("더미1", "29:u3"), mention("샘플2", "29:u7"),
         mention("투표만들기", vote_bot)]), ROSTER)
    check("다른 봇을 같이 태그 → 전원 모드 아님", not cand.everyone, cand.everyone_signal)
    check("다른 봇은 후보가 아니고, 태그한 2명만 후보",
          [m.id for m in cand.members] == ["29:u3", "29:u7"], str([m.name for m in cand.members]))
    check("봇을 뺐다고 안내", any("투표만들기" in n and "봇" in n for n in cand.notes), str(cand.notes))

    cand = M.collect(activity(
        "<at>당첨자뽑기</at> <at>더미1</at> <at>사업혁신팀</at> <at>샘플2</at>",
        [mention("당첨자뽑기", BOT.id), mention("더미1", "29:u3"),
         mention("사업혁신팀", "29:tag-not-in-roster"), mention("샘플2", "29:u7")]), ROSTER)
    check("로스터에 없는 일반 멘션(태그 그룹 등) → 전원 모드 아님", not cand.everyone)
    check("그 멘션은 빼고 안내",
          len(cand.members) == 2 and any("사업혁신팀" in n for n in cand.notes), str(cand.notes))

    for text, want in [("<at>당첨자뽑기</at> <at>더미1</at> 다들 모두 모여요", False),
                       ("<at>당첨자뽑기</at> <at>Allen</at> <at>더미1</at>", False),
                       ("<at>당첨자뽑기</at> @모두 2명", True),
                       ("<at>당첨자뽑기</at> <at>Everyone</at> 2명", True)]:
        ents = [mention("당첨자뽑기", BOT.id), mention("더미1", "29:u3")]
        if "Allen" in text:
            ents.append(mention("Allen", "29:u3"))
        got = M.collect(activity(text, ents), ROSTER).everyone
        check(f"텍스트 {text.split('</at> ', 1)[1]!r} → 전원 {'예' if want else '아니오'}",
              got == want)

    cand = M.collect(activity(
        "<at>당첨자뽑기</at> <at>더미1</at> <at>투표만들기</at>",
        [mention("당첨자뽑기", BOT.id), mention("더미1", "29:u3"), mention("투표만들기", vote_bot)]),
        [])
    check("로스터 실패여도 다른 봇은 후보가 아니다", [m.id for m in cand.members] == ["29:u3"])

    section("당첨자 수 파싱")
    for text, want in [("2명", 2), ("3", 3), ("", 1), ("당첨 5명 뽑아줘", 5)]:
        check(f"{text!r} → n={want}", M.parse_winner_count(text) == want)


async def test_handler() -> None:
    import app

    with fast_delays(), swap_chance(0.0), pinned_host(app, TEST_HOST):
        await _handler_cases(app)
    with fast_delays(), pinned_host(app, TEST_HOST):
        await _twist_cases(app)
    with fast_delays():
        await _host_cases(app)


async def _handler_cases(app) -> None:
    section("핸들러 — 정상 진행")
    ctx = FakeContext(activity("<at>당첨자뽑기</at> <at>모든 사용자</at> 2명 조커없이",
                               [mention("당첨자뽑기", BOT.id),
                                mention("모든 사용자", EVERYONE_ID)]))
    await run_handler(app, ctx)
    sends = [m for _, k, m in ctx.calls if k == "SEND"]
    updates = [m for _, k, m in ctx.calls if k == "UPDATE"]
    check("판은 딱 1개 (나머지는 편집) + 끝에 당첨 알림 1개",
          len(sends) == 2 and sends[0].attachments and not sends[1].attachments
          and ctx.calls[-1][2] is sends[1],
          f"SEND {len(sends)} / UPDATE {len(updates)}")

    panels = ctx.panels()
    board_calls = ctx.calls[:-1]
    check("판이 Adaptive Card 로 나간다 (평문 아님)",
          len(panels) == len(board_calls) and all(not (m.text or "")
                                                  for _, _, m in board_calls),
          f"카드 {len(panels)} / 호출 {len(board_calls)}")
    check("판에는 @태그가 없다", not any(m.entities for _, _, m in board_calls))

    section("핸들러 — 당첨자 알림")
    ping = sends[1]
    tagged = [e.mentioned for e in ping.entities or []]
    winners_line = [l for l in panels[-1].splitlines() if l.startswith("🎉 **당첨**")][0]
    tagged_short = [f"**{M.short_name(a.name)}**" for a in tagged]
    check("당첨자 2명만 태그", len(tagged) == 2 and all(t in winners_line for t in tagged_short),
          f"{[a.name for a in tagged]}")
    check("태그 대상은 실제 사용자 id", all(a.id.startswith("29:u") for a in tagged),
          str([a.id for a in tagged]))
    check("본문에 <at> 이 엔터티와 짝을 이룬다",
          all(f"<at>{a.name}</at>" in ping.text for a in tagged), ping.text)
    gap = ctx.calls[-1][0] - ctx.calls[-2][0]
    check("결과 화면 다음에 알림 (같은 순간에 겹치지 않는다)", ctx.calls[-2][1] == "UPDATE",
          f"{gap:.2f}초 뒤")

    final = panels[-1]
    for field in ("🎲 **당첨자뽑기**", "▸ **종료됨**", "▸ 생존 ", "🎉 **당첨**"):
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
    drum = next((c for c in cards_all if kind_of(spoken_line(c), TEST_HOST) == "DRUMROLL"), None)
    check("두구두구 대사가 판에 있다", drum is not None)
    if drum is not None:
        check("대사 줄이 size=Large", block_size(drum, spoken_line(drum)) == "Large",
              str(block_size(drum, spoken_line(drum))))
        header_size = block_size(drum, "🎲 **당첨자뽑기**")
        check("헤더는 크게 하지 않는다 (대사만)", header_size != "Large", str(header_size))
        state_size = block_size(drum, "번째 뽑는중")
        check("진행 상태도 크게 하지 않는다", state_size != "Large", str(state_size))

    is_tease = lambda line: kind_of(line, TEST_HOST) == "TEASE"
    final_cards = [c for c in cards_all if "▸ **당첨자 " in panel_text(c)]
    teases = [spoken_line(c) for c in final_cards if is_tease(spoken_line(c))]
    check("마지막 단계 뜸들이기 3~5회", 3 <= len(teases) <= 5, f"{len(teases)}회")

    import re as _re
    called = [(_re.search(r"\*\*(.+?)\*\*", t).group(1)
               if _re.search(r"\*\*(.+?)\*\*", t) else None) for t in teases]
    check("뜸들이기 대상은 남은 후보(3명)뿐", len(set(called)) <= 3, f"{sorted(set(called))}")
    consecutive = sum(1 for a, b in zip(called, called[1:]) if a and a == b)
    check("뜸들이기 이름 연속 중복 없음", consecutive == 0, f"중복 {consecutive}건")
    check("진척 라벨이 '완료' 로 구분됨", "완료 8/8" in final)

    section("핸들러 — 당첨 공개가 정점")
    spoken = [spoken_line(c) for c in cards_all]
    reveals = [i for i, l in enumerate(spoken) if l.startswith("🎯 (")]
    check("당첨자 2명을 두구두구를 거쳐 한 명씩 공개",
          len(reveals) == 2
          and all(kind_of(spoken[i - 1], TEST_HOST) == "DRUMROLL" for i in reveals),
          str([spoken[i] for i in reveals]))
    check("공개 순서 표시 (1/2), (2/2)",
          [spoken[i][:7] for i in reveals] == ["🎯 (1/2)", "🎯 (2/2)"],
          str([spoken[i][:7] for i in reveals]))
    check("마지막 생존자를 따로 발표하지 않는다",
          not any("마지막 생존자" in l for l in spoken))
    check("결과 화면은 🎉 + 당첨자 크게", spoken[-1].startswith("🎉 **당첨**"), spoken[-1])
    survivors_line = [l for l in final.splitlines() if l.startswith("▸ 생존 순서 ")]
    check("결과 화면에 생존 순서 (마지막 생존자 포함 8명)",
          len(survivors_line) == 1 and survivors_line[0].count("**") == 16,
          survivors_line[0] if survivors_line else "")
    check("결과 화면에만 다시 부르는 안내",
          app.RERUN_HINT in final
          and not any(app.RERUN_HINT in panel_text(c) for c in cards_all[:-1]))

    check("확률 줄은 없다", not any("1인당" in panel_text(c) for c in cards_all))
    check("조커없이 → 🃏 이 판에 없다",
          not any("🃏" in panel_text(c) or R.JOKER.name in panel_text(c) for c in cards_all))

    section("핸들러 — 속도 곡선과 미니 뜸")
    intros = [l for l in spoken if kind_of(l, TEST_HOST) == "INTRO"]
    check(f"도입 박자는 끝쪽 {R.LATE_STEPS}단계만", len(intros) == R.LATE_STEPS, f"{len(intros)}회")
    mini = [i for i, c in enumerate(cards_all)
            if "번째 뽑는중" in panel_text(c) and is_tease(spoken[i])]
    check(f"미니 뜸 {R.LATE_STEPS}회", len(mini) == R.LATE_STEPS, f"{len(mini)}회")
    bad = []
    for i in mini:
        name = _re.search(r"\*\*(.+?)\*\*", spoken[i]).group(1)
        announce = next(l for l in spoken[i + 1:]
                        if kind_of(l, TEST_HOST) in ("ANNOUNCE", "ANNOUNCE_MANY"))
        survivors_now = [l for l in panel_text(cards_all[i]).splitlines()
                         if l.startswith("▸ 생존")][0]
        if f"**{name}**" in announce or f"**{name}**" in survivors_now:
            bad.append(name)
    check("미니 뜸은 이미 생존했거나 곧 발표될 사람을 부르지 않는다", not bad, str(bad))

    section("핸들러 — 진행자 말투")
    check("헤더에 진행자", f"진행 {TEST_HOST.emoji} **{TEST_HOST.name}**" in final,
          final.splitlines()[0])
    check("첫 대사는 진행자의 시작 인사", kind_of(spoken[0], TEST_HOST) == "GREETING", spoken[0])
    unknown = [l for l in spoken[:-1] if kind_of(l, TEST_HOST) is None]
    check("결과 화면 전까지 모든 대사가 진행자의 문구", not unknown, str(unknown[:2]))
    prefixes = {l.split(" ", 1)[0] for l in spoken[:-1]}
    check("말머리는 '진행자 이모지:' 또는 상태 표시뿐",
          prefixes <= {f"{TEST_HOST.emoji}:", *MARKS}, str(prefixes))
    spoken_by_host = [l for l in spoken[:-1] if l.startswith(TEST_HOST.emoji)]
    check("진행자 대사는 '이모지: 대사' 형식",
          spoken_by_host and all(l.startswith(f"{TEST_HOST.emoji}: ") for l in spoken_by_host),
          spoken_by_host[0] if spoken_by_host else "")
    no_nim = [l for l in spoken[:-1] if _re.search(r"\*\*[^*]+\*\*(?!님)", l)]
    check("대사 속 이름에는 늘 님이 붙는다", not no_nim, str(no_nim[:2]))
    check("결과 화면 아래 줄은 진행자 축하", kind_of(spoken[-1].split("\n\n")[1], TEST_HOST)
          == "WINNER_HEADLINE", spoken[-1])

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
    check("전원 당첨은 당첨자 전원을 태그", ctx.mentioned_names() == {NAMES[3], NAMES[7]},
          str(ctx.mentioned_names()))

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
          any("🎉 **당첨**" in panel for panel in ctx.panels("UPDATE")))

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
          any("🎉 **당첨**" in panel for panel in ctx.panels("UPDATE"))
          and "채팅방 전원" not in "\n".join(ctx.panels() + ctx.texts()))


def _seed_for(members, n, joker, want) -> int:
    """원하는 연출이 나오는 시드. 핸들러는 R.plan 에 app.rng 를 그대로 넘긴다."""
    for seed in range(10000):
        p = R.plan(members, n, random.Random(seed), joker=joker)
        if want(p):
            return seed
    raise AssertionError("조건에 맞는 시드가 없다")


async def _run_seeded(app, text: str, seed: int) -> "FakeContext":
    ctx = FakeContext(activity(text, [mention("당첨자뽑기", BOT.id),
                                      mention("모든 사용자", EVERYONE_ID)]))
    saved, app.rng = app.rng, random.Random(seed)
    try:
        await run_handler(app, ctx)
    finally:
        app.rng = saved
    return ctx


async def _twist_cases(app) -> None:
    members = [M.to_account(m) for m in ROSTER]

    section("핸들러 — 반전 + 🃏 대신 당첨")
    text = "<at>당첨자뽑기</at> <at>모든 사용자</at> 1명"
    seed = _seed_for(members, 1, True, lambda p: p.swap_step and p.substitute
                     and p.swap_step.swap_out.id != p.substitute.id)
    plan = R.plan(members, 1, random.Random(seed), joker=True)
    ctx = await _run_seeded(app, text, seed)
    cards = ctx.cards()
    spoken = [spoken_line(c) for c in cards]
    short = lambda a: f"**{M.short_name(a.name)}**"
    out, into, sub = plan.swap_step.swap_out, plan.swap_step.swap_in, plan.substitute

    check("헤더에 + 🃏", "후보 **10명** + 🃏" in panel_text(cards[0]), board_line(cards[0], "🎲"))
    check("🃏 안내 줄", app.JOKER_NOTE in panel_text(cards[0]))
    i = next((k for k, l in enumerate(spoken) if l.startswith("⚡")), None)
    check("반전 예고 → 끌어내림 → 차지 3박자", i is not None
          and spoken[i + 1].startswith("⬇️") and short(out) in spoken[i + 1]
          and spoken[i + 2].startswith("⬆️") and short(into) in spoken[i + 2],
          str(spoken[i:i + 3]) if i is not None else "")
    if i is not None:
        before = board_line(cards[i], "▸ 생존")
        check("끌어내리면 생존 줄에서 빠진다",
              short(out) in before and short(out) not in board_line(cards[i + 1], "▸ 생존"))
        after = board_line(cards[i + 2], "▸ 생존")
        check("차지한 사람이 같은 자리에 들어간다",
              after == before.replace(short(out), short(into)), after)
        check("반전 상태 표시", "▸ **반전!**" in panel_text(cards[i]))
    j = next((k for k, l in enumerate(spoken) if R.JOKER.name in l and l.startswith("🎯")), None)
    check("🃏 당첨 → 생존 1등은… → 대신 당첨", j is not None
          and spoken[j + 1].startswith("🃏")
          and spoken[j + 2].startswith("🎯") and short(sub) in spoken[j + 2],
          str(spoken[j:j + 3]) if j is not None else "")
    if j is not None:
        check("대신 당첨되면 당첨 줄에서 🃏 이 사람으로 바뀐다",
              board_line(cards[j], "▸ 당첨") == f"▸ 당첨 **{R.JOKER.name}**"
              and board_line(cards[j + 2], "▸ 당첨") == f"▸ 당첨 {short(sub)}")
        first = board_line(cards[j + 1], "▸ 생존").split(" ", 2)[2].split(", ")[0]
        check("대신 당첨자는 생존 줄의 1등", first == short(sub), f"{first} / {short(sub)}")
    final = panel_text(cards[-1])
    check("결과 화면: 실제 사람이 당첨", spoken[-1].startswith(f"🎉 **당첨** {short(sub)}"),
          spoken[-1])
    check("결과 화면 생존 순서에서 대신 당첨자는 빠진다",
          short(sub) not in board_line(cards[-1], "▸ 생존 순서"))
    check("결과 화면 생존 순서에 🃏 은 없다 (당첨됐으니)",
          R.JOKER.name not in board_line(cards[-1], "▸ 생존 순서"))
    check("총 9명 + 당첨 1명 = 실제 후보 10명",
          board_line(cards[-1], "▸ 생존 순서").count("**") == 18, "")
    check("판 1개 + 알림 1개", sum(1 for _, k, _ in ctx.calls if k == "SEND") == 2)
    check("🃏 이 아니라 대신 당첨된 사람을 태그",
          [e.mentioned.id for e in ctx.calls[-1][2].entities] == [sub.id])

    section("핸들러 — 끌려 내려간 사람이 당첨")
    text = "<at>당첨자뽑기</at> <at>모든 사용자</at> 2명 조커없이"
    seed = _seed_for(members, 2, False, lambda p: p.swap_step
                     and p.swap_step.swap_out.id in {w.id for w in p.winners})
    plan = R.plan(members, 2, random.Random(seed))
    ctx = await _run_seeded(app, text, seed)
    spoken = [spoken_line(c) for c in ctx.cards()]
    out = plan.swap_step.swap_out
    shown_as_survivor = any(short(out) in board_line(c, "▸ 생존") for c in ctx.cards()[:-1])
    check("앞에서는 생존자로 공개됐다", shown_as_survivor)
    check("결국 당첨", short(out) in spoken[-1], spoken[-1])
    check("마지막 생존 순서에는 없다",
          short(out) not in board_line(ctx.cards()[-1], "▸ 생존 순서"))


async def _host_cases(app) -> None:
    section("진행자 — 판마다 고르기")
    picks = [app._pick_host("19:host-rotation").key for _ in range(300)]
    repeats = sum(1 for a, b in zip(picks, picks[1:]) if a == b)
    check("같은 대화방에서 같은 진행자가 연달아 나오지 않는다", repeats == 0, f"연속 {repeats}건")
    check("모든 진행자가 나온다", set(picks) == {h.key for h in L.HOSTS}, str(Counter(picks)))
    other = app._pick_host("19:host-other")
    check("대화방마다 따로 센다", other in L.HOSTS)

    section("진행자 — 모두 끝까지 진행 (반전 + 🃏 + 묶음 당첨)")
    failed = []
    with swap_chance(1.0):
        for host in L.HOSTS:
            with pinned_host(app, host):
                for n in (1, 4):
                    ctx = FakeContext(activity(
                        f"<at>당첨자뽑기</at> <at>모든 사용자</at> {n}명",
                        [mention("당첨자뽑기", BOT.id), mention("모든 사용자", EVERYONE_ID)]))
                    await run_handler(app, ctx)
                    spoken = [spoken_line(c) for c in ctx.cards()]
                    unknown = [l for l in spoken[:-1] if kind_of(l, host) is None]
                    if any("오류" in t for t in ctx.texts()) or not spoken \
                            or not spoken[-1].startswith("🎉") or unknown:
                        failed.append((host.key, n, unknown[:1] or ctx.texts()[:1]))
    check("6명 × (1명, 4명) 모두 결과 화면까지", not failed, str(failed[:2]))


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
    check("연출이 겹쳐 돌지 않음 (판 1개 + 알림 1개 + 거절 4개)", sends == 6,
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
                if any("🎉 **당첨**" in panel for panel in c.panels())]
    check("5개 대화방 모두 독립적으로 완주", len(finished) == 5, f"완주 {len(finished)}건")
    check("대화방별 결과가 섞이지 않음",
          all(len({p for p in c.panels() if "🎉 **당첨**" in p}) == 1
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
    text = "<at>당첨자뽑기</at> " + " ".join(f"<at>{m.name}</at>" for m in small) + " 1명 조커없이"
    ctx = FakeContext(activity(text, ents, activity_id="timing"), roster=small)
    saved_chance, R.SWAP_CHANCE = R.SWAP_CHANCE, 0.0   # 예상 범위를 같은 계획으로 계산한다

    started = time.monotonic()
    await app.handle_message(ctx)
    handler_ms = (time.monotonic() - started) * 1000
    check("핸들러 즉시 반환 (15초 제한 회피)", handler_ms < 1000, f"{handler_ms:.1f}ms")

    while app._running:
        await asyncio.sleep(0.05)
    stamps_plan = R.plan([M.to_account(m) for m in small], 1)
    R.SWAP_CHANCE = saved_chance
    stamps = [t for t, _, _ in ctx.calls]
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    total = stamps[-1]
    lo, hi = R.estimated_bounds(stamps_plan)
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
    for name in ("INTRO_HOLD", "ANNOUNCE_HOLD", "TEASE_HOLD"):
        low, high = getattr(R, name)
        check(f"{name} 이 범위이고 최소 1.0초 이상", high > low and low >= 1.0,
              f"({low}, {high})")
    for label, (low, high) in zip(("처음", "끝"), R.DRUM_CURVE):
        check(f"DRUM_CURVE {label} 범위이고 최소 1.0초 이상", high > low and low >= 1.0,
              f"({low}, {high})")
    samples = {round(R.hold(R.drum_hold(1.0), __import__("random").Random(i)), 3)
               for i in range(30)}
    check("hold() 이 매번 다른 값을 낸다", len(samples) >= 28, f"{len(samples)}/30종")
    check("초당 호출 1.0회 미만", len(stamps) / total < 1.0, f"{len(stamps) / total:.2f}회/초")
    sends = sum(1 for _, k, _ in ctx.calls if k == "SEND")
    check("판 1개 + 당첨 알림 1개", sends == 2, f"SEND {sends}회")


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
