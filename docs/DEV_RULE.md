# 개발 규칙

이 저장소에서 작업하는 사람과 에이전트 모두에게 적용된다.
각 항목은 실제로 사고가 났거나, 실행해서 확인한 사실에서 나왔다.

---

## 1. `.env` 파일은 건드리지 않는다 (절대)

**금지**

- `.env` 를 **쓰지 않는다** — `>`, `>>`, `sed -i`, `tee`, Write/Edit 도구 전부
- `.env` 를 **삭제하지 않는다** — `rm`, `git clean`
- `.env` 위에 **복사하지 않는다** — `cp .env.example .env`
- 이미 존재하는 `.env` 를 **정리(cleanup) 대상으로 삼지 않는다**

**허용**

- `.env.example` 작성/수정 (여기엔 실제 값이 없다)
- `./scripts/check-env.sh` 실행 — 읽기 전용이고 시크릿은 길이와 앞 3글자만 출력한다
- 사용자에게 "이 값을 이렇게 채우세요"라고 **안내**하기

**예외 하나** — `scripts/build-package.sh` 는 `TEAMS_APP_ID` 가 비어 있을 때만 그 한 줄을
`sed` 로 채운다. 의도된 동작이고 다른 키는 건드리지 않는다. 이 예외를 확장하지 않는다.

### 왜

`CLIENT_SECRET` 은 Teams 개발자 포털에서 **발급 시 1회만 표시된다.** 잃으면 복구 경로가 없고,
봇을 다시 만들어야 하며 그러면 `CLIENT_ID` 까지 바뀌어 엔드포인트 설정과 앱 패키지를 전부 다시 해야 한다.

2026-08-20, 검사 스크립트를 테스트하면서 `cp .env.example .env` 로 채워둔 `.env` 두 개를
덮어썼다. `CLIENT_SECRET` 이 소실되어 봇을 재생성해야 했다.

**1회성 크리덴셜은 복구 불가 자원으로 취급한다.**

---

## 2. 파괴적 테스트는 프로젝트 파일로 하지 않는다

스크립트의 실패 경로를 검증할 때는 임시 디렉터리에 가짜 트리를 만들어서 한다.
프로젝트의 실제 파일을 고쳐서 "실패하는지 보는" 짓을 하지 않는다.

```bash
# 이렇게
WORK=$(mktemp -d) && mkdir -p "$WORK/agent/src" && printf 'CLIENT_ID=bad\n' > "$WORK/agent/src/.env"

# 이렇게 하지 않는다
sed -i '' 's/^CLIENT_ID=.*/CLIENT_ID=bad/' roulette-agent/src/.env
```

## 3. 기존 파일을 덮어쓰는 명령에는 가드를 붙인다

```bash
[ -f "$f" ] || cp "$f.example" "$f"
```

`cp`, `sed -i`, `rm`, `>` 를 쓸 때는 대상이 이미 있는지 먼저 확인한다.
macOS 의 `sed -i ''` 는 백업을 남기지 않는다.

## 4. 되돌릴 수 없는 것을 하기 전에 확인한다

크리덴셜 발급/폐기, 봇 재생성, Teams 앱 재설치는 사용자에게 먼저 묻는다.

---

## 5. 서비스가 배정한 식별자는 조립하지 않는다

터널 ID, 호스트 이름, 리전 코드처럼 서비스가 만들어 주는 값은 **출력에서 복사**한다.
규칙이 있어 보여도 추측하지 않는다. `teams-agents` 로 터널을 만들면 실제 ID 는
`teams-agents.<리전>` 이 되고, URL 호스트는 그와 무관한 `a1b2c3d4` 이다.
틀린 ID 를 넘기면 `devtunnel` 은 오류 없이 빈 출력을 내므로 **조용히 실패한다.**

셸 변수로 한 번 잡아서 재사용한다:

```bash
TID=$(devtunnel list | grep -Eo 'teams-agents\.[a-z0-9]+' | head -1)
```

## 6. 테스트 픽스처는 실제 API 가 주는 타입으로 만든다

오프라인 테스트가 통과했는데 실환경에서 500 이 났다면, 대개 픽스처 타입이 실제와 다른 것이다.

2026-08-20, 로스터 픽스처를 `Account` 로 만들어 12개 케이스를 통과시켰다.
그런데 `get_members()` 는 **`TeamsChannelAccount`** 를 반환하고 이 둘은 상속 관계가 아니다.
`add_mention()` 이 pydantic 검증에서 거부해 실제 채팅에서 500 이 났다.

```python
# 픽스처를 만들기 전에 반환 타입을 확인한다
import inspect
print(inspect.signature(ConversationMemberClient.get))
# -> (self, conversation_id: str) -> List[TeamsChannelAccount]
```

**타입은 경계에서 한 번 정규화하고, 픽스처도 그 경계 밖의 진짜 타입으로 만든다.**

## 7. 검증한 사실만 단정한다

무료 티어 조건, SDK API 시그니처, 포털 UI 경로는 자주 바뀐다.
실행하거나 1차 출처를 확인한 것만 단정하고, 나머지는 "확인 필요"로 표시한다.

이미 정정한 사례:

| 처음 쓴 것 | 실제 |
|---|---|
| 개발자 포털 봇 목록 = `/bots` | `/tools/bots` |
| 경로 A 는 `TENANT_ID` 를 비운다 | 포털이 단일 테넌트 봇을 만들 수 있어 실측 필요 |
| Cloudflare Tunnel 로 호스팅 | 무료 Quick Tunnel 은 URL 이 매번 바뀌어 사용 불가 |
| Python 3.12+ 면 된다 | 3.14.6 은 venv 생성 실패. 3.13 사용 |
| `requirements.txt` 의 `dotenv` | PyPI 의 "Deprecated package". `python-dotenv` 사용 |
| 터널 ID = `teams-agents` | 리전 접미사가 붙는다: `teams-agents.<리전>` |
| URL = `https://<터널ID>-<포트>...` | 호스트는 터널 ID 와 무관한 생성 토큰: `a1b2c3d4-3978.<리전>` |
| 로스터 = `list[Account]` | `list[TeamsChannelAccount]`. 상속 관계 아님 |
| 표시명 완전일치로 사람 찾기 | 표시명이 `한글 (English)` 형식이라 실패. 별칭 매칭 필요 |
| 멘션 1개 = 엔터티 1개 | 표시명에 공백이 있으면 토큰마다 엔터티가 생김 (id 동일) |
| 카드 액션 `data` 는 항상 dict | 발신은 `SubmitActionData`, 수신은 dict |
| 지난 날짜는 내년으로 이월 | 하루 이틀 지난 건 오타. 60일 넘게 지난 경우만 이월 |
| 게이지 분모 = 최다 득표 | 1위가 항상 만점이라 안 움직인다. 분모는 참여자 수 |
| 카드 액션 라우팅 = `verb` | **`data["action"]`** 이다. verb 만 넣으면 조용히 매칭 실패 |

---

## 8. 프로젝트 고유 제약 (실행해서 확인함)

### 난수는 LLM 에 맡기지 않는다

추첨은 `random.SystemRandom()` 을 쓴다. LLM 출력은 편향되고 검증할 수 없다.
두 에이전트 모두 LLM 없이 동작한다 — 이게 월 $0 의 전제다.

### 15초 안에 응답을 반환한다

핸들러에서 긴 작업을 `await` 하면 Bot Framework 가 activity 를 재전송하고,
연출이 2~3번 겹쳐 돌아간다. 긴 작업은 `asyncio.create_task()` 로 분리하고
`activity.id` 로 중복을 제거한다.

### `strip_mentions_text()` 는 제자리에서 변경한다

`activity.text` 를 덮어쓰고 `self` 를 반환한다. **멘션을 먼저 추출한 뒤** 호출한다.
`entities` 는 건드리지 않으므로 순서만 지키면 된다.

### `add_text()` 는 구분자를 넣지 않는다

연속된 `add_mention()` 은 `<at>A</at><at>B</at>` 가 된다. 사이를 직접 채운다.

### 로스터 타입은 `Account` 가 아니다

`ctx.api.conversations.get_members()` → `list[TeamsChannelAccount]`.
`MentionEntity.mentioned` 는 `Account` 만 받는다. `mentions.to_account()` 로 경계에서 변환한다.

### 500 을 내면 Bot Framework 가 재전송한다

실측: 핸들러가 500 을 내자 약 1.6초 뒤 동일 activity 가 다시 왔다.
즉 예외가 나면 **연출이 두 번 실행된다.** `activity.id` 중복 제거가 선택이 아니라 필수다.

### 카드 액션 라우팅은 `verb` 가 아니라 `data["action"]` 을 본다

`@app.on_card_action_execute("vote")` 는 **`ctx.value.action.data["action"]`** 과 비교한다.
SDK 소스 (`apps/routing/activity_handlers.py`):

```python
data = ctx.value.action.data
action = data.get("action")
return action == action_or_handler
```

`ExecuteAction(verb="vote", data={"p": ...})` 처럼 **verb 만** 넣으면 매칭이 안 된다.
그러면 SDK 가 기본 200 을 돌려주고 Teams 는 **"응답이 앱으로 전송되었습니다"** 만 띄운 뒤
카드가 전혀 갱신되지 않는다. 버튼이 잠깐 비활성화됐다 풀리므로 **성공처럼 보인다.**

```python
C.ExecuteAction(title="참", verb="vote",
                data={"action": "vote", "p": poll_id, "i": 0})   # action 키가 필수
```

Adaptive Card 규격상 `verb` 도 함께 넣어 둔다.

### 핸들러를 직접 부르는 테스트는 라우팅 버그를 못 잡는다

위 버그는 **검증 101건이 통과한 상태로 실환경까지 갔다.** 테스트가
`await app.handle_vote(ctx)` 로 함수를 직접 불러서 라우팅 조건을 건드리지 않았기 때문이다.

```python
# 이렇게 — 라우터를 통과시킨다
handlers = app.app.router.select_handlers(activity)
assert len(handlers) == 1
for h in handlers:
    await h(ctx)

# 그리고 invoke 의 data 는 손으로 짜지 말고 **카드에 실린 것을 그대로** 쓴다.
# Teams 가 그렇게 되돌려 보내므로, 손으로 짜면 카드와 어긋나도 통과해 버린다.
```

**경계(라우팅·직렬화)를 지나가는 경로를 최소 한 번은 테스트가 통과해야 한다.**

### 카드 액션의 `data` 는 방향에 따라 타입이 다르다

| 방향 | 타입 | 접근 |
|---|---|---|
| 발신 `ExecuteAction.data` | dict 를 넣어도 **`SubmitActionData`** 로 감싸진다 | `model_dump()` |
| 수신 `AdaptiveCardInvokeAction.data` | 평범한 **`Dict[str, Any]`** | `.get()` |

핸들러는 `.get()` 이 맞고, 테스트가 발신 카드를 검사할 때는 `model_dump()` 가 필요하다.
직렬화 결과는 양쪽 모두 `{"p": ..., "i": ...}` 로 같다.

### 카드 열거형 값은 대문자로 시작한다

`TextBlock.size` = `Small|Default|Medium|Large|ExtraLarge`,
`weight` = `Lighter|Default|Bolder`, `spacing` = `None|ExtraSmall|…`.
소문자를 넣으면 pydantic 이 거부한다. 단 `ExecuteAction.style` 은
`default|positive|destructive` 로 **소문자**다.

### 카드 요소는 키워드 인자로만 만든다

`C.TextBlock("텍스트")` 는 `BaseModel.__init__() takes 1 positional argument` 로 실패한다.
`C.TextBlock(text="텍스트")` 로 쓴다.

### 진행 게이지의 분모를 최대값으로 잡지 않는다

막대·게이지를 **최대값 기준**으로 채우면 1위는 항상 만점, 최소값은 항상 0 이라
**값이 바뀌어도 화면이 안 변한다.** 항목이 적고 표본이 작을 때 특히 심하다.

투표 막대를 `count / max(counts)` 로 만들었더니 1위가 1표든 10표든 `██████████` 이었고,
사용자에게 "게이지가 움직이지 않는다" 로 보고됐다.

**분모는 의미가 고정된 값으로 잡는다** — 여기서는 참여자 수 (`count / voter_count`).
그리고 막대 10칸은 해상도가 낮으니 숫자(표수·퍼센트)를 함께 적는다.

테스트는 "렌더 결과가 존재한다" 가 아니라 **"값이 바뀔 때 렌더가 달라진다"** 를 검사해야
이런 버그를 잡는다.

### 프로세스 종료는 TERM 만으로 안 된다

uvicorn 이 `asyncio.sleep` 중일 때 `SIGTERM` 을 늦게(또는 아예) 처리하지 않아
프로세스가 살아남는다. 포트가 잡혀 있으면 새 프로세스가 `address already in use` 로
**뜬 직후 죽는데, 로그에는 "started successfully" 가 먼저 찍혀서 성공처럼 보인다.**

`scripts/_common.sh` 의 `stop_port` / `stop_tunnel` 은 TERM → 1.5초 대기 → **`KILL -9`**
순서로 처리하고, **포트가 실제로 비었는지 확인한 뒤** 반환한다.

`-9` 가 안전한 이유: vote-agent 는 SQLite 트랜잭션마다 커밋하고 roulette-agent 는
아무것도 저장하지 않는다.

### `die` 를 명령 치환 안에서 부르면 스크립트가 안 멈춘다

```bash
BG="$(parse_background "$1")"   # die 가 서브셸만 종료 → 잘못된 옵션이 통과
```

실제로 `--wat` 같은 오타가 오류를 출력하고도 그대로 실행됐다.
검증 함수는 **전역 변수를 설정하는 방식**으로 만들고 명령 치환으로 감싸지 않는다.

### start 스크립트는 시스템 전역 프로세스를 건드린다

`stop_tunnel` 은 `ps` 로 전체 프로세스를 훑는다. 임시 사본에서 테스트하면
**실제로 돌고 있는 터널이 죽는다** (규칙 2 를 우회하지 못하는 예외).
오류 경로만 시험하고, 정상 경로는 실제 저장소에서 돌린 뒤 상태를 복구한다.

### `PORT` 는 Dev Tunnel 포트와 짝이다

`roulette-agent` = 3978, `vote-agent` = 3979. 한쪽만 바꾸면 조용히 깨진다.

### 매니페스트 길이 제약

`name.short` 30자, `command.title` 32자, `description.short` 80자.
`build-package.sh` 가 **치환 후** 값으로 검사한다 (플레이스홀더 길이는 의미 없다).

### `TEAMS_APP_ID` 는 한 번 정해지면 유지한다

앱 신원이 바뀌면 Teams 에서 재설치해야 한다. `build-package.sh` 가 이를 보장한다.
