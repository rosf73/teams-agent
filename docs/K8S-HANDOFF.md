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
기기 코드가 나오면 브라우저에서 로그인한다. 로그인 정보는 볼륨에 남는다. 로그인이 만료되면(오래 안 쓰거나 회사 정책) 봇이 **아무 오류 없이 무응답**이 된다(`startHost` 주석과 같은 증상). 그때는 이 명령으로 다시 로그인한다.

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

## 7. 주의
- **branch-graph는 팀이 쓰는 운영 서비스다.** `branch-graph` Deployment, Secret, PVC, Ingress는 건드리지 않는다. 쿼터를 함께 쓴다는 점도 기억한다.
- Dev Tunnel은 Microsoft가 개발·테스트용으로 안내하는 서비스다(무료 한도 월 5GB, 분당 1500건). 지금도 같은 방식이라 위험이 새로 생기지는 않지만, 24시간 운영이 된다.
  - 장기적으로는 IT에 "봇 메시지 경로(`/api/messages`)만 외부 공개"를 요청하면 터널 없이 운영할 수 있다. 봇 SDK가 요청의 JWT를 검증하므로 안전하다.
- 비밀값(클라이언트 시크릿, 터널 토큰)은 채팅·로그·문서에 남기지 않는다.
