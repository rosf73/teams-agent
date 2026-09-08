"""당첨자뽑기 — 채팅에서 후보 중 당첨자를 긴장감 있게 뽑는다.

    @당첨자뽑기 @A @B @C @D @E 2명

생존자를 한 명씩 공개하며 후보 풀을 줄이고, 마지막 단계에서 마지막 생존자와
당첨자를 공개한다. 자세한 규칙은 docs/FEATURES.md.

연출 원칙 세 가지:

  1. 메시지는 **딱 한 개**. 전체 연출이 그 메시지를 계속 편집하는 방식이다.
     1분에 메시지 20개가 올라오면 채팅방이 업무에 방해가 된다.
  2. 사람을 **@태그하지 않는다.** 이름은 볼드 평문으로만 쓴다. 알림을 줄이는 게 목적이다.
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
DECKS = {
    name: lines.Deck(getattr(lines, name), rng)
    for name in ("INTRO", "DRUMROLL", "ANNOUNCE", "ANNOUNCE_MANY", "TEASE",
                 "FINAL_INTRO", "FINAL_ANNOUNCE", "WINNER_HEADLINE", "ALL_WINNERS")
}

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

USAGE = (
    "**사용법**\n"
    "- `@당첨자뽑기 @홍길동 @김철수 @이영희 2명` — 태그한 사람 중 2명을 뽑습니다\n"
    "- `@당첨자뽑기 @모든 사용자 3명` — 채팅방 전원 중 3명을 뽑습니다\n"
    "- 인원 수를 생략하면 1명을 뽑습니다\n"
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
                 winners_count: int, notes: list[str]) -> None:
        self._activities = ctx.api.conversations.activities(ctx.activity.conversation.id)
        self._ctx = ctx
        self._plan = plan
        self._winners_count = winners_count
        self._notes = notes
        self._activity_id: str | None = None

        self.state = "준비 중"
        self.survivors: list[Account] = []
        self.step = 0
        self.line = "🥁 곧 시작합니다…"

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

        body: list = [
            C.TextBlock(text=f"🎲 **당첨자뽑기** · 후보 **{self._plan.total}명** · "
                             f"당첨 **{self._winners_count}자리**",
                        wrap=True),
        ]
        for note in self._notes:
            body.append(C.TextBlock(text=note, size="Small", is_subtle=True,
                                    wrap=True, spacing="None"))
        body += [
            C.TextBlock(text=f"▸ **{self.state}**", wrap=True, spacing="Medium"),
            C.TextBlock(text=f"▸ 생존 {shown}", wrap=True, spacing="None"),
            C.TextBlock(text=f"▸ {bar} 완료 {self.step}/{total}",
                        wrap=True, spacing="None"),
            # 대사만 크게. 이것이 카드를 쓰는 이유다.
            C.TextBlock(text=self.line, size="Large", wrap=True, spacing="Medium"),
        ]
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
                    winners_count: int, notes: list[str]) -> None:
    """연출을 진행한다. 핸들러와 분리된 백그라운드 태스크에서 돈다."""
    panel = Panel(ctx, plan, winners_count, notes)
    await panel.open()
    await asyncio.sleep(R.hold(R.ANNOUNCE_HOLD, rng))

    for step in plan.steps:
        last = step.kind == "final"

        # 1박자 — 도입
        panel.state = ("마지막 생존자 뽑는중" if last
                       else f"{step.index}/{plan.total_steps}번째 뽑는중")
        if last:
            # 남아 있는 후보의 이름을 불러놓고 농담으로 돌린다.
            for teased in _tease_order(step.pool, R.tease_count(rng)):
                await panel.beat(
                    "🥁 " + DECKS["TEASE"].draw().format(name=M.short_name(teased.name)),
                    R.hold(R.TEASE_HOLD, rng))
            await panel.beat("🥁 " + DECKS["FINAL_INTRO"].draw(), R.hold(R.INTRO_HOLD, rng))
        else:
            await panel.beat("🥁 " + DECKS["INTRO"].draw(), R.hold(R.INTRO_HOLD, rng))

        # 2박자 — 두구두구. 발표 직전이라 대기를 가장 크게 흔든다.
        await panel.beat("🥁 " + DECKS["DRUMROLL"].draw(), R.hold(R.DRUM_HOLD, rng))

        # 3박자 — 발표
        panel.survivors.extend(step.survivors)
        panel.step = step.index
        if last:
            line = "😌 " + DECKS["FINAL_ANNOUNCE"].draw().format(name=M.short_name(step.survivors[0].name))
        elif len(step.survivors) == 1:
            line = "😌 " + DECKS["ANNOUNCE"].draw().format(name=M.short_name(step.survivors[0].name))
        else:
            joined = "**, **".join(M.short_name(a.name) for a in step.survivors)
            line = "😌 " + DECKS["ANNOUNCE_MANY"].draw().format(names=joined)
        await panel.beat(line, R.hold(R.ANNOUNCE_HOLD, rng))

    # 마무리 — 당첨자 공개
    panel.state = "종료됨"
    panel.line = (f"🎯 **당첨** {_names(plan.winners)}\n\n"
                  f"{DECKS['WINNER_HEADLINE'].draw()}")
    await panel.beat(panel.line, 0)


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

    plan = R.plan(candidates.members, n, rng)
    low, high = R.estimated_bounds(plan)
    logger.info("추첨 시작 conversation=%s 후보=%d n=%d 단계=%d 예상=%.0f~%.0f초",
                conversation_id, plan.total, n, plan.total_steps, low, high)

    # 전원 당첨은 뽑을 것이 없으니 연출도 없다.
    if plan.all_winners:
        await ctx.send(MessageActivityInput(
            text=f"🎯 {DECKS['ALL_WINNERS'].draw()}\n\n**당첨** {_names(plan.winners)}"
        ))
        return

    # 연출을 await 하면 15초 제한에 걸려 Bot Framework 가 activity 를 재전송한다.
    # 반드시 백그라운드로 넘기고 핸들러는 즉시 반환한다.
    _running.add(conversation_id)

    async def runner() -> None:
        try:
            await _run_draw(ctx, plan, n, notes)
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
