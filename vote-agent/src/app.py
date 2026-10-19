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
import time
from collections import OrderedDict

import cards
import compat
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

# SDK 가 모델링하지 못한 activity(예: installationUpdate action=upgrade)를
# 검증 전에 200 으로 끝낸다. compat.py 의 설명 참고.
app = App(http_server_adapter=compat.build_adapter())
store = Store()

# 재전송 방어. 500 이나 15초 초과 시 Bot Framework 가 같은 activity 를 다시 보낸다.
_seen: OrderedDict[str, None] = OrderedDict()
_SEEN_LIMIT = 500

# 투표별 락. 동시 클릭의 read-modify-write 경합을 막는다.
_locks: dict[str, asyncio.Lock] = {}

# 투표 클릭(invoke) activity.id → 그때 돌려준 결과 문구.
# 핸들러가 500 을 내면 Teams 가 **같은 invoke 를 다시 보낸다**(DEV_RULE 8절). 투표는 토글이라
# 다시 처리하면 표가 뒤집힌다. 2026-10-19 투표판 갱신이 TLS 연결 실패로 500 이 나자,
# 재전송이 방금 저장한 표를 취소해 버리고 "취소했습니다" 를 띄웠다.
_vote_results: OrderedDict[str, str] = OrderedDict()

# 투표판 갱신은 클릭 응답 **뒤에** 한다. Teams API 가 느리거나 실패해도 클릭한 사람은
# 바로 결과를 받는다 (2026-10-19 장애: 갱신이 늦어 응답 전에 Teams 가 끊고 "문제가 발생했습니다").
# 그릴 때마다 DB 최신 상태를 읽으므로, 밀린 갱신은 한 번으로 합친다.
REPAINT_ATTEMPTS = 3
REPAINT_RETRY_DELAY = 1.0  # 초. n 번째 실패 뒤 n 배 기다린다.
_paint_locks: dict[str, asyncio.Lock] = {}
_repaint_tasks: dict[str, asyncio.Task] = {}
_repaint_pending: set[str] = set()

# 마감 타이머. 프로세스가 죽으면 사라지므로 지연 검사와 함께 쓴다.
_timers: dict[str, asyncio.Task] = {}

USAGE = (
    "**사용법** — 줄바꿈(Shift+Enter)으로 나눕니다. 구분자를 쓰지 않으니 "
    "제목과 항목에 `/`, 링크, 특수문자를 자유롭게 넣을 수 있습니다.\n\n"
    "```\n"
    "@투표만들기 9/10(목) 회식 메뉴 선정\n"
    "땡땡식당 (https://naver.me/GdymUOVY)\n"
    "무슨식당 (https://naver.me/Fk738sTW)\n"
    "초밥집\n"
    "마감: 금요일 18시\n"
    "복수\n"
    "익명\n"
    "```\n\n"
    "- **첫 줄** = 제목, **이후 각 줄** = 항목 하나\n"
    "- `마감: …`, `복수`, `익명` 은 지시어입니다 (생략 가능)\n"
    "- `익명` 을 쓰면 투표판에 투표한 사람 이름이 나오지 않습니다\n"
    "- 제목만 쓰면 입력 폼이 열립니다 — `@투표만들기` 만 보내도 됩니다\n\n"
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


def _remember_vote(activity_id: str | None, message: str) -> None:
    if not activity_id:
        return
    _vote_results[activity_id] = message
    while len(_vote_results) > _SEEN_LIMIT:
        _vote_results.popitem(last=False)


def _schedule_repaint(ctx, poll_id: str) -> None:
    """투표판 갱신을 응답 뒤로 미룬다. 이미 진행 중이면 한 번 더 그리도록 표시만 한다."""
    _repaint_pending.add(poll_id)
    task = _repaint_tasks.get(poll_id)
    if task is None or task.done():
        _repaint_tasks[poll_id] = asyncio.create_task(_repaint_worker(ctx, poll_id))


async def _repaint_worker(ctx, poll_id: str) -> None:
    failures = 0
    while poll_id in _repaint_pending:
        _repaint_pending.discard(poll_id)
        poll = store.get_poll(poll_id)
        if poll is None:
            return
        try:
            await _repaint(ctx, poll)
            failures = 0
        except Exception:
            # 실패 내용은 _put_card 가 남겼다.
            failures += 1
            if failures >= REPAINT_ATTEMPTS:
                # 표는 이미 저장돼 있다. 다음 투표나 마감 때 최신 상태로 다시 그려진다.
                _repaint_pending.discard(poll_id)
                logger.error("투표판 갱신 포기 poll=%s (%d번 실패)", poll_id, failures)
                return
            _repaint_pending.add(poll_id)
            await asyncio.sleep(REPAINT_RETRY_DELAY * failures)


def _choices_text(poll_id: str, user_id: str) -> str:
    """`현재 선택: 1번째, 3번째`. 항목 이름은 길어서 순번만 쓴다."""
    picked = store.choices_of(poll_id, user_id)
    if not picked:
        return "현재 선택: 없음"
    return "현재 선택: " + ", ".join(f"{i + 1}번째" for i in picked)


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
    lock = _paint_locks.get(poll_id)
    if lock is not None and not lock.locked():
        _paint_locks.pop(poll_id, None)


def _ack(text: str) -> AdaptiveCardActionMessageResponse:
    """클릭한 사람에게만 보이는 짧은 확인. 채팅방에 메시지를 남기지 않는다."""
    return AdaptiveCardActionMessageResponse(
        status_code=200, type="application/vnd.microsoft.activity.message", value=text)


async def _repaint(ctx, poll: Poll) -> None:
    """투표판을 다시 그린다. 채팅방 전원의 화면이 함께 바뀐다.

    한 투표판은 한 번에 하나씩, **그리는 순간의 DB 상태로** 그린다. 그래야 늦게 끝난
    갱신이 더 새 상태(예: 마감판)를 예전 상태로 덮어쓰지 않는다.
    """
    async with _paint_locks.setdefault(poll.id, asyncio.Lock()):
        poll = store.get_poll(poll.id) or poll
        await _put_card(ctx, poll)


async def _put_card(ctx, poll: Poll) -> None:
    if not poll.activity_id:
        logger.warning("activity_id 가 없어 갱신을 건너뜀 poll=%s", poll.id)
        return
    # Teams API 호출이 느리거나 실패하면 클릭 응답이 늦어져 "문제가 발생했습니다" 가 뜬다.
    # 얼마나 자주 그런지 보려고 시간과 실패를 남긴다 (2026-10-19 장애).
    started = time.monotonic()
    try:
        await ctx.api.conversations.activities(poll.conversation_id).update(
            poll.activity_id, MessageActivityInput().add_card(cards.poll_card(poll)))
    except Exception as exc:
        logger.warning("투표판 갱신 실패 poll=%s %.2fs %s: %s",
                       poll.id, time.monotonic() - started, type(exc).__name__, exc)
        raise
    logger.info("투표판 갱신 poll=%s %.2fs", poll.id, time.monotonic() - started)


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

    request = parsing.parse_request(text)

    if request.error:
        await ctx.send(MessageActivityInput(text=f"⚠️ {request.error}\n\n{USAGE}"))
        return

    if request.needs_form:
        # 항목이 없으면 파싱하지 않고 폼을 띄운다. 있는 값은 미리 채워 둔다.
        # 이 카드가 곧 투표판이 된다 (새 메시지를 만들지 않는다).
        await ctx.send(MessageActivityInput().add_card(cards.form_card(
            creator_name, note=request.note,
            values={"title": request.title,
                    "options": "\n".join(request.options),
                    "deadline": request.deadline_text,
                    "multi": request.multi_select,
                    "anonymous": request.anonymous})))
        return

    poll = store.create_poll(
        conversation_id=activity.conversation.id, service_url=activity.service_url,
        creator_id=creator_id, creator_name=creator_name,
        title=request.title, options=request.options,
        multi_select=request.multi_select, closes_at=request.closes_at,
        anonymous=request.anonymous)

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
    anonymous = str(data.get("anonymous") or "").lower() == "true"

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
                        "deadline": deadline_text, "multi": multi,
                        "anonymous": anonymous})))
        return _ack(problem)

    poll = store.create_poll(
        conversation_id=activity.conversation.id, service_url=activity.service_url,
        creator_id=creator_id, creator_name=creator_name,
        title=title, options=options, multi_select=multi, closes_at=closes_at,
        anonymous=anonymous)
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
    started = time.monotonic()

    async with _lock(poll_id):
        poll = store.get_poll(poll_id)
        if poll is None:
            return _ack("이 투표를 찾을 수 없습니다. 새로 만들어 주세요.")

        previous = _vote_results.get(activity.id) if activity.id else None
        if previous is not None:
            # 표는 처음 요청에서 이미 저장됐다. 투표판만 다시 그리고 같은 문구를 돌려준다.
            logger.info("재전송 감지 — 표는 그대로 poll=%s act=%s", poll_id, _who(activity.id))
            _schedule_repaint(ctx, poll_id)
            return _ack(previous)

        poll = await _close_if_expired(ctx, poll)
        if poll.closed:
            await _repaint(ctx, poll)
            return _ack("이미 마감된 투표입니다.")

        message = store.toggle_vote(poll, option_idx, voter_id, voter_name)
        message = f"{message} ({_choices_text(poll_id, voter_id)})"
        # 저장 직후에 기록한다. 응답이 Teams 에 닿지 못해 재전송이 와도 다시 토글하지 않게.
        _remember_vote(activity.id, message)
        _schedule_repaint(ctx, poll_id)

    # 실명 대신 익명 해시를 남긴다.
    logger.info("투표 poll=%s idx=%s user=%s act=%s %.2fs", poll_id, option_idx,
                _who(voter_id), _who(activity.id or ""), time.monotonic() - started)
    return _ack(message)


@app.on_card_action_execute(cards.VERB_MINE)
async def handle_mine(ctx: ActivityContext[AdaptiveCardInvokeActivity]):
    """★ 내 선택 보기. 표와 투표판은 건드리지 않고, 누른 사람에게만 보여준다."""
    activity = ctx.activity
    poll_id = str((activity.value.action.data or {}).get("p") or "")
    author = activity.from_
    user_id = author.id if author else "unknown"

    if store.get_poll(poll_id) is None:
        return _ack("이 투표를 찾을 수 없습니다.")
    logger.info("내 선택 보기 poll=%s user=%s", poll_id, _who(user_id))
    if not store.choices_of(poll_id, user_id):
        return _ack("아직 선택한 항목이 없습니다.")
    return _ack(_choices_text(poll_id, user_id))


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
