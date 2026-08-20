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


# 각 박자를 보여준 뒤 다음 편집까지의 대기.
# 편집 호출 간격이 이 값이므로 1.0초 미만으로 내리면 Teams 전송 한도에 가까워진다.
INTRO_HOLD = 1.2
DRUM_HOLD = 1.8
ANNOUNCE_HOLD = 1.0
TEASE_HOLD = 1.3

# 마지막 단계에서 이름을 불러놓고 농담으로 돌리는 횟수.
TEASE_MIN, TEASE_MAX = 3, 5


def tease_count(rng: random.Random) -> int:
    return rng.randint(TEASE_MIN, TEASE_MAX)


def estimated_seconds(p: Plan, teases: int = (TEASE_MIN + TEASE_MAX) // 2) -> float:
    """판을 띄운 뒤 한 박자 + 단계당 3박자 + 마지막 단계의 뜸들이기까지."""
    step = INTRO_HOLD + DRUM_HOLD + ANNOUNCE_HOLD
    return ANNOUNCE_HOLD + len(p.steps) * step + teases * TEASE_HOLD
