#!/usr/bin/env bash
# .env 값이 형식적으로 맞는지 먼저 잡는다. Teams 왕복까지 가서 401 을 보고
# 되짚는 것보다 여기서 걸러내는 게 빠르다.
#
#   ./scripts/check-env.sh
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GUID='^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
FAIL=0
ALL_CLIENT_IDS=()

for pair in "roulette-agent:3978" "vote-agent:3979"; do
  AGENT="${pair%%:*}"; WANT_PORT="${pair##*:}"
  ENV_FILE="$ROOT/$AGENT/src/.env"
  echo "── $AGENT ────────────────────────────────"

  if [[ ! -f "$ENV_FILE" ]]; then
    echo "  ❌ .env 이 없다.  cp $AGENT/src/.env.example $AGENT/src/.env"
    FAIL=1; echo; continue
  fi

  get () { grep -E "^$1=" "$ENV_FILE" | tail -1 | cut -d= -f2- | tr -d '"'"'"' \r'; }
  CID="$(get CLIENT_ID)"; SEC="$(get CLIENT_SECRET)"
  TID="$(get TENANT_ID)"; PORT="$(get PORT)"; APPID="$(get TEAMS_APP_ID)"

  # CLIENT_ID = 개발자 포털의 Bot ID. GUID 여야 한다.
  if [[ -z "$CID" ]]; then
    echo "  ❌ CLIENT_ID 가 비어 있다 → dev.teams.microsoft.com/bots 의 Bot ID"
    FAIL=1
  elif [[ ! "$CID" =~ $GUID ]]; then
    echo "  ❌ CLIENT_ID 가 GUID 형식이 아니다: '$CID'"
    echo "     (시크릿이나 앱 이름을 잘못 넣었을 가능성. Bot ID 는 8-4-4-4-12 GUID)"
    FAIL=1
  else
    echo "  ✅ CLIENT_ID  $CID"
    ALL_CLIENT_IDS+=("$CID")
  fi

  # CLIENT_SECRET 은 GUID 가 아니다. GUID 면 Bot ID 를 잘못 넣은 것이다.
  if [[ -z "$SEC" ]]; then
    echo "  ❌ CLIENT_SECRET 이 비어 있다"
    FAIL=1
  elif [[ "$SEC" =~ $GUID ]]; then
    echo "  ❌ CLIENT_SECRET 이 GUID 다 → Bot ID 를 잘못 붙여넣었다"
    FAIL=1
  elif (( ${#SEC} < 20 )); then
    echo "  ⚠️  CLIENT_SECRET 이 ${#SEC}자로 짧다. 잘려서 붙었는지 확인"
  else
    echo "  ✅ CLIENT_SECRET  ${#SEC}자 (${SEC:0:3}···)"
  fi

  # TENANT_ID 는 비어 있어도 된다. 있으면 GUID 여야 한다.
  if [[ -z "$TID" ]]; then
    echo "  ·  TENANT_ID   비어 있음 → 멀티테넌트로 동작. 401 이 나면 채운다"
  elif [[ ! "$TID" =~ $GUID ]]; then
    echo "  ❌ TENANT_ID 가 GUID 형식이 아니다: '$TID'"
    FAIL=1
  else
    echo "  ✅ TENANT_ID  $TID"
  fi

  # PORT 는 Dev Tunnel 포트와 짝이다.
  if [[ "$PORT" != "$WANT_PORT" ]]; then
    echo "  ❌ PORT 가 '$PORT' 다. $AGENT 는 $WANT_PORT 여야 한다 (터널 포트와 짝)"
    FAIL=1
  else
    echo "  ✅ PORT       $PORT"
  fi

  # TEAMS_APP_ID 는 비워두는 게 정상. CLIENT_ID 와 같으면 혼동한 것이다.
  if [[ -n "$APPID" && "$APPID" == "$CID" ]]; then
    echo "  ❌ TEAMS_APP_ID 가 CLIENT_ID 와 같다. 별개의 값이다 — 비우면 스크립트가 만든다"
    FAIL=1
  elif [[ -z "$APPID" ]]; then
    echo "  ·  TEAMS_APP_ID 비어 있음 → build-package.sh 가 생성 (정상)"
  else
    echo "  ✅ TEAMS_APP_ID  $APPID"
  fi
  echo
done

# 두 봇이 같은 Bot ID 를 쓰면 하나만 동작한다.
if (( ${#ALL_CLIENT_IDS[@]} == 2 )) && [[ "${ALL_CLIENT_IDS[0]}" == "${ALL_CLIENT_IDS[1]}" ]]; then
  echo "❌ 두 에이전트의 CLIENT_ID 가 같다. 봇 2개는 서로 다른 Bot ID 여야 한다."
  FAIL=1
fi

if (( FAIL )); then
  echo "형식 검사 실패. 위 항목을 고치고 다시 실행한다."
  exit 1
fi
echo "✅ 형식 검사 통과. STEP 2 (Dev Tunnel) 로 진행."
