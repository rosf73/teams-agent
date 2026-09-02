# Teams 에이전트 무료 배포 가이드

`@당첨자뽑기` / `@투표만들기` 두 봇을 **월 $0**으로 사내 Teams 채팅방에 올리는 절차.
호스팅은 **사무실 Mac + Microsoft Dev Tunnels**. 클라우드 구독 없이 진행한다.

> 작업 규칙은 [DEV_RULE.md](./DEV_RULE.md) 를 먼저 읽는다. 특히 `.env` 는 건드리지 않는다.

---

## 0. 최종 상태

```
Teams 클라이언트 ──@멘션──> Bot Framework 서비스
                                   │ HTTPS POST /api/messages
        ┌──────────────────────────┴──────────────────────────┐
        ▼                                                     ▼
https://<생성토큰>-3978.<리전>.devtunnels.ms   https://<생성토큰>-3979.<리전>.devtunnels.ms
        │            devtunnel host  (터널 1개, 포트 2개)        │
        ▼                     사무실 Mac                       ▼
   roulette-agent :3978                              vote-agent :3979
```

### 비용 명세

| 항목 | 요금 | 근거 |
|---|---|---|
| 봇 등록 (Bot Service) | **$0** | Teams는 *standard channel* → **메시지 무제한 무료** |
| Entra 앱 등록 ×2 | $0 | Entra ID는 M365에 포함. Azure 구독 불필요 |
| Dev Tunnels | $0 | 5GB/월, 1500 req/분, 터널 10개 |
| 컴퓨트 | $0 | 보유 Mac |
| LLM | $0 | 사용하지 않음 (추첨 난수 = `SystemRandom`) |
| Teams 앱 배포 | $0 | 사이드로드 (사용자 지정 앱 업로드) |

> 유료로 오해하기 쉬운 부분: Bot Service에서 과금되는 *premium channel*은 Direct Line / Web Chat이다. Teams는 해당 없음.

### 사전 확인

- [ ] Teams에서 `앱 > 앱 관리 > 앱 업로드`에 **"사용자 지정 앱 업로드"** 항목이 보인다
      → 보이면 테넌트 정책이 이미 허용된 상태다
- [ ] Python **3.13** (`python3.13 -V`)
      이 머신의 기본 `python3`는 3.14.6인데 **3.14에서는 venv 생성이 실패했다.**
      SDK가 선언한 요구 버전은 `>=3.11,<4.0`이지만 3.13을 쓴다.
      없으면: `brew install python@3.13`
- [ ] 테스트용 그룹 채팅방 하나 (본인 + 동료 1명 정도)

---

## STEP 1 — 봇 등록 ×2

### 경로 A: Teams 개발자 포털 (Azure 구독 불필요, 먼저 시도)

<https://dev.teams.microsoft.com/tools/bots>

1. `Tools > Bot management` → **`+ New bot`**
2. 이름 `roulette-agent` → **Add**
3. 생성된 봇의 **Bot ID**를 기록 (= Entra 앱의 Client ID와 동일)
4. `Client secrets` → **`Add a client secret`** → **값을 즉시 기록**
   (이 화면을 벗어나면 다시 볼 수 없다)
5. `vote-agent` 이름으로 2~4 반복

포털이 **Entra 앱 등록과 시크릿까지 대신 만들어 준다.** Azure 구독도, 별도 앱 등록도 필요 없다.

> **`Multitenant bot creation is deprecated. Please use SingleTenant or UserAssignedMSI`**
> 이 오류가 나면 경로 A는 막힌 것이다 (2026-06-05부터 멀티테넌트 봇 생성이 차단됐다).
> 경로 B로 간다.

### 경로 B: Azure Bot F0 (경로 A가 막혔을 때)

Azure 구독이 필요하다. 무료 SKU만 쓰면 청구액은 $0이지만, 회사 테넌트가 비관리자의 구독 생성을 막아둘 수 있다. <https://portal.azure.com>

**B-1. Entra 앱 등록** — `Microsoft Entra ID > 앱 등록 > 새 등록`
- 이름: `roulette-agent`
- 지원되는 계정 유형: **이 조직 디렉터리의 계정만** (단일 테넌트)
- 등록 후 **애플리케이션(클라이언트) ID**, **디렉터리(테넌트) ID** 기록
- `인증서 및 암호 > 새 클라이언트 암호` → **값 즉시 기록**

**B-2. Azure Bot 리소스** — `리소스 만들기 > Azure Bot`
- 봇 핸들: `roulette-agent-bot`
- 가격 책정 계층: **F0 (무료)**
- 앱 유형: **단일 테넌트**
- 앱 ID: B-1의 클라이언트 ID / 테넌트 ID: B-1의 디렉터리 ID
- 생성 후 `채널` → **Microsoft Teams** 추가

**B-3.** `vote-agent` 이름으로 B-1~B-2 반복

### `CLIENT_ID` 는 개발자 포털의 **Bot ID** 다

포털이 봇을 만들 때 Entra 앱 등록까지 대신 해줬고, 그 앱의 Application(client) ID 가
**Bot ID** 로 표시된다. 같은 값이다.

- `Tools > Bot management` 목록의 **Bot ID** 열
- 또는 봇을 클릭했을 때의 URL: `.../bots/`**`<GUID>`**`/configure`

`8-4-4-4-12` 형태의 GUID 다. 봇 2개니까 **서로 다른 GUID 2개**가 나와야 한다.

### 헷갈리기 쉬운 값 4개

| `.env` 항목 | 무엇 | 어디서 |
|---|---|---|
| `CLIENT_ID` | 봇의 **Bot ID** | Bot management 목록 / URL |
| `CLIENT_SECRET` | 클라이언트 암호 | `Client secrets` 에서 발급, 1회만 표시 |
| `TENANT_ID` | 회사 테넌트 ID | 아래 조회 명령 |
| `TEAMS_APP_ID` | Teams **앱** 신원 (봇과 무관) | **비워둔다.** STEP 5 가 생성 |

`TEAMS_APP_ID` 에 Bot ID 를 넣지 않는다. 별개의 값이고, 빌드 스크립트가 만들어 `.env` 에 적어 넣는다.

### `TENANT_ID` — 비우고 시작해서, 401 이면 채운다

멀티테넌트 봇 생성은 2026-06-05 부터 차단됐다. 그런데도 개발자 포털의 `+ New bot` 이
성공한다면, 포털이 **단일 테넌트 봇을 만들었을 수 있다.** 그 경우 `TENANT_ID` 를 비우면 401 이 난다.
포털 UI 에서 봇의 앱 유형이 드러나지 않으므로, 실측으로 정한다.

1. `TENANT_ID` 를 **비운 채로** STEP 4~6 까지 진행한다
2. Teams 에서 봇을 태그해 본다 → 응답이 오면 끝 (멀티테넌트 봇)
3. 앱 로그에 401 / 토큰 검증 실패가 찍히면 `TENANT_ID` 를 채우고 재시작한다

테넌트 ID 조회 (인증 불필요한 공개 엔드포인트):

```bash
curl -s "https://login.microsoftonline.com/example.com/v2.0/.well-known/openid-configuration" | python3 -c "import sys,json; print(json.load(sys.stdin)['issuer'].rstrip('/').split('/')[-2])"
```

`example.com` → `<테넌트-GUID>`

`TENANT_ID` 를 비우면 SDK 가 이 경고를 낸다 (실행해서 확인함):

> `No tenant_id provided for Entra token validation. Issuer validation will be skipped, accepting tokens from any tenant.`

즉 **발급 테넌트 검증을 건너뛴다.** 동작하더라도, 채워서 검증을 켜는 쪽이 사내 도구로는 낫다.

### 값을 채운 뒤 형식 검사

Teams 왕복까지 가서 401 을 보고 되짚는 것보다, 형식 오류는 여기서 걸러낸다.

```bash
./scripts/check-env.sh
```

GUID 형식, 시크릿과 Bot ID 를 맞바꿔 넣은 경우, 포트 짝, 두 봇의 Bot ID 중복,
`TEAMS_APP_ID` 혼동을 잡는다.

> ⚠️ `.env` 는 이미 채운 값이 있으면 `cp .env.example .env` 로 덮어쓰지 않는다.
> `CLIENT_SECRET` 은 1회만 표시되므로 덮어쓰면 포털에서 재발급해야 한다.

---

## STEP 2 — Dev Tunnel (터널 1개, 포트 2개)

Cloudflare Tunnel의 무료 변종(Quick Tunnel)은 **재시작마다 URL이 바뀌어서 봇에 쓸 수 없다.**
고정 URL을 주는 named tunnel은 Cloudflare에 등록된 도메인이 필수인데 `example.com`은 회사 도메인이라 올릴 수 없다.
Dev Tunnels는 도메인 없이 고정 URL을 준다.

```bash
curl -sL https://aka.ms/DevTunnelCliInstall | bash
```

```bash
devtunnel user login
```

```bash
devtunnel create teams-agents -a
```

`-a` = 익명 접근 허용 (Bot Framework가 인증 없이 POST할 수 있어야 한다).
Bot Framework가 보낸 요청은 SDK가 JWT로 직접 검증하므로 터널이 열려 있어도 안전하다.

### ⚠️ 만들어진 터널 ID 에는 리전 접미사가 붙는다

`teams-agents` 로 만들어도 실제 ID 는 **`teams-agents.<리전>`** 이 된다 (예: `teams-agents.<리전>`).
**이후 모든 명령에는 접미사까지 포함한 전체 ID 를 쓴다.** `teams-agents` 만 쓰면
"그런 터널 없음" 으로 조용히 실패한다.

실제 ID 확인:

```bash
devtunnel list
```

아래에서는 이 값을 `$TID` 로 둔다. 셸에 잡아두면 오타가 없다:

```bash
TID=$(devtunnel list | grep -Eo 'teams-agents\.[a-z0-9]+' | head -1) && echo "TID=$TID"
```

### 포트 2개 추가

```bash
devtunnel port create "$TID" -p 3978 --protocol http
devtunnel port create "$TID" -p 3979 --protocol http
```

### URL 확인

```bash
devtunnel show "$TID"
```

`Ports` 섹션에 포트별 URL 이 나온다:

```
Tunnel ID             : teams-agents.<리전>
Ports                 : 2
  3978  http  https://a1b2c3d4-3978.<리전>.devtunnels.ms/  0 client connections
  3979  http  https://a1b2c3d4-3979.<리전>.devtunnels.ms/
```

### ⚠️ URL 은 조립할 수 없다 — 출력에서 복사한다

위 예에서 터널 ID 는 `teams-agents.<리전>` 인데 URL 호스트는 `a1b2c3d4-3978.<리전>` 이다.
**`a1b2c3d4` 은 터널 ID 와 무관한 별도 생성 토큰이다.** 리전(`<리전>`)도 서비스가 자동 배정한다.
즉 URL 의 어느 부분도 손으로 만들 수 없다. `show` 출력을 그대로 복사한다.

URL 만 뽑아내려면:

```bash
devtunnel show "$TID" | grep -Eo 'https://[a-zA-Z0-9-]+\.[a-z0-9]+\.devtunnels\.ms[^[:space:]]*'
```

`-3978` 이 roulette, `-3979` 가 vote 다. 각각 뒤에 **`/api/messages`** 를 붙여 기록한다:

```
ROULETTE_ENDPOINT : https://________-3978.____.devtunnels.ms/api/messages
VOTE_ENDPOINT     : https://________-3979.____.devtunnels.ms/api/messages
```

URL 이 안 보이면 `devtunnel host "$TID"` 로 호스팅 중인 상태에서 다시 확인한다.

터널은 **30일 무활동 시 만료**된다. 봇이 주기적으로 쓰이면 문제되지 않는다.

---

## STEP 3 — 엔드포인트 연결

- **경로 A**: 개발자 포털 `Tools > Bot management` → 봇 선택 → `Configure` →
  `Endpoint address`에 위 URL 입력 → **Save**
- **경로 B**: Azure Portal → Azure Bot 리소스 → `구성` →
  `메시징 엔드포인트`에 위 URL 입력 → **적용**

`/api/messages` 경로를 빠뜨리지 않는다.

---

## STEP 4 — 코드 (헬로월드 스모크 테스트)

이 단계의 목적은 **연결 경로 검증**이다. 룰렛/투표 로직은 아직 넣지 않는다.
대신 헬로월드가 **멘션 파싱과 멘션 발신을 그대로 검증**하도록 만들어 뒀다 — 두 에이전트의 가장 큰 기술 리스크가 여기서 판정된다.

### 4-1. 공용 venv

```bash
cd ~/dev/ms-teams-agent && python3.13 -m venv .venv && ./.venv/bin/pip install -U pip -r roulette-agent/src/requirements.txt
```

### 4-2. `.env` 작성 (봇 2개 각각)

```bash
for a in roulette-agent vote-agent; do [ -f $a/src/.env ] || cp $a/src/.env.example $a/src/.env; done
```

`-f` 검사를 붙인 이유: 이미 채운 `.env` 를 덮어쓰면 `CLIENT_SECRET` 을 잃는다 (포털에서 1회만 표시된다).

각 파일을 열어 STEP 1에서 기록한 값을 채운다. **`PORT`는 절대 바꾸지 않는다** (터널 포트와 짝이다):
`roulette-agent` = 3978, `vote-agent` = 3979.

### 4-3. 실행 (터미널 3개)

각각 별도 터미널에서. 스크립트가 **이전 프로세스를 정리한 뒤** 띄운다.

```bash
cd ~/dev/ms-teams-agent && ./scripts/startHost
```

```bash
cd ~/dev/ms-teams-agent && ./scripts/startRoulette
```

```bash
cd ~/dev/ms-teams-agent && ./scripts/startVote
```

터미널을 셋 다 열기 귀찮으면 `--bg` 를 붙인다. `logs/` 에 기록되고 셸을 닫아도 살아 있다.

```bash
cd ~/dev/ms-teams-agent && ./scripts/startHost --bg && ./scripts/startRoulette --bg && ./scripts/startVote --bg
```

```bash
tail -f ~/dev/ms-teams-agent/logs/*.log
```

각 스크립트가 하는 일:

| | 정리 | 확인 | 실행 |
|---|---|---|---|
| `startHost` | 기존 `devtunnel` 프로세스 | 바이너리 위치, 터널 ID(리전 접미사 포함), 포트 URL 출력 | `devtunnel host` |
| `startRoulette` | 포트 3978 점유 프로세스 | venv, `.env` 존재 | roulette-agent |
| `startVote` | 포트 3979 점유 프로세스 | venv, `.env` 존재 | vote-agent (기동 시 보존정책 정리) |
| `stopAll` | **roulette → vote → 터널** 순서로 전부 | 셋 다 내려갔는지 확인 | — |

전부 내리려면:

```bash
cd ~/dev/ms-teams-agent && ./scripts/stopAll
```

**종료 방식** — `TERM` 을 먼저 보내고 1.5초 안에 안 죽으면 **`KILL -9`** 로 확실히 끝낸다.
uvicorn 이 `asyncio.sleep` 중일 때 `TERM` 을 늦게(또는 아예) 처리하지 않아 프로세스가
살아남는 일이 반복됐기 때문이다. 포트를 실제로 비운 것까지 확인하고 나서 새로 띄운다.

`-9` 가 안전한 이유: vote-agent 는 SQLite 트랜잭션마다 커밋하므로 중간 상태가 남지 않고,
roulette-agent 는 아무것도 저장하지 않는다.

### 4-4. 터널 도달 확인 (Teams 전에)

STEP 2 에서 기록한 URL 을 셸에 잡아두고 찔러본다:

```bash
ROULETTE_ENDPOINT="https://________-3978.____.devtunnels.ms/api/messages"
```

```bash
curl -i -X POST "$ROULETTE_ENDPOINT" -H 'Content-Type: application/json' -d '{}'
```

**401 또는 400이 나오면 정상이다** — 터널과 앱까지 도달했고 인증에서 막힌 것이다.
연결 거부/타임아웃이면 터널이나 앱이 안 뜬 것이다.

---

## STEP 5 — 앱 패키지 zip ×2

`name.short`가 **@멘션 이름을 결정한다.** 그래서 `당첨자뽑기` / `투표만들기`로 넣어 뒀다.

```bash
cd ~/dev/ms-teams-agent && ./scripts/build-package.sh roulette-agent && ./scripts/build-package.sh vote-agent
```

스크립트가 `src/.env`의 `CLIENT_ID`를 읽어 매니페스트에 채우고, `TEAMS_APP_ID`가 비어 있으면 GUID를 생성해 `.env`에 적어 넣는다 (재실행해도 같은 GUID를 유지한다 — 앱 신원이 바뀌면 재설치를 해야 하기 때문이다).

결과물: `<agent>/appPackage/build/appPackage.zip`

---

## STEP 6 — Teams 설치 & 검증

1. Teams → `앱` → `앱 관리` → `앱 업로드` → **`사용자 지정 앱 업로드`**
2. `roulette-agent/appPackage/build/appPackage.zip` 선택 → **추가**
3. **"개인용으로 열기"를 누르지 말고**, 목록에서 **테스트 그룹 채팅방을 선택** → `이동`
4. `vote-agent` zip으로 반복

> 앱을 그룹 채팅에 설치하면 **그 채팅방 멤버 전원이 봇을 쓸 수 있다.** 각자 설치할 필요 없다.
> 공식 문서: *"You can upload your app to a team, chat, meeting, or for personal use depending on how you configured your app's scope."*

### 검증

채팅방에서:

```
@당첨자뽑기 @동료이름 2명
```

기대 응답:

```
✅ 연결 확인 완료
· 봇: 당첨자뽑기
· 대화 유형: groupChat
· 남은 텍스트: `2명`
· 태그된 사람 1명: @동료이름
```

이게 나오면 **다음 4가지가 전부 검증된 것이다**: 인증, 터널, 멘션 파싱(`entities` → `MentionEntity`), 멘션 발신(`add_mention`). 두 에이전트의 핵심 의존성이 모두 통과다.

`@투표만들기`도 같은 방식으로 확인한다.

---

## 개발 루프 — 무엇을 바꿨을 때 무엇을 다시 하는가

공식 문서:
*"You don't have to upload your custom app again if you make code changes
(these are reflected in Teams in real-time). However, you must reinstall if you change
any app configurations."*

| 무엇을 바꿨나 | 필요한 작업 |
|---|---|
| `app.py`, `config.py` 등 **코드** | **프로세스 재시작만.** zip·재설치 불필요 |
| `src/.env` 값 | 프로세스 재시작만 |
| `appPackage/manifest.json` (이름·botId·스코프·명령·아이콘) | zip 재빌드 (STEP 5) + Teams 재설치 (STEP 6) |
| Dev Tunnel URL 변경 | 봇 등록의 Endpoint address 수정 (STEP 3) |

### 종료는 `stopAll` 하나로 끝낸다

`lsof` 로 PID 를 찾아 `kill` 하지 않아도 된다. 순서(**roulette → vote → 터널**)와
`-9` 폴백까지 스크립트가 처리한다.

```bash
cd ~/dev/ms-teams-agent && ./scripts/stopAll
```

**왜 스크립트인가** — `Ctrl+C` 를 눌렀다고 죽은 게 아니다. uvicorn 이 `asyncio.sleep`
중일 때 `SIGTERM` 을 늦게(또는 아예) 처리하지 않아 살아남는다. 포트가 잡혀 있으면
새 프로세스가 `[Errno 48] address already in use` 로 **뜬 직후 바로 종료되는데,
로그에 "started successfully" 가 먼저 찍혀서 성공처럼 보인다.**

`stopAll` 은 `TERM` → 1.5초 대기 → **`KILL -9`** 로 확실히 끝내고,
**세 개가 정말 다 내려갔는지 확인한 뒤** 성공을 보고한다. 남아 있으면 `❌` 로 멈춘다.
아무것도 안 떠 있어도 그냥 성공한다 (몇 번 실행해도 안전).

`startRoulette` / `startVote` / `startHost` 는 각자 자기 몫을 **띄우기 전에 알아서 정리**하므로,
하나만 재시작할 때는 `stopAll` 없이 그냥 start 스크립트만 실행하면 된다.

```bash
cd ~/dev/ms-teams-agent && ./scripts/startRoulette
```

작업 디렉터리는 어디든 된다. `load_dotenv()` 가 `config.py` 위치를 기준으로 `.env` 를 찾으므로
저장소 루트에서 `.venv/bin/python roulette-agent/src/app.py` 로 실행해도 `.env` 는 정상 로드된다
(실행해서 확인함).

launchd 를 붙인 뒤에는 한 줄이면 된다:

```bash
launchctl kickstart -k gui/$(id -u)/com.example.teams.roulette
```

**새 코드가 돌고 있는지 확인하는 법**: 기동 로그의 첫 줄을 본다. 응답 형식이 예상과 다르면
거의 항상 재시작을 안 한 것이다.

---

## STEP 7 — 상시 가동

여기까지는 터미널을 닫으면 봇이 죽는다. **가장 흔한 장애는 Mac이 잠드는 것이다.**

### 7-1. 절전 해제

`시스템 설정 > 디스플레이 > 고급 > 디스플레이가 꺼져 있을 때 자동으로 잠자기 방지` 켜기.
전원 연결 상태를 유지한다. 클램셸(덮개 닫음)로 쓸 거면 외부 전원 + 외부 디스플레이가 필요하다.

### 7-2. launchd 등록 (프로세스 3개)

`devtunnel`의 절대 경로를 확인한다. launchd는 최소 PATH로 실행되므로 상대 경로는 실패한다.

```bash
which devtunnel
```

```bash
cd ~/dev/ms-teams-agent && ./scripts/install-launchd.sh "$(which devtunnel)"
```

세 개의 plist를 `~/Library/LaunchAgents/`에 설치하고 로드한다:

| 라벨 | 하는 일 |
|---|---|
| `com.example.teams.tunnel` | `devtunnel host <전체 터널 ID>` (스크립트가 ID 를 자동 탐지) |
| `com.example.teams.roulette` | roulette-agent :3978 |
| `com.example.teams.vote` | vote-agent :3979 |

`KeepAlive=true`라서 죽으면 자동 재시작되고, 재부팅 후에도 자동 기동된다.
로그: `~/dev/ms-teams-agent/logs/*.log`

```bash
tail -f ~/dev/ms-teams-agent/logs/*.log
```

해제:

```bash
cd ~/dev/ms-teams-agent && ./scripts/install-launchd.sh --uninstall
```

### 7-3. 코드를 고쳤을 때

위 [개발 루프](#개발-루프--무엇을-바꿨을-때-무엇을-다시-하는가) 참조.

```bash
launchctl kickstart -k gui/$(id -u)/com.example.teams.roulette
```

---

## 트러블슈팅

| 증상 | 원인 / 조치 |
|---|---|
| 봇을 태그해도 무응답 | `devtunnel show`의 URL과 봇 등록 엔드포인트가 다르다. 터널 재생성 시 URL이 바뀌었을 가능성 |
| 앱 로그에 401 / `Unauthorized` | `TENANT_ID` 불일치. 경로 A는 비우고, 경로 B는 채운다 (STEP 1 표) |
| `AADSTS7000215: Invalid client secret` | 시크릿 만료 또는 오타. 새로 발급받는다 |
| `[Errno 48] address already in use` | 이전 프로세스가 살아 있다. `./scripts/stopAll` 후 다시 start |
| 로그에 "started successfully" 뒤 바인드 에러 | 위와 같음. 기동 성공이 아니다 |
| 코드를 고쳤는데 예전 응답이 온다 | 프로세스를 재시작하지 않았다. zip 재빌드가 아니라 **재시작**이 필요하다 |
| 응답이 2~3번 중복 | 핸들러가 15초 내 반환하지 않아 Bot Framework가 재전송한 것. 긴 작업은 `asyncio.create_task()`로 분리 |
| `OPENAI_API_KEY` KeyError로 기동 실패 | 정리 전 `config.py`를 쓰고 있다. STEP 4의 파일로 교체 |
| 밤새 돌다가 아침에 죽어 있다 | Mac이 잠들었다. 7-1 |
| `devtunnel: command not found` (launchd) | plist에 절대 경로를 안 넣었다. 7-2 |
| 30일 방치 후 터널 접속 불가 | 무활동 만료. `devtunnel create`로 재생성 후 STEP 3 재실행 |

---

## 나중에: 클라우드로 옮길 때

Mac 의존이 부담스러워지면 코드 변경 없이 이전할 수 있다. 바꿀 것은 엔드포인트 URL 하나다.

| 대상 | 비용 | 특징 |
|---|---|---|
| Azure App Service **F1** | $0 영구 | 무료 플랜에 앱 10개 → 봇 2개 수용. 단 60 CPU분/일, 20분 유휴 시 재움 → 무료 크론으로 10분마다 핑 |
| Azure Container Apps | 무료 그랜트 내 $0 | 월 180,000 vCPU초 + 2백만 요청. scale-to-zero 필수 (상시 가동은 그랜트 초과) |
| Oracle Cloud Always Free | $0 영구 | 진짜 상시가동. ARM 4코어/24GB. Linux 운영 부담 |

**옮길 때 반드시 고칠 것**: `roulette-agent/infra/azure.parameters.json`의 `webAppSKU`가 `B3`(월 $100+)로 들어 있다. `F1`로 바꾸고 `infra/azure.bicep`의 `alwaysOn: true`도 `false`로 내려야 한다 (F1은 Always On 미지원 → 배포 실패).

---

## 이미 검증된 것 / 당신이 검증할 것

이 가이드를 쓰면서 로컬에서 실제로 실행해 확인한 것과, 아직 확인할 수 없는 것을 구분해 둔다.

### ✅ 실행해서 확인함 (SDK 2.0.15, Python 3.13.2)

| 항목 | 결과 |
|---|---|
| `microsoft-teams-apps` 2.0.15 설치 | 성공 (Python 3.13.2 / **3.14.6은 venv 생성 실패**) |
| `app.py` 기동 + `/api/messages` 서빙 | uvicorn `0.0.0.0:3978`, 미인증 POST에 **401** |
| `entities` → `MentionEntity` 역직렬화 | 성공 |
| `tagged_users()` 봇 제외 + 중복 제거 | `@봇 @홍길동 @김철수 @김철수` → `[홍길동, 김철수]` |
| `is_recipient_mentioned()` | `True` |
| `strip_mentions_text()` | `"2명"` 추출 → 인원 수 파싱 `2` |
| `add_mention()` | `<at>홍길동</at>, <at>김철수</at>` + entities 2개 |
| `AdaptiveCard` / `ExecuteAction` import | 성공 (vote-agent 용) |
| `build-package.sh` | zip 최상위에 manifest+아이콘, 재실행 시 `TEAMS_APP_ID` 유지 |
| 매니페스트 길이 제약 | 10항목 검사 통과 (`command.title` 32자 한도에 한 번 걸려서 수정함) |

**즉 두 에이전트의 최대 기술 리스크였던 멘션 파싱과 멘션 발신은 이미 판정이 끝났다.** 남은 것은 순수 로직이다.

### ❓ 당신의 테넌트에서만 확인 가능

| 항목 | 확인 방법 | 막혔을 때 |
|---|---|---|
| 개발자 포털에서 봇 생성 | `+ New bot` 이 성공하는지 | 경로 B (Azure Bot F0) |
| 비관리자의 Entra 앱 등록 권한 | 경로 B 의 B-1 이 되는지 | 관리자에게 요청 |
| 그룹 채팅에 앱 설치 | STEP 6-3 에서 채팅방이 목록에 뜨는지 | 팀 소유자 권한 필요 여부 확인 |
| 실제 Teams 왕복 | STEP 6 검증 메시지 | 트러블슈팅 표 |

## 출처

- [Azure AI Bot Service 요금](https://azure.microsoft.com/en-us/pricing/details/bot-services/) — standard channel 무제한 무료
- [사용자 지정 앱 업로드](https://learn.microsoft.com/en-us/microsoftteams/platform/concepts/deploy-and-publish/apps-upload) — 채팅/팀 범위 설치
- [Dev Tunnels 시작하기](https://learn.microsoft.com/en-us/azure/developer/dev-tunnels/get-started) / [CLI 명령](https://learn.microsoft.com/en-us/azure/developer/dev-tunnels/cli-commands) / [FAQ](https://learn.microsoft.com/en-us/azure/developer/dev-tunnels/faq)
- [TryCloudflare Quick Tunnel](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/trycloudflare/) — URL 비고정
- [멀티테넌트 봇 생성 중단](https://learn.microsoft.com/en-us/answers/questions/5912315/teams-bot-creation-fails-with-multitenant-bot-crea) (2026-06)
- [App Service 무료 F1 할당량](https://learn.microsoft.com/en-us/azure/app-service/web-sites-monitor)
