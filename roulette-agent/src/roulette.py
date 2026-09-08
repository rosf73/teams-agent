"""추첨 로직 — 순수 함수. 네트워크도 Teams 도 모른다.

핵심 규칙 (docs/FEATURES.md):

    탈락 라운드 수 = N - n - 1
    라운드마다 생존자를 공개하며 풀을 줄이고,
    n+1 명이 남는 마지막 라운드에서 생존자 1명 + 당첨자 n명을 동시에 공개한다.

불변식: 공개된 생존자 N-n 명 + 당첨자 n 명 = N 명. 미공개가 남지 않는다.
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


@dataclass
class Step:
    kind: Literal["drop", "final"]
    index: int              # 1-based. 진행 상태 표시에 쓴다.
    remaining_before: int   # 이 단계 시작 시점의 남은 후보 수
    survivors: list[Account]
    winners: list[Account] = field(default_factory=list)
    # 마지막 단계에서 뜸들이기용으로 이름을 부를 대상 (아직 후보에 남은 사람).
    pool: list[Account] = field(default_factory=list)

    @property
    def remaining_after(self) -> int:
        return self.remaining_before - len(self.survivors)


@dataclass
class Plan:
    candidates: list[Account]
    winners: list[Account]
    steps: list[Step]
    all_winners: bool = False

    @property
    def total(self) -> int:
        return len(self.candidates)

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


def plan(candidates: list[Account], n: int, rng: random.Random | None = None) -> Plan:
    """당첨자를 **먼저** 확정하고, 그 결과를 공개할 순서를 짠다.

    연출 중에 결과가 바뀌는 경로는 없다. 연출은 이미 정해진 것의 공개일 뿐이다.
    """
    rng = rng or random.SystemRandom()
    total = len(candidates)

    winners = rng.sample(candidates, n)
    winner_ids = {w.id for w in winners}
    survivors = [c for c in candidates if c.id not in winner_ids]
    rng.shuffle(survivors)

    if not survivors:
        # n == N. 생존자가 없으니 공개할 단계도 없다.
        return Plan(candidates=candidates, winners=winners, steps=[], all_winners=True)

    # 마지막 단계용으로 생존자 1명을 떼어 둔다.
    final_survivor = survivors[-1]
    dropping = survivors[:-1]

    # 마지막 단계까지 포함해 MAX_STEPS 를 넘지 않게 한다.
    sizes = _batch_sizes(len(dropping), MAX_STEPS - 1)

    steps: list[Step] = []
    remaining = total
    for size in sizes:
        chunk, dropping = dropping[:size], dropping[size:]
        steps.append(Step("drop", index=len(steps) + 1,
                          remaining_before=remaining, survivors=chunk))
        remaining -= len(chunk)

    # 뜸들이기는 아직 후보에 남아 있는 사람(마지막 생존자 + 당첨자들) 이름으로만 한다.
    # 이미 생존이 확정된 사람을 부르면 농담이 성립하지 않는다.
    steps.append(Step("final", index=len(steps) + 1, remaining_before=remaining,
                      survivors=[final_survivor], winners=winners,
                      pool=[final_survivor] + winners))

    return Plan(candidates=candidates, winners=winners, steps=steps)


# 각 박자를 보여준 뒤 다음 편집까지의 대기. **(최소, 최대) 범위**다.
#
# 고정 대기를 쓰면 몇 라운드 만에 "이만큼 기다리면 다음 대사가 뜬다" 는 것을
# 무의식중에 학습해 버려서 긴장이 사라진다. 매번 흔들어야 한다.
#
# 특히 발표 직전(DRUM)을 가장 크게 흔든다 — 여기가 긴장의 정점이다.
#
# ⚠️ 최소값은 1.0초 이상을 유지한다. 이 대기가 곧 편집 API 호출 간격이므로,
#    더 짧아지면 Teams 대화방 전송 한도에 가까워진다.
INTRO_HOLD = (1.0, 1.7)
DRUM_HOLD = (1.2, 3.2)
ANNOUNCE_HOLD = (1.0, 1.5)
TEASE_HOLD = (1.0, 2.1)

# 마지막 단계에서 이름을 불러놓고 농담으로 돌리는 횟수.
TEASE_MIN, TEASE_MAX = 3, 5


def tease_count(rng: random.Random) -> int:
    return rng.randint(TEASE_MIN, TEASE_MAX)


def hold(spec: tuple[float, float], rng: random.Random) -> float:
    """(최소, 최대) 범위에서 대기 시간을 뽑는다."""
    low, high = spec
    return rng.uniform(low, high)


def estimated_bounds(p: Plan) -> tuple[float, float]:
    """(최소, 최대) 예상 소요. 대기가 범위라서 단일 값으로 말할 수 없다.

    판을 띄운 뒤 한 박자 + 단계당 3박자 + 마지막 단계의 뜸들이기까지 센다.
    """
    def total(index: int, teases: int) -> float:
        step = INTRO_HOLD[index] + DRUM_HOLD[index] + ANNOUNCE_HOLD[index]
        return ANNOUNCE_HOLD[index] + len(p.steps) * step + teases * TEASE_HOLD[index]

    return total(0, TEASE_MIN), total(1, TEASE_MAX)


def estimated_seconds(p: Plan) -> float:
    """예상 소요의 중간값. 로그에 한 줄로 적을 때 쓴다."""
    low, high = estimated_bounds(p)
    return (low + high) / 2
