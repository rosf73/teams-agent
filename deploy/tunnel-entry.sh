#!/bin/sh
# tunnel 컨테이너의 진입점. `devtunnel host` 가 끝나도 컨테이너는 살려 두고 다시 시도한다.
#
# 왜 컨테이너를 죽이지 않는가: 처음 배포하면 아직 로그인 전이라 host 가 바로 실패한다.
# 그때 컨테이너가 CrashLoopBackOff 에 빠지면 `kubectl exec … devtunnel user login -d` 로
# 들어갈 수가 없다. 그래서 여기서 재시도하고, 실패할 때마다 로그인 방법을 로그에 남긴다.
set -u

: "${TUNNEL_ID:?TUNNEL_ID 가 비어 있다 (devtunnel list 출력에서 복사한 teams-agents.<리전>)}"
mkdir -p "$HOME"

while :; do
    devtunnel host "$TUNNEL_ID"
    echo "[tunnel-entry] devtunnel host 종료 (exit $?). 30초 뒤 다시 시도한다."
    echo "[tunnel-entry] 로그인이 필요하면: kubectl exec -it deploy/teams-agents -c tunnel -- devtunnel user login -d"
    sleep 30
done
