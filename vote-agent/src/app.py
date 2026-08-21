"""투표만들기 — 채팅에서 실시간 집계되는 투표를 만든다.

    @투표만들기                                          → 생성 폼
    @투표만들기 회식장소 / 삼겹살, 치킨 / 금요일 18시 / 복수    → 바로 생성

원칙은 roulette-agent 와 같다.

  1. 투표 하나당 메시지 **한 개**. 생성 폼 → 투표판 → 마감판이 모두 같은 activity 다.
  2. 사람을 **@태그하지 않는다.** 투표자 이름은 평문으로만 쓴다.
  3. 누가 투표하면 그 메시지를 갱신해 **채팅방 전원의 화면이 함께 바뀐다.**

LLM 을 쓰지 않는다. 마감 표현은 정규식으로 해석하고, 애매하면 오류로 돌린다.
"""

import asyncio
import hashlib
import logging
from collections import OrderedDict

import cards
import parsing
from config import Config
from microsoft_teams.api import (
    AdaptiveCardActionMessageResponse,
    AdaptiveCardInvokeActivity,
    MessageActivity,
    MessageActivityInput,
)
from microsoft_teams.apps import ActivityContext, App
from store import Poll, Store, now

config = Config()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
logger = logging.getLogger(config.AGENT_NAME)

app = App()
store = Store()

# 재전송 방어. 500 이나 15초 초과 시 Bot Framework 가 같은 activity 를 다시 보낸다.
_seen: OrderedDict[str, None] = OrderedDict()
_SEEN_LIMIT = 500

# 투표별 락. 동시 클릭의 read-modify-write 경합을 막는다.
_locks: dict[str, asyncio.Lock] = {}

# 마감 타이머. 프로세스가 죽으면 사라지므로 지연 검사와 함께 쓴다.
_timers: dict[str, asyncio.Task] = {}

USAGE = (
    "**사용법**\n"
    "- `@투표만들기` — 생성 폼을 띄웁니다\n"
    "- `@투표만들기 회식장소 / 삼겹살, 치킨, 초밥` — 바로 만듭니다\n"
    "- `@투표만들기 회식장소 / 삼겹살, 치킨 / 금요일 18시 / 복수` — 마감과 복수선택까지\n\n"
    "마감 표현: `금요일 18시`, `내일 오후 3시`, `8월 22일 18시`, `3시간`"
)


def _already_handled(activity_id: str | None) -> bool:
    if not activity_id:
        return False
    if activity_id in _seen:
        return True
    _seen[activity_id] = None
    while len(_seen) > _SEEN_LIMIT:
        _seen.popitem(last=False)
    return False


def _lock(poll_id: str) -> asyncio.Lock:
    return _locks.setdefault(poll_id, asyncio.Lock())


def _who(user_id: str) -> str:
    """로그용 익명 식별자.

    실명을 로그에 남기면 클라우드의 로그 저장소(App Service 로그 스트림,
    Log Analytics 등)로 개인정보가 퍼져 나가고 보존 기간도 우리 통제 밖이다.
    같은 사람인지 구분할 수 있으면 디버깅에는 충분하다.
    """
    return hashlib.sha256(user_id.encode()).hexdigest()[:8]


def _forget(poll_id: str) -> None:
    """끝난 투표의 프로세스 내 상태를 버린다.

    `_locks` 는 투표마다 하나씩 쌓이므로 오래 돌리면 계속 늘어난다.
    """
    _locks.pop(poll_id, None)
    _timers.pop(poll_id, None)


def _ack(text: str) -> AdaptiveCardActionMessageResponse:
    """클릭한 사람에게만 보이는 짧은 확인. 채팅방에 메시지를 남기지 않는다."""
    return AdaptiveCardActionMessageResponse(
        status_code=200, type="application/vnd.microsoft.activity.message", value=text)


async def _repaint(ctx, poll: Poll) -> None:
    """투표판을 다시 그린다. 채팅방 전원의 화면이 함께 바뀐다."""
    if not poll.activity_id:
        logger.warning("activity_id 가 없어 갱신을 건너뜀 poll=%s", poll.id)
        return
    await ctx.api.conversations.activities(poll.conversation_id).update(
        poll.activity_id, MessageActivityInput().add_card(cards.poll_card(poll)))


def _schedule_close(ctx, poll: Poll) -> None:
    """마감 시각에 자동으로 닫는다.

    타이머만 믿지 않는다. 프로세스가 죽으면 사라지므로 투표가 들어올 때마다
    지연 검사도 함께 한다 (`_close_if_expired`).
    """
    if poll.closed or not poll.closes_at:
        return
    delay = (poll.closes_at - now()).total_seconds()
    existing = _timers.pop(poll.id, None)
    if existing:
        existing.cancel()

    async def runner() -> None:
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            async with _lock(poll.id):
                if store.close_poll(poll.id):
                    fresh = store.get_poll(poll.id)
                    if fresh:
                        await _repaint(ctx, fresh)
                        logger.info("마감 (타이머) poll=%s", poll.id)
            _forget(poll.id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("마감 타이머 실패 poll=%s", poll.id)
        finally:
            _timers.pop(poll.id, None)

    _timers[poll.id] = asyncio.create_task(runner())


async def _close_if_expired(ctx, poll: Poll) -> Poll:
    """마감 시각이 지났으면 먼저 닫는다. 타이머가 유실된 경우의 안전망."""
    if not poll.closed and poll.is_expired():
        if store.close_poll(poll.id):
            logger.info("마감 (지연 검사) poll=%s", poll.id)
        return store.get_poll(poll.id) or poll
    return poll


@app.on_message
async def handle_message(ctx: ActivityContext[MessageActivity]) -> None:
    activity = ctx.activity
    if _already_handled(activity.id):
        logger.info("중복 activity 무시: %s", activity.id)
        return

    text = (activity.strip_mentions_text().text or "").strip()
    author = activity.from_
    creator_id = author.id if author else "unknown"
    creator_name = (author.name if author else None) or "알 수 없음"

    fields, error = parsing.parse_one_liner(text)
    if error:
        await ctx.send(MessageActivityInput(text=f"⚠️ {error}\n\n{USAGE}"))
        return

    if fields is None:
        # 한 줄 생성 시도가 아니면 폼을 띄운다. 이 카드가 곧 투표판이 된다.
        await ctx.send(MessageActivityInput().add_card(cards.form_card(creator_name)))
        return

    poll = store.create_poll(
        conversation_id=activity.conversation.id, service_url=activity.service_url,
        creator_id=creator_id, creator_name=creator_name,
        title=fields["title"], options=fields["options"],
        multi_select=fields["multi_select"], closes_at=fields["closes_at"])

    sent = await ctx.send(MessageActivityInput().add_card(cards.poll_card(poll)))
    store.set_activity_id(poll.id, sent.id)
    poll = store.get_poll(poll.id) or poll
    _schedule_close(ctx, poll)
    logger.info("투표 생성 poll=%s 항목=%d 마감=%s",
                poll.id, len(poll.options), parsing.format_deadline(poll.closes_at))


@app.on_card_action_execute(cards.VERB_CREATE)
async def handle_create(ctx: ActivityContext[AdaptiveCardInvokeActivity]):
    activity = ctx.activity
    data = activity.value.action.data or {}
    author = activity.from_
    creator_id = author.id if author else "unknown"
    creator_name = (author.name if author else None) or "알 수 없음"

    title = str(data.get("title") or "").strip()
    options = parsing.parse_options_block(str(data.get("options") or ""))
    deadline_text = str(data.get("deadline") or "").strip()
    multi = str(data.get("multi") or "").lower() == "true"

    if not title:
        problem = "제목을 적어주세요."
    elif len(title) > parsing.MAX_TITLE:
        problem = f"제목이 너무 깁니다 (최대 {parsing.MAX_TITLE}자)."
    else:
        problem = parsing.validate_options(options)

    closes_at = None
    if not problem:
        closes_at, problem = parsing.parse_deadline(deadline_text)

    if problem:
        # 폼을 유지하고 오류만 얹는다. 입력한 값은 보존한다.
        await ctx.api.conversations.activities(activity.conversation.id).update(
            activity.reply_to_id,
            MessageActivityInput().add_card(cards.form_card(
                creator_name, error=problem,
                values={"title": title, "options": data.get("options"),
                        "deadline": deadline_text, "multi": multi})))
        return _ack(problem)

    poll = store.create_poll(
        conversation_id=activity.conversation.id, service_url=activity.service_url,
        creator_id=creator_id, creator_name=creator_name,
        title=title, options=options, multi_select=multi, closes_at=closes_at)
    # 폼 카드가 붙어 있던 그 메시지를 투표판으로 바꾼다. 새 메시지를 만들지 않는다.
    store.set_activity_id(poll.id, activity.reply_to_id)
    poll = store.get_poll(poll.id) or poll

    await _repaint(ctx, poll)
    _schedule_close(ctx, poll)
    logger.info("투표 생성(폼) poll=%s 항목=%d 마감=%s",
                poll.id, len(poll.options), parsing.format_deadline(poll.closes_at))
    return _ack("투표를 시작했습니다.")


@app.on_card_action_execute(cards.VERB_VOTE)
async def handle_vote(ctx: ActivityContext[AdaptiveCardInvokeActivity]):
    activity = ctx.activity
    data = activity.value.action.data or {}
    poll_id = str(data.get("p") or "")
    try:
        option_idx = int(data.get("i"))
    except (TypeError, ValueError):
        return _ack("잘못된 선택입니다.")

    author = activity.from_
    voter_id = author.id if author else "unknown"
    voter_name = (author.name if author else None) or "알 수 없음"

    async with _lock(poll_id):
        poll = store.get_poll(poll_id)
        if poll is None:
            return _ack("이 투표를 찾을 수 없습니다. 새로 만들어 주세요.")

        poll = await _close_if_expired(ctx, poll)
        if poll.closed:
            await _repaint(ctx, poll)
            return _ack("이미 마감된 투표입니다.")

        message = store.toggle_vote(poll, option_idx, voter_id, voter_name)
        fresh = store.get_poll(poll_id) or poll
        await _repaint(ctx, fresh)

    # 실명 대신 익명 해시를 남긴다.
    logger.info("투표 poll=%s idx=%s user=%s", poll_id, option_idx, _who(voter_id))
    return _ack(message)


@app.on_card_action_execute(cards.VERB_CLOSE)
async def handle_close(ctx: ActivityContext[AdaptiveCardInvokeActivity]):
    activity = ctx.activity
    poll_id = str((activity.value.action.data or {}).get("p") or "")
    author = activity.from_
    actor_id = author.id if author else "unknown"

    async with _lock(poll_id):
        poll = store.get_poll(poll_id)
        if poll is None:
            return _ack("이 투표를 찾을 수 없습니다.")
        if poll.closed:
            return _ack("이미 마감되었습니다.")
        if actor_id != poll.creator_id:
            return _ack(f"만든 사람({cards.short_name(poll.creator_name)})만 마감할 수 있습니다.")

        store.close_poll(poll_id)
        fresh = store.get_poll(poll_id) or poll
        await _repaint(ctx, fresh)

    timer = _timers.pop(poll_id, None)
    if timer:
        timer.cancel()
    _forget(poll_id)
    logger.info("마감 (수동) poll=%s", poll_id)
    return _ack("투표를 마감했습니다.")


if __name__ == "__main__":
    logger.info("%s 기동 — 포트 %s · DB %s", config.AGENT_NAME, config.PORT, store.path)

    # 보존 기간이 지난 투표를 정리한다. 실명과 사용자 id 가 들어 있으므로
    # 필요 이상으로 들고 있지 않는다.
    purged_closed, purged_stale = store.purge_old()
    if purged_closed or purged_stale:
        logger.info("보존정책 정리 — 마감 %d건, 방치 %d건 삭제", purged_closed, purged_stale)
    logger.info("DB 현황 %s", store.counts())

    asyncio.run(app.start())
