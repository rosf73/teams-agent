# 사내 Kubernetes로 옮기기 — 인계 문서

> 2026-10-16, branch-graph 작업 세션에서 작성했다. 클러스터에서 **직접 확인한 사실**과 **제안 구성**을 구분해 적는다.
> 이 저장소의 [DEV_RULE.md](DEV_RULE.md)(특히 `.env`를 건드리지 않는 규칙)를 그대로 따른다.

## 0. 결론

- **옮길 수 있다.** 지금 Mac에서 하는 일(봇 2개 + `devtunnel host`)을 Pod 하나로 그대로 옮긴다.
- Teams가 봇에 요청하려면 **밖에서 들어오는 HTTPS**가 필요하다. 그런데 사내 클러스터는 사내 전용이라 밖에서 들어올 수 없다. 그래서 **Dev Tunnel을 클러스터 안에서 계속 쓴다.** 터널은 안에서 밖으로 **나가는 연결**만 쓴다.
- 같은 터널(`teams-agents.<리전>`)과 같은 포트(3978/3979)를 쓰면 **봇 주소가 바뀌지 않는다.** 봇 등록이나 앱 패키지를 다시 할 필요가 없다.

```
[Teams/Bot Framework] ⇄ *.devtunnels.ms ⇄ (나가는 연결) ⇄ Pod "teams-agents"
                                                          ├ devtunnel host  → localhost:3978, 3979
                                                          ├ roulette-agent :3978 (당첨자뽑기)
                                                          └ vote-agent     :3979 (투표만들기, SQLite)
```
한 Pod 안의 컨테이너는 `localhost`를 함께 쓴다. 그래서 터널이 지금처럼 `localhost:3978/3979`로 넘겨주면 된다.

---

## 1. 클러스터 접속 (확인한 사실)

| 항목 | 값 |
|---|---|
| 네임스페이스 | `ns-branch-graph-dev` (branch-graph와 함께 쓴다. 이름은 신경 쓰지 않기로 함) |
| 접속 파일 | `~/.kube/ns-branch-graph-dev.yaml` → `export KUBECONFIG=~/.kube/ns-branch-graph-dev.yaml` |
| 클러스터 | MicroK8s 계열, 노드 여러 대(예: `k8s-worker-07`). API `https://10.255.0.233:16443` |
| 네트워크 조건 | **사내망 또는 회사 VPN**에서만 접속된다 |
| 권한 | deployments, statefulsets, pvc, secrets, services, configmaps, ingresses, cronjobs, `pods/exec` 생성 **가능** |
| 쿼터 (`default-quota`) | pods 20, requests.cpu 2, requests.memory 4Gi, requests.storage 20Gi. branch-graph가 이미 cpu 120m, mem 288Mi, storage 5Gi를 쓴다 |
| 기본 리소스 (`default-limits`) | 컨테이너마다 request 100m/128Mi, limit 1 CPU/1Gi(지정하지 않으면 적용) |
| 저장소 클래스 | 기본 `nfs-csi`(ReadWriteOnce). 가이드의 `ceph-rbd`는 **없다** |
| 이미 있는 Secret | `harbor-alti-pull`(이미지 받기용), `altimedia-wildcard-tls`, `branch-graph`(**건드리지 않는다**) |

**Claude Code에서 실행할 때:** `kubectl`, `docker`, `curl` 같은 네트워크 명령은 샌드박스 프록시 때문에 실패한다. 샌드박스 밖에서 실행한다. curl은 `--noproxy '*'`를 붙인다.

## 2. 나가는 연결 (클러스터 Pod에서 확인, 2026-10-16)

| 대상 (443) | 결과 | 용도 |
|---|---|---|
| `global.rel.tunnels.api.visualstudio.com` | ✅ | Dev Tunnel 서비스 |
| `login.microsoftonline.com` | ✅ | 봇 토큰 발급(Entra) |
| `smba.trafficmanager.net` | ✅ | 봇이 Teams로 응답 보내기 |
| `pypi.org` | ✅ | 참고 |

**들어오는 연결은 불가능하다.** 노드 IP가 사설망이다. Ingress(클래스 `public`, 사내 DNS)는 사내 사용자용이고, Microsoft 클라우드에서는 닿지 않는다.

## 3. 이미지 레지스트리

- Harbor: `docker.altimedia.com`(사내 LDAP 계정으로 로그인). branch-graph는 프로젝트 `branch-graph`를 쓴다. 새 이미지도 같은 프로젝트에 둘 수 있다(예: `docker.altimedia.com/branch-graph/teams-agents`).
  - 다른 프로젝트를 쓰면 `harbor-alti-pull` Secret으로 받을 수 있는지 먼저 확인한다.
- 로그인: 사용자가 직접 `docker login docker.altimedia.com`을 한다. **자주 풀린다.** 배포 전에 이 명령으로 확인한다. 401이면 사용자에게 다시 로그인을 요청한다.
  ```bash
  docker buildx imagetools inspect docker.altimedia.com/branch-graph/app:20261016-1240
  ```
- 빌드: `docker buildx build --platform linux/amd64 -t <이미지>:<YYYYMMDD-HHMM> --push .` (노드는 amd64)
- 태그는 날짜·시각으로 고정한다(`latest` 금지). branch-graph는 `kustomization.yaml`의 `newTag`를 스크립트가 바꾼다.

## 4. 제안 구성

### 4.1 리소스
| 리소스 | 내용 |
|---|---|
| Deployment `teams-agents` | `replicas: 1`, `strategy: Recreate`. 볼륨이 ReadWriteOnce이고 같은 터널을 두 곳에서 host하면 안 된다. 컨테이너 3개(4.2) |
| PVC `teams-agents-data` | 1Gi, `nfs-csi`. `votes.db`와 devtunnel 로그인 정보를 둔다 |
| Secret `teams-roulette`, `teams-vote` | 각 봇의 `.env` 값(4.3) |
| Service / Ingress | **없다**(들어오는 길이 없다) |

### 4.2 컨테이너
| 컨테이너 | 실행 | 포인트 |
|---|---|---|
| `roulette` | `python app.py` (작업 디렉터리 `roulette-agent/src`) | `envFrom: secretRef teams-roulette` |
| `vote` | `python app.py` (작업 디렉터리 `vote-agent/src`) | `envFrom: secretRef teams-vote`, `VOTE_DB_PATH=/data/votes.db` (`store.py`가 읽는 환경 변수) |
| `tunnel` | `devtunnel host <터널 ID>` | 이미지에 devtunnel CLI를 넣는다. `HOME`을 볼륨 아래(예: `/data/devtunnel-home`)로 두어 로그인 정보가 재시작 뒤에도 남게 한다. 실제 저장 위치는 로그인 후 확인한다 → 현재 디렉터리 아래였다(6절) |

- 이미지는 하나로 만들고(Python 3.13 + 두 봇 코드 + devtunnel CLI) 컨테이너마다 `command`만 다르게 하면 단순하다.
- 터널 ID에는 리전 접미사가 붙는다(`teams-agents.<리전>`). 조립하지 말고 `devtunnel list` 출력에서 복사해 설정값으로 넣는다(`scripts/_common.sh`의 경고와 같다).

### 4.3 비밀값 — `.env`는 읽기만, 값은 화면에 내지 않는다
`.env`를 수정하거나 복사하지 않는다. **사용자가 직접** 아래 명령으로 Secret을 만든다. 값은 클러스터로만 가고 화면과 채팅에는 나오지 않는다.
```bash
kubectl --kubeconfig ~/.kube/ns-branch-graph-dev.yaml create secret generic teams-roulette --from-env-file=roulette-agent/src/.env
kubectl --kubeconfig ~/.kube/ns-branch-graph-dev.yaml create secret generic teams-vote --from-env-file=vote-agent/src/.env
```
- 확인은 **키 이름만** 본다. `kubectl get secret teams-roulette -o jsonpath='{.data}'`를 키 목록으로만 출력하고, 값은 출력하지 않는다.
- 값 하나만 바꿀 때는 숨김 입력을 쓴다(branch-graph에서 쓰는 방식).
  ```bash
  read -rs "V?CLIENT_SECRET: "; echo; kubectl --kubeconfig ~/.kube/ns-branch-graph-dev.yaml patch secret teams-roulette --type merge -p "{\"stringData\":{\"CLIENT_SECRET\":\"$V\"}}"; unset V
  ```
  이 명령은 터미널에서 직접 실행한다. Claude Code 입력창의 `!`로 실행하면 입력란이 제대로 동작하지 않을 수 있다.

### 4.4 Dev Tunnel 로그인 (처음 한 번, 사용자)
```bash
kubectl --kubeconfig ~/.kube/ns-branch-graph-dev.yaml exec -it deploy/teams-agents -c tunnel -- devtunnel user login -d
```
기기 코드가 나오면 브라우저에서 로그인한다. 로그인 정보는 볼륨에 남는다. 로그인이 만료되면(오래 안 쓰거나 회사 정책) 봇이 **아무 오류 없이 무응답**이 된다(`startHost` 주석과 같은 증상). 그때는 이 명령으로 다시 로그인한다. 로그인이 유효한데 무응답인 경우도 있었다(7절, host 프로세스 멈춤).

### 4.5 보안·운영 기준 (branch-graph에서 쓰는 것과 같게)
- `automountServiceAccountToken: false`, Pod `seccompProfile: RuntimeDefault`, `fsGroup`
- 컨테이너마다 `runAsNonRoot`, `allowPrivilegeEscalation: false`, `capabilities.drop: [ALL]`, `readOnlyRootFilesystem: true` + `/tmp` emptyDir
- 리소스를 지정한다(예: 봇 50m/128Mi 요청, 터널 20m/64Mi). 쿼터를 branch-graph와 함께 쓴다.
- 상태 검사: 봇은 포트 `tcpSocket`(3978/3979) 정도로 시작한다. 터널은 프로세스가 죽으면 재시작되는 것으로 충분하다.
- 참고할 파일(복사해서 고쳐 쓰기 좋다)
  - `~/dev/branch-graph/Dockerfile`
  - `~/dev/branch-graph/deploy/k8s/{deployment,pvc,kustomization}.yaml`
  - `~/dev/branch-graph/deploy/k8s/release.sh`(테스트 → 빌드·푸시 → 태그 교체 → apply → rollout)

## 5. 전환 순서

1. 이미지 빌드·푸시(3절), Secret 생성(4.3), `kubectl apply`
2. **Mac 쪽을 먼저 끈다.** 같은 터널을 두 곳에서 host하지 않기 위해서다. `./scripts/stopAll`을 실행하고, launchd를 쓰고 있었다면 `./scripts/install-launchd.sh --uninstall`도 실행한다.
3. Dev Tunnel 로그인(4.4) → `tunnel` 컨테이너 로그에 host 연결이 보이는지 확인한다(`kubectl logs deploy/teams-agents -c tunnel`).
4. Teams에서 `@당첨자뽑기`, `@투표만들기`를 실제로 호출해 확인한다. 투표는 하나 만든 뒤 Pod를 재시작하고 남아 있는지 본다(볼륨 확인).
5. 기존 `vote-agent/data/votes.db`를 옮길지 정한다. 옮기려면 `kubectl cp`로 볼륨에 넣고, 넣는 동안은 vote 컨테이너를 멈춘다.

**되돌리기:** `kubectl scale deploy/teams-agents --replicas=0` → Mac에서 `./scripts/startHost --bg && ./scripts/startRoulette --bg && ./scripts/startVote --bg`

## 6. 배포하며 확인한 것 (2026-10-16)

위 4~5절은 배포 전에 쓴 제안이다. 실제로 배포해 보니 아래가 달랐거나 새로 확인됐다. 구현은 `Dockerfile`, `deploy/`에 반영했다.

| 처음 생각한 것 | 실제 | 반영 |
|---|---|---|
| devtunnel CLI만 이미지에 넣으면 된다 | .NET 프로그램이라 **ICU가 없으면 시작하자마자 죽는다**(`Couldn't find a valid ICU package`) | `Dockerfile`에서 `libicu76` 설치. 패키지 이름에 버전이 붙으니 베이스 이미지(trixie)가 바뀌면 맞춰 고친다 |
| `HOME`을 볼륨 아래로 두면 로그인 정보가 남는다 | 설정·토큰(`DevTunnels/devtunnels.json`, `devtunnels-tokens-microsoft`)은 HOME이 아니라 **현재 디렉터리** 아래에 쓴다. `/app`에서 실행하면 `Read-only file system : '/app/DevTunnels'`로 실패한다 | `deploy/devtunnel-shim.sh`가 `/usr/local/bin/devtunnel` 자리에서 항상 `$HOME`으로 이동해 실행한다. `kubectl exec`로 불러도 같은 위치를 쓴다 |
| 로그인 전에는 tunnel 컨테이너가 재시작을 반복해도 된다 | CrashLoopBackOff에 빠지면 `kubectl exec`로 들어가 로그인할 수 없다 | `deploy/tunnel-entry.sh`가 `devtunnel host`가 끝나도 컨테이너를 살려 두고 30초마다 다시 시도한다. 로그인 전 로그에는 `Unauthorized`와 로그인 안내가 반복된다 |
| 로그인 정보가 NFS 볼륨에서 유지되는지 모른다 | Pod를 재시작해도 로그인이 남고 터널이 바로 다시 붙는다 | — |

그 밖에 확인한 것:
- .NET이 처음 실행할 때 `$HOME/.net`에 약 46MB를 풀어 둔다(볼륨 1Gi 안에서 충분하다).
- 시간대 데이터는 `python:3.13-slim`에 들어 있다. 투표 마감 해석(`Asia/Seoul`)에 추가 패키지가 필요 없다.
- `.dockerignore`는 허용 목록 방식이다. `.env`와 `votes.db`는 빌드 컨텍스트에 들어갈 수 없다.
- 터널 클라이언트 로그의 `ClientSSH: Session closed unexpectedly … ConnectionLost`는 들어온 연결 하나가 끊긴 것이다. host는 계속 살아 있으니 이 줄만으로 장애로 보지 않는다.
- 배포는 `./deploy/k8s/release.sh`로 한다(테스트 → Secret 존재 확인 → 빌드·푸시 → 태그 교체 → apply → rollout).
- 쿼터 사용량(branch-graph 포함): CPU 240m/2, 메모리 608Mi/4Gi, 저장소 6Gi/20Gi.

## 7. 장애 기록

### 2026-10-17 ~ 10-19: 터널 host가 살아 있는 채로 멈춤 (약 1.5일 무응답)

**증상.** Teams에서 `@당첨자뽑기`를 불러도 반응이 없었다. Pod는 3/3 Running, 재시작 0회였고 봇 두 개도 정상이었다. 봇 로그의 마지막 요청은 10/16이었다.

**원인.** 10/17 14:44 UTC(23:44 KST)에 relay 연결이 끊겼다(`Connection to host tunnel relay closed`). 이런 끊김은 그 전에도 하루 몇 번씩 있었고 매번 스스로 다시 붙었다. 이번에는 다시 붙기 전에 토큰을 갱신하다가 401을 받았다.
```
devtunnel: Error connecting host tunnel session: Not authorizedUnauthorized. Refreshed tunnel access token is not valid.
```
그 뒤 `devtunnel host` 프로세스는 **종료하지도 다시 시도하지도 않고 떠 있기만 했다.** 프로세스가 끝나지 않아 `tunnel-entry.sh`의 재시도 루프도 돌지 않았다. 로그인은 유효했다(`devtunnel user show`, `devtunnel show` 모두 정상). 토큰 갱신이 왜 401을 받았는지는 확인하지 못했다.

**진단.** 핵심은 터널에 붙은 host 수다.
```bash
kubectl exec deploy/teams-agents -c tunnel -- devtunnel show teams-agents.jpe1 | grep "Host connections"
```
`Host connections : 0`인데 로그인이 유효하면 host 프로세스가 멈춘 것이다. 로그인이 풀린 경우(4.4절)와 증상은 같지만 조치가 다르다.

**복구.** 멈춘 host 프로세스만 종료했다. `tunnel-entry.sh`가 30초 안에 다시 띄웠고 Pod는 재시작하지 않았다. 이미지에 `kill` 실행 파일이 없어서 셸 내장 명령으로 보낸다.
```bash
kubectl exec deploy/teams-agents -c tunnel -- sh -c 'kill <devtunnel host PID>'   # PID는 /proc/*/cmdline에서 "devtunnel host"를 찾는다
```

**재발 방지.** tunnel 컨테이너에 liveness probe를 붙였다(`deploy/k8s/deployment.yaml`). `devtunnel show`로 host 수를 보고 0이면 실패로 친다.
- 주기는 30분, 2회 연속 실패 시 재시작한다. 같은 일이 생기면 **최대 약 1시간 뒤** 스스로 복구된다. 5분 주기도 비용은 무시할 수준(한 번에 13~15초, 메모리 최대 95MiB)이었고, 사용자가 30분 × 2회를 골랐다.
- 더 빨리 복구해야 하면 위 복구 명령을 직접 쓴다.
- 로그인이 풀린 경우에는 probe가 재시작을 반복해도 고쳐지지 않는다. 4.4절의 명령으로 다시 로그인한다.

### 2026-10-19: 투표 클릭에 "문제가 발생했습니다", 재전송으로 표가 뒤집힘

**증상.** 익명 투표(`40bfe84187ff`)를 진행하던 중 두 번 문제가 생겼다.
- 17:30 KST: 투표 버튼을 누르자 카드 아래에 "문제가 발생했습니다. 다시 시도하세요."가 떴다. 표는 실제로 저장됐다.
- 17:35 KST: 2번 항목을 눌렀는데 "나나마구로 선택을 취소했습니다"가 떴고 표 수는 그대로였다. 사용자는 투표를 의도했다.

**원인.** 투표 처리 순서가 "DB 저장 → Teams API로 투표판 갱신(PUT) → 클릭 응답"이었다. 갱신이 끝나야 응답이 나갔다.
- 17:30: 갱신이 늦어져 응답 전에 Teams가 연결을 끊었다. uvicorn 로그에 그 요청의 `200 OK` 줄이 없다. 응답을 보내기 전에 상대가 끊으면 이 줄을 남기지 않는다.
- 17:35: 갱신이 TLS 연결 단계에서 실패했다(`httpx.ConnectError`, `start_tls`). 그래서 500을 냈다. Teams가 2.6초 뒤 **같은 클릭을 다시 보냈고**, 투표 처리에는 재전송을 걸러 내는 장치가 없었다. 이미 저장한 표가 다시 토글돼 취소됐다. DEV_RULE 8절("500을 내면 Bot Framework가 재전송한다")이 경고한 그대로다.

**Teams API 횟수 제한(429)과는 관계없다.** 이 Pod의 로그에는 Teams API가 4xx/5xx를 돌려준 기록이 없다. 17:30 갱신은 늦었지만 2xx로 끝났고, 17:35는 HTTP 응답을 받기 전 TLS 연결에서 실패했다. 클러스터에서 `smba.trafficmanager.net`으로 나가는 연결이 가끔 느리거나 실패하는 것으로 보이지만, 얼마나 자주인지는 아직 모른다(아래 로그로 지켜본다).

**수정** (`vote-agent/src/app.py`, 이미지 `20261019-1756`).
1. **재전송 걸러 내기.** 표를 저장한 직후 invoke `activity.id`와 결과 문구를 기억한다. 같은 ID가 다시 오면 표는 건드리지 않고 같은 문구를 돌려준다. Teams가 같은 ID로 다시 보낸다는 건 아직 직접 확인하지 못했다. 다시 일어나면 아래 로그의 `act=`로 확인한다.
2. **응답 먼저, 갱신은 나중에.** 결과 문구는 DB 저장 순간 정해지므로 바로 응답한다(실측 0.01~0.03초). 투표판 갱신은 백그라운드에서 한다.
   - 실패하면 최대 3번(1초, 2초 간격) 다시 시도하고, 그래도 안 되면 `투표판 갱신 포기`를 남긴다. 표는 저장돼 있어 다음 갱신 때 반영된다.
   - 투표판은 한 번에 하나씩, **그리는 순간의 DB 상태로** 그린다. 그래서 늦게 끝난 갱신이 마감판을 예전 상태로 덮지 않는다. 밀린 갱신은 한 번으로 합친다.
   - "처리 중"을 먼저 띄우고 나중에 결과로 바꾸는 방식도 검토했다. 클릭 응답은 한 번만 보낼 수 있어서 쓸 수 없었다.
3. **로그.** 지켜볼 때 이 줄들을 본다.
   ```
   투표 poll=… idx=… user=<해시> act=<요청 ID 해시> 0.02s      ← 클릭 응답까지 걸린 시간
   투표판 갱신 poll=… 0.42s                                   ← Teams API PUT 시간
   투표판 갱신 실패 poll=… 0.00s ConnectError: …              ← 실패와 예외 종류
   재전송 감지 — 표는 그대로 poll=… act=…                       ← 걸러 낸 재전송
   ```

**배포 중 겪은 것.** VPN이 끊겼다 다시 붙은 뒤 한동안 Mac에서 `docker.altimedia.com`을 찾지 못해 이미지 푸시가 실패했다(`no such host`).
- `nslookup … 10.255.0.10`으로 사내 DNS에 직접 물으면 바로 답이 왔다. 그러니 시스템 리졸버(mDNSResponder)에 남은 "없음" 캐시였다(`dns-sd -G v4 docker.altimedia.com`에서 `No Such Record`가 먼저 보였다).
- 잠시 뒤 저절로 풀렸다. 급하면 DNS 캐시를 비운다(관리자 권한 필요). 클러스터 API는 IP로 접속하므로 이때도 됐다.

## 8. 주의
- **branch-graph는 팀이 쓰는 운영 서비스다.** `branch-graph` Deployment, Secret, PVC, Ingress는 건드리지 않는다. 쿼터를 함께 쓴다는 점도 기억한다.
- Dev Tunnel은 Microsoft가 개발·테스트용으로 안내하는 서비스다(무료 한도 월 5GB, 분당 1500건). 지금도 같은 방식이라 위험이 새로 생기지는 않지만, 24시간 운영이 된다.
  - 장기적으로는 IT에 "봇 메시지 경로(`/api/messages`)만 외부 공개"를 요청하면 터널 없이 운영할 수 있다. 봇 SDK가 요청의 JWT를 검증하므로 안전하다.
- 비밀값(클라이언트 시크릿, 터널 토큰)은 채팅·로그·문서에 남기지 않는다.
