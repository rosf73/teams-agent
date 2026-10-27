"""연출 문구 풀.

LLM 을 쓰지 않는다. 생성 지연이 연출의 호흡을 깨고, 미리 써둔 문구가 실제로 더 낫다.

판마다 **진행자** 한 명이 대사를 전부 맡는다. 진행자는 말투(voice)를 하나 갖고,
같은 말투의 진행자는 같은 문구 풀을 쓴다. 진행자마다 다른 것은 이모지·이름·
시작 인사·두구두구 효과음 몇 개뿐이다.

말투는 과장하지 않는다. 듣는 사람이 직장인이라 유치하면 안 되고, 살짝 어설픈 정도가 맞다.

- dry    무덤덤하게 살살 긁는다            🧛 뱀파이어 · 🤖 로봇
- kid    약간 유순한데 얄미운 어린이        🧙 마법사 · 💩 똥
- polite 존댓말                         🧝 엘프 · 🦹 악당

`{name}` / `{names}` 자리에는 **님까지 붙은** 이름이 들어간다 (`**더미1**님`).
그래서 문구는 이름 뒤에 `님` 을 쓰지 않고, 조사는 받침이 있는 쪽(이/은/을)만 쓴다.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Host:
    key: str
    emoji: str      # 대사 앞 말머리. 성별 표시가 없는 이모지를 쓴다
    name: str
    voice: str
    greeting: tuple[str, ...]   # 판을 띄울 때의 첫 대사
    drumroll: tuple[str, ...]   # 공용 두구두구에 더하는 효과음


HOSTS = (
    Host("villain", "🦹", "악당", "polite",
         ("시작하겠습니다. 기대는 하지 마시고요.", "자, 누가 살아남는지 보시죠."),
         ("두구두구. 되셨죠?",)),
    Host("vampire", "🧛", "뱀파이어", "dry",
         ("밤이 길겠네. 시작할게.", "조용히들 해. 시작한다."),
         ("……",)),
    Host("wizard", "🧙", "마법사", "kid",
         ("주문 준비 완료! 시작한다!", "내가 진행할게!"),
         ("수리수리… 두구두구…",)),
    Host("poop", "💩", "똥", "kid",
         ("오늘은 내가 진행이야!", "시작할게! 냄새는 신경 쓰지 마."),
         ("부글부글…",)),
    Host("robot", "🤖", "로봇", "dry",
         ("추첨 모드 켬. 시작한다.", "삐빅. 진행 시작."),
         ("삐빅… 두구두구…", "계산 중… 두구두구…")),
    Host("elf", "🧝", "엘프", "polite",
         ("곧 시작하겠습니다.", "오늘 진행은 제가 맡겠습니다."),
         ("…………",)),
)

# 모든 진행자가 함께 쓰는 두구두구. 진행자 효과음이 여기에 더해진다.
DRUMROLL = (
    "두구두구두구…",
    "두구두구두구두구…",
    "두구… 두구… 두구…",
    "드르르르르륵…",
)

# 말투마다 반드시 있어야 하는 풀. {name} / {names} 가 필요한 풀은 값으로 표시한다.
POOLS = {
    "INTRO": None,              # 끝쪽 탈락 단계의 도입
    "ANNOUNCE": "name",         # 생존 발표
    "ANNOUNCE_MANY": "names",   # 묶음 생존 발표
    "TEASE": "name",            # 뜸들이기 · 미니 뜸
    "FINAL_INTRO": None,        # 당첨자 공개 직전
    "WINNER_REVEAL": "name",
    "WINNER_REVEAL_MANY": "names",
    "WINNER_HEADLINE": None,    # 결과 화면
    "SWAP_INTRO": None,         # 반전
    "SWAP_OUT": "name",
    "SWAP_IN": "name",
    "JOKER_FOLLOW": None,       # 🃏 이 뽑혔을 때
    "SUBSTITUTE": "name",
}

VOICES = {
    # ── 무덤덤, 음슴체 ─────────────────────────────────────────
    "dry": {
        "INTRO": (
            "다음은 누굴까?",
            "자, 다음.",
            "기대는 안 하는 게 좋음.",
            "다들 표정 관리 잘하네.",
            "다음 명단 뽑는 중임.",
        ),
        "ANNOUNCE": (
            "{name}이 생존이라니 믿을 수 없음.",
            "아쉽게도 {name}이 살아남았음.",
            "{name}은 통과임.",
            "{name}은 살았음. 운이 좋은듯.",
            "{name} 생존임.",
        ),
        "ANNOUNCE_MANY": (
            "{names} 다 생존함. 이럴 순 없음.",
            "아… {names}이 살아남았음.",
            "{names}은 통과. 운이 다들 좋았음.",
        ),
        "TEASE": (
            "{name}, 긴장 중?",
            "{name}… 뽑히면 좋겠음.",
            "{name}이 떨고 있는 게 보임.",
            "{name}일까? 아닐까? 맞을까? 틀릴까?",
            "{name}이 손에 땀을 흘리고 있음.",
            "{name}… 이라고 할 뻔했는데 사실 아님.",
        ),
        "FINAL_INTRO": (
            "이제 진짜 끝임.",
            "당첨자 발표함. 마음의 준비 하셈.",
            "도망칠 곳은 없습니다.",
        ),
        "WINNER_REVEAL": (
            "당첨은… {name}!",
            "{name}, 당첨임!",
            "{name}이 걸렸음…",
        ),
        "WINNER_REVEAL_MANY": (
            "당첨은… {names}.",
            "{names}, 당첨이야.",
        ),
        "WINNER_HEADLINE": (
            "당첨을 축하드립니다.",
            "축하함.",
            "와우.",
        ),
        "SWAP_INTRO": (
            "그냥 끝나면 재미없잖음.",
            "잠깐, 하나 바꿀거임.",
            "마음이 바뀌었음.",
        ),
        "SWAP_OUT": (
            "{name}, 다시 내려와야 할듯. 슈퍼생존권 발동임.",
            "{name}의 생존을 취소하겠음. 조작아니고 슈퍼생존권 발동함.",
            "미안하지만 {name}의 생존이 다른 사람 때문에 취소됨.",
        ),
        "SWAP_IN": (
            "그 자리는 {name}이 가져가게 되었음.",
            "{name}이 끌어내리고 그 자리를 차지함.",
            "{name}의 슈퍼생존권이 성공적으로 발동!",
        ),
        "JOKER_FOLLOW": (
            "1등이대신당첨이 뽑혔음. 그럼 생존 1등은…",
            "조커! 생존자 중에 1등을 끌어내리자!",
        ),
        "SUBSTITUTE": (
            "{name}, 1등이니까 대신 당첨임. 축하함.",
            "가장 먼저 살아남은 {name}이 당첨. 세상이 어쩔 수 없음.",
        ),
    },
    # ── 약간 유순한데 얄미운 어린이 ───────────────────────────────────
    "kid": {
        "INTRO": (
            "다음 생존자 뽑을게!",
            "이번엔 누구게?",
            "누가 살아남을까~?",
            "나만 알고 있지롱.",
            "궁금하지? 조금만 기다려!",
            "자, 간다!",
        ),
        "ANNOUNCE": (
            "{name}이 생존했어!",
            "{name}은 통과했어.",
            "{name} 살았다! 좋겠다~",
            "{name}은 무사해! 다행이지?",
            "{name} 생존! 야호!",
        ),
        "ANNOUNCE_MANY": (
            "{names} 생존했어!",
            "{names}은 통과했어!",
            "{names} 살았다! 좋겠다~",
        ),
        "TEASE": (
            "{name}! …은 아닌가봐.",
            "{name}인 줄 알았지?",
            "{name}, 떨려? 떨리지?",
            "{name}… 아 아니다!",
            "{name} 표정 봐! 아직이야.",
            "{name}일까~ 아닐까~",
        ),
        "FINAL_INTRO": (
            "이제 당첨자 발표할게!",
            "마지막이야! 다들 숨 참아!",
            "두근두근… 이제 진짜야!",
        ),
        "WINNER_REVEAL": (
            "당첨은… {name}!",
            "{name} 당첨!!",
            "{name}이 뽑혔어!",
        ),
        "WINNER_REVEAL_MANY": (
            "당첨은… {names}!",
            "{names} 당첨!!",
        ),
        "WINNER_HEADLINE": (
            "당첨 축하해!!",
            "축하해~ 부럽다!",
            "축하축하!",
        ),
        "SWAP_INTRO": (
            "아 잠깐! 이거 바꿀래!",
            "반전 하나 넣어 볼게!",
            "히히, 이대로는 재미없지!",
        ),
        "SWAP_OUT": (
            "{name}, 미안! 다시 내려와!",
            "{name}은 생존 취소야!",
            "{name}, 한 번 더 해야 돼!",
        ),
        "SWAP_IN": (
            "{name}이 대신 올라갔어!",
            "그 자리는 {name} 거야!",
            "{name}, 축하해! 대신 살았어!",
        ),
        "JOKER_FOLLOW": (
            "1등이대신당첨이 뽑혔어! 그럼 1등은…",
            "조커다! 생존 1등 누구였더라…",
        ),
        "SUBSTITUTE": (
            "{name}이 1등이었지? 대신 당첨!",
            "1등 {name}, 축하해!!",
        ),
    },
    # ── 존댓말. 예전 기본 말투와 비슷하다 ─────────────────────────────
    "polite": {
        "INTRO": (
            "이번에 살아남을 분은 누구일까요?",
            "다음 생존자를 뽑아보겠습니다.",
            "숨 한 번 크게 쉬시고…",
            "심장 뛰는 소리 들리시나요?",
            "누군가는 웃고, 누군가는 남습니다.",
        ),
        "ANNOUNCE": (
            "{name}이 생존했습니다!",
            "{name}, 생존!",
            "{name}은 이번에도 무사 통과입니다!",
            "축하합니다, {name} 생존!",
            "{name}, 오늘은 살았습니다!",
        ),
        "ANNOUNCE_MANY": (
            "{names}이 살아남았습니다!",
            "{names}, 생존!",
            "{names}, 무사 통과입니다!",
        ),
        "TEASE": (
            "{name}, 긴장되시나요?",
            "{name}!!! …일까요?",
            "{name}… 인 줄 아셨죠?",
            "{name}은 잠시 후에 다시 뵙겠습니다.",
            "{name}, 표정이 좋으신데… 아직입니다.",
        ),
        "FINAL_INTRO": (
            "이제 도망갈 곳이 없습니다.",
            "당첨자를 발표하겠습니다.",
            "운명이 갈리는 순간입니다.",
        ),
        "WINNER_REVEAL": (
            "당첨은… {name}!",
            "{name}, 당첨입니다!",
            "운명이 {name}을 골랐습니다!",
        ),
        "WINNER_REVEAL_MANY": (
            "당첨은… {names}!",
            "{names}, 당첨입니다!",
        ),
        "WINNER_HEADLINE": (
            "축하합니다!",
            "주인공이 정해졌습니다!",
            "결과가 나왔습니다!",
        ),
        "SWAP_INTRO": (
            "잠깐! 반전입니다!",
            "이대로 끝날 리가 없죠. 반전입니다!",
        ),
        "SWAP_OUT": (
            "{name}, 다시 후보로 내려오셔야겠습니다.",
            "죄송합니다, {name}. 생존 취소입니다!",
        ),
        "SWAP_IN": (
            "그 자리는 {name}이 차지합니다!",
            "{name}이 대신 살아남습니다!",
        ),
        "JOKER_FOLLOW": (
            "1등이대신당첨이 뽑혔습니다! 생존 1등은…",
            "조커가 나왔습니다. 그렇다면 생존 1등은…",
        ),
        "SUBSTITUTE": (
            "가장 먼저 살아남았던 {name}이 당첨입니다!",
            "생존 1등 {name}, 대신 당첨입니다!",
        ),
    },
}


def pool(host: Host, key: str) -> tuple[str, ...]:
    """진행자가 이 박자에 쓸 문구 풀."""
    if key == "GREETING":
        return host.greeting
    if key == "DRUMROLL":
        return DRUMROLL + host.drumroll
    return VOICES[host.voice][key]


# 진행자 없이 바로 끝나는 전원 당첨 안내.
ALL_WINNERS = (
    "고민할 것도 없습니다. 전원 당첨!",
    "뽑을 자리가 후보 수와 같네요. 전원 당첨입니다!",
)


class Deck:
    """같은 문구가 연속으로 나오지 않게 셔플한 덱에서 꺼낸다.

    random.choice 는 바로 이전 것을 또 뽑는다. 실제로 같은 도입 문구가
    3연속 나와서 연출이 단조로워졌다.
    """

    def __init__(self, pool: tuple[str, ...], rng) -> None:
        self._pool = pool
        self._rng = rng
        self._deck: list[str] = []
        self._last: str | None = None

    def draw(self) -> str:
        if not self._deck:
            self._deck = list(self._pool)
            self._rng.shuffle(self._deck)
            # 덱을 새로 깔았을 때 직전 문구가 맨 앞이면 뒤로 보낸다.
            if len(self._deck) > 1 and self._deck[-1] == self._last:
                self._deck[0], self._deck[-1] = self._deck[-1], self._deck[0]
        self._last = self._deck.pop()
        return self._last
