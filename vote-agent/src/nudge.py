"""미투표자 알림 — 누가 안 했는지 계산하고, 보낼 메시지를 만든다.

투표판은 사람을 @태그하지 않는다(app.py 원칙 2). 알림은 예외다. 재촉하려면 알림이
가야 하므로 **새 메시지**에 멘션을 넣고, 투표 카드를 인용해 어느 투표인지 보여 준다.

익명 투표는 누가 안 했는지 드러내지 않는다. 사람 수만 쓴다.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from microsoft_teams.api import Account, MessageActivityInput

# 마감 이 시간 전부터 자동 알림을 보낸다.
REMIND_BEFORE = timedelta(hours=1)

# Bot Framework 에서 봇 id 는 28: 로, 사람 id 는 29: 로 시작한다.
BOT_ID_PREFIX = "28:"

AUTO_HEADLINE = "투표가 종료되기까지 1시간도 남지 않았어요!"
AUTO_TAIL = " 마감 전에 투표해주세요"
NUDGE_TAIL = " 마감되기 전에 투표를 완료해주세요!"


def is_due(closes_at: datetime | None, at: datetime) -> bool:
    """자동 알림을 보낼 때인가. 마감이 지났으면 보내지 않는다."""
    return bool(closes_at) and closes_at - REMIND_BEFORE <= at < closes_at  # type: ignore[operator]


def non_voters(members, voter_ids: set[str]) -> list[Account]:
    """채팅방 명단에서 봇과 투표한 사람을 뺀다. 명단 순서를 유지한다."""
    out: list[Account] = []
    for member in members:
        member_id = getattr(member, "id", None) or ""
        if not member_id or member_id.startswith(BOT_ID_PREFIX) or member_id in voter_ids:
            continue
        out.append(Account(id=member_id, name=getattr(member, "name", None) or "이름 없음"))
    return out


def _mentions(msg: MessageActivityInput, people: list[Account]) -> MessageActivityInput:
    for i, account in enumerate(people):
        if i:
            msg.add_text(" ")
        msg.add_mention(account)
    return msg


def _quoted(msg: MessageActivityInput, activity_id: str | None) -> MessageActivityInput:
    # 인용은 맨 마지막에 붙인다. prepend_quote 가 지금까지의 본문 앞에 자리표시자를 넣는다.
    return msg.prepend_quote(activity_id) if activity_id else msg


def auto_message(activity_id: str | None, people: list[Account]) -> MessageActivityInput:
    """마감 1시간 전 자동 알림."""
    msg = _mentions(MessageActivityInput(text=f"{AUTO_HEADLINE}\n\n"), people)
    msg.add_text(AUTO_TAIL)
    return _quoted(msg, activity_id)


def nudge_message(activity_id: str | None, people: list[Account],
                  anonymous: bool) -> MessageActivityInput:
    """만든 사람이 누르는 '투표 종용하기'."""
    if anonymous:
        msg = MessageActivityInput(text=f"미투표자는 {len(people)}명입니다.{NUDGE_TAIL}")
    else:
        msg = _mentions(MessageActivityInput(text=""), people)
        msg.add_text(NUDGE_TAIL)
    return _quoted(msg, activity_id)
