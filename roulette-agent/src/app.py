"""당첨자뽑기 — 채팅에서 후보 중 당첨자를 긴장감 있게 뽑는다.

    @당첨자뽑기 @A @B @C @D @E 2명

생존자를 한 명씩 공개하며 후보 풀을 줄이고, 마지막 단계에서 당첨자를 한 명씩
공개한다. 자세한 규칙은 docs/FEATURES.md.

연출 원칙 세 가지:

  1. 메시지는 **딱 한 개**. 전체 연출이 그 메시지를 계속 편집하는 방식이다.
     1분에 메시지 20개가 올라오면 채팅방이 업무에 방해가 된다.
  2. 사람을 **@태그하지 않는다.** 이름은 볼드 평문으로만 쓴다. 알림을 줄이는 게 목적이다.
     예외는 하나 — 끝나면 **당첨자만** 태그한 짧은 메시지를 하나 보내 당첨자에게 알림이 가게 한다.
  3. 같은 자리에 **고정된 판**을 두고 값만 바꾼다. 메시지를 다 읽어야 현황을 아는 방식은
     눈에 들어오지 않는다.

판은 **Adaptive Card** 다. 평문 메시지로는 대사 줄을 크게 만들 수 없다 —
공식 문서의 지원 표에서 `Header (levels 1–3)` 은 text-only 메시지에서 ❌ 이고
rich card 에서만 ✔️ 다. 카드의 `TextBlock(size="Large")` 로 대사만 키운다.

LLM 을 쓰지 않는다. 난수는 random.SystemRandom, 문구는 고정 풀에서 고른다.
"""

import asyncio
import logging
import random
from collections import OrderedDict

import compat
import lines
import mentions as M
import microsoft_teams.cards as C
import roulette as R
from config import Config
from microsoft_teams.api import Account, MessageActivity, MessageActivityInput
from microsoft_teams.apps import ActivityContext, App

config = Config()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
logger = logging.getLogger(config.AGENT_NAME)

# SDK 가 모델링하지 못한 activity(예: installationUpdate action=upgrade)를
# 검증 전에 200 으로 끝낸다. compat.py 의 설명 참고.
app = App(http_server_adapter=compat.build_adapter())
rng = random.SystemRandom()

# 문구는 덱에서 꺼낸다. random.choice 는 같은 것을 연속으로 뽑는다.
# 덱은 (진행자, 박자) 마다 하나다. 판이 바뀌어도 같은 진행자면 덱이 이어진다.
_decks: dict[tuple[str, str], lines.Deck] = {}
ALL_WINNERS_DECK = lines.Deck(lines.ALL_WINNERS, rng)

# 대화방별 직전 진행자. 같은 진행자가 연달아 나오지 않게 한다.
_last_host: OrderedDict[str, str] = OrderedDict()

# ── 중복 실행 방어 ────────────────────────────────────────────────
# 핸들러가 500 을 내거나 15초 안에 응답하지 않으면 Bot Framework 가 같은 activity 를
# 재전송한다 (실측: 약 1.6초 뒤). 그러면 연출이 겹쳐서 두 번 돌아간다.
_seen_activities: OrderedDict[str, None] = OrderedDict()
_SEEN_LIMIT = 500

# 대화방에서 동시에 두 판이 돌면 판이 뒤섞인다.
_running: set[str] = set()

# 생존자 나열이 길어지면 판이 읽기 어려워진다.
SURVIVOR_PREVIEW = 20

# 백틱(코드 스팬)으로 감싸지 않는다. Teams 가 배경 박스를 그려서 빈 막대가
# 오히려 채워진 것처럼 보인다. 채움/빈칸이 확실히 구분되는 글자를 쓴다.
BAR_WIDTH = 8
BAR_FILLED = "▰"
BAR_EMPTY = "▱"

# 결과 화면 아래에 작게 붙인다. 카드 안의 @이름 은 멘션이 아니라 평문이다.
RERUN_HINT = "다시 뽑으려면 **@당첨자뽑기** 를 다시 불러 주세요."
JOKER_NOTE = "🃏 **1등이대신당첨** 이 뽑히면 생존 1등이 대신 당첨됩니다 · 빼려면 조커없이"

USAGE = (
    "**사용법**\n"
    "- `@당첨자뽑기 @홍길동 @김철수 @이영희 2명` — 태그한 사람 중 2명을 뽑습니다\n"
    "- `@당첨자뽑기 @모든 사용자 3명` — 채팅방 전원 중 3명을 뽑습니다\n"
    "- 인원 수를 생략하면 1명을 뽑습니다\n"
    "- 🃏 `1등이대신당첨` 이 후보로 함께 들어갑니다. 뽑히면 생존 1등이 대신 당첨됩니다. "
    "빼려면 `조커없이` 를 붙여 주세요\n"
    "- 자기 자신은 Teams 에서 @태그가 안 되니 `@본인이름` 처럼 그냥 쳐주세요"
)


def _already_handled(activity_id: str | None) -> bool:
    """같은 activity 를 두 번 처리하지 않는다."""
    if not activity_id:
        return False
    if activity_id in _seen_activities:
        return True
    _seen_activities[activity_id] = None
    while len(_seen_activities) > _SEEN_LIMIT:
        _seen_activities.popitem(last=False)
    return False


def _names(accounts: list[Account]) -> str:
    """이름을 볼드 평문으로 나열한다. @태그는 쓰지 않는다."""
    return ", ".join(f"**{M.short_name(a.name)}**" for a in accounts)


def _called(accounts: list[Account]) -> str:
    """대사 속 이름. 님은 늘 붙인다 — 문구가 조사를 이/은/을 로만 쓰는 근거다."""
    return ", ".join(f"**{M.short_name(a.name)}**님" for a in accounts)


def _say(host: lines.Host, key: str, who: list[Account] | None = None,
         mark: str | None = None) -> str:
    """진행자의 대사 한 줄. 말머리는 `진행자 이모지:` 다 (말하는 사람 표시).

    당첨·반전처럼 상태가 바뀌는 순간은 mark(🎯, ⚡ …)를 말머리로 쓴다. 판을 훑어볼 때
    그 순간이 한눈에 보여야 한다.
    """
    deck = _decks.get((host.key, key))
    if deck is None:
        deck = _decks[(host.key, key)] = lines.Deck(lines.pool(host, key), rng)
    text = deck.draw()
    if who is not None:
        text = text.format(name=_called(who), names=_called(who))
    return f"{mark} {text}" if mark else f"{host.emoji}: {text}"


def _winner_ping(prefix: str, winners: list[Account]) -> MessageActivityInput:
    """당첨자만 @태그한 메시지. 알림은 이것 하나만 간다.

    판(카드)을 편집하면서 멘션을 넣는 방식은 쓰지 않는다. 편집에 추가된 멘션으로 알림이
    가는지는 확인하지 못했다. 새 메시지의 멘션은 알림이 간다.
    """
    msg = MessageActivityInput(text=prefix)
    for i, account in enumerate(winners):
        if i:
            msg.add_text(", ")
        msg.add_mention(account)
    return msg


def _pick_host(conversation_id: str) -> lines.Host:
    """판마다 진행자를 정한다. 같은 대화방에서 직전 진행자는 피한다."""
    last = _last_host.pop(conversation_id, None)
    host = rng.choice([h for h in lines.HOSTS if h.key != last])
    _last_host[conversation_id] = host.key
    while len(_last_host) > _SEEN_LIMIT:
        _last_host.popitem(last=False)
    return host


def _tease_order(pool: list[Account], count: int) -> list[Account]:
    """뜸들이기 대상 순서. 같은 이름이 연속으로 나오지 않게 한다.

    rng.choice 를 그냥 쓰면 후보가 3명일 때 같은 사람이 연달아 불린다.
    """
    order: list[Account] = []
    while len(order) < count:
        chunk = list(pool)
        rng.shuffle(chunk)
        if order and len(chunk) > 1 and chunk[0].id == order[-1].id:
            chunk[0], chunk[-1] = chunk[-1], chunk[0]
        order.extend(chunk)
    return order[:count]


class Panel:
    """연출 전체를 담는 단 하나의 메시지 (Adaptive Card).

    첫 전송 이후에는 같은 activity 를 계속 편집한다. 필드 값을 바꿔 render() 를
    호출하면 판이 갱신된다.
    """

    def __init__(self, ctx: ActivityContext[MessageActivity], plan: R.Plan,
                 winners_count: int, notes: list[str], host: lines.Host) -> None:
        self._activities = ctx.api.conversations.activities(ctx.activity.conversation.id)
        self._ctx = ctx
        self._plan = plan
        self._winners_count = winners_count
        self._notes = notes
        self._activity_id: str | None = None
        self.host = host

        self.state = "준비 중"
        self.survivors: list[Account] = []
        self.winners: list[Account] = []     # 지금까지 공개된 당첨자
        self.step = 0
        self.line = _say(host, "GREETING")
        self.finished = False

    def _card(self) -> C.AdaptiveCard:
        total = self._plan.total_steps
        filled = round(BAR_WIDTH * self.step / total) if total else 0
        bar = BAR_FILLED * filled + BAR_EMPTY * (BAR_WIDTH - filled)

        if self.survivors:
            shown = _names(self.survivors[:SURVIVOR_PREVIEW])
            if len(self.survivors) > SURVIVOR_PREVIEW:
                shown += f" +{len(self.survivors) - SURVIVOR_PREVIEW}명"
        else:
            shown = "—"

        joker = " + 🃏" if self._plan.joker else ""
        body: list = [
            C.TextBlock(text=f"🎲 **당첨자뽑기** · 후보 **{self._plan.total}명**{joker} · "
                             f"당첨 **{self._winners_count}자리** · "
                             f"진행 {self.host.emoji} **{self.host.name}**",
                        wrap=True),
        ]
        for note in self._notes:
            body.append(C.TextBlock(text=note, size="Small", is_subtle=True,
                                    wrap=True, spacing="None"))
        body.append(C.TextBlock(text=f"▸ **{self.state}**", wrap=True, spacing="Medium"))
        label = "생존 순서" if self.finished else "생존"
        body.append(C.TextBlock(text=f"▸ {label} {shown}", wrap=True, spacing="None"))
        if self.winners:
            body.append(C.TextBlock(text=f"▸ 당첨 {_names(self.winners)}",
                                    wrap=True, spacing="None"))
        body += [
            C.TextBlock(text=f"▸ {bar} 완료 {self.step}/{total}",
                        wrap=True, spacing="None"),
            # 대사만 크게. 이것이 카드를 쓰는 이유다.
            C.TextBlock(text=self.line, size="Large", wrap=True, spacing="Medium"),
        ]
        if self.finished:
            body.append(C.TextBlock(text=RERUN_HINT, size="Small", is_subtle=True,
                                    wrap=True, spacing="Medium"))
        return C.AdaptiveCard(body=body)

    async def open(self) -> None:
        sent = await self._ctx.send(MessageActivityInput().add_card(self._card()))
        self._activity_id = sent.id

    async def beat(self, line: str, hold: float) -> None:
        """대사를 바꿔 한 박자 보여주고 기다린다."""
        self.line = line
        await self._activities.update(
            self._activity_id, MessageActivityInput().add_card(self._card()))
        await asyncio.sleep(hold)


async def _run_draw(ctx: ActivityContext[MessageActivity], plan: R.Plan,
                    winners_count: int, notes: list[str], host: lines.Host) -> None:
    """연출을 진행한다. 핸들러와 분리된 백그라운드 태스크에서 돈다."""
    panel = Panel(ctx, plan, winners_count, notes, host)
    await panel.open()
    await asyncio.sleep(R.hold(R.INTRO_HOLD, rng))   # 시작 인사도 기다리는 대사다

    for step in plan.steps:
        if step.kind == "final":
            await _reveal_winners(panel, plan, step)
            break

        panel.state = f"{step.index}/{plan.total_steps}번째 뽑는중"
        # 앞쪽 단계는 도입 없이 바로 두구두구로 간다. 대기를 줄이지 않고 박자를 빼서
        # 속도를 낸다 (편집 간격 1.0초 하한, roulette.py).
        if step.late:
            await panel.beat(_say(host, "INTRO"), R.hold(R.INTRO_HOLD, rng))
            await panel.beat(_say(host, "TEASE", [step.tease]), R.hold(R.TEASE_HOLD, rng))

        await panel.beat(_say(host, "DRUMROLL"), R.hold(R.drum_hold(step.position), rng))

        panel.survivors.extend(step.survivors)
        panel.step = step.index
        key = "ANNOUNCE" if len(step.survivors) == 1 else "ANNOUNCE_MANY"
        await panel.beat(_say(host, key, step.survivors), R.hold(R.ANNOUNCE_HOLD, rng))

        if step.swap_out:
            await _swap(panel, step)

    # 마무리 — 끝까지 불리지 않은 사람이 마지막 생존자다
    panel.survivors.extend(plan.steps[-1].survivors)
    panel.state = "종료됨"
    panel.finished = True
    headline = _say(host, "WINNER_HEADLINE")
    await panel.beat(f"🎉 **당첨** {_names(plan.final_winners)}\n\n{headline}",
                     R.hold(R.ANNOUNCE_HOLD, rng))

    # 결과 화면을 한 박자 보여준 뒤 당첨자에게 알림. 실패해도 추첨은 이미 끝났으니
    # "추첨 중 오류" 안내를 띄우지 않는다.
    try:
        await ctx.send(_winner_ping("🎉 당첨 ", plan.final_winners))
    except Exception:
        logger.exception("당첨자 알림 전송 실패")


async def _swap(panel: Panel, step: R.Step) -> None:
    """반전. 공개된 생존자 1명이 다시 후보로 내려가고, 안 불린 1명이 그 자리를 차지한다."""
    host = panel.host
    panel.state = "반전!"
    await panel.beat(_say(host, "SWAP_INTRO", mark="⚡"), R.hold(R.INTRO_HOLD, rng))

    at = panel.survivors.index(step.swap_out)
    panel.survivors.pop(at)
    await panel.beat(_say(host, "SWAP_OUT", [step.swap_out], mark="⬇️"),
                     R.hold(R.ANNOUNCE_HOLD, rng))

    panel.survivors.insert(at, step.swap_in)
    await panel.beat(_say(host, "SWAP_IN", [step.swap_in], mark="⬆️"),
                     R.hold(R.ANNOUNCE_HOLD, rng))


async def _reveal_winners(panel: Panel, plan: R.Plan, step: R.Step) -> None:
    """마지막 단계. 연출의 정점은 생존자가 아니라 당첨자 공개다.

    마지막 생존자를 먼저 부르면 1명 추첨에서는 그 순간 당첨자가 정해져 버린다.
    그래서 당첨자를 묶음마다 두구두구를 거쳐 하나씩 부르고, 생존자는 남는 사람으로 둔다.
    """
    host = panel.host
    panel.state = "당첨자 뽑는중"
    # 남아 있는 후보의 이름을 불러놓고 농담으로 돌린다.
    for teased in _tease_order(step.pool, R.tease_count(rng)):
        await panel.beat(_say(host, "TEASE", [teased]), R.hold(R.TEASE_HOLD, rng))
    await panel.beat(_say(host, "FINAL_INTRO"), R.hold(R.INTRO_HOLD, rng))

    batches = step.winner_batches
    for k, batch in enumerate(batches, 1):
        if len(batches) > 1:
            panel.state = f"당첨자 {k}/{len(batches)} 뽑는중"
        await panel.beat(_say(host, "DRUMROLL"), R.hold(R.drum_hold(step.position), rng))

        panel.winners.extend(batch)
        if k == len(batches):
            panel.step = step.index
        key = "WINNER_REVEAL" if len(batch) == 1 else "WINNER_REVEAL_MANY"
        mark = f"🎯 ({k}/{len(batches)})" if len(batches) > 1 else "🎯"
        await panel.beat(_say(host, key, batch, mark=mark), R.hold(R.ANNOUNCE_HOLD, rng))

        if any(w.id == R.JOKER.id for w in batch):
            await _substitute(panel, plan)


async def _substitute(panel: Panel, plan: R.Plan) -> None:
    """🃏 이 뽑혔다. 생존 순서 1등을 불러 그 자리에 앉힌다."""
    host = panel.host
    await panel.beat(_say(host, "JOKER_FOLLOW", mark="🃏"), R.hold(R.drum_hold(1.0), rng))
    sub = plan.substitute
    panel.winners[panel.winners.index(R.JOKER)] = sub
    panel.survivors.remove(sub)
    await panel.beat(_say(host, "SUBSTITUTE", [sub], mark="🎯"), R.hold(R.ANNOUNCE_HOLD, rng))


@app.on_message
async def handle_message(ctx: ActivityContext[MessageActivity]) -> None:
    activity = ctx.activity

    if _already_handled(activity.id):
        logger.info("중복 activity 무시: %s", activity.id)
        return

    conversation_id = activity.conversation.id

    # 로스터는 후보 확정에 필요하다. 실패해도 명시적 태그만으로는 진행할 수 있다.
    try:
        roster = list(await ctx.api.conversations.get_members(conversation_id))
    except Exception as exc:
        logger.warning("로스터 조회 실패, 명시적 태그만 사용: %r", exc)
        roster = []

    candidates = M.collect(activity, roster)
    # 후보 판정 근거를 남긴다. 전원 모드 오판(2026-10-27)을 로그로는 알 수 없었다.
    logger.info("후보 판정 conversation=%s 후보=%d 전원=%s 근거=%s 안내=%s",
                conversation_id, len(candidates.members), candidates.everyone,
                candidates.everyone_signal or "-", candidates.notes or "-")
    n = M.parse_winner_count(activity.text or "")

    notes = list(candidates.notes)
    for res in candidates.ignored:
        notes.append(f"`{res.token}` 은(는) 못 찾아서 제외했습니다 ({res.reason})")

    rejection = R.validate(len(candidates.members), n)
    if candidates.error or rejection:
        body = f"⚠️ {candidates.error or rejection.message}"
        if notes:
            body += "\n\n" + "\n".join(f"- {note}" for note in notes)
        await ctx.send(MessageActivityInput(text=f"{body}\n\n{USAGE}"))
        return

    if conversation_id in _running:
        await ctx.send(MessageActivityInput(
            text="⏳ 이미 추첨이 진행 중입니다. 끝난 뒤에 다시 불러주세요."
        ))
        return

    plan = R.plan(candidates.members, n, rng, joker=M.wants_joker(activity.text or ""))
    if plan.joker:
        notes.append(JOKER_NOTE)
    # 전원 당첨은 뽑을 것이 없으니 연출도 진행자도 없다.
    if plan.all_winners:
        logger.info("추첨 시작 conversation=%s 후보=%d n=%d 전원 당첨",
                    conversation_id, plan.total, n)
        await ctx.send(_winner_ping(f"🎯 {ALL_WINNERS_DECK.draw()}\n\n**당첨** ",
                                    plan.winners))
        return

    # 계획을 짠 뒤에 고른다. 계획이 rng 를 먼저 쓰게 해야 시드로 재현할 수 있다.
    host = _pick_host(conversation_id)
    low, high = R.estimated_bounds(plan)
    swap = plan.swap_step
    logger.info("추첨 시작 conversation=%s 후보=%d n=%d 단계=%d 예상=%.0f~%.0f초 "
                "진행=%s 조커=%s 반전=%s 대신당첨=%s",
                conversation_id, plan.total, n, plan.total_steps, low, high,
                host.key, plan.joker, swap.index if swap else "-", bool(plan.substitute))

    # 연출을 await 하면 15초 제한에 걸려 Bot Framework 가 activity 를 재전송한다.
    # 반드시 백그라운드로 넘기고 핸들러는 즉시 반환한다.
    _running.add(conversation_id)

    async def runner() -> None:
        try:
            await _run_draw(ctx, plan, n, notes, host)
        except Exception:
            logger.exception("연출 중 오류 conversation=%s", conversation_id)
            try:
                await ctx.send(MessageActivityInput(
                    text="⚠️ 추첨 중 오류가 났습니다. 다시 시도해 주세요."
                ))
            except Exception:
                logger.exception("오류 안내 전송도 실패")
        finally:
            _running.discard(conversation_id)

    asyncio.create_task(runner())


if __name__ == "__main__":
    logger.info("%s 기동 — 포트 %s", config.AGENT_NAME, config.PORT)
    asyncio.run(app.start())
