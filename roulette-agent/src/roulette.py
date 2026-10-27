"""추첨 로직 — 순수 함수. 네트워크도 Teams 도 모른다.

핵심 규칙 (docs/FEATURES.md):

    탈락 라운드 수 = N - n - 1
    라운드마다 생존자를 공개하며 풀을 줄이고,
    n+1 명이 남는 마지막 라운드에서 당첨자를 한 명씩 공개한다.
    끝까지 불리지 않은 1명이 마지막 생존자다.

불변식: 공개된 생존자 N-n 명 + 당첨자 n 명 = N 명. 미공개가 남지 않는다.

두 가지 장치가 있다. 둘 다 결과를 먼저 정한 뒤 그 결과에 맞춰 공개 순서만 짠다.
그래서 각자의 당첨 확률은 장치가 없을 때와 같다.

- 반전: 탈락 단계 중간에 한 번, 공개된 생존자 1명을 다시 후보로 끌어내리고
  아직 안 불린 비당첨자 1명이 그 자리를 차지한다.
- 🃏 1등이대신당첨: 가상 후보. 이것이 당첨되면 생존 순서 1등이 대신 당첨된다.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Literal

from microsoft_teams.api import Account

MIN_CANDIDATES = 2
MAX_CANDIDATES = 30

# 전체 단계 수 상한 (마지막 단계 포함). 넘으면 앞쪽 단계에서 생존자를 묶어 공개한다.
# 후보 30명을 한 명씩 가면 단계당 4초 × 29 = 2분이 넘어 지루해진다.
MAX_STEPS = 12

# 당첨자 공개 횟수 상한. 30명 중 29명을 뽑을 때 29번 두구두구를 하면 지루하다.
# 넘으면 앞쪽 공개에서 당첨자를 묶는다.
MAX_WINNER_REVEALS = 3

# 가상 후보. 실제 사람과 id 가 겹치지 않게 Teams id 형식(29:/28:)을 쓰지 않는다.
JOKER = Account(id="roulette:joker", name="1등이대신당첨")

# 반전이 일어나는 판의 비율. 2026-10-27 사용자 결정으로 항상 (처음엔 0.5).
# 대신 시점(SWAP_WINDOW)과 누가 내려가는지가 매번 달라서 그것으로 흔든다.
SWAP_CHANCE = 1.0
# 반전 시점. 탈락 단계 중 이 비율 구간에서 흔든다. 고정하면 시점을 학습한다.
SWAP_WINDOW = (0.4, 0.6)

# 끝에서 이 수만큼의 탈락 단계만 도입 박자와 미니 뜸을 넣는다. 앞쪽은 빠르게 넘긴다.
LATE_STEPS = 2


@dataclass
class Step:
    kind: Literal["drop", "final"]
    index: int              # 1-based. 진행 상태 표시에 쓴다.
    remaining_before: int   # 이 단계 시작 시점의 남은 후보 수
    survivors: list[Account]
    winners: list[Account] = field(default_factory=list)
    # 마지막 단계에서 뜸들이기용으로 이름을 부를 대상 (아직 후보에 남은 사람).
    pool: list[Account] = field(default_factory=list)
    # 마지막 단계에서 당첨자를 공개하는 묶음. 한 묶음이 두구두구 한 번이다.
    winner_batches: list[list[Account]] = field(default_factory=list)
    # 0.0(첫 단계) ~ 1.0(당첨자 공개). 두구두구 대기 곡선의 위치다.
    position: float = 0.0
    # 끝쪽 탈락 단계. 도입 박자를 넣고 미니 뜸을 한 번 한다.
    late: bool = False
    # 미니 뜸에서 부를 사람. 아직 미공개이고 이번 단계에 발표될 사람도 아니다.
    tease: Account | None = None
    # 이 단계 발표 뒤의 반전. swap_out 이 다시 후보로 내려가고 swap_in 이 그 자리를 차지한다.
    swap_out: Account | None = None
    swap_in: Account | None = None

    @property
    def remaining_after(self) -> int:
        return self.remaining_before - len(self.survivors)


@dataclass
class Plan:
    candidates: list[Account]   # 실제 사람만. 🃏 은 들어가지 않는다
    winners: list[Account]      # 추첨에서 뽑힌 자리. 🃏 이 들어 있을 수 있다
    steps: list[Step]
    all_winners: bool = False
    joker: bool = False         # 🃏 이 후보에 들어갔는가
    substitute: Account | None = None   # 🃏 이 뽑혔을 때 대신 당첨되는 생존 1등

    @property
    def total(self) -> int:
        return len(self.candidates)

    @property
    def final_winners(self) -> list[Account]:
        """실제 당첨자. 🃏 자리는 생존 1등으로 바뀐다."""
        return [self.substitute if w.id == JOKER.id else w for w in self.winners]

    @property
    def swap_step(self) -> Step | None:
        return next((st for st in self.steps if st.swap_out), None)

    @property
    def total_steps(self) -> int:
        return len(self.steps)


@dataclass
class Rejection:
    message: str


def validate(candidate_count: int, n: int) -> Rejection | None:
    """추첨을 시작할 수 있는지 본다. 문제가 없으면 None."""
    if candidate_count == 0:
        return Rejection("후보를 못 찾았습니다.")
    if candidate_count < MIN_CANDIDATES:
        return Rejection(f"후보가 {candidate_count}명입니다. 2명 이상 태그해 주세요.")
    if candidate_count > MAX_CANDIDATES:
        return Rejection(f"후보가 {candidate_count}명입니다. 한 번에 {MAX_CANDIDATES}명까지만 됩니다.")
    if n < 1:
        return Rejection("뽑을 인원은 1명 이상이어야 합니다.")
    if n > candidate_count:
        return Rejection(f"후보가 {candidate_count}명인데 {n}명을 뽑을 수는 없습니다.")
    return None


def _batch_sizes(count: int, max_steps: int) -> list[int]:
    """count 개를 max_steps 이내의 단계로 최대한 고르게 쪼갠다."""
    if count <= max_steps:
        return [1] * count
    base, extra = divmod(count, max_steps)
    # 큰 묶음을 앞에 두면 뒤로 갈수록 한 번에 나오는 인원이 줄어 긴장이 올라간다.
    return [base + 1] * extra + [base] * (max_steps - extra)


def _plan_swap(order: list[Account], winners: list[Account], sizes: list[int],
               rng: random.Random) -> tuple[int, Account, Account] | None:
    """반전을 짠다. order(생존자 공개 순서)를 고쳐 쓰고 (단계 번호, 내려갈 사람, 차지할 사람).

    끌려 내려간 사람은 다시 후보가 되므로, 그 사람이 결국 당첨될 확률은 그 시점에 남은
    다른 후보와 같아야 한다 (n / 남은 인원). 늘 당첨되거나 늘 다시 살아남으면 금방 읽힌다.
    당첨된다면 그 사람은 앞에서 생존자인 척 공개된 것이다.
    """
    if not sizes or rng.random() >= SWAP_CHANCE:
        return None
    k = min(len(sizes), max(1, round(len(sizes) * rng.uniform(*SWAP_WINDOW))))
    r = sum(sizes[:k])
    shown, later = order[:r], order[r:]
    remaining = len(later) + len(winners)   # 반전 직후 남은 후보 수. 바꿔치기라 전후가 같다

    i = rng.randrange(r)
    if rng.random() < len(winners) / remaining:
        out = rng.choice(winners)
        # 원래 그 자리에 공개될 사람은 뒤로 미룬다.
        later.insert(rng.randrange(len(later) + 1), shown[i])
        shown[i] = out
        into = later.pop(rng.randrange(len(later)))
    else:
        out = shown[i]
        j = rng.randrange(len(later))
        # 차지한 사람이 원래 공개될 자리에서 끌려 내려간 사람이 다시 살아남는다.
        into, later[j] = later[j], out
    order[:] = shown + later
    return k, out, into


def plan(candidates: list[Account], n: int, rng: random.Random | None = None,
         joker: bool = False) -> Plan:
    """당첨자를 **먼저** 확정하고, 그 결과를 공개할 순서를 짠다.

    연출 중에 결과가 바뀌는 경로는 없다. 연출은 이미 정해진 것의 공개일 뿐이다.
    """
    rng = rng or random.SystemRandom()
    # 전원 당첨이면 🃏 을 넣지 않는다. 대신 받을 생존자가 없다.
    joker = joker and n < len(candidates)
    pool = list(candidates) + ([JOKER] if joker else [])
    by_id = {a.id: a for a in pool}

    winners = rng.sample(pool, n)
    winner_ids = {w.id for w in winners}
    order = [c for c in pool if c.id not in winner_ids]
    rng.shuffle(order)

    if not order:
        # n == N. 생존자가 없으니 공개할 단계도 없다.
        return Plan(candidates=candidates, winners=winners, steps=[], all_winners=True)

    # 마지막 단계용으로 생존자 1명을 남기고, MAX_STEPS 를 넘지 않게 묶는다.
    sizes = _batch_sizes(len(order) - 1, MAX_STEPS - 1)
    swap = _plan_swap(order, winners, sizes, rng)
    dropping, final_survivor = order[:-1], order[-1]

    steps: list[Step] = []
    remaining = len(pool)
    shown: list[str] = []      # 지금 판에 생존자로 떠 있는 사람 (공개 순서대로)
    for i, size in enumerate(sizes):
        chunk, dropping = dropping[:size], dropping[size:]
        step = Step("drop", index=len(steps) + 1, remaining_before=remaining,
                    survivors=chunk, position=i / len(sizes),
                    late=i >= len(sizes) - LATE_STEPS)
        if step.late:
            # 미니 뜸은 아직 남은 사람만 부른다. 이번 단계에 발표될 사람을 부르면
            # "X! …아닙니다" 바로 뒤에 "X 생존" 이 떠서 농담이 깨진다.
            skip = set(shown) | {a.id for a in chunk}
            step.tease = rng.choice([a for a in pool if a.id not in skip])
        shown += [a.id for a in chunk]
        if swap and step.index == swap[0]:
            step.swap_out, step.swap_in = swap[1], swap[2]
            # 차지한 사람은 끌려 내려간 사람의 자리(순서)에 들어간다.
            shown[shown.index(step.swap_out.id)] = step.swap_in.id
        steps.append(step)
        remaining -= len(chunk)
    shown.append(final_survivor.id)

    # 뜸들이기는 아직 후보에 남아 있는 사람(마지막 생존자 + 당첨자들) 이름으로만 한다.
    # 이미 생존이 확정된 사람을 부르면 농담이 성립하지 않는다.
    batches, rest = [], list(winners)
    for size in _batch_sizes(n, MAX_WINNER_REVEALS):
        batches.append(rest[:size])
        rest = rest[size:]
    steps.append(Step("final", index=len(steps) + 1, remaining_before=remaining,
                      survivors=[final_survivor], winners=winners,
                      pool=[final_survivor] + winners, winner_batches=batches,
                      position=1.0))

    # 🃏 이 뽑혔으면 끝났을 때의 생존 순서 1등이 대신 당첨된다. 🃏 이 당첨자라
    # 생존자는 모두 실제 사람이다.
    substitute = by_id[shown[0]] if JOKER.id in winner_ids else None
    return Plan(candidates=candidates, winners=winners, steps=steps,
                joker=joker, substitute=substitute)


# 각 박자를 보여준 뒤 다음 편집까지의 대기. **(최소, 최대) 범위**다.
#
# 고정 대기를 쓰면 몇 라운드 만에 "이만큼 기다리면 다음 대사가 뜬다" 는 것을
# 무의식중에 학습해 버려서 긴장이 사라진다. 매번 흔들어야 한다.
#
# 두구두구는 곡선을 따른다. 앞쪽 단계는 짧게 넘기고, 당첨자 공개에서 가장 길다.
# 곡선 위에서도 범위로 흔든다 — 곡선만 두면 그 곡선을 학습한다.
#
# ⚠️ 최소값은 1.0초 이상을 유지한다. 이 대기가 곧 편집 API 호출 간격이므로,
#    더 짧아지면 Teams 대화방 전송 한도에 가까워진다.
#    빠르게 하려면 대기를 줄이지 말고 박자를 뺀다 (앞쪽 단계의 도입 박자).
#
# 2026-10-27 기다리는 대사(도입·두구두구·뜸들이기)만 (+0.5, +1.0) 늘렸다. 너무 빨리 지나간다는
# 피드백. 발표(생존·당첨·자리 바꿈)는 원래 길이로 둔다 — 결과는 바로 보여도 된다.
INTRO_HOLD = (1.5, 2.7)
DRUM_CURVE = ((1.5, 2.8), (2.7, 4.4))   # (첫 단계 범위, 당첨자 공개 범위)
ANNOUNCE_HOLD = (1.0, 1.5)
TEASE_HOLD = (1.5, 3.1)

# 마지막 단계에서 이름을 불러놓고 농담으로 돌리는 횟수.
TEASE_MIN, TEASE_MAX = 3, 5


def tease_count(rng: random.Random) -> int:
    return rng.randint(TEASE_MIN, TEASE_MAX)


def hold(spec: tuple[float, float], rng: random.Random) -> float:
    """(최소, 최대) 범위에서 대기 시간을 뽑는다."""
    low, high = spec
    return rng.uniform(low, high)


def drum_hold(position: float) -> tuple[float, float]:
    """곡선 위 position(0~1) 에서의 두구두구 대기 범위."""
    (a_low, a_high), (b_low, b_high) = DRUM_CURVE
    return (a_low + (b_low - a_low) * position, a_high + (b_high - a_high) * position)


def estimated_bounds(p: Plan) -> tuple[float, float]:
    """(최소, 최대) 예상 소요. 대기가 범위라서 단일 값으로 말할 수 없다.

    app._run_draw 의 박자를 그대로 센다. 결과 화면을 한 박자 보여준 뒤 당첨자 알림을 보낸다.
    """
    def total(index: int, teases: int) -> float:
        seconds = INTRO_HOLD[index]                  # 판을 띄운 뒤 시작 인사
        for st in p.steps:
            drum = drum_hold(st.position)[index]
            if st.kind == "drop":
                seconds += drum + ANNOUNCE_HOLD[index]
                if st.late:
                    seconds += INTRO_HOLD[index] + TEASE_HOLD[index]
                if st.swap_out:                      # 반전 예고 · 내려감 · 차지
                    seconds += INTRO_HOLD[index] + 2 * ANNOUNCE_HOLD[index]
            else:
                seconds += teases * TEASE_HOLD[index] + INTRO_HOLD[index]
                seconds += len(st.winner_batches) * (drum + ANNOUNCE_HOLD[index])
        if p.substitute:                             # 🃏 → 생존 1등은… → 대신 당첨
            seconds += drum_hold(1.0)[index] + ANNOUNCE_HOLD[index]
        return seconds + ANNOUNCE_HOLD[index]        # 결과 화면 → 당첨자 알림

    return total(0, TEASE_MIN), total(1, TEASE_MAX)


def estimated_seconds(p: Plan) -> float:
    """예상 소요의 중간값. 로그에 한 줄로 적을 때 쓴다."""
    low, high = estimated_bounds(p)
    return (low + high) / 2
