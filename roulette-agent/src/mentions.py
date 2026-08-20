"""후보 수집 — 멘션 엔터티, 평문 @이름, 전원 모드를 하나로 정리한다.

이 테넌트에서 실측으로 확인한 두 가지가 설계를 결정한다.

1. Teams 는 표시명이 공백을 포함하면 멘션을 **공백 단위로 쪼갠다.**
   `더미1 (Foxtrot Dummy)` 한 명을 태그하면 엔터티가 3개 온다 —
   name=`더미1`, name=`(Jeong`, name=`Choi)` 이고 **id 는 모두 같다.**
   따라서 id 로 중복을 제거하고, 표시명은 엔터티가 아니라 **로스터에서** 가져와야 한다.

2. 표시명 형식이 `한글이름 (English Name)` 이다.
   본인을 넣기 위해 치는 평문은 `@샘플2` 인데 로스터 값은 `샘플2 (Echo Sample)` 이라
   완전일치로는 절대 안 맞는다. 별칭 매칭이 필요하다.

3. `@모든 사용자` 의 멘션 id 는 실제 사용자와 형태가 같다 (`29:` + 긴 문자열).
   id 패턴으로는 구분할 수 없다. **로스터에 없는 id** 라는 점만이 구조적 신호다.
   봇 자신도 로스터에 없으므로 반드시 먼저 제외해야 한다.

4. `get_members()` 는 `Account` 가 아니라 **`TeamsChannelAccount`** 를 반환하고,
   이 둘은 상속 관계가 아니다. `add_mention(account=...)` 에 그대로 넘기면
   pydantic 검증에서 500 이 난다. **경계에서 `Account` 로 변환한다.**
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from microsoft_teams.api import Account, MentionEntity, MessageActivity, TeamsChannelAccount

# 전원 모드로 볼 표시명. Teams 클라이언트 언어에 따라 달라진다.
EVERYONE_NAMES = {"모든 사용자", "모두", "전원", "everyone", "all"}

# 표시명의 괄호 부분: "더미1 (Foxtrot Dummy)" -> "더미1", "Foxtrot Dummy"
_PAREN = re.compile(r"^(?P<head>[^(]+?)\s*\((?P<inner>.+)\)\s*$")


def _norm(text: str) -> str:
    return (text or "").strip().casefold()


def name_aliases(name: str) -> set[str]:
    """표시명 하나에서 매칭에 쓸 별칭 집합을 만든다.

    '더미1 (Foxtrot Dummy)' -> {'더미1 (jeong choi)', '더미1', 'jeong choi', 'jeongchoi', '더미1(jeongchoi)'}
    """
    name = (name or "").strip()
    if not name:
        return set()

    out = {_norm(name), _norm(name).replace(" ", "")}
    m = _PAREN.match(name)
    if m:
        head, inner = m.group("head"), m.group("inner")
        out |= {_norm(head), _norm(head).replace(" ", "")}
        out |= {_norm(inner), _norm(inner).replace(" ", "")}
    return {a for a in out if a}


def short_name(name: str | None) -> str:
    """표시할 때 쓸 짧은 이름. `더미1 (Foxtrot Dummy)` -> `더미1`.

    이 테넌트 표시명은 `한글이름 (English Name)` 이라 8명을 나열하면 판이 넘친다.
    괄호가 없는 이름(게스트 등)은 그대로 둔다.
    """
    name = (name or "").strip()
    m = _PAREN.match(name)
    return m.group("head").strip() if m else name


def to_account(member: Account | TeamsChannelAccount) -> Account:
    """로스터 멤버를 `Account` 로 정규화한다.

    `get_members()` 는 `TeamsChannelAccount` 를 주는데 `Account` 의 서브클래스가 아니다.
    `MentionEntity.mentioned` 는 `Account` 만 받으므로 그대로 넘기면 pydantic 이 거부한다.
    멘션에 필요한 것은 id 와 name 뿐이다.
    """
    if isinstance(member, Account):
        return member
    return Account(
        id=member.id,
        name=member.name,
        aad_object_id=getattr(member, "aad_object_id", None),
    )


def mention_entities(activity: MessageActivity) -> list[MentionEntity]:
    return [e for e in (activity.entities or []) if isinstance(e, MentionEntity)]


@dataclass
class Resolution:
    """평문 @토큰 해석 결과."""

    token: str
    account: Account | None
    reason: str


@dataclass
class Candidates:
    members: list[Account] = field(default_factory=list)
    everyone: bool = False
    everyone_signal: str = ""
    resolutions: list[Resolution] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def ignored(self) -> list[Resolution]:
        return [r for r in self.resolutions if r.account is None]


def resolve_token(token: str, roster: list) -> Resolution:
    """평문 `@이름` 토큰을 로스터의 한 사람으로 해석한다.

    별칭 완전일치를 먼저 보고, 없으면 별칭 접두사 일치를 본다.
    어느 단계든 **정확히 1명** 일 때만 채택한다. 조용히 추측하지 않는다.
    """
    key = _norm(token.lstrip("@"))
    if not key:
        return Resolution(token, None, "빈 토큰")

    exact = [m for m in roster if key in name_aliases(m.name or "")]
    if len(exact) == 1:
        return Resolution(token, exact[0], "별칭 완전일치")
    if len(exact) > 1:
        return Resolution(token, None, f"완전일치 {len(exact)}명 — 모호함")

    prefix = [m for m in roster if any(a.startswith(key) for a in name_aliases(m.name or ""))]
    if len(prefix) == 1:
        return Resolution(token, prefix[0], "별칭 접두사 일치")
    if len(prefix) > 1:
        names = ", ".join((m.name or "?") for m in prefix[:4])
        return Resolution(token, None, f"접두사 일치 {len(prefix)}명 ({names}) — 모호함")

    return Resolution(token, None, "로스터에 없음")


def collect(activity: MessageActivity, roster: list) -> Candidates:
    """멘션 + 평문 + 전원 모드를 종합해 후보를 확정한다."""
    bot_id = activity.recipient.id if activity.recipient else None
    # 경계에서 한 번만 정규화한다. 이후 모든 코드는 Account 만 다룬다.
    roster = [to_account(m) for m in roster]
    roster_by_id = {m.id: m for m in roster}
    result = Candidates()

    # ── 전원 모드 판정 ─────────────────────────────────────────────
    # 주 신호는 "멘션 id 가 로스터에 없음" 이다. 로케일에 의존하지 않는다.
    # 단 **로스터가 있을 때만** 유효하다 — 비어 있으면 모든 멘션이 이 조건에
    # 걸려서 실제 사용자 태그를 전원 모드로 오인한다.
    # 그리고 봇을 먼저 제외해야 한다. 봇은 로스터에 포함되지 않는다.
    if roster:
        for e in mention_entities(activity):
            acct = e.mentioned
            if acct.id == bot_id:
                continue
            if acct.id not in roster_by_id:
                result.everyone = True
                result.everyone_signal = f"멘션 `{acct.name}` 의 id 가 로스터에 없음"
                break

    if not result.everyone:
        raw = _norm(activity.text or "")
        hit = next((t for t in EVERYONE_NAMES if t in raw), None)
        if hit:
            result.everyone = True
            result.everyone_signal = f"원본 텍스트에서 `{hit}` 발견"

    if result.everyone:
        result.members = [m for m in roster if m.id != bot_id]
        if not result.members:
            # 로스터를 못 가져왔으면 전원으로 확장할 방법이 없다.
            # 조용히 0명으로 두면 "후보를 못 찾았습니다" 만 나와 원인을 알 수 없다.
            result.everyone = False
            result.error = ("채팅방 참여자 목록을 가져올 수 없어 전원 모드를 쓸 수 없습니다. "
                            "뽑을 사람을 직접 태그해 주세요.")
            return result
        result.notes.append(f"채팅방 전원 {len(result.members)}명을 후보로 넣었습니다")
        return result

    # ── 멘션 엔터티 ────────────────────────────────────────────────
    # Teams 가 공백으로 쪼갠 조각들이 같은 id 로 여러 번 오므로 id 로 합친다.
    # 표시명은 엔터티(`더미1`)가 아니라 로스터(`더미1 (Foxtrot Dummy)`)에서 가져온다.
    seen: set[str] = set()
    for e in mention_entities(activity):
        acct = e.mentioned
        if acct.id == bot_id or acct.id in seen:
            continue
        seen.add(acct.id)
        result.members.append(roster_by_id.get(acct.id, acct))

    # ── 평문 @이름 (본인 포함용) ────────────────────────────────────
    leftover = (activity.strip_mentions_text().text or "").strip()
    plain = [t for t in leftover.split() if t.startswith("@")]
    if plain and not roster:
        result.notes.append("참여자 목록을 못 가져와서 평문 `@이름` 은 해석하지 못했습니다")
    for token in (plain if roster else []):
        res = resolve_token(token, roster)
        result.resolutions.append(res)
        if res.account and res.account.id not in seen:
            seen.add(res.account.id)
            result.members.append(res.account)

    return result


def parse_winner_count(text: str, default: int = 1) -> int:
    """텍스트 끝의 `N명` / `N` 을 당첨자 수로 읽는다. 없으면 default."""
    matches = re.findall(r"(\d+)\s*명?", text or "")
    return int(matches[-1]) if matches else default
