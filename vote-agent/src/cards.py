"""Adaptive Card 빌더.

카드는 **하나의 메시지**로 시작해 끝까지 그 자리에서 갱신된다.
생성 폼 → 투표판 → 마감판이 모두 같은 activity 다. 채팅방에 메시지는 하나만 남는다.

막대는 이미지 없이 유니코드 블록으로 그린다. 어느 클라이언트에서도 같게 보인다.
"""

from __future__ import annotations

import re

import microsoft_teams.cards as C

from parsing import MAX_LABEL, MAX_OPTIONS, MAX_TITLE, format_deadline
from store import Poll

BAR_WIDTH = 10
VOTER_PREVIEW = 15

# 백틱(코드 스팬)으로 감싸지 않는다. Teams 가 코드 스팬에 배경 박스를 그려서
# 0% 일 때 `░░░░░░░░░░` 가 오히려 **채워진 것처럼** 보였다.
# 대신 채움/빈칸이 확실히 구분되는 글자를 쓴다. 둘 다 같은 폭이라 정렬도 유지된다.
BAR_FILLED = "▰"
BAR_EMPTY = "▱"

# ⚠️ SDK 라우터는 `action.verb` 가 아니라 **data["action"]** 으로 핸들러를 고른다.
#    (activity_handlers.py: `action = ctx.value.action.data.get("action")`)
#    verb 만 넣으면 매칭이 안 되고, SDK 가 기본 200 을 돌려주며 Teams 는
#    "응답이 앱으로 전송되었습니다" 만 띄운 뒤 아무 일도 일어나지 않는다.
#    Adaptive Card 규격상 verb 도 함께 넣어 둔다.
ACTION_VOTE = "vote"
ACTION_CLOSE = "close"
ACTION_CREATE = "create"

# 이전 이름 호환
VERB_VOTE = ACTION_VOTE
VERB_CLOSE = ACTION_CLOSE
VERB_CREATE = ACTION_CREATE


def _action(name: str, **payload) -> dict:
    """액션 데이터. `action` 키가 라우팅에 쓰인다."""
    return {"action": name, **payload}

_PAREN = re.compile(r"^(?P<head>[^(]+?)\s*\((?P<inner>.+)\)\s*$")


def short_name(name: str | None) -> str:
    """`샘플2 (Echo Sample)` -> `샘플2`. 투표자 15명을 나열하면 카드가 넘친다."""
    name = (name or "").strip()
    m = _PAREN.match(name)
    return m.group("head").strip() if m else name


def _bar(count: int, participants: int) -> str:
    """막대는 **참여자 수** 기준으로 채운다.

    최다 득표(top) 기준으로 채우면 1위 항목이 1표든 10표든 항상 꽉 찬 상태이고
    0표는 항상 빈 상태여서, 후보 2~3개에 표가 몇 개뿐인 실제 상황에서는
    막대가 고정돼 보인다. 참여자 기준이면 "참여자 중 몇 명이 골랐나" 가 되어
    표가 하나 들어올 때마다 모든 막대가 움직인다.

    복수 선택에서도 한 항목의 득표는 참여자 수를 넘을 수 없으므로 항상 0~100% 다.
    """
    ratio = (count / participants) if participants else 0.0
    filled = round(BAR_WIDTH * ratio)
    return BAR_FILLED * filled + BAR_EMPTY * (BAR_WIDTH - filled)


def _percent(count: int, participants: int) -> int:
    return round(100 * count / participants) if participants else 0


def _voter_line(names: list[str]) -> str:
    if not names:
        return "—"
    shown = [short_name(n) for n in names[:VOTER_PREVIEW]]
    text = ", ".join(shown)
    if len(names) > VOTER_PREVIEW:
        text += f" +{len(names) - VOTER_PREVIEW}명"
    return text


def poll_card(poll: Poll) -> C.AdaptiveCard:
    """투표판. 마감되면 버튼이 사라지고 결과가 강조된다."""
    participants = poll.voter_count
    leaders = {o.idx for o in poll.leaders} if poll.closed else set()

    if poll.closed:
        # 예정 시각이 아니라 **실제로 닫힌 시각**을 보여준다.
        # 만든 사람이 미리 닫으면 예정 시각은 사실과 다르다.
        status = f"마감됨 · {format_deadline(poll.closed_at)}"
    elif poll.closes_at:
        status = f"진행 중 · {format_deadline(poll.closes_at)} 마감"
    else:
        status = "진행 중 · 마감 없음"
    if poll.multi_select:
        status += " · 복수 선택 가능"

    body: list = [
        C.TextBlock(text=f"📊 **{poll.title}**", size="Large", weight="Bolder", wrap=True),
        C.TextBlock(text=status, size="Small", is_subtle=True, wrap=True, spacing="None"),
    ]

    for option in poll.options:
        mark = "🏆 " if option.idx in leaders else ""
        body.append(C.TextBlock(
            text=f"{mark}**{option.idx + 1}. {option.label}**  "
                 f"{_bar(option.count, participants)}  "
                 f"**{option.count}표** · {_percent(option.count, participants)}%",
            wrap=True, spacing="Medium",
            color="Good" if option.idx in leaders else None))
        body.append(C.TextBlock(text=_voter_line(option.voters),
                                size="Small", is_subtle=True, wrap=True, spacing="None"))

    footer = (f"총 **{poll.total_votes}표** · **{poll.voter_count}명** 참여 · "
              f"만든 사람 {short_name(poll.creator_name)}")
    if poll.closed and poll.leaders:
        won = ", ".join(f"**{o.label}**" for o in poll.leaders)
        prefix = "공동 1위" if len(poll.leaders) > 1 else "결과"
        footer = f"🏆 {prefix} — {won}\n\n" + footer
    body.append(C.TextBlock(text=footer, size="Small", is_subtle=True,
                            wrap=True, spacing="Medium"))

    actions: list = []
    if not poll.closed:
        for option in poll.options:
            actions.append(C.ExecuteAction(
                title=option.label, verb=ACTION_VOTE,
                data=_action(ACTION_VOTE, p=poll.id, i=option.idx)))
        # secondary 로 두면 오버플로 메뉴로 밀려서 투표 버튼과 섞이지 않는다.
        actions.append(C.ExecuteAction(
            title="투표 마감하기", verb=ACTION_CLOSE,
            data=_action(ACTION_CLOSE, p=poll.id),
            style="destructive", mode="secondary"))

    return C.AdaptiveCard(body=body, actions=actions)


def form_card(creator_name: str, error: str | None = None,
              values: dict | None = None, note: str | None = None) -> C.AdaptiveCard:
    """생성 폼. 제출되면 이 카드가 그대로 투표판으로 바뀐다."""
    values = values or {}
    body: list = [
        C.TextBlock(text="📊 **투표 만들기**", size="Large", weight="Bolder", wrap=True),
        C.TextBlock(text=f"{short_name(creator_name)} 님이 만드는 중입니다. "
                         "만든 사람만 제출할 수 있습니다.",
                    size="Small", is_subtle=True, wrap=True, spacing="None"),
    ]
    if error:
        body.append(C.TextBlock(text=f"⚠️ {error}", color="Attention",
                                wrap=True, spacing="Medium"))
    if note:
        # 오류가 아니라 안내다. 빨간색을 쓰지 않는다.
        body.append(C.TextBlock(text=f"ℹ️ {note}", color="Accent",
                                wrap=True, spacing="Medium"))

    body += [
        C.TextInput(id="title", label="제목", is_required=True,
                    max_length=MAX_TITLE, placeholder="회식 장소",
                    value=values.get("title"),
                    error_message="제목을 적어주세요"),
        C.TextInput(id="options", label=f"항목 — 한 줄에 하나씩 (최대 {MAX_OPTIONS}개)",
                    is_required=True, is_multiline=True,
                    placeholder="땡땡식당 (https://naver.me/xxxx)\n무슨식당\n초밥집",
                    value=values.get("options"),
                    error_message="항목을 2개 이상 적어주세요"),
        C.TextInput(id="deadline", label="마감 (비우면 마감 없음)",
                    placeholder="금요일 18시 / 8월 22일 18시 / 3시간",
                    value=values.get("deadline")),
        C.ToggleInput(id="multi", title="복수 선택 허용",
                      value="true" if values.get("multi") else "false"),
    ]

    return C.AdaptiveCard(body=body, actions=[
        C.ExecuteAction(title="투표 시작", verb=ACTION_CREATE, style="positive",
                        data=_action(ACTION_CREATE), associated_inputs="auto"),
    ])


def notice_card(text: str) -> C.AdaptiveCard:
    """오류·안내 전용 단독 카드."""
    return C.AdaptiveCard(body=[C.TextBlock(text=text, wrap=True)])
